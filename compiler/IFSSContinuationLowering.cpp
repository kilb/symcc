// This file is part of SymCC.
//
// Lower a bounded multi-continuation region to an explicit exit-id and
// live-out tuple. Arm-local effects remain before per-edge capture blocks.

#include "IFSSContinuationLowering.h"

#include "SiteId.h"

#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <functional>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr unsigned kMaxContinuationExits = 8;
constexpr unsigned kMaxContinuationSlots = 8;
constexpr unsigned kMaxContinuationBlocks = 32;
constexpr unsigned kMaxContinuationPaths = 64;
constexpr char kContinuationSchema[] = "bounded-continuation-tuple-v1";

struct ContinuationExit {
  BranchInst *sourceTerminator = nullptr;
  unsigned successorIndex = 0;
  BasicBlock *destination = nullptr;
  unsigned destinationOrdinal = 0;
};

struct ContinuationSlot {
  PHINode *destinationPhi = nullptr;
  unsigned destinationOrdinal = 0;
  uint64_t originalPhiSite = 0;
  SmallVector<Value *, kMaxContinuationExits> values;
};

struct ContinuationRegion {
  BranchInst *controller = nullptr;
  SmallVector<ContinuationExit, kMaxContinuationExits> exits;
  SmallVector<BasicBlock *, kMaxContinuationExits> destinations;
  SmallVector<ContinuationSlot, kMaxContinuationSlots> slots;
  unsigned blocks = 0;
  unsigned paths = 0;
};

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

bool memoryTupleEnabled() {
  return enabled(std::getenv("SYMCC_IFSS_CONTINUATION_MEMORY"));
}

Metadata *integerMetadata(LLVMContext &context, unsigned bits,
                          uint64_t value) {
  return ConstantAsMetadata::get(
      ConstantInt::get(IntegerType::get(context, bits), value));
}

bool supportedSlotType(Type *type) {
  return type->isIntegerTy() || type->isFloatingPointTy() ||
         type->isPointerTy();
}

bool hasBoundaryPHI(const BasicBlock &block) {
  return !block.empty() && isa<PHINode>(block.front());
}

bool collectContinuationRegion(BranchInst &controller,
                               ContinuationRegion &result) {
  if (!controller.isConditional() ||
      controller.getSuccessor(0) == controller.getSuccessor(1) ||
      controller.getMetadata("symcc.ifss_continuation") != nullptr)
    return false;

  Function &function = *controller.getFunction();
  DominatorTree dominators(function);
  BasicBlock *controllerBlock = controller.getParent();
  SmallPtrSet<BasicBlock *, kMaxContinuationBlocks> regionBlocks;
  SmallPtrSet<BasicBlock *, kMaxContinuationBlocks> visiting;
  unsigned paths = 0;

  auto isBoundary = [&](BasicBlock *block) {
    return block != controllerBlock &&
           (!dominators.dominates(controllerBlock, block) ||
            hasBoundaryPHI(*block));
  };
  auto addExit = [&](BranchInst *source, unsigned successorIndex,
                     BasicBlock *destination) {
    ++paths;
    if (paths > kMaxContinuationPaths)
      return false;
    auto duplicate = std::find_if(
        result.exits.begin(), result.exits.end(),
        [&](const ContinuationExit &exit) {
          return exit.sourceTerminator == source &&
                 exit.successorIndex == successorIndex;
        });
    if (duplicate == result.exits.end()) {
      if (result.exits.size() >= kMaxContinuationExits)
        return false;
      result.exits.push_back({source, successorIndex, destination, 0});
    }
    return true;
  };

  std::function<bool(BasicBlock *)> enumerate =
      [&](BasicBlock *block) {
        if (block == nullptr ||
            !dominators.dominates(controllerBlock, block) ||
            block->hasAddressTaken() || block->isEHPad() ||
            !visiting.insert(block).second)
          return false;
        regionBlocks.insert(block);
        if (regionBlocks.size() > kMaxContinuationBlocks) {
          visiting.erase(block);
          return false;
        }

        auto *branch = dyn_cast<BranchInst>(block->getTerminator());
        if (branch == nullptr) {
          visiting.erase(block);
          return false;
        }
        for (unsigned index = 0; index < branch->getNumSuccessors();
             ++index) {
          BasicBlock *successor = branch->getSuccessor(index);
          bool valid =
              isBoundary(successor)
                  ? addExit(branch, index, successor)
                  : enumerate(successor);
          if (!valid) {
            visiting.erase(block);
            return false;
          }
        }
        visiting.erase(block);
        return true;
      };

  if (!enumerate(controllerBlock) || result.exits.size() < 2)
    return false;

  for (ContinuationExit &exit : result.exits) {
    BasicBlock *destination = exit.destination;
    if (destination->hasAddressTaken() || destination->isEHPad())
      return false;
    auto found = std::find(
        result.destinations.begin(), result.destinations.end(), destination);
    if (found == result.destinations.end()) {
      exit.destinationOrdinal = result.destinations.size();
      result.destinations.push_back(destination);
    } else {
      exit.destinationOrdinal =
          static_cast<unsigned>(found - result.destinations.begin());
    }
  }
  if (result.destinations.size() < 2)
    return false;

  for (unsigned destinationOrdinal = 0;
       destinationOrdinal < result.destinations.size();
       ++destinationOrdinal) {
    BasicBlock *destination = result.destinations[destinationOrdinal];
    DenseMap<BasicBlock *, unsigned> expectedEdges;
    for (const ContinuationExit &exit : result.exits)
      if (exit.destination == destination)
        ++expectedEdges[exit.sourceTerminator->getParent()];

    for (PHINode &phi : destination->phis()) {
      if (!supportedSlotType(phi.getType()) ||
          result.slots.size() >= kMaxContinuationSlots)
        return false;
      for (const auto &entry : expectedEdges) {
        BasicBlock *source = entry.first;
        Value *incomingValue = nullptr;
        unsigned incomingEdges = 0;
        for (unsigned index = 0; index < phi.getNumIncomingValues();
             ++index) {
          if (phi.getIncomingBlock(index) != source)
            continue;
          Value *value = phi.getIncomingValue(index);
          if (isa<UndefValue, PoisonValue>(value) ||
              (incomingValue != nullptr && incomingValue != value))
            return false;
          incomingValue = value;
          ++incomingEdges;
        }
        if (incomingValue == nullptr || incomingEdges != entry.second)
          return false;
      }

      ContinuationSlot slot;
      slot.destinationPhi = &phi;
      slot.destinationOrdinal = destinationOrdinal;
      slot.originalPhiSite = stableSiteId(phi);
      for (const ContinuationExit &exit : result.exits) {
        Value *value = nullptr;
        if (exit.destination == destination)
          value = phi.getIncomingValueForBlock(
              exit.sourceTerminator->getParent());
        slot.values.push_back(value);
      }
      result.slots.push_back(std::move(slot));
    }
  }
  if (result.slots.empty() && !memoryTupleEnabled())
    return false;

  result.controller = &controller;
  result.blocks = regionBlocks.size() - 1;
  result.paths = paths;
  return true;
}

MDNode *continuationSummary(LLVMContext &context, uint64_t controllerSite,
                            const ContinuationRegion &region) {
  Metadata *operands[] = {
      MDString::get(context, kContinuationSchema),
      integerMetadata(context, 64, controllerSite),
      integerMetadata(context, 32, region.exits.size()),
      integerMetadata(context, 32, region.destinations.size()),
      integerMetadata(context, 32, region.slots.size()),
      integerMetadata(context, 32, region.blocks),
      integerMetadata(context, 32, region.paths),
  };
  return MDNode::get(context, operands);
}

void attachExitMetadata(Instruction &instruction, uint64_t controllerSite,
                        unsigned exitOrdinal,
                        uint64_t sourceTerminatorSite,
                        unsigned successorIndex,
                        unsigned destinationOrdinal) {
  LLVMContext &context = instruction.getContext();
  Metadata *operands[] = {
      MDString::get(context, kContinuationSchema),
      integerMetadata(context, 64, controllerSite),
      integerMetadata(context, 32, exitOrdinal),
      integerMetadata(context, 64, sourceTerminatorSite),
      integerMetadata(context, 32, successorIndex),
      integerMetadata(context, 32, destinationOrdinal),
  };
  instruction.setMetadata("symcc.ifss_continuation_exit",
                          MDNode::get(context, operands));
}

bool lowerContinuationRegion(ContinuationRegion &region) {
  BranchInst *controller = region.controller;
  Function *function = controller->getFunction();
  LLVMContext &context = controller->getContext();
  const uint64_t controllerSite = stableSiteId(*controller);
  MDNode *summary =
      continuationSummary(context, controllerSite, region);
  controller->setMetadata("symcc.ifss_continuation", summary);

  BasicBlock *insertBefore = region.destinations.front();
  BasicBlock *dispatch = BasicBlock::Create(
      context, "ifss.cont.dispatch", function, insertBefore);
  SmallVector<BasicBlock *, kMaxContinuationExits> captures;
  SmallVector<BasicBlock *, kMaxContinuationExits> trampolines;
  SmallVector<uint64_t, kMaxContinuationExits> sourceSites;
  captures.reserve(region.exits.size());
  trampolines.reserve(region.exits.size());
  sourceSites.reserve(region.exits.size());

  for (unsigned ordinal = 0; ordinal < region.exits.size(); ++ordinal) {
    ContinuationExit &exit = region.exits[ordinal];
    uint64_t sourceSite = stableSiteId(*exit.sourceTerminator);
    sourceSites.push_back(sourceSite);
    BasicBlock *capture = BasicBlock::Create(
        context, "ifss.cont.capture", function, dispatch);
    auto *captureBranch = BranchInst::Create(dispatch, capture);
    captureBranch->setDebugLoc(exit.sourceTerminator->getDebugLoc());
    attachExitMetadata(
        *captureBranch, controllerSite, ordinal, sourceSite,
        exit.successorIndex, exit.destinationOrdinal);
    exit.sourceTerminator->setSuccessor(exit.successorIndex, capture);
    captures.push_back(capture);

    BasicBlock *trampoline = BasicBlock::Create(
        context, "ifss.cont.resume", function, insertBefore);
    trampolines.push_back(trampoline);
  }

  auto *exitId = PHINode::Create(
      Type::getInt8Ty(context), region.exits.size(),
      "ifss.cont.exit_id", dispatch);
  for (unsigned ordinal = 0; ordinal < captures.size(); ++ordinal)
    exitId->addIncoming(
        ConstantInt::get(Type::getInt8Ty(context), ordinal),
        captures[ordinal]);
  exitId->setMetadata("symcc.ifss_continuation", summary);

  SmallVector<PHINode *, kMaxContinuationSlots> liveOuts;
  liveOuts.reserve(region.slots.size());
  for (unsigned slotOrdinal = 0; slotOrdinal < region.slots.size();
       ++slotOrdinal) {
    ContinuationSlot &slot = region.slots[slotOrdinal];
    auto *liveOut = PHINode::Create(
        slot.destinationPhi->getType(), region.exits.size(),
        "ifss.cont.liveout", dispatch);
    Constant *neutral = Constant::getNullValue(
        slot.destinationPhi->getType());
    for (unsigned exitOrdinal = 0;
         exitOrdinal < region.exits.size(); ++exitOrdinal)
      liveOut->addIncoming(
          slot.values[exitOrdinal] == nullptr
              ? static_cast<Value *>(neutral)
              : slot.values[exitOrdinal],
          captures[exitOrdinal]);
    Metadata *operands[] = {
        MDString::get(context, kContinuationSchema),
        integerMetadata(context, 64, controllerSite),
        integerMetadata(context, 32, slotOrdinal),
        integerMetadata(context, 32, slot.destinationOrdinal),
        integerMetadata(context, 64, slot.originalPhiSite),
    };
    MDNode *slotProof = MDNode::get(context, operands);
    liveOut->setMetadata("symcc.ifss_continuation_liveout", slotProof);
    slot.destinationPhi->setMetadata(
        "symcc.ifss_continuation_liveout", slotProof);
    liveOuts.push_back(liveOut);
  }

  SmallVector<BasicBlock *, kMaxContinuationExits> testBlocks;
  testBlocks.push_back(dispatch);
  for (unsigned ordinal = 1; ordinal + 1 < region.exits.size();
       ++ordinal)
    testBlocks.push_back(BasicBlock::Create(
        context, "ifss.cont.test", function, insertBefore));

  for (unsigned ordinal = 0; ordinal + 1 < region.exits.size();
       ++ordinal) {
    IRBuilder<> builder(testBlocks[ordinal]);
    auto *matches = cast<ICmpInst>(builder.CreateICmpEQ(
        exitId, ConstantInt::get(Type::getInt8Ty(context), ordinal),
        "ifss.cont.matches"));
    BasicBlock *falseDestination =
        ordinal + 2 < region.exits.size()
            ? testBlocks[ordinal + 1]
            : trampolines.back();
    auto *branch = builder.CreateCondBr(
        matches, trampolines[ordinal], falseDestination);
    matches->setDebugLoc(controller->getDebugLoc());
    branch->setDebugLoc(controller->getDebugLoc());
    matches->setMetadata("symcc.ifss_continuation", summary);
    branch->setMetadata("symcc.ifss_continuation", summary);
    matches->setMetadata("symcc.ifss_force_partition", summary);
    branch->setMetadata("symcc.ifss_force_partition", summary);
  }

  for (unsigned ordinal = 0; ordinal < region.exits.size(); ++ordinal) {
    ContinuationExit &exit = region.exits[ordinal];
    auto *resume = BranchInst::Create(
        exit.destination, trampolines[ordinal]);
    resume->setDebugLoc(exit.sourceTerminator->getDebugLoc());
    attachExitMetadata(
        *resume, controllerSite, ordinal, sourceSites[ordinal],
        exit.successorIndex, exit.destinationOrdinal);
  }

  for (unsigned slotOrdinal = 0; slotOrdinal < region.slots.size();
       ++slotOrdinal) {
    ContinuationSlot &slot = region.slots[slotOrdinal];
    PHINode *destinationPhi = slot.destinationPhi;
    SmallPtrSet<BasicBlock *, kMaxContinuationExits> sources;
    for (const ContinuationExit &exit : region.exits)
      if (exit.destinationOrdinal == slot.destinationOrdinal)
        sources.insert(exit.sourceTerminator->getParent());
    for (unsigned index = destinationPhi->getNumIncomingValues();
         index != 0; --index)
      if (sources.count(destinationPhi->getIncomingBlock(index - 1)) != 0)
        destinationPhi->removeIncomingValue(index - 1, false);
    for (unsigned exitOrdinal = 0;
         exitOrdinal < region.exits.size(); ++exitOrdinal)
      if (region.exits[exitOrdinal].destinationOrdinal ==
          slot.destinationOrdinal)
        destinationPhi->addIncoming(
            liveOuts[slotOrdinal], trampolines[exitOrdinal]);
  }
  return true;
}

} // namespace

bool lowerIFSSContinuations(Module &module) {
  if (!enabled(std::getenv("SYMCC_IFSS_CONTINUATION_STATE")))
    return false;

  std::vector<ContinuationRegion> regions;
  for (Function &function : module) {
    if (function.isDeclaration())
      continue;
    for (Instruction &instruction : instructions(function)) {
      auto *branch = dyn_cast<BranchInst>(&instruction);
      if (branch == nullptr)
        continue;
      ContinuationRegion region;
      if (collectContinuationRegion(*branch, region)) {
        regions.push_back(std::move(region));
        break;
      }
    }
  }
  if (regions.empty())
    return false;

  initializeStableSiteIds(module);
  for (ContinuationRegion &region : regions)
    lowerContinuationRegion(region);
  return true;
}

char IFSSContinuationLoweringLegacyPass::ID = 0;

bool IFSSContinuationLoweringLegacyPass::runOnModule(Module &module) {
  return lowerIFSSContinuations(module);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses IFSSContinuationLoweringPass::run(
    Module &module, ModuleAnalysisManager &) {
  return lowerIFSSContinuations(module) ? PreservedAnalyses::none()
                                        : PreservedAnalyses::all();
}
#endif

} // namespace symcc
