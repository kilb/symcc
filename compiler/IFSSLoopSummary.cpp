// This file is part of SymCC.
//
// Accelerate a strictly bounded, side-effect-free affine natural loop into
// closed-form integer state before symbolic instrumentation.

#include "IFSSLoopSummary.h"

#include "ManifestWriter.h"
#include "SiteId.h"

#include <llvm/ADT/APInt.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallString.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/Analysis/LoopInfo.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>
#include <llvm/Transforms/Utils/BasicBlockUtils.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/FormatVariadic.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/raw_ostream.h>

#include <algorithm>
#include <cstdlib>
#include <optional>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr uint64_t kMaxLoopTripCount = 8;
constexpr unsigned kMaxLoopStates = 8;
constexpr unsigned kMaxTriangularLoopStates = 4;
constexpr unsigned kMaxAffineExpressionInstructions = 32;
constexpr unsigned kMaxLoopBreaks = 3;
constexpr char kLoopSummarySchema[] = "bounded-affine-loop-summary-v1";
constexpr char kTriangularLoopSummarySchema[] =
    "bounded-upper-triangular-loop-summary-v1";
constexpr char kBreakLoopSummarySchema[] =
    "bounded-break-loop-exit-v1";
constexpr char kMultiBreakLoopSummarySchema[] =
    "bounded-multi-break-loop-exit-v2";
constexpr char kLoopManifestSchema[] =
    "symcc-ifss-loop-recurrence-manifest-v1";
constexpr char kBreakLoopManifestSchema[] =
    "symcc-ifss-loop-exit-manifest-v1";
constexpr char kMultiBreakExitSemantics[] =
    "post-update-priority-equality-break-v2";

struct AffineLoopState {
  PHINode *phi = nullptr;
  Value *initialValue = nullptr;
  ConstantInt *step = nullptr;
  Instruction *update = nullptr;
};

struct TriangularRecurrence {
  unsigned bitWidth = 0;
  std::vector<std::vector<APInt>> matrix;
  std::vector<APInt> offset;
};

struct LoopBreak {
  BasicBlock *checkBlock = nullptr;
  BranchInst *branch = nullptr;
  ICmpInst *condition = nullptr;
  BasicBlock *exit = nullptr;
  Value *value = nullptr;
};

struct AffineLoopRegion {
  BasicBlock *preheader = nullptr;
  BasicBlock *header = nullptr;
  BasicBlock *updateBlock = nullptr;
  BasicBlock *latch = nullptr;
  BasicBlock *exit = nullptr;
  BranchInst *headerBranch = nullptr;
  PHINode *induction = nullptr;
  BinaryOperator *inductionUpdate = nullptr;
  Value *tripCount = nullptr;
  uint64_t maximumTripCount = 0;
  SmallVector<BasicBlock *, kMaxLoopBreaks + 2> loopBlocks;
  SmallVector<LoopBreak, kMaxLoopBreaks> breaks;
  SmallVector<AffineLoopState, kMaxLoopStates> states;
  std::optional<TriangularRecurrence> triangular;
};

struct AffineForm {
  APInt constant;
  std::vector<APInt> coefficients;
  SmallPtrSet<Instruction *, kMaxAffineExpressionInstructions> instructions;

  AffineForm(unsigned bitWidth, unsigned stateCount)
      : constant(bitWidth, 0),
        coefficients(stateCount, APInt(bitWidth, 0)) {}
};

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

std::string unsignedText(const APInt &value) {
  SmallString<40> storage;
  value.toString(storage, 10, false);
  return storage.str().str();
}

Metadata *integerMetadata(LLVMContext &context, unsigned bits,
                          uint64_t value) {
  return ConstantAsMetadata::get(
      ConstantInt::get(IntegerType::get(context, bits), value));
}

std::optional<uint64_t> unsignedUpperBound(Value *value,
                                           unsigned depth = 0) {
  if (value == nullptr || depth > 8 || !value->getType()->isIntegerTy())
    return std::nullopt;
  if (isa<UndefValue, PoisonValue>(value))
    return std::nullopt;
  unsigned bits = value->getType()->getIntegerBitWidth();
  if (bits <= 3)
    return (uint64_t{1} << bits) - 1;

  if (auto *constant = dyn_cast<ConstantInt>(value)) {
    if (constant->getValue().getActiveBits() > 64)
      return std::nullopt;
    return constant->getZExtValue();
  }
  if (auto *freeze = dyn_cast<FreezeInst>(value))
    return unsignedUpperBound(freeze->getOperand(0), depth + 1);
  if (auto *cast = dyn_cast<CastInst>(value)) {
    if (cast->getOpcode() == Instruction::Trunc) {
      unsigned destinationBits =
          cast->getType()->getIntegerBitWidth();
      if (destinationBits <= 3)
        return (uint64_t{1} << destinationBits) - 1;
    }
    if (cast->getOpcode() == Instruction::ZExt)
      return unsignedUpperBound(cast->getOperand(0), depth + 1);
    return std::nullopt;
  }
  if (auto *select = dyn_cast<SelectInst>(value)) {
    auto trueBound =
        unsignedUpperBound(select->getTrueValue(), depth + 1);
    auto falseBound =
        unsignedUpperBound(select->getFalseValue(), depth + 1);
    if (!trueBound || !falseBound)
      return std::nullopt;
    return std::max(*trueBound, *falseBound);
  }
  auto *binary = dyn_cast<BinaryOperator>(value);
  if (binary == nullptr)
    return std::nullopt;
  if (binary->getOpcode() == Instruction::And) {
    auto *left = dyn_cast<ConstantInt>(binary->getOperand(0));
    auto *right = dyn_cast<ConstantInt>(binary->getOperand(1));
    ConstantInt *mask = left != nullptr ? left : right;
    if (mask != nullptr && mask->getValue().getActiveBits() <= 64)
      return mask->getZExtValue();
  }
  if (binary->getOpcode() == Instruction::URem) {
    auto *divisor = dyn_cast<ConstantInt>(binary->getOperand(1));
    if (divisor != nullptr && !divisor->isZero() &&
        divisor->getValue().getActiveBits() <= 64)
      return divisor->getZExtValue() - 1;
  }
  return std::nullopt;
}

bool isPlainAdd(BinaryOperator &operation) {
  return operation.getOpcode() == Instruction::Add &&
         !operation.hasNoUnsignedWrap() &&
         !operation.hasNoSignedWrap();
}

bool usesPhiAndConstant(BinaryOperator &operation, PHINode &phi,
                        ConstantInt *&constant) {
  if (!isPlainAdd(operation))
    return false;
  if (operation.getOperand(0) == &phi)
    constant = dyn_cast<ConstantInt>(operation.getOperand(1));
  else if (operation.getOperand(1) == &phi)
    constant = dyn_cast<ConstantInt>(operation.getOperand(0));
  else
    return false;
  return constant != nullptr && constant->getType() == phi.getType();
}

bool mergeAffineForm(AffineForm &destination, const AffineForm &source,
                     bool subtract) {
  if (destination.constant.getBitWidth() !=
          source.constant.getBitWidth() ||
      destination.coefficients.size() != source.coefficients.size())
    return false;
  destination.constant =
      subtract ? destination.constant - source.constant
               : destination.constant + source.constant;
  for (unsigned index = 0; index < destination.coefficients.size();
       ++index)
    destination.coefficients[index] =
        subtract
            ? destination.coefficients[index] -
                  source.coefficients[index]
            : destination.coefficients[index] +
                  source.coefficients[index];
  destination.instructions.insert(
      source.instructions.begin(), source.instructions.end());
  return true;
}

bool scaleAffineForm(AffineForm &form, const APInt &factor) {
  if (form.constant.getBitWidth() != factor.getBitWidth())
    return false;
  form.constant *= factor;
  for (APInt &coefficient : form.coefficients)
    coefficient *= factor;
  return true;
}

bool parseAffineForm(
    Value *value, ArrayRef<PHINode *> states, BasicBlock &latch,
    unsigned bitWidth, AffineForm &result, unsigned depth = 0) {
  if (value == nullptr || depth > 16 ||
      !value->getType()->isIntegerTy(bitWidth))
    return false;
  if (auto *constant = dyn_cast<ConstantInt>(value)) {
    result.constant = constant->getValue();
    return true;
  }
  for (unsigned ordinal = 0; ordinal < states.size(); ++ordinal)
    if (value == states[ordinal]) {
      result.coefficients[ordinal] = APInt(bitWidth, 1);
      return true;
    }

  auto *operation = dyn_cast<BinaryOperator>(value);
  if (operation == nullptr || operation->getParent() != &latch ||
      operation->hasNoUnsignedWrap() ||
      operation->hasNoSignedWrap())
    return false;
  if (operation->getOpcode() != Instruction::Add &&
      operation->getOpcode() != Instruction::Sub &&
      operation->getOpcode() != Instruction::Mul)
    return false;

  if (operation->getOpcode() == Instruction::Mul) {
    auto *leftConstant =
        dyn_cast<ConstantInt>(operation->getOperand(0));
    auto *rightConstant =
        dyn_cast<ConstantInt>(operation->getOperand(1));
    ConstantInt *factor =
        leftConstant != nullptr ? leftConstant : rightConstant;
    Value *operand =
        leftConstant != nullptr ? operation->getOperand(1)
                                : operation->getOperand(0);
    if (factor == nullptr ||
        !parseAffineForm(
            operand, states, latch, bitWidth, result, depth + 1) ||
        !scaleAffineForm(result, factor->getValue()))
      return false;
  } else {
    AffineForm left(bitWidth, states.size());
    AffineForm right(bitWidth, states.size());
    if (!parseAffineForm(
            operation->getOperand(0), states, latch, bitWidth, left,
            depth + 1) ||
        !parseAffineForm(
            operation->getOperand(1), states, latch, bitWidth, right,
            depth + 1) ||
        !mergeAffineForm(left, right,
                         operation->getOpcode() == Instruction::Sub))
      return false;
    result = std::move(left);
  }
  result.instructions.insert(operation);
  return result.instructions.size() <= kMaxAffineExpressionInstructions;
}

bool collectTriangularRecurrence(
    ArrayRef<AffineLoopState> states, BasicBlock &latch,
    TriangularRecurrence &recurrence,
    SmallPtrSetImpl<Instruction *> &allowedUpdates) {
  if (states.empty() || states.size() > kMaxTriangularLoopStates)
    return false;
  auto *stateType = dyn_cast<IntegerType>(states.front().phi->getType());
  if (stateType == nullptr || stateType->getBitWidth() > 4096)
    return false;
  for (const AffineLoopState &state : states)
    if (state.phi->getType() != stateType ||
        (!isa<ConstantInt>(state.initialValue) &&
         !isa<Argument>(state.initialValue) &&
         !isa<Instruction>(state.initialValue)))
      return false;

  const unsigned bitWidth = stateType->getBitWidth();
  recurrence.bitWidth = bitWidth;
  recurrence.matrix.reserve(states.size());
  recurrence.offset.reserve(states.size());
  bool hasCrossStateTerm = false;
  SmallVector<PHINode *, kMaxLoopStates> statePhis;
  for (const AffineLoopState &state : states)
    statePhis.push_back(state.phi);

  for (unsigned row = 0; row < states.size(); ++row) {
    AffineForm form(bitWidth, states.size());
    if (!parseAffineForm(
            states[row].update, statePhis, latch, bitWidth, form))
      return false;
    if (!form.coefficients[row].isOne())
      return false;
    for (unsigned column = 0; column < row; ++column)
      if (!form.coefficients[column].isZero())
        return false;
    for (unsigned column = row + 1; column < states.size(); ++column)
      hasCrossStateTerm |= !form.coefficients[column].isZero();
    allowedUpdates.insert(
        form.instructions.begin(), form.instructions.end());
    recurrence.matrix.push_back(std::move(form.coefficients));
    recurrence.offset.push_back(std::move(form.constant));
  }
  return hasCrossStateTerm;
}

bool collectAffineLoop(Loop &loop, AffineLoopRegion &result) {
  if (loop.getNumBlocks() < 2 ||
      loop.getNumBlocks() > kMaxLoopBreaks + 2 ||
      loop.getNumBackEdges() != 1)
    return false;
  BasicBlock *preheader = loop.getLoopPreheader();
  BasicBlock *header = loop.getHeader();
  BasicBlock *latch = loop.getLoopLatch();
  if (preheader == nullptr || header == nullptr || latch == nullptr ||
      header == latch || preheader->hasAddressTaken() ||
      header->hasAddressTaken() || preheader->isEHPad() ||
      header->isEHPad())
    return false;

  auto *preheaderBranch =
      dyn_cast<BranchInst>(preheader->getTerminator());
  auto *headerBranch = dyn_cast<BranchInst>(header->getTerminator());
  if (preheaderBranch == nullptr || !preheaderBranch->isUnconditional() ||
      preheaderBranch->getSuccessor(0) != header ||
      headerBranch == nullptr || !headerBranch->isConditional() ||
      !loop.contains(headerBranch->getSuccessor(0)))
    return false;
  BasicBlock *updateBlock = headerBranch->getSuccessor(0);
  if (updateBlock == header || updateBlock->hasAddressTaken() ||
      updateBlock->isEHPad())
    return false;
  BasicBlock *exit = headerBranch->getSuccessor(1);
  if (loop.contains(exit) || exit->hasAddressTaken() || exit->isEHPad())
    return false;

  SmallVector<LoopBreak, kMaxLoopBreaks> breaks;
  SmallPtrSet<BasicBlock *, kMaxLoopBreaks + 1> chainBlocks;
  SmallPtrSet<BasicBlock *, kMaxLoopBreaks> breakExits;
  BasicBlock *current = updateBlock;
  bool first = true;
  while (true) {
    if (!loop.contains(current) || current == header ||
        current->hasAddressTaken() || current->isEHPad() ||
        !chainBlocks.insert(current).second)
      return false;
    auto *branch = dyn_cast<BranchInst>(current->getTerminator());
    if (branch == nullptr)
      return false;
    if (branch->isUnconditional()) {
      BasicBlock *next = branch->getSuccessor(0);
      if (next == header)
        break;
      if (!first || !loop.contains(next) ||
          next->getUniquePredecessor() != current)
        return false;
      current = next;
      first = false;
      continue;
    }
    if (breaks.size() >= kMaxLoopBreaks)
      return false;
    BasicBlock *breakExit = branch->getSuccessor(0);
    BasicBlock *next = branch->getSuccessor(1);
    if (breakExit == exit || loop.contains(breakExit) ||
        breakExit->hasAddressTaken() || breakExit->isEHPad() ||
        !breakExits.insert(breakExit).second ||
        !loop.contains(next) ||
        (next != header && next->getUniquePredecessor() != current))
      return false;
    auto *condition = dyn_cast<ICmpInst>(branch->getCondition());
    if (condition == nullptr)
      return false;
    breaks.push_back(
        {current, branch, condition, breakExit, nullptr});
    if (next == header)
      break;
    current = next;
    first = false;
  }
  if (current != latch ||
      loop.getNumBlocks() != chainBlocks.size() + 1)
    return false;
  SmallVector<BasicBlock *, kMaxLoopBreaks + 1> exitBlocks;
  loop.getExitBlocks(exitBlocks);
  if (exitBlocks.size() != breaks.size() + 1 ||
      std::find(exitBlocks.begin(), exitBlocks.end(), exit) ==
          exitBlocks.end())
    return false;
  for (const LoopBreak &loopBreak : breaks)
    if (std::find(
            exitBlocks.begin(), exitBlocks.end(), loopBreak.exit) ==
        exitBlocks.end())
      return false;

  auto *condition = dyn_cast<ICmpInst>(headerBranch->getCondition());
  if (condition == nullptr ||
      condition->getPredicate() != ICmpInst::ICMP_ULT)
    return false;
  auto *induction = dyn_cast<PHINode>(condition->getOperand(0));
  Value *tripCount = condition->getOperand(1);
  if (induction == nullptr || induction->getParent() != header ||
      !tripCount->getType()->isIntegerTy() ||
      induction->getType() != tripCount->getType())
    return false;
  if (auto *tripInstruction = dyn_cast<Instruction>(tripCount))
    if (loop.contains(tripInstruction->getParent()))
      return false;

  auto maximumTripCount = unsignedUpperBound(tripCount);
  if (!maximumTripCount || *maximumTripCount > kMaxLoopTripCount)
    return false;

  if (induction->getNumIncomingValues() != 2)
    return false;
  auto *initialInduction =
      dyn_cast_or_null<ConstantInt>(
          induction->getIncomingValueForBlock(preheader));
  auto *inductionUpdate =
      dyn_cast_or_null<BinaryOperator>(
          induction->getIncomingValueForBlock(latch));
  ConstantInt *inductionStep = nullptr;
  if (initialInduction == nullptr || !initialInduction->isZero() ||
      inductionUpdate == nullptr ||
      !usesPhiAndConstant(
          *inductionUpdate, *induction, inductionStep) ||
      !inductionStep->isOne())
    return false;

  for (LoopBreak &loopBreak : breaks) {
    ICmpInst *breakCondition = loopBreak.condition;
    if (breakCondition->getPredicate() != ICmpInst::ICMP_EQ)
      return false;
    Value *breakValue = nullptr;
    if (breakCondition->getOperand(0) == induction)
      breakValue = breakCondition->getOperand(1);
    else if (breakCondition->getOperand(1) == induction)
      breakValue = breakCondition->getOperand(0);
    else
      return false;
    if (breakValue->getType() != induction->getType() ||
        isa<UndefValue, PoisonValue>(breakValue))
      return false;
    if (auto *breakInstruction = dyn_cast<Instruction>(breakValue))
      if (loop.contains(breakInstruction->getParent()))
        return false;
    loopBreak.value = breakValue;
  }

  SmallPtrSet<Instruction *, kMaxLoopStates + 1> allowedUpdates;
  allowedUpdates.insert(inductionUpdate);
  SmallVector<AffineLoopState, kMaxLoopStates> states;
  for (PHINode &phi : header->phis()) {
    if (&phi == induction)
      continue;
    if (!phi.getType()->isIntegerTy() ||
        phi.getNumIncomingValues() != 2 ||
        states.size() >= kMaxLoopStates)
      return false;
    Value *initial = phi.getIncomingValueForBlock(preheader);
    auto *update = dyn_cast_or_null<Instruction>(
        phi.getIncomingValueForBlock(latch));
    if (initial == nullptr || isa<UndefValue, PoisonValue>(initial) ||
        update == nullptr || update->getParent() != updateBlock)
      return false;
    states.push_back({&phi, initial, nullptr, update});
  }
  if (states.empty())
    return false;

  bool independent = true;
  for (AffineLoopState &state : states) {
    auto *update = dyn_cast<BinaryOperator>(state.update);
    if (update == nullptr ||
        !usesPhiAndConstant(*update, *state.phi, state.step)) {
      independent = false;
      break;
    }
  }
  std::optional<TriangularRecurrence> triangular;
  if (independent) {
    for (const AffineLoopState &state : states)
      allowedUpdates.insert(state.update);
  } else {
    TriangularRecurrence recurrence;
    if (!collectTriangularRecurrence(
            states, *updateBlock, recurrence, allowedUpdates))
      return false;
    triangular = std::move(recurrence);
  }

  for (Instruction &instruction : *header)
    if (!isa<PHINode>(&instruction) && &instruction != condition &&
        &instruction != headerBranch &&
        !isa<DbgInfoIntrinsic>(&instruction))
      return false;
  for (BasicBlock *block : chainBlocks) {
    const LoopBreak *blockBreak = nullptr;
    for (const LoopBreak &loopBreak : breaks)
      if (loopBreak.checkBlock == block) {
        blockBreak = &loopBreak;
        break;
      }
    for (Instruction &instruction : *block) {
      if (isa<DbgInfoIntrinsic>(&instruction))
        continue;
      if (&instruction == block->getTerminator())
        continue;
      if (block == updateBlock &&
          allowedUpdates.count(&instruction) != 0)
        continue;
      if (blockBreak != nullptr &&
          &instruction == blockBreak->condition)
        continue;
      return false;
    }
  }

  bool hasObservedState = false;
  SmallPtrSet<Value *, 32> summarizedValues;
  summarizedValues.insert(induction);
  if (!breaks.empty())
    summarizedValues.insert(inductionUpdate);
  for (const AffineLoopState &state : states) {
    summarizedValues.insert(state.phi);
    if (!breaks.empty())
      summarizedValues.insert(state.update);
  }
  for (BasicBlock *block : loop.blocks())
    for (Instruction &instruction : *block)
      for (User *user : instruction.users()) {
        auto *userInstruction = dyn_cast<Instruction>(user);
        if (userInstruction != nullptr &&
            loop.contains(userInstruction->getParent()))
          continue;
        if (summarizedValues.count(&instruction) == 0)
          return false;
        if (!breaks.empty()) {
          auto *exitPhi = dyn_cast_or_null<PHINode>(userInstruction);
          const bool beforeUpdate =
              &instruction == induction ||
              std::any_of(
                  states.begin(), states.end(),
                  [&](const AffineLoopState &state) {
                    return &instruction == state.phi;
                  });
          if (exitPhi == nullptr)
            return false;
          if (beforeUpdate) {
            if (exitPhi->getParent() != exit)
              return false;
          } else if (std::none_of(
                         breaks.begin(), breaks.end(),
                         [&](const LoopBreak &loopBreak) {
                           return exitPhi->getParent() ==
                                  loopBreak.exit;
                         })) {
            return false;
          }
        }
        if (&instruction != induction &&
            &instruction != inductionUpdate)
          hasObservedState = true;
      }
  if (!hasObservedState)
    return false;

  auto validateExitPhis = [&](BasicBlock &exitBlock,
                               BasicBlock &source,
                               bool afterUpdate) {
    for (PHINode &phi : exitBlock.phis()) {
      unsigned sourceEdges = 0;
      for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
        if (phi.getIncomingBlock(index) != &source)
          continue;
        ++sourceEdges;
        Value *incoming = phi.getIncomingValue(index);
        auto *incomingInstruction = dyn_cast<Instruction>(incoming);
        if (incomingInstruction == nullptr ||
            !loop.contains(incomingInstruction->getParent()))
          continue;
        bool valid =
            afterUpdate
                ? incomingInstruction == inductionUpdate ||
                      std::any_of(
                          states.begin(), states.end(),
                          [&](const AffineLoopState &state) {
                            return incomingInstruction == state.update;
                          })
                : incomingInstruction == induction ||
                      std::any_of(
                          states.begin(), states.end(),
                          [&](const AffineLoopState &state) {
                            return incomingInstruction == state.phi;
                          });
        if (!valid)
          return false;
      }
      if (sourceEdges != 1)
        return false;
    }
    return true;
  };
  if (!validateExitPhis(*exit, *header, false))
    return false;
  for (const LoopBreak &loopBreak : breaks)
    if (!validateExitPhis(
            *loopBreak.exit, *loopBreak.checkBlock, true))
      return false;

  result.preheader = preheader;
  result.header = header;
  result.updateBlock = updateBlock;
  result.latch = latch;
  result.exit = exit;
  result.headerBranch = headerBranch;
  result.induction = induction;
  result.inductionUpdate = inductionUpdate;
  result.tripCount = tripCount;
  result.maximumTripCount = *maximumTripCount;
  result.loopBlocks.push_back(header);
  for (BasicBlock *block : chainBlocks)
    result.loopBlocks.push_back(block);
  result.breaks = std::move(breaks);
  result.states = std::move(states);
  result.triangular = std::move(triangular);
  return true;
}

MDNode *loopSummaryMetadata(LLVMContext &context, uint64_t headerSite,
                            uint64_t branchSite, uint64_t maximumTripCount,
                            unsigned stateCount, unsigned stateOrdinal,
                            uint64_t phiSite, uint64_t updateSite) {
  Metadata *operands[] = {
      MDString::get(context, kLoopSummarySchema),
      integerMetadata(context, 64, headerSite),
      integerMetadata(context, 64, branchSite),
      integerMetadata(context, 64, maximumTripCount),
      integerMetadata(context, 32, stateCount),
      integerMetadata(context, 32, stateOrdinal),
      integerMetadata(context, 64, phiSite),
      integerMetadata(context, 64, updateSite),
  };
  return MDNode::get(context, operands);
}

std::string initialValueIdentity(const Value &value) {
  if (const auto *constant = dyn_cast<ConstantInt>(&value))
    return "constant:" + unsignedText(constant->getValue());
  if (const auto *argument = dyn_cast<Argument>(&value))
    return "argument:" + std::to_string(argument->getArgNo());
  if (const auto *instruction = dyn_cast<Instruction>(&value))
    return "instruction:" + std::to_string(stableSiteId(*instruction));
  std::string text;
  raw_string_ostream stream(text);
  value.printAsOperand(stream, false);
  stream.flush();
  return "operand:" + text;
}

uint64_t recurrenceFingerprint(const AffineLoopRegion &region) {
  assert(region.triangular && "missing triangular recurrence");
  const TriangularRecurrence &recurrence = *region.triangular;
  uint64_t fingerprint = mixSiteIdText(
      1469598103934665603ULL, kTriangularLoopSummarySchema);
  fingerprint = mixSiteIdText(
      fingerprint, stableModuleIdentity(*region.header->getModule()));
  fingerprint = mixSiteIdText(
      fingerprint, region.header->getParent()->getName());
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.header));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.headerBranch));
  fingerprint =
      mixSiteIdInteger(fingerprint, region.maximumTripCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, recurrence.bitWidth);
  fingerprint =
      mixSiteIdInteger(fingerprint, region.states.size());
  for (unsigned row = 0; row < region.states.size(); ++row) {
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*region.states[row].phi));
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*region.states[row].update));
    fingerprint = mixSiteIdText(
        fingerprint,
        initialValueIdentity(*region.states[row].initialValue));
    for (const APInt &coefficient : recurrence.matrix[row])
      fingerprint =
          mixSiteIdText(fingerprint, unsignedText(coefficient));
    fingerprint = mixSiteIdText(
        fingerprint, unsignedText(recurrence.offset[row]));
  }
  return fingerprint;
}

MDNode *triangularLoopSummaryMetadata(
    LLVMContext &context, uint64_t headerSite, uint64_t branchSite,
    uint64_t maximumTripCount, unsigned stateCount,
    unsigned stateOrdinal, unsigned bitWidth, uint64_t fingerprint,
    uint64_t phiSite, uint64_t updateSite) {
  Metadata *operands[] = {
      MDString::get(context, kTriangularLoopSummarySchema),
      integerMetadata(context, 64, headerSite),
      integerMetadata(context, 64, branchSite),
      integerMetadata(context, 64, maximumTripCount),
      integerMetadata(context, 32, stateCount),
      integerMetadata(context, 32, stateOrdinal),
      integerMetadata(context, 32, bitWidth),
      integerMetadata(context, 64, fingerprint),
      integerMetadata(context, 64, phiSite),
      integerMetadata(context, 64, updateSite),
  };
  return MDNode::get(context, operands);
}

MDNode *breakLoopSummaryMetadata(
    LLVMContext &context, uint64_t headerSite,
    uint64_t headerBranchSite, uint64_t breakConditionSite,
    uint64_t normalExitSite, uint64_t breakExitSite,
    uint64_t maximumTripCount, unsigned stateCount) {
  Metadata *operands[] = {
      MDString::get(context, kBreakLoopSummarySchema),
      integerMetadata(context, 64, headerSite),
      integerMetadata(context, 64, headerBranchSite),
      integerMetadata(context, 64, breakConditionSite),
      integerMetadata(context, 64, normalExitSite),
      integerMetadata(context, 64, breakExitSite),
      integerMetadata(context, 64, maximumTripCount),
      integerMetadata(context, 32, stateCount),
  };
  return MDNode::get(context, operands);
}

MDNode *multiBreakLoopSummaryMetadata(
    LLVMContext &context, const AffineLoopRegion &region,
    uint64_t headerSite, uint64_t headerBranchSite) {
  SmallVector<Metadata *, 16> operands;
  operands.push_back(
      MDString::get(context, kMultiBreakLoopSummarySchema));
  operands.push_back(integerMetadata(context, 64, headerSite));
  operands.push_back(
      integerMetadata(context, 64, headerBranchSite));
  operands.push_back(
      integerMetadata(context, 64, stableSiteId(*region.exit)));
  operands.push_back(integerMetadata(
      context, 64, region.maximumTripCount));
  operands.push_back(
      integerMetadata(context, 32, region.states.size()));
  operands.push_back(
      integerMetadata(context, 32, region.breaks.size()));
  for (const LoopBreak &loopBreak : region.breaks) {
    operands.push_back(integerMetadata(
        context, 64, stableSiteId(*loopBreak.condition)));
    operands.push_back(integerMetadata(
        context, 64, stableSiteId(*loopBreak.exit)));
  }
  return MDNode::get(context, operands);
}

struct RecurrencePower {
  std::vector<std::vector<APInt>> matrix;
  std::vector<APInt> offset;
};

std::vector<RecurrencePower> recurrencePowers(
    const TriangularRecurrence &recurrence,
    unsigned maximumTripCount) {
  const unsigned stateCount = recurrence.matrix.size();
  const unsigned bitWidth = recurrence.bitWidth;
  std::vector<RecurrencePower> powers;
  RecurrencePower current;
  current.matrix.assign(
      stateCount,
      std::vector<APInt>(stateCount, APInt(bitWidth, 0)));
  current.offset.assign(stateCount, APInt(bitWidth, 0));
  for (unsigned ordinal = 0; ordinal < stateCount; ++ordinal)
    current.matrix[ordinal][ordinal] = APInt(bitWidth, 1);
  powers.push_back(current);

  for (unsigned trip = 1; trip <= maximumTripCount; ++trip) {
    RecurrencePower next;
    next.matrix.assign(
        stateCount,
        std::vector<APInt>(stateCount, APInt(bitWidth, 0)));
    next.offset.assign(stateCount, APInt(bitWidth, 0));
    for (unsigned row = 0; row < stateCount; ++row) {
      next.offset[row] = recurrence.offset[row];
      for (unsigned middle = 0; middle < stateCount; ++middle) {
        next.offset[row] +=
            recurrence.matrix[row][middle] * current.offset[middle];
        for (unsigned column = 0; column < stateCount; ++column)
          next.matrix[row][column] +=
              recurrence.matrix[row][middle] *
              current.matrix[middle][column];
      }
    }
    powers.push_back(next);
    current = std::move(next);
  }
  return powers;
}

std::string buildLoopManifestLine(const AffineLoopRegion &region) {
  if (!region.triangular)
    return {};
  const TriangularRecurrence &recurrence = *region.triangular;
  const uint64_t fingerprint = recurrenceFingerprint(region);
  const std::vector<RecurrencePower> powers = recurrencePowers(
      recurrence, region.maximumTripCount);

  json::Object record;
  record["schema"] = kLoopManifestSchema;
  record["recurrence_schema"] = kTriangularLoopSummarySchema;
  record["module"] =
      stableModuleIdentity(*region.header->getModule()).str();
  record["function"] = region.header->getParent()->getName().str();
  record["header_site"] =
      std::to_string(stableSiteId(*region.header));
  record["branch_site"] =
      std::to_string(stableSiteId(*region.headerBranch));
  record["maximum_trip_count"] =
      static_cast<int64_t>(region.maximumTripCount);
  record["state_count"] =
      static_cast<int64_t>(region.states.size());
  record["state_bits"] =
      static_cast<int64_t>(recurrence.bitWidth);

  json::Array states;
  for (unsigned ordinal = 0; ordinal < region.states.size(); ++ordinal) {
    const AffineLoopState &state = region.states[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["phi_site"] = std::to_string(stableSiteId(*state.phi));
    item["update_site"] =
        std::to_string(stableSiteId(*state.update));
    item["initial_identity"] =
        initialValueIdentity(*state.initialValue);
    json::Array coefficients;
    for (const APInt &coefficient : recurrence.matrix[ordinal])
      coefficients.push_back(unsignedText(coefficient));
    item["coefficients"] = std::move(coefficients);
    item["offset"] = unsignedText(recurrence.offset[ordinal]);
    states.push_back(std::move(item));
  }
  record["states"] = std::move(states);

  json::Array powerRecords;
  for (unsigned trip = 0; trip < powers.size(); ++trip) {
    json::Object item;
    item["trip"] = static_cast<int64_t>(trip);
    json::Array matrix;
    for (const std::vector<APInt> &row : powers[trip].matrix) {
      json::Array coefficients;
      for (const APInt &coefficient : row)
        coefficients.push_back(unsignedText(coefficient));
      matrix.push_back(std::move(coefficients));
    }
    item["matrix"] = std::move(matrix);
    json::Array offset;
    for (const APInt &value : powers[trip].offset)
      offset.push_back(unsignedText(value));
    item["offset"] = std::move(offset);
    powerRecords.push_back(std::move(item));
  }
  record["powers"] = std::move(powerRecords);
  record["proof_fingerprint"] = std::to_string(fingerprint);
  return formatv("{0}", json::Value(std::move(record))).str();
}

uint64_t breakLoopFingerprint(const AffineLoopRegion &region) {
  assert(region.breaks.size() == 1 && "missing single break exit");
  const LoopBreak &loopBreak = region.breaks.front();
  uint64_t fingerprint =
      mixSiteIdText(1469598103934665603ULL, kBreakLoopManifestSchema);
  fingerprint = mixSiteIdText(
      fingerprint, stableModuleIdentity(*region.header->getModule()));
  fingerprint = mixSiteIdText(
      fingerprint, region.header->getParent()->getName());
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.header));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.headerBranch));
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*loopBreak.condition));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.exit));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*loopBreak.exit));
  fingerprint =
      mixSiteIdInteger(fingerprint, region.maximumTripCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, region.states.size());
  fingerprint = mixSiteIdText(
      fingerprint, initialValueIdentity(*region.tripCount));
  fingerprint = mixSiteIdText(
      fingerprint, initialValueIdentity(*loopBreak.value));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.induction));
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*region.inductionUpdate));
  fingerprint = mixSiteIdText(
      fingerprint,
      region.triangular ? "upper-triangular-v1" : "independent-add-v1");
  if (region.triangular) {
    fingerprint =
        mixSiteIdInteger(fingerprint, recurrenceFingerprint(region));
  } else {
    for (const AffineLoopState &state : region.states)
      fingerprint =
          mixSiteIdText(fingerprint, unsignedText(state.step->getValue()));
  }
  for (const AffineLoopState &state : region.states) {
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*state.phi));
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*state.update));
    fingerprint = mixSiteIdInteger(
        fingerprint, state.phi->getType()->getIntegerBitWidth());
    fingerprint = mixSiteIdText(
        fingerprint, initialValueIdentity(*state.initialValue));
  }
  for (uint64_t trip = 0; trip <= region.maximumTripCount; ++trip)
    for (uint64_t breakAt = 0;
         breakAt <= region.maximumTripCount; ++breakAt) {
      const bool taken = breakAt < trip;
      fingerprint = mixSiteIdInteger(fingerprint, trip);
      fingerprint = mixSiteIdInteger(fingerprint, breakAt);
      fingerprint = mixSiteIdInteger(fingerprint, taken ? 1 : 0);
      fingerprint = mixSiteIdInteger(
          fingerprint, taken ? breakAt + 1 : trip);
    }
  return fingerprint;
}

std::string buildBreakLoopManifestLine(
    const AffineLoopRegion &region) {
  if (region.breaks.size() != 1)
    return {};
  const LoopBreak &loopBreak = region.breaks.front();
  const uint64_t fingerprint = breakLoopFingerprint(region);
  json::Object record;
  record["schema"] = kBreakLoopManifestSchema;
  record["exit_semantics"] = "post-update-equality-break-v1";
  record["module"] =
      stableModuleIdentity(*region.header->getModule()).str();
  record["function"] = region.header->getParent()->getName().str();
  record["header_site"] =
      std::to_string(stableSiteId(*region.header));
  record["header_branch_site"] =
      std::to_string(stableSiteId(*region.headerBranch));
  record["break_condition_site"] =
      std::to_string(stableSiteId(*loopBreak.condition));
  record["normal_exit_site"] =
      std::to_string(stableSiteId(*region.exit));
  record["break_exit_site"] =
      std::to_string(stableSiteId(*loopBreak.exit));
  record["maximum_trip_count"] =
      static_cast<int64_t>(region.maximumTripCount);
  record["state_count"] =
      static_cast<int64_t>(region.states.size());
  record["trip_identity"] =
      initialValueIdentity(*region.tripCount);
  record["break_value_identity"] =
      initialValueIdentity(*loopBreak.value);
  record["induction_phi_site"] =
      std::to_string(stableSiteId(*region.induction));
  record["induction_update_site"] =
      std::to_string(stableSiteId(*region.inductionUpdate));
  record["recurrence_kind"] =
      region.triangular ? "upper-triangular-v1"
                        : "independent-add-v1";
  record["recurrence_fingerprint"] =
      region.triangular
          ? std::to_string(recurrenceFingerprint(region))
          : "";

  json::Array states;
  for (unsigned ordinal = 0; ordinal < region.states.size(); ++ordinal) {
    const AffineLoopState &state = region.states[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["phi_site"] = std::to_string(stableSiteId(*state.phi));
    item["update_site"] =
        std::to_string(stableSiteId(*state.update));
    item["initial_identity"] =
        initialValueIdentity(*state.initialValue);
    item["bits"] = static_cast<int64_t>(
        state.phi->getType()->getIntegerBitWidth());
    item["step"] =
        region.triangular ? "" : unsignedText(state.step->getValue());
    item["normal_liveout_site"] =
        std::to_string(stableSiteId(*state.phi));
    item["break_liveout_site"] =
        std::to_string(stableSiteId(*state.update));
    states.push_back(std::move(item));
  }
  record["states"] = std::move(states);

  json::Array table;
  for (uint64_t trip = 0; trip <= region.maximumTripCount; ++trip)
    for (uint64_t breakAt = 0;
         breakAt <= region.maximumTripCount; ++breakAt) {
      const bool taken = breakAt < trip;
      json::Object item;
      item["trip"] = static_cast<int64_t>(trip);
      item["break_at"] = static_cast<int64_t>(breakAt);
      item["break_taken"] = taken;
      item["executions"] =
          static_cast<int64_t>(taken ? breakAt + 1 : trip);
      table.push_back(std::move(item));
    }
  record["execution_table"] = std::move(table);
  record["proof_fingerprint"] = std::to_string(fingerprint);
  return formatv("{0}", json::Value(std::move(record))).str();
}

SmallVector<uint64_t, kMaxLoopBreaks> breakValuesForOrdinal(
    uint64_t ordinal, uint64_t side, unsigned breakCount) {
  SmallVector<uint64_t, kMaxLoopBreaks> values(breakCount, 0);
  for (unsigned index = breakCount; index-- > 0;) {
    values[index] = ordinal % side;
    ordinal /= side;
  }
  return values;
}

std::pair<int, uint64_t> priorityBreakDecision(
    uint64_t trip, ArrayRef<uint64_t> breakValues) {
  int winner = -1;
  uint64_t winnerAt = trip;
  for (unsigned ordinal = 0; ordinal < breakValues.size(); ++ordinal)
    if (breakValues[ordinal] < winnerAt) {
      winner = static_cast<int>(ordinal);
      winnerAt = breakValues[ordinal];
    }
  return {winner, winner >= 0 ? winnerAt + 1 : trip};
}

uint64_t multiBreakLoopFingerprint(const AffineLoopRegion &region) {
  assert(region.breaks.size() >= 2 && "missing multiple breaks");
  uint64_t fingerprint =
      mixSiteIdText(1469598103934665603ULL, kBreakLoopManifestSchema);
  fingerprint =
      mixSiteIdText(fingerprint, kMultiBreakLoopSummarySchema);
  fingerprint = mixSiteIdText(
      fingerprint, stableModuleIdentity(*region.header->getModule()));
  fingerprint = mixSiteIdText(
      fingerprint, region.header->getParent()->getName());
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.header));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.headerBranch));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.exit));
  fingerprint =
      mixSiteIdInteger(fingerprint, region.maximumTripCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, region.states.size());
  fingerprint =
      mixSiteIdInteger(fingerprint, region.breaks.size());
  for (unsigned ordinal = 0; ordinal < region.breaks.size(); ++ordinal) {
    const LoopBreak &loopBreak = region.breaks[ordinal];
    fingerprint = mixSiteIdInteger(fingerprint, ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*loopBreak.condition));
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*loopBreak.exit));
    fingerprint = mixSiteIdText(
        fingerprint, initialValueIdentity(*loopBreak.value));
  }
  fingerprint = mixSiteIdText(
      fingerprint, initialValueIdentity(*region.tripCount));
  fingerprint =
      mixSiteIdInteger(fingerprint, stableSiteId(*region.induction));
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*region.inductionUpdate));
  fingerprint = mixSiteIdText(
      fingerprint,
      region.triangular ? "upper-triangular-v1" : "independent-add-v1");
  if (region.triangular) {
    fingerprint =
        mixSiteIdInteger(fingerprint, recurrenceFingerprint(region));
  } else {
    for (const AffineLoopState &state : region.states)
      fingerprint =
          mixSiteIdText(fingerprint, unsignedText(state.step->getValue()));
  }
  for (const AffineLoopState &state : region.states) {
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*state.phi));
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*state.update));
    fingerprint = mixSiteIdInteger(
        fingerprint, state.phi->getType()->getIntegerBitWidth());
    fingerprint = mixSiteIdText(
        fingerprint, initialValueIdentity(*state.initialValue));
  }

  const uint64_t side = region.maximumTripCount + 1;
  uint64_t combinations = 1;
  for (unsigned ordinal = 0; ordinal < region.breaks.size(); ++ordinal)
    combinations *= side;
  for (uint64_t trip = 0; trip <= region.maximumTripCount; ++trip)
    for (uint64_t combination = 0; combination < combinations;
         ++combination) {
      SmallVector<uint64_t, kMaxLoopBreaks> values =
          breakValuesForOrdinal(
              combination, side, region.breaks.size());
      auto [winner, executions] =
          priorityBreakDecision(trip, values);
      fingerprint = mixSiteIdInteger(fingerprint, trip);
      for (uint64_t value : values)
        fingerprint = mixSiteIdInteger(fingerprint, value);
      fingerprint =
          mixSiteIdInteger(fingerprint, winner >= 0 ? 1 : 0);
      fingerprint = mixSiteIdInteger(
          fingerprint, static_cast<uint64_t>(winner + 1));
      fingerprint = mixSiteIdInteger(fingerprint, executions);
    }
  return fingerprint;
}

std::string buildMultiBreakLoopManifestLine(
    const AffineLoopRegion &region) {
  if (region.breaks.size() < 2)
    return {};
  json::Object record;
  record["schema"] = kBreakLoopManifestSchema;
  record["exit_semantics"] = kMultiBreakExitSemantics;
  record["module"] =
      stableModuleIdentity(*region.header->getModule()).str();
  record["function"] = region.header->getParent()->getName().str();
  record["header_site"] =
      std::to_string(stableSiteId(*region.header));
  record["header_branch_site"] =
      std::to_string(stableSiteId(*region.headerBranch));
  record["normal_exit_site"] =
      std::to_string(stableSiteId(*region.exit));
  record["maximum_trip_count"] =
      static_cast<int64_t>(region.maximumTripCount);
  record["state_count"] =
      static_cast<int64_t>(region.states.size());
  record["break_count"] =
      static_cast<int64_t>(region.breaks.size());
  record["trip_identity"] =
      initialValueIdentity(*region.tripCount);
  record["induction_phi_site"] =
      std::to_string(stableSiteId(*region.induction));
  record["induction_update_site"] =
      std::to_string(stableSiteId(*region.inductionUpdate));
  record["recurrence_kind"] =
      region.triangular ? "upper-triangular-v1"
                        : "independent-add-v1";
  record["recurrence_fingerprint"] =
      region.triangular
          ? std::to_string(recurrenceFingerprint(region))
          : "";

  json::Array breaks;
  for (unsigned ordinal = 0; ordinal < region.breaks.size(); ++ordinal) {
    const LoopBreak &loopBreak = region.breaks[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["condition_site"] =
        std::to_string(stableSiteId(*loopBreak.condition));
    item["exit_site"] =
        std::to_string(stableSiteId(*loopBreak.exit));
    item["value_identity"] =
        initialValueIdentity(*loopBreak.value);
    breaks.push_back(std::move(item));
  }
  record["breaks"] = std::move(breaks);

  json::Array states;
  for (unsigned ordinal = 0; ordinal < region.states.size(); ++ordinal) {
    const AffineLoopState &state = region.states[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["phi_site"] = std::to_string(stableSiteId(*state.phi));
    item["update_site"] =
        std::to_string(stableSiteId(*state.update));
    item["initial_identity"] =
        initialValueIdentity(*state.initialValue);
    item["bits"] = static_cast<int64_t>(
        state.phi->getType()->getIntegerBitWidth());
    item["step"] =
        region.triangular ? "" : unsignedText(state.step->getValue());
    item["normal_liveout_site"] =
        std::to_string(stableSiteId(*state.phi));
    json::Array breakLiveouts;
    for (unsigned breakOrdinal = 0;
         breakOrdinal < region.breaks.size(); ++breakOrdinal)
      breakLiveouts.push_back(
          std::to_string(stableSiteId(*state.update)));
    item["break_liveout_sites"] = std::move(breakLiveouts);
    states.push_back(std::move(item));
  }
  record["states"] = std::move(states);

  const uint64_t side = region.maximumTripCount + 1;
  uint64_t combinations = 1;
  for (unsigned ordinal = 0; ordinal < region.breaks.size(); ++ordinal)
    combinations *= side;
  json::Array table;
  for (uint64_t trip = 0; trip <= region.maximumTripCount; ++trip)
    for (uint64_t combination = 0; combination < combinations;
         ++combination) {
      SmallVector<uint64_t, kMaxLoopBreaks> values =
          breakValuesForOrdinal(
              combination, side, region.breaks.size());
      auto [winner, executions] =
          priorityBreakDecision(trip, values);
      json::Object item;
      item["trip"] = static_cast<int64_t>(trip);
      json::Array valueRecords;
      for (uint64_t value : values)
        valueRecords.push_back(static_cast<int64_t>(value));
      item["break_at"] = std::move(valueRecords);
      item["break_taken"] = winner >= 0;
      item["winner"] = static_cast<int64_t>(winner);
      item["executions"] = static_cast<int64_t>(executions);
      table.push_back(std::move(item));
    }
  record["execution_table"] = std::move(table);
  record["proof_fingerprint"] =
      std::to_string(multiBreakLoopFingerprint(region));
  return formatv("{0}", json::Value(std::move(record))).str();
}

void writeLoopManifest(StringRef path,
                       ArrayRef<std::string> records) {
  (void)appendManifestRecords(path, records);
}

void attachProof(Value *value, StringRef name, MDNode *proof) {
  if (auto *instruction = dyn_cast<Instruction>(value))
    instruction->setMetadata(name, proof);
}

using APMatrix = std::vector<std::vector<APInt>>;

APMatrix multiplyMatrices(const APMatrix &left, const APMatrix &right,
                          unsigned bitWidth) {
  const unsigned size = left.size();
  APMatrix result(
      size, std::vector<APInt>(size, APInt(bitWidth, 0)));
  for (unsigned row = 0; row < size; ++row)
    for (unsigned middle = 0; middle < size; ++middle)
      for (unsigned column = 0; column < size; ++column)
        result[row][column] +=
            left[row][middle] * right[middle][column];
  return result;
}

std::vector<APMatrix> nilpotentPowers(
    const TriangularRecurrence &recurrence) {
  const unsigned stateCount = recurrence.matrix.size();
  const unsigned bitWidth = recurrence.bitWidth;
  APMatrix nilpotent = recurrence.matrix;
  for (unsigned ordinal = 0; ordinal < stateCount; ++ordinal)
    nilpotent[ordinal][ordinal] -= APInt(bitWidth, 1);

  std::vector<APMatrix> powers;
  APMatrix identity(
      stateCount,
      std::vector<APInt>(stateCount, APInt(bitWidth, 0)));
  for (unsigned ordinal = 0; ordinal < stateCount; ++ordinal)
    identity[ordinal][ordinal] = APInt(bitWidth, 1);
  powers.push_back(std::move(identity));
  for (unsigned exponent = 1; exponent < stateCount; ++exponent)
    powers.push_back(
        multiplyMatrices(powers.back(), nilpotent, bitWidth));
  return powers;
}

Value *addScaledValue(IRBuilder<> &builder, Value *result, Value *value,
                      const APInt &scale, MDNode *proof) {
  if (scale.isZero())
    return result;
  Value *term = value;
  if (!scale.isOne()) {
    term = builder.CreateMul(
        term, ConstantInt::get(value->getType(), scale),
        "ifss.loop.affine.term");
    attachProof(term, "symcc.ifss_loop_summary", proof);
  }
  result =
      builder.CreateAdd(result, term, "ifss.loop.affine.value");
  attachProof(result, "symcc.ifss_loop_summary", proof);
  return result;
}

bool lowerTriangularLoop(AffineLoopRegion &region,
                         DenseMap<Value *, Value *> &summaries,
                         IRBuilder<> &builder, uint64_t headerSite,
                         uint64_t branchSite,
                         Value *executionCount) {
  assert(region.triangular && "missing triangular recurrence");
  const TriangularRecurrence &recurrence = *region.triangular;
  const uint64_t fingerprint = recurrenceFingerprint(region);
  const unsigned stateCount = region.states.size();
  const unsigned bitWidth = recurrence.bitWidth;
  std::vector<APMatrix> powers = nilpotentPowers(recurrence);

  Type *binomialType = Type::getInt16Ty(region.header->getContext());
  Value *trip = builder.CreateZExtOrTrunc(
      executionCount, binomialType, "ifss.loop.trip.wide");
  SmallVector<Value *, kMaxTriangularLoopStates + 1> binomials;
  binomials.push_back(ConstantInt::get(binomialType, 1));
  for (unsigned degree = 1; degree <= stateCount; ++degree) {
    Value *factor = builder.CreateSub(
        trip, ConstantInt::get(binomialType, degree - 1),
        "ifss.loop.binomial.factor");
    Value *product = builder.CreateMul(
        binomials.back(), factor, "ifss.loop.binomial.product");
    Value *binomial = builder.CreateUDiv(
        product, ConstantInt::get(binomialType, degree),
        "ifss.loop.binomial");
    binomials.push_back(binomial);
  }
  Type *stateType = region.states.front().phi->getType();
  SmallVector<Value *, kMaxTriangularLoopStates + 1> stateBinomials;
  for (Value *binomial : binomials)
    stateBinomials.push_back(builder.CreateZExtOrTrunc(
        binomial, stateType, "ifss.loop.binomial.state"));

  std::vector<std::vector<APInt>> offsetPowers(
      stateCount,
      std::vector<APInt>(stateCount, APInt(bitWidth, 0)));
  for (unsigned exponent = 0; exponent < stateCount; ++exponent)
    for (unsigned row = 0; row < stateCount; ++row)
      for (unsigned column = 0; column < stateCount; ++column)
        offsetPowers[exponent][row] +=
            powers[exponent][row][column] *
            recurrence.offset[column];

  for (unsigned ordinal = 0; ordinal < stateCount; ++ordinal) {
    AffineLoopState &state = region.states[ordinal];
    MDNode *proof = triangularLoopSummaryMetadata(
        region.header->getContext(), headerSite, branchSite,
        region.maximumTripCount, stateCount, ordinal,
        recurrence.bitWidth, fingerprint, stableSiteId(*state.phi),
        stableSiteId(*state.update));

    Value *summary = ConstantInt::get(stateType, 0);
    for (unsigned column = 0; column < stateCount; ++column)
      for (unsigned degree = 0; degree < stateCount; ++degree) {
        const APInt &coefficient = powers[degree][ordinal][column];
        if (coefficient.isZero())
          continue;
        Value *scaledInitial = builder.CreateMul(
            stateBinomials[degree],
            region.states[column].initialValue,
            "ifss.loop.initial.term");
        attachProof(
            scaledInitial, "symcc.ifss_loop_summary", proof);
        summary = addScaledValue(
            builder, summary, scaledInitial, coefficient, proof);
      }
    for (unsigned degree = 0; degree < stateCount; ++degree)
      summary = addScaledValue(
          builder, summary, stateBinomials[degree + 1],
          offsetPowers[degree][ordinal], proof);

    if (auto *instruction = dyn_cast<Instruction>(summary))
      instruction->setName("ifss.loop.state");
    attachProof(summary, "symcc.ifss_loop_summary", proof);
    summaries[state.phi] = summary;
  }
  return true;
}

bool lowerAffineLoop(AffineLoopRegion &region) {
  LLVMContext &context = region.header->getContext();
  const uint64_t headerSite = stableSiteId(*region.header);
  const uint64_t branchSite = stableSiteId(*region.headerBranch);
  IRBuilder<> builder(region.preheader->getTerminator());
  DenseMap<Value *, Value *> summaries;
  Value *executionCount = region.tripCount;
  Value *breakTaken = nullptr;
  Value *winnerOrdinal = nullptr;
  MDNode *breakProof = nullptr;
  if (region.breaks.size() == 1) {
    const LoopBreak &loopBreak = region.breaks.front();
    breakTaken = builder.CreateICmpULT(
        loopBreak.value, region.tripCount, "ifss.loop.break_taken");
    Value *breakIterations = builder.CreateAdd(
        loopBreak.value,
        ConstantInt::get(loopBreak.value->getType(), 1),
        "ifss.loop.break_iterations");
    executionCount = builder.CreateSelect(
        breakTaken, breakIterations, region.tripCount,
        "ifss.loop.executions");
    breakProof = breakLoopSummaryMetadata(
        context, headerSite, branchSite,
        stableSiteId(*loopBreak.condition),
        stableSiteId(*region.exit), stableSiteId(*loopBreak.exit),
        region.maximumTripCount, region.states.size());
    attachProof(
        breakTaken, "symcc.ifss_loop_break", breakProof);
    attachProof(
        breakIterations, "symcc.ifss_loop_break", breakProof);
    attachProof(
        executionCount, "symcc.ifss_loop_break", breakProof);
  } else if (region.breaks.size() >= 2) {
    breakProof = multiBreakLoopSummaryMetadata(
        context, region, headerSite, branchSite);
    Value *winnerAt = region.tripCount;
    winnerOrdinal = ConstantInt::get(Type::getInt8Ty(context), 0);
    for (unsigned ordinal = 0; ordinal < region.breaks.size();
         ++ordinal) {
      Value *better = builder.CreateICmpULT(
          region.breaks[ordinal].value, winnerAt,
          "ifss.loop.break_better");
      winnerAt = builder.CreateSelect(
          better, region.breaks[ordinal].value, winnerAt,
          "ifss.loop.break_at");
      winnerOrdinal = builder.CreateSelect(
          better, ConstantInt::get(Type::getInt8Ty(context), ordinal),
          winnerOrdinal, "ifss.loop.break_winner");
      attachProof(
          better, "symcc.ifss_loop_break", breakProof);
      attachProof(
          winnerAt, "symcc.ifss_loop_break", breakProof);
      attachProof(
          winnerOrdinal, "symcc.ifss_loop_break", breakProof);
    }
    breakTaken = builder.CreateICmpULT(
        winnerAt, region.tripCount, "ifss.loop.break_taken");
    Value *breakIterations = builder.CreateAdd(
        winnerAt, ConstantInt::get(winnerAt->getType(), 1),
        "ifss.loop.break_iterations");
    executionCount = builder.CreateSelect(
        breakTaken, breakIterations, region.tripCount,
        "ifss.loop.executions");
    attachProof(
        breakTaken, "symcc.ifss_loop_break", breakProof);
    attachProof(
        breakIterations, "symcc.ifss_loop_break", breakProof);
    attachProof(
        executionCount, "symcc.ifss_loop_break", breakProof);
  }
  summaries[region.induction] = executionCount;
  if (!region.breaks.empty())
    summaries[region.inductionUpdate] = executionCount;

  if (region.triangular) {
    lowerTriangularLoop(
        region, summaries, builder, headerSite, branchSite,
        executionCount);
  } else {
    for (unsigned ordinal = 0; ordinal < region.states.size(); ++ordinal) {
      AffineLoopState &state = region.states[ordinal];
      Type *stateType = state.phi->getType();
      Value *tripForState = executionCount;
      if (tripForState->getType() != stateType) {
        unsigned tripBits =
            tripForState->getType()->getIntegerBitWidth();
        unsigned stateBits = stateType->getIntegerBitWidth();
        tripForState =
            tripBits < stateBits
                ? builder.CreateZExt(
                      tripForState, stateType, "ifss.loop.trip")
                : builder.CreateTrunc(
                      tripForState, stateType, "ifss.loop.trip");
      }
      auto *delta = BinaryOperator::CreateMul(
          tripForState, state.step, "ifss.loop.delta",
          region.preheader->getTerminator());
      auto *summary = BinaryOperator::CreateAdd(
          state.initialValue, delta, "ifss.loop.state",
          region.preheader->getTerminator());
      summary->setDebugLoc(state.update->getDebugLoc());
      MDNode *proof = loopSummaryMetadata(
          context, headerSite, branchSite, region.maximumTripCount,
          region.states.size(), ordinal, stableSiteId(*state.phi),
          stableSiteId(*state.update));
      delta->setMetadata("symcc.ifss_loop_summary", proof);
      summary->setMetadata("symcc.ifss_loop_summary", proof);
      summaries[state.phi] = summary;
      if (!region.breaks.empty())
        summaries[state.update] = summary;
    }
  }

  if (region.triangular && !region.breaks.empty())
    for (const AffineLoopState &state : region.states)
      summaries[state.update] = summaries.lookup(state.phi);

  for (const auto &entry : summaries) {
    auto *original = cast<Instruction>(entry.first);
    for (Use &use : make_early_inc_range(original->uses())) {
      auto *user = dyn_cast<Instruction>(use.getUser());
      if (user == nullptr ||
          std::find(
              region.loopBlocks.begin(), region.loopBlocks.end(),
              user->getParent()) != region.loopBlocks.end())
        continue;
      use.set(entry.second);
    }
  }

  auto *preheaderBranch =
      cast<BranchInst>(region.preheader->getTerminator());
  MDNode *coreProof =
      region.triangular
          ? triangularLoopSummaryMetadata(
                context, headerSite, branchSite,
                region.maximumTripCount, region.states.size(),
                region.states.size(), region.triangular->bitWidth,
                recurrenceFingerprint(region),
                stableSiteId(*region.induction),
                stableSiteId(*region.inductionUpdate))
          : loopSummaryMetadata(
                context, headerSite, branchSite,
                region.maximumTripCount, region.states.size(),
                region.states.size(), stableSiteId(*region.induction),
                stableSiteId(*region.inductionUpdate));
  BasicBlock *normalSource = region.preheader;
  SmallVector<BasicBlock *, kMaxLoopBreaks> breakSources;
  if (region.breaks.empty()) {
    preheaderBranch->setSuccessor(0, region.exit);
    preheaderBranch->setMetadata(
        "symcc.ifss_loop_summary", coreProof);
  } else if (region.breaks.size() == 1) {
    const LoopBreak &loopBreak = region.breaks.front();
    auto *exitBranch = BranchInst::Create(
        loopBreak.exit, region.exit, breakTaken, preheaderBranch);
    exitBranch->setDebugLoc(loopBreak.branch->getDebugLoc());
    exitBranch->setMetadata(
        "symcc.ifss_loop_summary", coreProof);
    exitBranch->setMetadata("symcc.ifss_loop_break", breakProof);
    preheaderBranch->eraseFromParent();
    breakSources.push_back(region.preheader);
  } else {
    Function *function = region.header->getParent();
    SmallVector<BasicBlock *, kMaxLoopBreaks> dispatchBlocks;
    for (unsigned ordinal = 0; ordinal < region.breaks.size();
         ++ordinal)
      dispatchBlocks.push_back(BasicBlock::Create(
          context, "ifss.loop.break.dispatch", function,
          region.exit));
    for (unsigned ordinal = 0; ordinal < region.breaks.size();
         ++ordinal) {
      BasicBlock *dispatch = dispatchBlocks[ordinal];
      IRBuilder<> dispatchBuilder(dispatch);
      Value *ordinalMatch = dispatchBuilder.CreateICmpEQ(
          winnerOrdinal,
          ConstantInt::get(Type::getInt8Ty(context), ordinal),
          "ifss.loop.break_match");
      Value *selected = dispatchBuilder.CreateAnd(
          breakTaken, ordinalMatch, "ifss.loop.break_selected");
      BasicBlock *fallback =
          ordinal + 1 < dispatchBlocks.size()
              ? dispatchBlocks[ordinal + 1]
              : region.exit;
      auto *branch = dispatchBuilder.CreateCondBr(
          selected, region.breaks[ordinal].exit, fallback);
      attachProof(
          ordinalMatch, "symcc.ifss_loop_break", breakProof);
      attachProof(
          selected, "symcc.ifss_loop_break", breakProof);
      branch->setMetadata(
          "symcc.ifss_loop_summary", coreProof);
      branch->setMetadata(
          "symcc.ifss_loop_break", breakProof);
      breakSources.push_back(dispatch);
    }
    normalSource = dispatchBlocks.back();
    preheaderBranch->setSuccessor(0, dispatchBlocks.front());
    preheaderBranch->setMetadata(
        "symcc.ifss_loop_summary", coreProof);
    preheaderBranch->setMetadata(
        "symcc.ifss_loop_break", breakProof);
  }

  auto addExitIncoming = [&](BasicBlock &exit, BasicBlock &oldSource,
                             BasicBlock &newSource) {
    for (PHINode &phi : exit.phis())
      for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
        if (phi.getIncomingBlock(index) != &oldSource)
          continue;
        Value *incoming = phi.getIncomingValue(index);
        auto found = summaries.find(incoming);
        phi.addIncoming(
            found != summaries.end() ? found->second : incoming,
            &newSource);
      }
  };
  addExitIncoming(*region.exit, *region.header, *normalSource);
  for (unsigned ordinal = 0; ordinal < region.breaks.size();
       ++ordinal)
    addExitIncoming(
        *region.breaks[ordinal].exit,
        *region.breaks[ordinal].checkBlock,
        *breakSources[ordinal]);

  DeleteDeadBlocks(region.loopBlocks);
  return true;
}

} // namespace

bool summarizeIFSSLoops(Module &module) {
  if (!enabled(std::getenv("SYMCC_IFSS_LOOP_SUMMARY")))
    return false;

  std::vector<AffineLoopRegion> regions;
  for (Function &function : module) {
    if (function.isDeclaration())
      continue;
    DominatorTree dominators(function);
    LoopInfo loops(dominators);
    for (Loop *loop : loops.getLoopsInPreorder()) {
      AffineLoopRegion region;
      if (collectAffineLoop(*loop, region)) {
        regions.push_back(std::move(region));
        break;
      }
    }
  }
  if (regions.empty())
    return false;

  initializeStableSiteIds(module);
  SmallVector<std::string, 2> manifestRecords;
  SmallVector<std::string, 2> exitManifestRecords;
  const char *manifestPath =
      std::getenv("SYMCC_IFSS_LOOP_MANIFEST_OUT");
  const char *exitManifestPath =
      std::getenv("SYMCC_IFSS_LOOP_EXIT_MANIFEST_OUT");
  if (manifestPath != nullptr && *manifestPath != '\0')
    for (const AffineLoopRegion &region : regions) {
      std::string record = buildLoopManifestLine(region);
      if (!record.empty())
        manifestRecords.push_back(std::move(record));
    }
  if (exitManifestPath != nullptr && *exitManifestPath != '\0')
    for (const AffineLoopRegion &region : regions) {
      std::string record =
          region.breaks.size() >= 2
              ? buildMultiBreakLoopManifestLine(region)
              : buildBreakLoopManifestLine(region);
      if (!record.empty())
        exitManifestRecords.push_back(std::move(record));
    }
  for (AffineLoopRegion &region : regions)
    lowerAffineLoop(region);
  if (manifestPath != nullptr)
    writeLoopManifest(manifestPath, manifestRecords);
  if (exitManifestPath != nullptr)
    writeLoopManifest(exitManifestPath, exitManifestRecords);
  return true;
}

char IFSSLoopSummaryLegacyPass::ID = 0;

bool IFSSLoopSummaryLegacyPass::runOnModule(Module &module) {
  return summarizeIFSSLoops(module);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses IFSSLoopSummaryPass::run(
    Module &module, ModuleAnalysisManager &) {
  return summarizeIFSSLoops(module) ? PreservedAnalyses::none()
                                    : PreservedAnalyses::all();
}
#endif

} // namespace symcc
