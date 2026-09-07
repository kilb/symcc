// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.
//
// SymCC is distributed in the hope that it will be useful, but WITHOUT ANY
// WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
// A PARTICULAR PURPOSE. See the GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License along with
// SymCC. If not, see <https://www.gnu.org/licenses/>.

#include "Symbolizer.h"

#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <functional>
#include <vector>
#include <llvm/Analysis/AliasAnalysis.h>
#include <llvm/Analysis/MemoryLocation.h>
#include <llvm/Analysis/MemorySSA.h>
#include <llvm/Analysis/PostDominators.h>
#include <llvm/Analysis/ValueTracking.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/GetElementPtrTypeIterator.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Intrinsics.h>
#include <llvm/Support/ModRef.h>
#include <llvm/Transforms/Utils/BasicBlockUtils.h>

#include "Runtime.h"

using namespace llvm;

namespace {

constexpr unsigned kMaxVeritestingRegionDepth = 12;
constexpr unsigned kMaxVeritestingRegionBlocks = 32;
constexpr unsigned kMaxIFSSMergeArms = 8;
constexpr unsigned kMaxIFSSRegionPaths = 64;
constexpr unsigned kMaxIFSSMemoryDefChain = 8;
constexpr char kScheduleAtomicMetadata[] =
    "symcc.schedule.atomic.instrumented";

struct IFSSPathStep {
  BranchInst *branch = nullptr;
  bool takeTrue = false;
};

using IFSSRegionPath = SmallVector<IFSSPathStep, 8>;
using IFSSRegionPaths =
    DenseMap<BasicBlock *, SmallVector<IFSSRegionPath, 2>>;

bool envEnabled(const char *name) {
  const char *value = std::getenv(name);
  if (value == nullptr || *value == '\0')
    return false;
  return StringRef(value).lower() != "0" && StringRef(value).lower() != "false" &&
         StringRef(value).lower() != "off" && StringRef(value).lower() != "no";
}

uint64_t typeStoreBytes(const DataLayout &dataLayout, Type *type) {
#if LLVM_VERSION_MAJOR >= 11
  return dataLayout.getTypeStoreSize(type).getFixedValue();
#else
  return dataLayout.getTypeStoreSize(type);
#endif
}

uint8_t scheduleAtomicOrder(AtomicOrdering ordering) {
  switch (ordering) {
  case AtomicOrdering::Acquire:
    return 2;
  case AtomicOrdering::Release:
    return 3;
  case AtomicOrdering::AcquireRelease:
    return 4;
  case AtomicOrdering::SequentiallyConsistent:
    return 5;
  case AtomicOrdering::NotAtomic:
  case AtomicOrdering::Unordered:
  case AtomicOrdering::Monotonic:
    return 0;
  default:
    return 0;
  }
}

bool isSupportedMergedType(Type *type) {
  return type->isIntegerTy() || type->isFloatingPointTy() ||
         type->isPointerTy();
}

bool isSupportedRegionBinaryOpcode(unsigned opcode) {
  switch (opcode) {
  case Instruction::Add:
  case Instruction::Sub:
  case Instruction::Mul:
  case Instruction::Shl:
  case Instruction::LShr:
  case Instruction::AShr:
  case Instruction::And:
  case Instruction::Or:
  case Instruction::Xor:
  case Instruction::FAdd:
  case Instruction::FSub:
  case Instruction::FMul:
    return true;
  default:
    return false;
  }
}

bool isSupportedRegionCastOpcode(unsigned opcode) {
  switch (opcode) {
  case Instruction::Trunc:
  case Instruction::ZExt:
  case Instruction::SExt:
  case Instruction::FPTrunc:
  case Instruction::FPExt:
  case Instruction::FPToUI:
  case Instruction::FPToSI:
  case Instruction::UIToFP:
  case Instruction::SIToFP:
  case Instruction::PtrToInt:
  case Instruction::IntToPtr:
  case Instruction::BitCast:
    return true;
  default:
    return false;
  }
}

bool isNumericDataIntrinsic(const IntrinsicInst &intrinsic) {
  switch (intrinsic.getIntrinsicID()) {
  case Intrinsic::bswap:
  case Intrinsic::ctpop:
  case Intrinsic::ctlz:
  case Intrinsic::cttz:
  case Intrinsic::fshl:
  case Intrinsic::fshr:
#if LLVM_VERSION_MAJOR > 11
  case Intrinsic::smin:
  case Intrinsic::smax:
  case Intrinsic::umin:
  case Intrinsic::umax:
#endif
    return true;
  default:
    return false;
  }
}

Value *findDataOrigin(Value *value, SmallPtrSetImpl<Value *> &visited,
                      unsigned depth = 0) {
  if (value == nullptr || depth > 16 || !visited.insert(value).second)
    return nullptr;
  if (auto *load = dyn_cast<LoadInst>(value))
    return load->getPointerOperand();
  if (auto *cast = dyn_cast<CastInst>(value))
    return findDataOrigin(cast->getOperand(0), visited, depth + 1);
  if (auto *unary = dyn_cast<UnaryOperator>(value))
    return findDataOrigin(unary->getOperand(0), visited, depth + 1);
  if (auto *intrinsic = dyn_cast<IntrinsicInst>(value)) {
    if (isNumericDataIntrinsic(*intrinsic)) {
      for (Value *argument : intrinsic->args())
        if (Value *origin =
                findDataOrigin(argument, visited, depth + 1))
          return origin;
    }
    return nullptr;
  }
  if (auto *binary = dyn_cast<BinaryOperator>(value)) {
    Value *left =
        findDataOrigin(binary->getOperand(0), visited, depth + 1);
    Value *right =
        findDataOrigin(binary->getOperand(1), visited, depth + 1);
    return left != nullptr ? left : right;
  }
  return nullptr;
}

Value *findDataOrigin(Value *value) {
  SmallPtrSet<Value *, 16> visited;
  Value *origin = findDataOrigin(value, visited);
  if (origin == nullptr || !origin->getType()->isPointerTy() ||
      cast<PointerType>(origin->getType())->getAddressSpace() != 0)
    return nullptr;
  return origin;
}

bool reachesIntegerComparison(Value *value,
                              SmallPtrSetImpl<Value *> &visited,
                              unsigned depth = 0) {
  if (value == nullptr || depth > 16 || !visited.insert(value).second)
    return false;
  for (User *user : value->users()) {
    if (isa<ICmpInst>(user))
      return true;
    if (isa<CastInst>(user) || isa<UnaryOperator>(user) ||
        isa<BinaryOperator>(user)) {
      if (reachesIntegerComparison(user, visited, depth + 1))
        return true;
      continue;
    }
    if (auto *intrinsic = dyn_cast<IntrinsicInst>(user))
      if (isNumericDataIntrinsic(*intrinsic) &&
          reachesIntegerComparison(user, visited, depth + 1))
        return true;
  }
  return false;
}

bool reachesIntegerComparison(Value *value) {
  SmallPtrSet<Value *, 16> visited;
  return reachesIntegerComparison(value, visited);
}

bool mayModifyLocationConservatively(AAResults &aliasAnalysis,
                                     Instruction &instruction,
                                     const MemoryLocation &location) {
  if (!instruction.mayWriteToMemory())
    return false;
  if (isa<CallBase>(&instruction))
    return true;
  return isModSet(aliasAnalysis.getModRefInfo(&instruction, location));
}

bool verifyAcyclicEasyRegion(
    BasicBlock *entry, BasicBlock *merge,
    SmallPtrSetImpl<BasicBlock *> *regionBlocks = nullptr) {
  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> visiting;
  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> verified;
  unsigned blocks = 0;
  std::function<bool(BasicBlock *)> visit = [&](BasicBlock *block) {
    if (block == merge)
      return true;
    if (verified.count(block) != 0)
      return true;
    if (block == nullptr || visiting.count(block) != 0 ||
        ++blocks > kMaxVeritestingRegionBlocks)
      return false;

    auto *terminator = block->getTerminator();
    if (terminator == nullptr || terminator->getNumSuccessors() == 0 ||
        !(isa<BranchInst>(terminator) || isa<SwitchInst>(terminator)))
      return false;

    visiting.insert(block);
    for (BasicBlock *successor : successors(block))
      if (!visit(successor))
        return false;
    visiting.erase(block);
    verified.insert(block);
    if (regionBlocks != nullptr)
      regionBlocks->insert(block);
    return true;
  };
  return visit(entry);
}

bool buildIFSSRegionPartition(
    ArrayRef<BasicBlock *> endpoints, BasicBlock *merge,
    DominatorTree &dominators, PostDominatorTree &postDominators,
    BasicBlock *&controller, IFSSRegionPaths &pathsByPredecessor) {
  if (endpoints.size() < 2 ||
      endpoints.size() > kMaxIFSSMergeArms || merge == nullptr)
    return false;

  controller = endpoints.front();
  for (BasicBlock *endpoint : endpoints.drop_front()) {
    controller =
        dominators.findNearestCommonDominator(controller, endpoint);
    if (controller == nullptr)
      return false;
  }
  if (controller == merge ||
      !postDominators.dominates(merge, controller))
    return false;

  auto *rootBranch = dyn_cast<BranchInst>(controller->getTerminator());
  if (rootBranch == nullptr || !rootBranch->isConditional())
    return false;
  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> regionBlocks;
  for (BasicBlock *successor : successors(controller))
    if (!verifyAcyclicEasyRegion(successor, merge, &regionBlocks) ||
        regionBlocks.size() > kMaxVeritestingRegionBlocks)
      return false;

  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> visiting;
  unsigned pathCount = 0;
  IFSSRegionPath path;
  std::function<bool(BasicBlock *)> enumerate = [&](BasicBlock *block) {
    if (block == nullptr || block == merge ||
        !visiting.insert(block).second)
      return false;
    auto *branch = dyn_cast<BranchInst>(block->getTerminator());
    if (branch == nullptr) {
      visiting.erase(block);
      return false;
    }
    if (branch->isUnconditional()) {
      BasicBlock *successor = branch->getSuccessor(0);
      if (successor == merge) {
        if (++pathCount > kMaxIFSSRegionPaths) {
          visiting.erase(block);
          return false;
        }
        pathsByPredecessor[block].push_back(path);
      } else if (!enumerate(successor)) {
        visiting.erase(block);
        return false;
      }
    } else {
      for (unsigned successorIndex = 0; successorIndex < 2;
           ++successorIndex) {
        BasicBlock *successor = branch->getSuccessor(successorIndex);
        if (successor == merge) {
          // A conditional edge entering a block-keyed PHI requires edge
          // splitting before it can be represented without ambiguity.
          visiting.erase(block);
          return false;
        }
        path.push_back({branch, successorIndex == 0});
        bool valid = enumerate(successor);
        path.pop_back();
        if (!valid) {
          visiting.erase(block);
          return false;
        }
      }
    }
    visiting.erase(block);
    return true;
  };
  if (!enumerate(controller))
    return false;

  SmallPtrSet<BasicBlock *, kMaxIFSSMergeArms> endpointSet;
  for (BasicBlock *endpoint : endpoints)
    if (!endpointSet.insert(endpoint).second ||
        pathsByPredecessor.find(endpoint) == pathsByPredecessor.end())
      return false;
  return pathsByPredecessor.size() == endpoints.size();
}

} // namespace

Symbolizer::Symbolizer(Module &M, Function &F, AAResults *aliasAnalysis,
                       MemorySSA *memorySSA)
    : runtime(M), dataLayout(M.getDataLayout()),
      ptrBits(M.getDataLayout().getPointerSizeInBits()),
      intPtrType(M.getDataLayout().getIntPtrType(M.getContext())),
      scheduleMemoryTracing(envEnabled("SYMCC_DPOR_MEMORY")),
      aliasAnalysis(aliasAnalysis), memorySSA(memorySSA) {
  for (Argument &argument : F.args())
    siteIds[&argument] = symcc::stableSiteId(argument);
  for (BasicBlock &block : F) {
    siteIds[&block] = symcc::stableSiteId(block);
    for (Instruction &instruction : block) {
      originalInstructions.insert(&instruction);
      siteIds[&instruction] = symcc::stableSiteId(instruction);
    }
  }
}

void Symbolizer::symbolizeFunctionArguments(Function &F) {
  // The main function doesn't receive symbolic arguments.
  if (F.getName() == "main")
    return;

  IRBuilder<> IRB(F.getEntryBlock().getFirstNonPHI());

  for (auto &arg : F.args()) {
    if (!arg.user_empty())
      symbolicExpressions[&arg] = IRB.CreateCall(runtime.getParameterExpression,
                                                 IRB.getInt8(arg.getArgNo()));
  }
}

void Symbolizer::insertBasicBlockNotification(llvm::BasicBlock &B) {
  IRBuilder<> IRB(&*B.getFirstInsertionPt());
  IRB.CreateCall(runtime.notifyBasicBlock, getTargetPreferredInt(&B));
  if (scheduleMemoryTracing)
    IRB.CreateCall(runtime.notifyScheduleBlock, getTargetPreferredInt(&B));
}

bool Symbolizer::tryBuildImplicitFlowPHI(PHINode &phi,
                                         PHINode &symbolicPHI,
                                         DominatorTree &dominators,
                                         PostDominatorTree &postDominators) {
  if (phi.getNumIncomingValues() > 2)
    return tryBuildMultiArmImplicitFlowPHI(
        phi, symbolicPHI, dominators, postDominators);
  if (phi.getNumIncomingValues() != 2)
    return false;

  Type *type = phi.getType();
  if (!isSupportedMergedType(type))
    return false;

  BasicBlock *left = phi.getIncomingBlock(0);
  BasicBlock *right = phi.getIncomingBlock(1);
  BasicBlock *controller =
      dominators.findNearestCommonDominator(left, right);
  BasicBlock *merge = phi.getParent();
  if (controller == nullptr || controller == merge ||
      !postDominators.dominates(merge, controller))
    return false;

  auto *branch = dyn_cast<BranchInst>(controller->getTerminator());
  if (branch != nullptr && branch->isConditional() &&
      (branch->getMetadata("symcc.ifss_switch_shared") != nullptr ||
       branch->getMetadata("symcc.ifss_force_partition") != nullptr))
    return tryBuildMultiArmImplicitFlowPHI(
        phi, symbolicPHI, dominators, postDominators);
  if (branch == nullptr || !branch->isConditional() ||
      getSymbolicExpression(branch->getCondition()) == nullptr)
      return false;

  BasicBlock *trueSuccessor = branch->getSuccessor(0);
  BasicBlock *falseSuccessor = branch->getSuccessor(1);
  if (!verifyAcyclicEasyRegion(trueSuccessor, merge) ||
      !verifyAcyclicEasyRegion(falseSuccessor, merge))
    return false;
  bool trueSelectsLeft = dominators.dominates(trueSuccessor, left);
  bool falseSelectsLeft = dominators.dominates(falseSuccessor, left);
  bool trueSelectsRight = dominators.dominates(trueSuccessor, right);
  bool falseSelectsRight = dominators.dominates(falseSuccessor, right);

  Value *trueValue = nullptr;
  Value *falseValue = nullptr;
  if (trueSelectsLeft && !falseSelectsLeft && falseSelectsRight &&
      !trueSelectsRight) {
    trueValue = phi.getIncomingValue(0);
    falseValue = phi.getIncomingValue(1);
  } else if (trueSelectsRight && !falseSelectsRight && falseSelectsLeft &&
             !trueSelectsLeft) {
    trueValue = phi.getIncomingValue(1);
    falseValue = phi.getIncomingValue(0);
  } else {
    return false;
  }

  auto insertIt = phi.getParent()->getFirstInsertionPt();
  if (insertIt == phi.getParent()->end())
    return false;
  Instruction *insertBefore = &*insertIt;

  Value *conditionExpr = getSymbolicExpression(branch->getCondition());
  if (conditionExpr == nullptr ||
      !canUseValueAt(branch->getCondition(), insertBefore, dominators) ||
      !canUseValueAt(conditionExpr, insertBefore, dominators))
    return false;

  RegionValue trueRegion;
  RegionValue falseRegion;
  if (!trySynthesizeRegionValue(trueValue, insertBefore, controller,
                                dominators, 0, trueRegion) ||
      !trySynthesizeRegionValue(falseValue, insertBefore, controller,
                                dominators, 0, falseRegion))
    return false;

  IRBuilder<> IRB(insertBefore);
  SymbolicComputation computation;
  if (trueRegion.computation.firstInstruction != nullptr)
    computation.merge(trueRegion.computation);
  if (falseRegion.computation.firstInstruction != nullptr)
    computation.merge(falseRegion.computation);

  auto regionArg = [](const RegionValue &source) {
    return RegionRuntimeArg{
        source.concreteValue, source.expressionValue, true,
        source.computation.firstInstruction == nullptr ||
            source.expressionValue == nullptr};
  };
  auto ite = forceBuildRuntimeCallWithExpressions(
      IRB, runtime.buildIte,
      {{branch->getCondition(), conditionExpr, true},
       regionArg(trueRegion),
       regionArg(falseRegion)});
  computation.merge(ite);

  symbolicPHI.replaceAllUsesWith(ite.lastInstruction);
  registerSymbolicComputation(computation, &phi);
  return true;
}

bool Symbolizer::tryBuildMultiArmImplicitFlowPHI(
    PHINode &phi, PHINode &symbolicPHI, DominatorTree &dominators,
    PostDominatorTree &postDominators) {
  const unsigned arms = phi.getNumIncomingValues();
  if (arms < 2 || arms > kMaxIFSSMergeArms ||
      !isSupportedMergedType(phi.getType()))
    return false;

  BasicBlock *merge = phi.getParent();
  SmallVector<BasicBlock *, kMaxIFSSMergeArms> endpoints;
  endpoints.reserve(arms);
  for (unsigned index = 0; index < arms; ++index)
    endpoints.push_back(phi.getIncomingBlock(index));
  BasicBlock *controller = nullptr;
  IFSSRegionPaths pathsByPredecessor;
  if (!buildIFSSRegionPartition(
          endpoints, merge, dominators, postDominators, controller,
          pathsByPredecessor))
    return false;
  auto *rootBranch = dyn_cast<BranchInst>(controller->getTerminator());
  if (rootBranch == nullptr ||
      getSymbolicExpression(rootBranch->getCondition()) == nullptr)
    return false;

  auto insertIt = merge->getFirstInsertionPt();
  if (insertIt == merge->end())
    return false;
  Instruction *insertBefore = &*insertIt;
  IRBuilder<> IRB(insertBefore);
  auto rollback = [&]() {
    auto instruction = merge->getFirstInsertionPt();
    while (instruction != merge->end() && &*instruction != insertBefore)
      instruction = instruction->eraseFromParent();
    return false;
  };

  auto mergeIfAny = [](SymbolicComputation &target,
                       const RegionValue &source) {
    if (source.computation.firstInstruction != nullptr)
      target.merge(source.computation);
  };
  auto regionArg = [](const RegionValue &source) {
    return RegionRuntimeArg{
        source.concreteValue, source.expressionValue, true,
        source.computation.firstInstruction == nullptr ||
            source.expressionValue == nullptr};
  };
  auto negate = [&](const RegionValue &source, RegionValue &result) {
    result.concreteValue = IRB.CreateNot(source.concreteValue, "ifss.not");
    result.computation = source.computation;
    auto *trueExpression =
        IRB.CreateCall(runtime.buildBool, {IRB.getInt1(true)});
    result.computation.merge(
        SymbolicComputation(trueExpression, trueExpression, {}));
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildBoolXor,
        {regionArg(source),
         {IRB.getInt1(true), trueExpression, true, false}});
    result.computation.merge(expression);
    result.expressionValue = expression.lastInstruction;
  };
  auto combine = [&](const RegionValue &left, const RegionValue &right,
                     bool conjunction, RegionValue &result) {
    result.concreteValue =
        conjunction
            ? IRB.CreateAnd(left.concreteValue, right.concreteValue,
                            "ifss.and")
            : IRB.CreateOr(left.concreteValue, right.concreteValue,
                           "ifss.or");
    mergeIfAny(result.computation, left);
    mergeIfAny(result.computation, right);
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, conjunction ? runtime.buildBoolAnd : runtime.buildBoolOr,
        {regionArg(left), regionArg(right)});
    result.computation.merge(expression);
    result.expressionValue = expression.lastInstruction;
  };

  SmallVector<Value *, kMaxVeritestingRegionBlocks> uniqueConditions;
  SmallPtrSet<Value *, kMaxVeritestingRegionBlocks> seenConditions;
  for (unsigned index = 0; index + 1 < arms; ++index)
    for (const IFSSRegionPath &regionPath :
         pathsByPredecessor[phi.getIncomingBlock(index)])
      for (const IFSSPathStep &step : regionPath)
        if (seenConditions.insert(step.branch->getCondition()).second)
          uniqueConditions.push_back(step.branch->getCondition());

  DenseMap<Value *, RegionValue> conditionCache;
  SmallVector<SymbolicComputation, kMaxVeritestingRegionBlocks>
      conditionComputations;
  for (Value *conditionValue : uniqueConditions) {
    RegionValue condition;
    if (!trySynthesizeRegionValue(
            conditionValue, insertBefore, controller, dominators, 0,
            condition))
      return rollback();
    if (condition.computation.firstInstruction != nullptr) {
      if (condition.computation.inputs.empty())
        return rollback();
      conditionComputations.push_back(condition.computation);
      if (auto *expression =
              dyn_cast_or_null<Instruction>(condition.expressionValue))
        expression->setMetadata(
            "symcc.ifss_condition",
            MDNode::get(
                conditionValue->getContext(),
                {
                    MDString::get(
                        conditionValue->getContext(),
                        "partition-condition-cache-v1"),
                    ConstantAsMetadata::get(
                        getTargetPreferredInt(conditionValue)),
                }));
    }
    condition.computation = SymbolicComputation();
    conditionCache[conditionValue] = condition;
  }

  SmallVector<RegionValue, kMaxIFSSMergeArms> predicates;
  SmallVector<RegionValue, kMaxIFSSMergeArms> values;
  predicates.reserve(arms - 1);
  values.reserve(arms);
  for (unsigned index = 0; index < arms; ++index) {
    BasicBlock *incoming = phi.getIncomingBlock(index);
    if (index + 1 < arms) {
      RegionValue incomingPredicate;
      bool haveIncomingPredicate = false;
      for (const IFSSRegionPath &regionPath :
           pathsByPredecessor[incoming]) {
        RegionValue pathPredicate;
        bool havePathPredicate = false;
        for (const IFSSPathStep &step : regionPath) {
          auto cached =
              conditionCache.find(step.branch->getCondition());
          if (cached == conditionCache.end())
            return rollback();
          RegionValue condition = cached->second;
          RegionValue literal;
          if (step.takeTrue)
            literal = condition;
          else
            negate(condition, literal);
          if (!havePathPredicate) {
            pathPredicate = literal;
            havePathPredicate = true;
          } else {
            RegionValue conjunction;
            combine(pathPredicate, literal, true, conjunction);
            pathPredicate = conjunction;
          }
        }
        if (!havePathPredicate)
          return rollback();
        if (!haveIncomingPredicate) {
          incomingPredicate = pathPredicate;
          haveIncomingPredicate = true;
        } else {
          RegionValue disjunction;
          combine(incomingPredicate, pathPredicate, false, disjunction);
          incomingPredicate = disjunction;
        }
      }
      if (!haveIncomingPredicate)
        return rollback();
      predicates.push_back(incomingPredicate);
    }

    RegionValue incomingValue;
    if (!trySynthesizeRegionValue(
            phi.getIncomingValue(index), insertBefore, controller, dominators,
            0, incomingValue))
      return rollback();
    values.push_back(incomingValue);
  }

  RegionValue merged = values.back();
  for (unsigned index = arms - 1; index-- > 0;) {
    RegionValue next;
    next.concreteValue =
        IRB.CreateSelect(predicates[index].concreteValue,
                         values[index].concreteValue, merged.concreteValue,
                         "ifss.state");
    mergeIfAny(next.computation, predicates[index]);
    mergeIfAny(next.computation, values[index]);
    mergeIfAny(next.computation, merged);
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildIte,
        {regionArg(predicates[index]), regionArg(values[index]),
         regionArg(merged)});
    next.computation.merge(expression);
    next.expressionValue = expression.lastInstruction;
    merged = next;
  }

  symbolicPHI.replaceAllUsesWith(merged.expressionValue);
  for (const SymbolicComputation &condition : conditionComputations)
    registerSymbolicComputation(condition);
  registerSymbolicComputation(merged.computation, &phi);
  return true;
}

bool Symbolizer::tryBuildImplicitFlowMemoryLoad(
    LoadInst &load, DominatorTree &dominators,
    PostDominatorTree &postDominators) {
  if (aliasAnalysis == nullptr || memorySSA == nullptr || !load.isSimple() ||
      !isSupportedMergedType(load.getType()))
    return false;

  auto *loadAccess =
      dyn_cast_or_null<MemoryUse>(memorySSA->getMemoryAccess(&load));
  auto *memoryPhi =
      loadAccess == nullptr
          ? nullptr
          : dyn_cast<MemoryPhi>(loadAccess->getDefiningAccess());
  const unsigned arms =
      memoryPhi == nullptr ? 0 : memoryPhi->getNumIncomingValues();
  if (arms < 2 || arms > kMaxIFSSMergeArms ||
      memoryPhi->getBlock() != load.getParent())
    return false;

  SmallVector<StoreInst *, kMaxIFSSMergeArms> stores(arms, nullptr);
  SmallVector<
      SmallVector<Instruction *, kMaxIFSSMemoryDefChain>,
      kMaxIFSSMergeArms>
      skippedDefinitions(arms);
  SmallVector<BasicBlock *, kMaxIFSSMergeArms> incomingBlocks(arms, nullptr);
  MemoryLocation loadLocation = MemoryLocation::get(&load);
  auto findMustAliasStore =
      [&](MemoryAccess *access,
          SmallVectorImpl<Instruction *> &skipped) -> StoreInst * {
    while (auto *memoryDef = dyn_cast_or_null<MemoryDef>(access)) {
      Instruction *instruction = memoryDef->getMemoryInst();
      auto *store = dyn_cast_or_null<StoreInst>(instruction);
      if ((store != nullptr && !store->isSimple()) ||
          isa_and_nonnull<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(
              instruction))
        return nullptr;
      if (store != nullptr &&
          aliasAnalysis->alias(
              loadLocation, MemoryLocation::get(store)) ==
              AliasResult::MustAlias) {
        if (!store->isSimple() ||
            store->getValueOperand()->getType() != load.getType() ||
            isa<UndefValue, PoisonValue>(store->getValueOperand()))
          return nullptr;
        return store;
      }
      if (instruction == nullptr ||
          mayModifyLocationConservatively(
              *aliasAnalysis, *instruction, loadLocation) ||
          skipped.size() >= kMaxIFSSMemoryDefChain)
        return nullptr;
      skipped.push_back(instruction);
      access = memoryDef->getDefiningAccess();
    }
    return nullptr;
  };
  for (unsigned index = 0; index < arms; ++index) {
    stores[index] = findMustAliasStore(
        memoryPhi->getIncomingValue(index), skippedDefinitions[index]);
    incomingBlocks[index] = memoryPhi->getIncomingBlock(index);
    if (stores[index] == nullptr ||
        stores[index]->getParent() != incomingBlocks[index] ||
        originalInstructions.count(stores[index]) == 0)
      return false;
    auto *terminator =
        dyn_cast<BranchInst>(incomingBlocks[index]->getTerminator());
    if (terminator == nullptr || !terminator->isUnconditional() ||
        terminator->getSuccessor(0) != load.getParent())
      return false;
  }

  BasicBlock *merge = load.getParent();
  BasicBlock *controller = nullptr;
  IFSSRegionPaths pathsByPredecessor;
  if (!buildIFSSRegionPartition(
          incomingBlocks, merge, dominators, postDominators, controller,
          pathsByPredecessor))
    return false;
  auto *rootBranch = dyn_cast<BranchInst>(controller->getTerminator());
  if (rootBranch == nullptr ||
      getSymbolicExpression(rootBranch->getCondition()) == nullptr)
    return false;

  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> regionBlocks;
  if (!verifyAcyclicEasyRegion(rootBranch->getSuccessor(0), merge,
                               &regionBlocks) ||
      !verifyAcyclicEasyRegion(rootBranch->getSuccessor(1), merge,
                               &regionBlocks))
    return false;
  for (BasicBlock *block : regionBlocks) {
    for (Instruction &instruction : *block) {
      if (std::find(stores.begin(), stores.end(), &instruction) !=
              stores.end() ||
          originalInstructions.count(&instruction) == 0 ||
          !instruction.mayWriteToMemory())
        continue;
      if (memorySSA->getMemoryAccess(&instruction) == nullptr ||
          mayModifyLocationConservatively(
              *aliasAnalysis, instruction, loadLocation))
        return false;
    }
  }

  Instruction *rollbackAnchor = load.getPrevNode();
  auto rollback = [&]() {
    Instruction *instruction =
        rollbackAnchor == nullptr ? &load.getParent()->front()
                                  : rollbackAnchor->getNextNode();
    while (instruction != &load) {
      Instruction *next = instruction->getNextNode();
      instruction->eraseFromParent();
      instruction = next;
    }
    return false;
  };

  auto mergeIfAny = [](SymbolicComputation &target,
                       const RegionValue &source) {
    if (source.computation.firstInstruction != nullptr)
      target.merge(source.computation);
  };
  auto regionArg = [](const RegionValue &source) {
    return RegionRuntimeArg{
        source.concreteValue, source.expressionValue, true,
        source.computation.firstInstruction == nullptr ||
            source.expressionValue == nullptr};
  };

  IRBuilder<> IRB(&load);
  auto negate = [&](const RegionValue &source, RegionValue &result) {
    result.concreteValue =
        IRB.CreateNot(source.concreteValue, "ifss.memory.not");
    result.computation = source.computation;
    auto *trueExpression =
        IRB.CreateCall(runtime.buildBool, {IRB.getInt1(true)});
    result.computation.merge(
        SymbolicComputation(trueExpression, trueExpression, {}));
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildBoolXor,
        {regionArg(source),
         {IRB.getInt1(true), trueExpression, true, false}});
    result.computation.merge(expression);
    result.expressionValue = expression.lastInstruction;
  };
  auto combine = [&](const RegionValue &left, const RegionValue &right,
                     bool conjunction, RegionValue &result) {
    result.concreteValue =
        conjunction
            ? IRB.CreateAnd(left.concreteValue, right.concreteValue,
                            "ifss.memory.and")
            : IRB.CreateOr(left.concreteValue, right.concreteValue,
                           "ifss.memory.or");
    mergeIfAny(result.computation, left);
    mergeIfAny(result.computation, right);
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, conjunction ? runtime.buildBoolAnd : runtime.buildBoolOr,
        {regionArg(left), regionArg(right)});
    result.computation.merge(expression);
    result.expressionValue = expression.lastInstruction;
  };

  SmallVector<Value *, kMaxVeritestingRegionBlocks> uniqueConditions;
  SmallPtrSet<Value *, kMaxVeritestingRegionBlocks> seenConditions;
  for (unsigned index = 0; index + 1 < arms; ++index)
    for (const IFSSRegionPath &regionPath :
         pathsByPredecessor[incomingBlocks[index]])
      for (const IFSSPathStep &step : regionPath)
        if (seenConditions.insert(step.branch->getCondition()).second)
          uniqueConditions.push_back(step.branch->getCondition());

  DenseMap<Value *, RegionValue> conditionCache;
  SmallVector<SymbolicComputation, kMaxVeritestingRegionBlocks>
      conditionComputations;
  for (Value *conditionValue : uniqueConditions) {
    RegionValue condition;
    if (!trySynthesizeRegionValue(
            conditionValue, &load, controller, dominators, 0, condition))
      return rollback();
    if (condition.computation.firstInstruction != nullptr) {
      if (condition.computation.inputs.empty())
        return rollback();
      conditionComputations.push_back(condition.computation);
      if (auto *expression =
              dyn_cast_or_null<Instruction>(condition.expressionValue))
        expression->setMetadata(
            "symcc.ifss_condition",
            MDNode::get(
                conditionValue->getContext(),
                {
                    MDString::get(
                        conditionValue->getContext(),
                        "partition-condition-cache-v1"),
                    ConstantAsMetadata::get(
                        getTargetPreferredInt(conditionValue)),
                }));
    }
    condition.computation = SymbolicComputation();
    conditionCache[conditionValue] = condition;
  }

  SmallVector<RegionValue, kMaxIFSSMergeArms> predicates;
  SmallVector<RegionValue, kMaxIFSSMergeArms> values;
  predicates.reserve(arms - 1);
  values.reserve(arms);
  for (unsigned index = 0; index < arms; ++index) {
    BasicBlock *incoming = incomingBlocks[index];
    if (index + 1 < arms) {
      RegionValue incomingPredicate;
      bool haveIncomingPredicate = false;
      for (const IFSSRegionPath &regionPath :
           pathsByPredecessor[incoming]) {
        RegionValue pathPredicate;
        bool havePathPredicate = false;
        for (const IFSSPathStep &step : regionPath) {
          auto cached =
              conditionCache.find(step.branch->getCondition());
          if (cached == conditionCache.end())
            return rollback();
          RegionValue condition = cached->second;
          RegionValue literal;
          if (step.takeTrue)
            literal = condition;
          else
            negate(condition, literal);
          if (!havePathPredicate) {
            pathPredicate = literal;
            havePathPredicate = true;
          } else {
            RegionValue conjunction;
            combine(pathPredicate, literal, true, conjunction);
            pathPredicate = conjunction;
          }
        }
        if (!havePathPredicate)
          return rollback();
        if (!haveIncomingPredicate) {
          incomingPredicate = pathPredicate;
          haveIncomingPredicate = true;
        } else {
          RegionValue disjunction;
          combine(incomingPredicate, pathPredicate, false, disjunction);
          incomingPredicate = disjunction;
        }
      }
      if (!haveIncomingPredicate)
        return rollback();
      predicates.push_back(incomingPredicate);
    }

    RegionValue value;
    if (!trySynthesizeRegionValue(
            stores[index]->getValueOperand(), &load, controller, dominators,
            0, value))
      return rollback();
    values.push_back(value);
  }

  RegionValue merged = values.back();
  for (unsigned index = arms - 1; index-- > 0;) {
    RegionValue next;
    next.concreteValue =
        IRB.CreateSelect(predicates[index].concreteValue,
                         values[index].concreteValue, merged.concreteValue,
                         "ifss.memory.state.value");
    mergeIfAny(next.computation, predicates[index]);
    mergeIfAny(next.computation, values[index]);
    mergeIfAny(next.computation, merged);
    auto expression = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildIte,
        {regionArg(predicates[index]), regionArg(values[index]),
         regionArg(merged)});
    next.computation.merge(expression);
    next.expressionValue = expression.lastInstruction;
    merged = next;
  }
  auto *mergedInstruction = cast<Instruction>(merged.expressionValue);
  mergedInstruction->setName("ifss.memory.state");

  LLVMContext &context = load.getContext();
  SmallVector<Metadata *, 96> proof;
  int trueIndex = -1;
  int falseIndex = -1;
  const bool forcedPartition =
      rootBranch->getMetadata("symcc.ifss_switch_shared") != nullptr ||
      rootBranch->getMetadata("symcc.ifss_force_partition") != nullptr;
  if (arms == 2 && !forcedPartition) {
    for (unsigned index = 0; index < arms; ++index) {
      bool selectedByTrue = dominators.dominates(
          rootBranch->getSuccessor(0), incomingBlocks[index]);
      bool selectedByFalse = dominators.dominates(
          rootBranch->getSuccessor(1), incomingBlocks[index]);
      if (selectedByTrue && !selectedByFalse)
        trueIndex = static_cast<int>(index);
      if (selectedByFalse && !selectedByTrue)
        falseIndex = static_cast<int>(index);
    }
  }
  if (trueIndex >= 0 && falseIndex >= 0 && trueIndex != falseIndex) {
    const auto &trueSkipped = skippedDefinitions[trueIndex];
    const auto &falseSkipped = skippedDefinitions[falseIndex];
    bool hasSkippedDefinitions =
        !trueSkipped.empty() || !falseSkipped.empty();
    proof.push_back(MDString::get(
        context, hasSkippedDefinitions ? "must-alias-memoryssa-chain-v1"
                                       : "must-alias-memoryssa-v1"));
    proof.push_back(
        ConstantAsMetadata::get(getTargetPreferredInt(&load)));
    proof.push_back(
        ConstantAsMetadata::get(getTargetPreferredInt(rootBranch)));
    proof.push_back(ConstantAsMetadata::get(
        getTargetPreferredInt(stores[trueIndex])));
    proof.push_back(ConstantAsMetadata::get(
        getTargetPreferredInt(stores[falseIndex])));
    if (hasSkippedDefinitions) {
      proof.push_back(ConstantAsMetadata::get(ConstantInt::get(
          Type::getInt32Ty(context), trueSkipped.size())));
      for (Instruction *instruction : trueSkipped)
        proof.push_back(
            ConstantAsMetadata::get(getTargetPreferredInt(instruction)));
      proof.push_back(ConstantAsMetadata::get(ConstantInt::get(
          Type::getInt32Ty(context), falseSkipped.size())));
      for (Instruction *instruction : falseSkipped)
        proof.push_back(
            ConstantAsMetadata::get(getTargetPreferredInt(instruction)));
    }
  } else {
    proof.push_back(
        MDString::get(context, "must-alias-memoryssa-multi-v1"));
    proof.push_back(
        ConstantAsMetadata::get(getTargetPreferredInt(&load)));
    proof.push_back(
        ConstantAsMetadata::get(getTargetPreferredInt(rootBranch)));
    proof.push_back(ConstantAsMetadata::get(
        ConstantInt::get(Type::getInt32Ty(context), arms)));
    for (unsigned index = 0; index < arms; ++index) {
      proof.push_back(ConstantAsMetadata::get(
          getTargetPreferredInt(incomingBlocks[index])));
      proof.push_back(
          ConstantAsMetadata::get(getTargetPreferredInt(stores[index])));
      proof.push_back(ConstantAsMetadata::get(ConstantInt::get(
          Type::getInt32Ty(context),
          pathsByPredecessor[incomingBlocks[index]].size())));
      proof.push_back(ConstantAsMetadata::get(ConstantInt::get(
          Type::getInt32Ty(context), skippedDefinitions[index].size())));
      for (Instruction *instruction : skippedDefinitions[index])
        proof.push_back(
            ConstantAsMetadata::get(getTargetPreferredInt(instruction)));
    }
  }
  mergedInstruction->setMetadata(
      "symcc.ifss_memory", MDNode::get(context, proof));

  Value *oldExpression = getSymbolicExpression(&load);
  if (oldExpression == nullptr)
    return rollback();
  oldExpression->replaceAllUsesWith(merged.expressionValue);
  for (const SymbolicComputation &condition : conditionComputations)
    registerSymbolicComputation(condition);
  registerSymbolicComputation(merged.computation, &load);
  return true;
}

void Symbolizer::finalizePHINodes() {
  SmallPtrSet<PHINode *, 32> nodesToErase;
  Function *function = !phiNodes.empty()
                           ? phiNodes.front()->getFunction()
                           : (!memoryMergeLoads.empty()
                                  ? memoryMergeLoads.front()->getFunction()
                                  : nullptr);
  std::optional<DominatorTree> dominators;
  std::optional<PostDominatorTree> postDominators;
  if (function != nullptr) {
    dominators.emplace(*function);
    postDominators.emplace(*function);
  }

  for (auto *phi : phiNodes) {
    auto symbolicPHI = cast<PHINode>(symbolicExpressions[phi]);

    bool allConcrete =
        std::all_of(phi->op_begin(), phi->op_end(), [this](Value *input) {
          return (getSymbolicExpression(input) == nullptr);
        });
    if (dominators && postDominators &&
        tryBuildImplicitFlowPHI(*phi, *symbolicPHI, *dominators,
                                *postDominators)) {
      nodesToErase.insert(symbolicPHI);
      continue;
    }

    if (allConcrete) {
      nodesToErase.insert(symbolicPHI);
      continue;
    }

    for (unsigned incoming = 0, totalIncoming = phi->getNumIncomingValues();
         incoming < totalIncoming; incoming++) {
      symbolicPHI->setIncomingValue(
          incoming,
          getSymbolicExpressionOrNull(phi->getIncomingValue(incoming)));
    }
  }

  if (dominators && postDominators)
    for (LoadInst *load : memoryMergeLoads)
      tryBuildImplicitFlowMemoryLoad(
          *load, *dominators, *postDominators);

  for (auto *symbolicPHI : nodesToErase) {
    symbolicPHI->replaceAllUsesWith(
        ConstantPointerNull::get(cast<PointerType>(symbolicPHI->getType())));
    symbolicPHI->eraseFromParent();
  }

  // Replacing all uses has fixed uses of the symbolic PHI nodes in existing
  // code, but the nodes may still be referenced via symbolicExpressions. We
  // therefore invalidate symbolicExpressions, meaning that it cannot be used
  // after this point.
  symbolicExpressions.clear();
}

void Symbolizer::shortCircuitExpressionUses() {
  for (auto &symbolicComputation : expressionUses) {
    assert(!symbolicComputation.inputs.empty() &&
           "Symbolic computation has no inputs");

    IRBuilder<> IRB(symbolicComputation.firstInstruction);

    // Build the check whether any input expression is non-null (i.e., there
    // is a symbolic input).
    auto *nullExpression =
        ConstantPointerNull::get(IRB.getInt8Ty()->getPointerTo());
    std::vector<Value *> nullChecks;
    for (const auto &input : symbolicComputation.inputs) {
      nullChecks.push_back(
          IRB.CreateICmpEQ(nullExpression, input.getSymbolicOperand()));
    }
    auto *allConcrete = nullChecks[0];
    for (unsigned argIndex = 1; argIndex < nullChecks.size(); argIndex++) {
      allConcrete = IRB.CreateAnd(allConcrete, nullChecks[argIndex]);
    }

    // The main branch: if we don't enter here, we can short-circuit the
    // symbolic computation. Otherwise, we need to check all input expressions
    // and create an output expression.
    auto *head = symbolicComputation.firstInstruction->getParent();
    auto *slowPath = SplitBlock(head, symbolicComputation.firstInstruction);
    auto *tail = SplitBlock(slowPath,
                            symbolicComputation.lastInstruction->getNextNode());
    ReplaceInstWithInst(head->getTerminator(),
                        BranchInst::Create(tail, slowPath, allConcrete));

    // In the slow case, we need to check each input expression for null
    // (i.e., the input is concrete) and create an expression from the
    // concrete value if necessary.
    auto numUnknownConcreteness = std::count_if(
        symbolicComputation.inputs.begin(), symbolicComputation.inputs.end(),
        [&](const Input &input) {
          return (input.getSymbolicOperand() != nullExpression);
        });
    for (unsigned argIndex = 0; argIndex < symbolicComputation.inputs.size();
         argIndex++) {
      auto &argument = symbolicComputation.inputs[argIndex];
      auto *originalArgExpression = argument.getSymbolicOperand();
      auto *argCheckBlock = symbolicComputation.firstInstruction->getParent();

      // We only need a run-time check for concreteness if the argument isn't
      // known to be concrete at compile time already. However, there is one
      // exception: if the computation only has a single argument of unknown
      // concreteness, then we know that it must be symbolic since we ended up
      // in the slow path. Therefore, we can skip expression generation in
      // that case.
      bool needRuntimeCheck = originalArgExpression != nullExpression;
      if (needRuntimeCheck && (numUnknownConcreteness == 1))
        continue;

      if (needRuntimeCheck) {
        auto *argExpressionBlock = SplitBlockAndInsertIfThen(
            nullChecks[argIndex], symbolicComputation.firstInstruction,
            /* unreachable */ false);
        IRB.SetInsertPoint(argExpressionBlock);
      } else {
        IRB.SetInsertPoint(symbolicComputation.firstInstruction);
      }

      auto *newArgExpression =
          createValueExpression(argument.concreteValue, IRB);

      Value *finalArgExpression;
      if (needRuntimeCheck) {
        IRB.SetInsertPoint(symbolicComputation.firstInstruction);
        auto *argPHI = IRB.CreatePHI(IRB.getInt8Ty()->getPointerTo(), 2);
        argPHI->addIncoming(originalArgExpression, argCheckBlock);
        argPHI->addIncoming(newArgExpression, newArgExpression->getParent());
        finalArgExpression = argPHI;
      } else {
        finalArgExpression = newArgExpression;
      }

      argument.replaceOperand(finalArgExpression);
    }

    // Finally, the overall result (if the computation produces one) is null
    // if we've taken the fast path and the symbolic expression computed above
    // if short-circuiting wasn't possible.
    if (!symbolicComputation.lastInstruction->use_empty()) {
      IRB.SetInsertPoint(&tail->front());
      auto *finalExpression = IRB.CreatePHI(IRB.getInt8Ty()->getPointerTo(), 2);
      symbolicComputation.lastInstruction->replaceAllUsesWith(finalExpression);
      finalExpression->addIncoming(
          ConstantPointerNull::get(IRB.getInt8Ty()->getPointerTo()), head);
      finalExpression->addIncoming(
          symbolicComputation.lastInstruction,
          symbolicComputation.lastInstruction->getParent());
    }
  }
}

void Symbolizer::handleIntrinsicCall(CallBase &I) {
  auto *callee = I.getCalledFunction();

  switch (callee->getIntrinsicID()) {
  case Intrinsic::dbg_value:
  case Intrinsic::is_constant:
  case Intrinsic::trap:
    // These are safe to ignore.
    break;
  case Intrinsic::memcpy: {
    IRBuilder<> IRB(&I);

    tryAlternative(IRB, I.getOperand(0));
    tryAlternative(IRB, I.getOperand(1));
    tryAlternative(IRB, I.getOperand(2));

    // The intrinsic allows both 32 and 64-bit integers to specify the length;
    // convert to the right type if necessary. This may truncate the value on
    // 32-bit architectures. However, what's the point of specifying a length to
    // memcpy that is larger than your address space?

    IRB.CreateCall(runtime.memcpy,
                   {I.getOperand(0), I.getOperand(1),
                    IRB.CreateZExtOrTrunc(I.getOperand(2), intPtrType)});
    break;
  }
  case Intrinsic::memset: {
    IRBuilder<> IRB(&I);

    tryAlternative(IRB, I.getOperand(0));
    tryAlternative(IRB, I.getOperand(2));

    // The comment on memcpy's length parameter applies analogously.

    IRB.CreateCall(runtime.memset,
                   {I.getOperand(0),
                    getSymbolicExpressionOrNull(I.getOperand(1)),
                    IRB.CreateZExtOrTrunc(I.getOperand(2), intPtrType)});
    break;
  }
  case Intrinsic::memmove: {
    IRBuilder<> IRB(&I);

    tryAlternative(IRB, I.getOperand(0));
    tryAlternative(IRB, I.getOperand(1));
    tryAlternative(IRB, I.getOperand(2));

    // The comment on memcpy's length parameter applies analogously.

    IRB.CreateCall(runtime.memmove,
                   {I.getOperand(0), I.getOperand(1),
                    IRB.CreateZExtOrTrunc(I.getOperand(2), intPtrType)});
    break;
  }
  case Intrinsic::stacksave: {
    // The intrinsic returns an opaque pointer that should only be passed to
    // the stackrestore intrinsic later. We treat the pointer as a constant.
    break;
  }
  case Intrinsic::stackrestore:
    // Ignored; see comment on stacksave above.
    break;
  case Intrinsic::expect:
    // Just a hint for the optimizer; the value is the first parameter.
    if (auto *expr = getSymbolicExpression(I.getArgOperand(0)))
      symbolicExpressions[&I] = expr;
    break;
  case Intrinsic::fabs: {
    // Floating-point absolute value; use the runtime to build the
    // corresponding symbolic expression.

    IRBuilder<> IRB(&I);
    auto abs = buildRuntimeCall(IRB, runtime.buildFloatAbs, I.getOperand(0));
    registerSymbolicComputation(abs, &I);
    break;
  }
  case Intrinsic::returnaddress:
  case Intrinsic::frameaddress:
  case Intrinsic::addressofreturnaddress: {
    // Obtain the return address of the current function or one of its parents
    // on the stack. We just concretize.

    errs() << "Warning: using concrete value for return/frame address\n";
    break;
  }
  case Intrinsic::bswap: {
    // Bswap changes the endian-ness of integer values.

    IRBuilder<> IRB(&I);
    auto swapped = buildRuntimeCall(IRB, runtime.buildBswap, I.getOperand(0));
    registerSymbolicComputation(swapped, &I);
    break;
  }

// Overflow arithmetic
#define DEF_OVF_ARITH_BUILDER(intrinsic_op, runtime_name)                      \
  case Intrinsic::s##intrinsic_op##_with_overflow:                             \
  case Intrinsic::u##intrinsic_op##_with_overflow: {                           \
    IRBuilder<> IRB(&I);                                                       \
                                                                               \
    bool isSigned =                                                            \
        I.getIntrinsicID() == Intrinsic::s##intrinsic_op##_with_overflow;      \
    auto overflow = buildRuntimeCall(                                          \
        IRB, runtime.build##runtime_name,                                      \
        {{I.getOperand(0), true},                                              \
         {I.getOperand(1), true},                                              \
         {IRB.getInt1(isSigned), false},                                       \
         {IRB.getInt1(dataLayout.isLittleEndian() ? 1 : 0), false}});          \
    registerSymbolicComputation(overflow, &I);                                 \
                                                                               \
    break;                                                                     \
  }

    DEF_OVF_ARITH_BUILDER(add, AddOverflow)
    DEF_OVF_ARITH_BUILDER(sub, SubOverflow)
    DEF_OVF_ARITH_BUILDER(mul, MulOverflow)

#undef DEF_OVF_ARITH_BUILDER

// Saturating arithmetic
#define DEF_SAT_ARITH_BUILDER(intrinsic_op, runtime_name)                      \
  case Intrinsic::intrinsic_op##_sat: {                                        \
    IRBuilder<> IRB(&I);                                                       \
    auto result = buildRuntimeCall(IRB, runtime.build##runtime_name,           \
                                   {I.getOperand(0), I.getOperand(1)});        \
    registerSymbolicComputation(result, &I);                                   \
    break;                                                                     \
  }

    DEF_SAT_ARITH_BUILDER(sadd, SAddSat)
    DEF_SAT_ARITH_BUILDER(uadd, UAddSat)
    DEF_SAT_ARITH_BUILDER(ssub, SSubSat)
    DEF_SAT_ARITH_BUILDER(usub, USubSat)
#if LLVM_VERSION_MAJOR > 11
    DEF_SAT_ARITH_BUILDER(sshl, SShlSat)
    DEF_SAT_ARITH_BUILDER(ushl, UShlSat)
#endif

#undef DEF_SAT_ARITH_BUILDER

  case Intrinsic::fshl:
  case Intrinsic::fshr: {
    IRBuilder<> IRB(&I);
    auto funnelShift = buildRuntimeCall(
        IRB,
        I.getIntrinsicID() == Intrinsic::fshl ? runtime.buildFshl
                                              : runtime.buildFshr,
        {I.getOperand(0), I.getOperand(1), I.getOperand(2)});
    registerSymbolicComputation(funnelShift, &I);
    break;
  }
#if LLVM_VERSION_MAJOR > 11
  case Intrinsic::abs: {
    // Integer absolute value

    IRBuilder<> IRB(&I);
    auto abs = buildRuntimeCall(IRB, runtime.buildAbs, I.getOperand(0));
    registerSymbolicComputation(abs, &I);
    break;
  }
  case Intrinsic::smin:
  case Intrinsic::smax:
  case Intrinsic::umin:
  case Intrinsic::umax: {
    if (!I.getType()->isIntegerTy()) {
      errs() << "Warning: unhandled vector LLVM intrinsic "
             << callee->getName() << "; the result will be concretized\n";
      break;
    }
    IRBuilder<> IRB(&I);
    SymFnT handler;
    switch (I.getIntrinsicID()) {
    case Intrinsic::smin:
      handler = runtime.buildSignedMin;
      break;
    case Intrinsic::smax:
      handler = runtime.buildSignedMax;
      break;
    case Intrinsic::umin:
      handler = runtime.buildUnsignedMin;
      break;
    case Intrinsic::umax:
      handler = runtime.buildUnsignedMax;
      break;
    default:
      llvm_unreachable("Unexpected integer min/max intrinsic");
    }
    auto extremum =
        buildRuntimeCall(IRB, handler, {I.getOperand(0), I.getOperand(1)});
    registerSymbolicComputation(extremum, &I);
    break;
  }
#endif
  case Intrinsic::eh_typeid_for:
    // This intrinsic returns a constant for our purposes.
    break;
  default:
    errs() << "Warning: unhandled LLVM intrinsic " << callee->getName()
           << "; the result will be concretized\n";
    break;
  }
}

void Symbolizer::handleInlineAssembly(CallInst &I) {
  if (I.getType()->isVoidTy()) {
    errs() << "Warning: skipping over inline assembly " << I << '\n';
    return;
  }

  errs() << "Warning: losing track of symbolic expressions at inline assembly "
         << I << '\n';
}

void Symbolizer::handleFunctionCall(CallBase &I, Instruction *returnPoint) {
  auto *callee = I.getCalledFunction();
  if (callee != nullptr && callee->isIntrinsic()) {
    handleIntrinsicCall(I);
    return;
  }

  IRBuilder<> IRB(returnPoint);
  IRB.CreateCall(runtime.notifyRet, getTargetPreferredInt(&I));
  IRB.SetInsertPoint(&I);
  IRB.CreateCall(runtime.notifyCall, getTargetPreferredInt(&I));

  if (callee == nullptr)
    tryAlternative(IRB, I.getCalledOperand());

  for (Use &arg : I.args())
    IRB.CreateCall(runtime.setParameterExpression,
                   {ConstantInt::get(IRB.getInt8Ty(), arg.getOperandNo()),
                    getSymbolicExpressionOrNull(arg)});

  if (!I.user_empty()) {
    // The result of the function is used somewhere later on. Since we have no
    // way of knowing whether the function is instrumented (and thus sets a
    // proper return expression), we have to account for the possibility that
    // it's not: in that case, we'll have to treat the result as an opaque
    // concrete value. Therefore, we set the return expression to null here in
    // order to avoid accidentally using whatever is stored there from the
    // previous function call. (If the function is instrumented, it will just
    // override our null with the real expression.)
    IRB.CreateCall(runtime.setReturnExpression,
                   ConstantPointerNull::get(IRB.getInt8Ty()->getPointerTo()));
    IRB.SetInsertPoint(returnPoint);
    symbolicExpressions[&I] = IRB.CreateCall(runtime.getReturnExpression);
  }
}

void Symbolizer::visitBinaryOperator(BinaryOperator &I) {
  // Binary operators propagate into the symbolic expression.

  IRBuilder<> IRB(&I);
  SymFnT handler = runtime.binaryOperatorHandlers.at(I.getOpcode());

  // Special case: the run-time library distinguishes between "and" and "or"
  // on Boolean values and bit vectors.
  if (I.getOperand(0)->getType() == IRB.getInt1Ty()) {
    switch (I.getOpcode()) {
    case Instruction::And:
      handler = runtime.buildBoolAnd;
      break;
    case Instruction::Or:
      handler = runtime.buildBoolOr;
      break;
    case Instruction::Xor:
      handler = runtime.buildBoolXor;
      break;
    default:
      errs() << "Can't handle Boolean operator " << I << '\n';
      llvm_unreachable("Unknown Boolean operator");
      break;
    }
  }

  assert(handler && "Unable to handle binary operator");
  auto runtimeCall =
      buildRuntimeCall(IRB, handler, {I.getOperand(0), I.getOperand(1)});
  registerSymbolicComputation(runtimeCall, &I);
}

void Symbolizer::visitUnaryOperator(UnaryOperator &I) {
  IRBuilder<> IRB(&I);
  SymFnT handler = runtime.unaryOperatorHandlers.at(I.getOpcode());

  assert(handler && "Unable to handle unary operator");
  auto runtimeCall = buildRuntimeCall(IRB, handler, I.getOperand(0));
  registerSymbolicComputation(runtimeCall, &I);
}

void Symbolizer::visitFreezeInst(FreezeInst &I) {
  // SymCC does not maintain a separate poison lattice. For every value that
  // already has a symbolic expression, freeze is therefore the identity on
  // that expression while LLVM retains the concrete execution semantics.
  if (auto *expression = getSymbolicExpression(I.getOperand(0)))
    symbolicExpressions[&I] = expression;
}

void Symbolizer::instrumentValueProfileForPathSite(
    IRBuilder<> &IRB, Value *condition, Instruction &site) {
  auto *comparison = dyn_cast<ICmpInst>(condition);
  if (comparison == nullptr)
    return;
  auto *integer_type = dyn_cast<IntegerType>(
      comparison->getOperand(0)->getType());
  auto *left_constant = dyn_cast<ConstantInt>(comparison->getOperand(0));
  auto *right_constant = dyn_cast<ConstantInt>(comparison->getOperand(1));
  Value *concrete = nullptr;
  if (left_constant != nullptr && right_constant == nullptr)
    concrete = comparison->getOperand(1);
  else if (right_constant != nullptr && left_constant == nullptr)
    concrete = comparison->getOperand(0);
  if (integer_type == nullptr || concrete == nullptr ||
      integer_type->getBitWidth() > 64)
    return;
  Value *symbolic = getSymbolicExpression(condition);
  if (symbolic == nullptr)
    return;
  IRB.CreateCall(runtime.notifyValueProfile,
                 {getTargetPreferredInt(&site),
                  IRB.CreateZExtOrTrunc(concrete, IRB.getInt64Ty()),
                  IRB.getInt8(integer_type->getBitWidth()), symbolic});
}

void Symbolizer::visitSelectInst(SelectInst &I) {
  // Select is like the ternary operator ("?:") in C. Record the concrete
  // condition as a path choice, but preserve both arms in the value expression.
  // Keeping the ITE is essential for later targets whose feasibility requires
  // changing this earlier choice.

  IRBuilder<> IRB(&I);
  // Hydra-generated selects replace one deliberately eliminated expensive
  // branch. They still need an ITE value, but turning each inserted operand
  // select back into a solver path choice would recreate the fork fan-out that
  // the control-flow transformation removed.
  if (I.getMetadata("symcc.hydra_select") == nullptr) {
    instrumentValueProfileForPathSite(IRB, I.getCondition(), I);
    auto pathConstraint = buildRuntimeCall(
        IRB, runtime.pushPathConstraint,
        {{I.getCondition(), true},
         {I.getCondition(), false},
         {getTargetPreferredInt(&I), false}});
    registerSymbolicComputation(pathConstraint);
  }

  auto ite = buildRuntimeCall(
      IRB, runtime.buildIte,
      {I.getCondition(), I.getTrueValue(), I.getFalseValue()});
  registerSymbolicComputation(ite, &I);
}

void Symbolizer::visitCmpInst(CmpInst &I) {
  // ICmp is integer comparison, FCmp compares floating-point values; we
  // simply include either in the resulting expression.

  IRBuilder<> IRB(&I);
  // Data Coverage distinguishes immediate predicates from predicates whose
  // operands originate in static storage. The runtime filters dynamic origins
  // against the module object registry.
  if (isa<ICmpInst>(I)) {
    auto *integerType = dyn_cast<IntegerType>(I.getOperand(0)->getType());
    auto *leftConstant = dyn_cast<ConstantInt>(I.getOperand(0));
    auto *rightConstant = dyn_cast<ConstantInt>(I.getOperand(1));
    Value *leftOrigin = findDataOrigin(I.getOperand(0));
    Value *rightOrigin = findDataOrigin(I.getOperand(1));
    ConstantInt *constant = nullptr;
    Value *concrete = nullptr;
    if (leftConstant && !rightConstant) {
      constant = leftConstant;
      concrete = I.getOperand(1);
    } else if (rightConstant && !leftConstant) {
      constant = rightConstant;
      concrete = I.getOperand(0);
    }
    if (integerType && integerType->getBitWidth() <= 64) {
      auto bits = integerType->getBitWidth();
      if (leftOrigin != nullptr || rightOrigin != nullptr) {
        auto *bytePointer = IRB.getInt8Ty()->getPointerTo();
        auto *nullOrigin = ConstantPointerNull::get(
            cast<PointerType>(bytePointer));
        IRB.CreateCall(
            runtime.notifyDataCompareExtended,
            {
                getTargetPreferredInt(&I),
                IRB.CreateZExtOrTrunc(I.getOperand(0), IRB.getInt64Ty()),
                IRB.CreateZExtOrTrunc(I.getOperand(1), IRB.getInt64Ty()),
                leftOrigin != nullptr
                    ? IRB.CreatePointerCast(leftOrigin, bytePointer)
                    : nullOrigin,
                rightOrigin != nullptr
                    ? IRB.CreatePointerCast(rightOrigin, bytePointer)
                    : nullOrigin,
                IRB.getInt8(bits),
                IRB.getInt8(
                    I.getPredicate() == CmpInst::ICMP_EQ ||
                            I.getPredicate() == CmpInst::ICMP_NE
                        ? 0
                        : 1),
            });
      }
      if (constant != nullptr) {
        auto *concrete64 =
            IRB.CreateZExtOrTrunc(concrete, IRB.getInt64Ty());
        IRB.CreateCall(runtime.notifyDataCompare,
                       {getTargetPreferredInt(&I), concrete64,
                        IRB.getInt64(constant->getValue().getZExtValue()),
                        IRB.getInt8(bits)});
      }
    }
  }
  SymFnT handler = runtime.comparisonHandlers.at(I.getPredicate());
  assert(handler && "Unable to handle icmp/fcmp variant");
  auto runtimeCall =
      buildRuntimeCall(IRB, handler, {I.getOperand(0), I.getOperand(1)});
  registerSymbolicComputation(runtimeCall, &I);
}

void Symbolizer::visitReturnInst(ReturnInst &I) {
  // Upon return, we just store the expression for the return value.

  if (I.getReturnValue() == nullptr)
    return;

  // We can't short-circuit this call because the return expression needs to
  // be set even if it's null; otherwise we break the caller. Therefore,
  // create the call directly without registering it for short-circuit
  // processing.
  IRBuilder<> IRB(&I);
  IRB.CreateCall(runtime.setReturnExpression,
                 getSymbolicExpressionOrNull(I.getReturnValue()));
}

void Symbolizer::visitBranchInst(BranchInst &I) {
  // Br can jump conditionally or unconditionally. We are only interested in
  // the former case, in which we push the branch condition or its negation to
  // the path constraints.

  if (I.isUnconditional())
    return;

  IRBuilder<> IRB(&I);
  instrumentValueProfileForPathSite(IRB, I.getCondition(), I);
  if (scheduleMemoryTracing) {
    Value *successor = IRB.CreateSelect(
        I.getCondition(),
        ConstantInt::get(
            intPtrType, symcc::stableSiteId(*I.getSuccessor(0))),
        ConstantInt::get(
            intPtrType, symcc::stableSiteId(*I.getSuccessor(1))));
    IRB.CreateCall(
        runtime.notifyScheduleBranch,
        {
            getTargetPreferredInt(&I),
            IRB.CreateZExt(I.getCondition(), IRB.getInt64Ty()),
            successor,
        });
  }
  auto runtimeCall = buildRuntimeCall(IRB, runtime.pushPathConstraint,
                                      {{I.getCondition(), true},
                                       {I.getCondition(), false},
                                       {getTargetPreferredInt(&I), false}});
  registerSymbolicComputation(runtimeCall);
}

void Symbolizer::visitIndirectBrInst(IndirectBrInst &I) {
  IRBuilder<> IRB(&I);
  tryAlternative(IRB, I.getAddress());
}

void Symbolizer::visitCallInst(CallInst &I) {
  if (I.isInlineAsm())
    handleInlineAssembly(I);
  else
    handleFunctionCall(I, I.getNextNode());
}

void Symbolizer::visitInvokeInst(InvokeInst &I) {
  // Invoke is like a call but additionally establishes an exception handler. We
  // can obtain the return expression only in the success case, but the target
  // block may have multiple incoming edges (i.e., our edge may be critical). In
  // this case, we split the edge and query the return expression in the new
  // block that is specific to our edge.
  auto *newBlock = SplitCriticalEdge(I.getParent(), I.getNormalDest());
  handleFunctionCall(I, newBlock != nullptr
                            ? newBlock->getFirstNonPHI()
                            : I.getNormalDest()->getFirstNonPHI());
}

void Symbolizer::visitAllocaInst(AllocaInst & /*unused*/) {
  // Nothing to do: the shadow for the newly allocated memory region will be
  // created on first write; until then, the memory contents are concrete.
}

void Symbolizer::visitLoadInst(LoadInst &I) {
  IRBuilder<> IRB(&I);

  auto *addr = I.getPointerOperand();
  auto *dataType = I.getType();
  uint64_t byteWidth = typeStoreBytes(dataLayout, dataType);
  if (scheduleMemoryTracing && byteWidth != 0) {
    Value *scheduleAddress =
        IRB.CreatePointerCast(addr, IRB.getInt8Ty()->getPointerTo());
    if (I.isAtomic()) {
      if (I.getMetadata(kScheduleAtomicMetadata) == nullptr)
        IRB.CreateCall(
            runtime.notifyScheduleAtomic,
            {
                scheduleAddress,
                ConstantInt::get(intPtrType, byteWidth),
                IRB.getInt8(0),
                IRB.getInt8(scheduleAtomicOrder(I.getOrdering())),
                IRB.getInt8(0),
                IRB.getInt8(0),
            });
    } else if (!I.isVolatile()) {
      IRB.CreateCall(
          runtime.notifyScheduleRead,
          {
              scheduleAddress,
              ConstantInt::get(intPtrType, byteWidth),
          });
    }
  }
  if (!reachesIntegerComparison(&I)) {
    if (byteWidth != 0 && byteWidth <= UINT32_MAX / 8) {
      IRB.CreateCall(
          runtime.notifyDataLoad,
          {
              getTargetPreferredInt(&I),
              IRB.CreatePointerCast(
                  addr, IRB.getInt8Ty()->getPointerTo()),
              IRB.getInt32(static_cast<uint32_t>(byteWidth * 8)),
          });
    }
  }
  tryAlternative(IRB, addr);

  auto *data = IRB.CreateCall(
      runtime.readMemory,
      {IRB.CreatePtrToInt(addr, intPtrType),
       ConstantInt::get(intPtrType, dataLayout.getTypeStoreSize(dataType)),
       IRB.getInt1(isLittleEndian(dataType) ? 1 : 0)});

  symbolicExpressions[&I] = convertBitVectorExprForType(IRB, data, dataType);
  if (memorySSA != nullptr && I.isSimple() &&
      I.getMetadata("symcc.ifss_continuation_memory_source") == nullptr)
    memoryMergeLoads.push_back(&I);
}

void Symbolizer::visitStoreInst(StoreInst &I) {
  IRBuilder<> IRB(&I);

  tryAlternative(IRB, I.getPointerOperand());

  // Make sure that the expression corresponding to the stored value is of
  // bit-vector kind. Shortcutting the runtime calls that we emit here (e.g.,
  // for floating-point values) is tricky, so instead we make sure that any
  // runtime function we call can handle null expressions.

  auto V = I.getValueOperand();
  uint64_t byteWidth = typeStoreBytes(dataLayout, V->getType());
  if (scheduleMemoryTracing && byteWidth != 0) {
    Value *scheduleAddress = IRB.CreatePointerCast(
        I.getPointerOperand(), IRB.getInt8Ty()->getPointerTo());
    if (I.isAtomic()) {
      if (I.getMetadata(kScheduleAtomicMetadata) == nullptr)
        IRB.CreateCall(
            runtime.notifyScheduleAtomic,
            {
                scheduleAddress,
                ConstantInt::get(intPtrType, byteWidth),
                IRB.getInt8(1),
                IRB.getInt8(scheduleAtomicOrder(I.getOrdering())),
                IRB.getInt8(0),
                IRB.getInt8(0),
            });
    } else if (!I.isVolatile()) {
      IRB.CreateCall(
          runtime.notifyScheduleWrite,
          {
              scheduleAddress,
              ConstantInt::get(intPtrType, byteWidth),
          });
    }
  }
  auto maybeConversion =
      convertExprForTypeToBitVectorExpr(IRB, V, getSymbolicExpression(V));

  IRB.CreateCall(
      runtime.writeMemory,
      {IRB.CreatePtrToInt(I.getPointerOperand(), intPtrType),
       ConstantInt::get(intPtrType, byteWidth),
       maybeConversion ? maybeConversion->lastInstruction
                       : getSymbolicExpressionOrNull(V),
       IRB.getInt1(isLittleEndian(V->getType()) ? 1 : 0)});
}

void Symbolizer::visitAtomicRMWInst(AtomicRMWInst &I) {
  if (!scheduleMemoryTracing ||
      I.getMetadata(kScheduleAtomicMetadata) != nullptr)
    return;
  IRBuilder<> IRB(&I);
  uint64_t byteWidth = typeStoreBytes(
      dataLayout, I.getValOperand()->getType());
  if (byteWidth == 0)
    return;
  IRB.CreateCall(
      runtime.notifyScheduleAtomic,
      {
          IRB.CreatePointerCast(
              I.getPointerOperand(), IRB.getInt8Ty()->getPointerTo()),
          ConstantInt::get(intPtrType, byteWidth),
          IRB.getInt8(2),
          IRB.getInt8(scheduleAtomicOrder(I.getOrdering())),
          IRB.getInt8(0),
          IRB.getInt8(static_cast<uint8_t>(I.getOperation())),
      });
}

void Symbolizer::visitAtomicCmpXchgInst(AtomicCmpXchgInst &I) {
  if (!scheduleMemoryTracing ||
      I.getMetadata(kScheduleAtomicMetadata) != nullptr)
    return;
  IRBuilder<> before(&I);
  uint64_t byteWidth = typeStoreBytes(
      dataLayout, I.getCompareOperand()->getType());
  if (byteWidth == 0)
    return;
  Value *address = before.CreatePointerCast(
      I.getPointerOperand(), before.getInt8Ty()->getPointerTo());
  Value *group = before.CreateCall(
      runtime.notifyScheduleAtomic,
      {
          address,
          ConstantInt::get(intPtrType, byteWidth),
          before.getInt8(3),
          before.getInt8(scheduleAtomicOrder(I.getSuccessOrdering())),
          before.getInt8(scheduleAtomicOrder(I.getFailureOrdering())),
          before.getInt8(0),
      });
  IRBuilder<> after(I.getNextNode());
  Value *success = after.CreateExtractValue(&I, 1);
  after.CreateCall(
      runtime.notifyScheduleAtomicResult,
      {group, address, success});
}

void Symbolizer::visitFenceInst(FenceInst &I) {
  if (!scheduleMemoryTracing ||
      I.getMetadata(kScheduleAtomicMetadata) != nullptr)
    return;
  IRBuilder<> IRB(&I);
  Value *site = IRB.CreateIntToPtr(
      getTargetPreferredInt(&I), IRB.getInt8Ty()->getPointerTo());
  IRB.CreateCall(
      runtime.notifyScheduleAtomic,
      {
          site,
          ConstantInt::get(intPtrType, 0),
          IRB.getInt8(4),
          IRB.getInt8(scheduleAtomicOrder(I.getOrdering())),
          IRB.getInt8(0),
          IRB.getInt8(0),
      });
}

void Symbolizer::visitGetElementPtrInst(GetElementPtrInst &I) {
  // GEP performs address calculations but never actually accesses memory. In
  // order to represent the result of a GEP symbolically, we start from the
  // symbolic expression of the original pointer and duplicate its
  // computations at the symbolic level.

  // If everything is compile-time concrete, we don't need to emit code.
  if (getSymbolicExpression(I.getPointerOperand()) == nullptr &&
      std::all_of(I.idx_begin(), I.idx_end(), [this](Value *index) {
        return (getSymbolicExpression(index) == nullptr);
      })) {
    return;
  }

  // If there are no indices or if they are all zero we can return early as
  // well.
  if (std::all_of(I.idx_begin(), I.idx_end(), [](Value *index) {
        auto *ci = dyn_cast<ConstantInt>(index);
        return (ci != nullptr && ci->isZero());
      })) {
    symbolicExpressions[&I] = getSymbolicExpression(I.getPointerOperand());
    return;
  }

  IRBuilder<> IRB(&I);
  SymbolicComputation symbolicComputation;
  Value *currentAddress = I.getPointerOperand();
  const unsigned addressSpace = I.getPointerAddressSpace();
  const unsigned pointerWidth =
      dataLayout.getPointerSizeInBits(addressSpace);
  const unsigned indexWidth = dataLayout.getIndexSizeInBits(addressSpace);
  if (dataLayout.isNonIntegralAddressSpace(addressSpace) ||
      pointerWidth != ptrBits || indexWidth == 0 || indexWidth > pointerWidth) {
    errs() << "Warning: unsupported GEP pointer/index layout " << I
           << "; the result will be concretized\n";
    return;
  }
  auto *indexType = IRB.getIntNTy(indexWidth);

  auto appendAddressOffset = [&](Value *offset, bool lookupOffsetExpression) {
    const bool lookupCurrentExpression =
        currentAddress == I.getPointerOperand();
    if (indexWidth == pointerWidth) {
      symbolicComputation.merge(forceBuildRuntimeCall(
          IRB, runtime.binaryOperatorHandlers[Instruction::Add],
          {{offset, lookupOffsetExpression},
           {currentAddress, lookupCurrentExpression}}));
      currentAddress = symbolicComputation.lastInstruction;
      return;
    }

    // DataLayout may use fewer address bits for GEP arithmetic than for the
    // pointer representation. LLVM updates only that low index-width slice;
    // carry out of the slice must not alter the pointer's high bits.
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, runtime.buildTrunc,
        {{currentAddress, lookupCurrentExpression},
         {IRB.getInt8(indexWidth), false}}));
    Value *lowAddress = symbolicComputation.lastInstruction;
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, runtime.binaryOperatorHandlers[Instruction::Add],
        {{lowAddress, false}, {offset, lookupOffsetExpression}}));
    Value *lowSum = symbolicComputation.lastInstruction;

    APInt highMask = APInt::getHighBitsSet(
        pointerWidth, pointerWidth - indexWidth);
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, runtime.binaryOperatorHandlers[Instruction::And],
        {{currentAddress, lookupCurrentExpression},
         {ConstantInt::get(intPtrType, highMask), true}}));
    Value *highAddress = symbolicComputation.lastInstruction;
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, runtime.buildZExt,
        {{lowSum, false},
         {IRB.getInt8(pointerWidth - indexWidth), false}}));
    Value *extendedLowSum = symbolicComputation.lastInstruction;
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, runtime.binaryOperatorHandlers[Instruction::Or],
        {{highAddress, false}, {extendedLowSum, false}}));
    currentAddress = symbolicComputation.lastInstruction;
  };

  for (auto type_it = gep_type_begin(I), type_end = gep_type_end(I);
       type_it != type_end; ++type_it) {
    auto *index = type_it.getOperand();

    // There are two cases for the calculation:
    // 1. If the indexed type is a struct, we need to add the offset of the
    //    desired member.
    // 2. If it is an array or a pointer, compute the offset of the desired
    //    element.
    if (auto *structType = type_it.getStructTypeOrNull()) {
      // Structs can only be indexed with constants
      // (https://llvm.org/docs/LangRef.html#getelementptr-instruction).

      unsigned memberIndex = cast<ConstantInt>(index)->getZExtValue();
      uint64_t memberOffset =
          dataLayout.getStructLayout(structType)->getElementOffset(memberIndex);
      appendAddressOffset(ConstantInt::get(indexType, memberOffset), true);
    } else {
      if (auto *ci = dyn_cast<ConstantInt>(index);
          ci != nullptr && ci->isZero()) {
        // Fast path: an index of zero means that no calculations are
        // performed.
        continue;
      }

      // TODO optimize? If the index is constant, we can perform the
      // multiplication ourselves instead of having the solver do it. Also, if
      // the element size is 1, we can omit the multiplication.

      TypeSize elementSize =
          dataLayout.getTypeAllocSize(type_it.getIndexedType());
      Value *elementSizeValue = ConstantInt::get(
          indexType, elementSize.getKnownMinValue());
      if (elementSize.isScalable()) {
        // A scalable vector occupies vscale times its known minimum size.
        // vscale is concrete for one execution but must participate in the
        // symbolic address expression whenever the index is symbolic.
        elementSizeValue = IRB.CreateVScale(
            cast<Constant>(elementSizeValue), "symcc.gep.element.size");
      }
      const unsigned sourceWidth = index->getType()->getIntegerBitWidth();
      Value *normalizedIndex = index;
      bool lookupNormalizedExpression = true;
      if (sourceWidth < indexWidth) {
        symbolicComputation.merge(forceBuildRuntimeCall(
            IRB, runtime.buildSExt,
            {{index, true},
             {IRB.getInt8(indexWidth - sourceWidth), false}}));
        normalizedIndex = symbolicComputation.lastInstruction;
        lookupNormalizedExpression = false;
      } else if (sourceWidth > indexWidth) {
        symbolicComputation.merge(forceBuildRuntimeCall(
            IRB, runtime.buildTrunc,
            {{index, true}, {IRB.getInt8(indexWidth), false}}));
        normalizedIndex = symbolicComputation.lastInstruction;
        lookupNormalizedExpression = false;
      }

      symbolicComputation.merge(forceBuildRuntimeCall(
          IRB, runtime.binaryOperatorHandlers[Instruction::Mul],
          {{normalizedIndex, lookupNormalizedExpression},
           {elementSizeValue, true}}));
      appendAddressOffset(symbolicComputation.lastInstruction, false);
    }
  }

  registerSymbolicComputation(symbolicComputation, &I);
}

void Symbolizer::visitBitCastInst(BitCastInst &I) {
  if (I.getSrcTy()->isIntegerTy() && I.getDestTy()->isFloatingPointTy()) {
    IRBuilder<> IRB(&I);
    auto conversion =
        buildRuntimeCall(IRB, runtime.buildBitsToFloat,
                         {{I.getOperand(0), true},
                          {IRB.getInt1(I.getDestTy()->isDoubleTy()), false}});
    registerSymbolicComputation(conversion, &I);
    return;
  }

  if (I.getSrcTy()->isFloatingPointTy() && I.getDestTy()->isIntegerTy()) {
    IRBuilder<> IRB(&I);
    auto conversion = buildRuntimeCall(IRB, runtime.buildFloatToBits,
                                       {{I.getOperand(0), true}});
    registerSymbolicComputation(conversion);
    return;
  }

  assert(I.getSrcTy()->isPointerTy() && I.getDestTy()->isPointerTy() &&
         "Unhandled non-pointer bit cast");
  if (auto *expr = getSymbolicExpression(I.getOperand(0)))
    symbolicExpressions[&I] = expr;
}

void Symbolizer::visitTruncInst(TruncInst &I) {
  IRBuilder<> IRB(&I);

  if (getSymbolicExpression(I.getOperand(0)) == nullptr)
    return;

  SymbolicComputation symbolicComputation;
  symbolicComputation.merge(forceBuildRuntimeCall(
      IRB, runtime.buildTrunc,
      {{I.getOperand(0), true},
       {IRB.getInt8(I.getDestTy()->getIntegerBitWidth()), false}}));

  if (I.getDestTy()->isIntegerTy() &&
      I.getDestTy()->getIntegerBitWidth() == 1) {
    // convert from byte back to a bool (i1)
    symbolicComputation.merge(
        forceBuildRuntimeCall(IRB, runtime.buildBitToBool,
                              {{symbolicComputation.lastInstruction, false}}));
  }

  registerSymbolicComputation(symbolicComputation, &I);
}

void Symbolizer::visitIntToPtrInst(IntToPtrInst &I) {
  auto *expr = getSymbolicExpression(I.getOperand(0));
  if (expr == nullptr)
    return;

  const unsigned sourceBits = I.getSrcTy()->getIntegerBitWidth();
  if (sourceBits == ptrBits) {
    symbolicExpressions[&I] = expr;
    return;
  }

  IRBuilder<> IRB(&I);
  if (sourceBits < ptrBits) {
    auto conversion = buildRuntimeCall(
        IRB, runtime.buildZExt,
        {{I.getOperand(0), true},
         {IRB.getInt8(ptrBits - sourceBits), false}});
    registerSymbolicComputation(conversion, &I);
  } else {
    auto conversion = buildRuntimeCall(
        IRB, runtime.buildTrunc,
        {{I.getOperand(0), true}, {IRB.getInt8(ptrBits), false}});
    registerSymbolicComputation(conversion, &I);
  }
}

void Symbolizer::visitPtrToIntInst(PtrToIntInst &I) {
  auto *expr = getSymbolicExpression(I.getOperand(0));
  if (expr == nullptr)
    return;

  const unsigned destinationBits = I.getDestTy()->getIntegerBitWidth();
  if (destinationBits == ptrBits) {
    symbolicExpressions[&I] = expr;
    return;
  }

  IRBuilder<> IRB(&I);
  if (destinationBits < ptrBits) {
    auto conversion = buildRuntimeCall(
        IRB, runtime.buildTrunc,
        {{I.getOperand(0), true}, {IRB.getInt8(destinationBits), false}});
    registerSymbolicComputation(conversion, &I);
  } else {
    auto conversion = buildRuntimeCall(
        IRB, runtime.buildZExt,
        {{I.getOperand(0), true},
         {IRB.getInt8(destinationBits - ptrBits), false}});
    registerSymbolicComputation(conversion, &I);
  }
}

void Symbolizer::visitSIToFPInst(SIToFPInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion =
      buildRuntimeCall(IRB, runtime.buildIntToFloat,
                       {{I.getOperand(0), true},
                        {IRB.getInt1(I.getDestTy()->isDoubleTy()), false},
                        {/* is_signed */ IRB.getInt1(true), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitUIToFPInst(UIToFPInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion =
      buildRuntimeCall(IRB, runtime.buildIntToFloat,
                       {{I.getOperand(0), true},
                        {IRB.getInt1(I.getDestTy()->isDoubleTy()), false},
                        {/* is_signed */ IRB.getInt1(false), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitFPExtInst(FPExtInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion =
      buildRuntimeCall(IRB, runtime.buildFloatToFloat,
                       {{I.getOperand(0), true},
                        {IRB.getInt1(I.getDestTy()->isDoubleTy()), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitFPTruncInst(FPTruncInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion =
      buildRuntimeCall(IRB, runtime.buildFloatToFloat,
                       {{I.getOperand(0), true},
                        {IRB.getInt1(I.getDestTy()->isDoubleTy()), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitFPToSI(FPToSIInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion = buildRuntimeCall(
      IRB, runtime.buildFloatToSignedInt,
      {{I.getOperand(0), true},
       {IRB.getInt8(I.getType()->getIntegerBitWidth()), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitFPToUI(FPToUIInst &I) {
  IRBuilder<> IRB(&I);
  auto conversion = buildRuntimeCall(
      IRB, runtime.buildFloatToUnsignedInt,
      {{I.getOperand(0), true},
       {IRB.getInt8(I.getType()->getIntegerBitWidth()), false}});
  registerSymbolicComputation(conversion, &I);
}

void Symbolizer::visitCastInst(CastInst &I) {
  auto opcode = I.getOpcode();
  if (opcode != Instruction::SExt && opcode != Instruction::ZExt) {
    errs() << "Warning: unhandled cast instruction " << I << '\n';
    return;
  }

  IRBuilder<> IRB(&I);

  SymFnT target;

  switch (I.getOpcode()) {
  case Instruction::SExt:
    target = runtime.buildSExt;
    break;
  case Instruction::ZExt:
    target = runtime.buildZExt;
    break;
  default:
    llvm_unreachable("Unknown cast opcode");
  }

  // LLVM bitcode represents Boolean values as i1. In Z3, those are a not a
  // bit-vector sort, so trying to cast one into a bit vector of any length
  // raises an error. The run-time library provides a dedicated conversion
  // function for this case.
  if (I.getSrcTy()->getIntegerBitWidth() == 1) {

    SymbolicComputation symbolicComputation;
    symbolicComputation.merge(forceBuildRuntimeCall(IRB, runtime.buildBoolToBit,
                                                    {{I.getOperand(0), true}}));
    symbolicComputation.merge(forceBuildRuntimeCall(
        IRB, target,
        {{symbolicComputation.lastInstruction, false},
         {IRB.getInt8(I.getDestTy()->getIntegerBitWidth() - 1), false}}));

    registerSymbolicComputation(symbolicComputation, &I);

  } else {
    auto symbolicCast =
        buildRuntimeCall(IRB, target,
                         {{I.getOperand(0), true},
                          {IRB.getInt8(I.getDestTy()->getIntegerBitWidth() -
                                       I.getSrcTy()->getIntegerBitWidth()),
                           false}});
    registerSymbolicComputation(symbolicCast, &I);
  }
}

void Symbolizer::visitPHINode(PHINode &I) {
  // PHI nodes just assign values based on the origin of the last jump, so we
  // assign the corresponding symbolic expression the same way.

  phiNodes.push_back(&I); // to be finalized later, see finalizePHINodes

  IRBuilder<> IRB(&I);
  unsigned numIncomingValues = I.getNumIncomingValues();
  auto *exprPHI =
      IRB.CreatePHI(IRB.getInt8Ty()->getPointerTo(), numIncomingValues);
  for (unsigned incoming = 0; incoming < numIncomingValues; incoming++) {
    exprPHI->addIncoming(
        // The null pointer will be replaced in finalizePHINodes.
        ConstantPointerNull::get(
            cast<PointerType>(IRB.getInt8Ty()->getPointerTo())),
        I.getIncomingBlock(incoming));
  }

  symbolicExpressions[&I] = exprPHI;
}

void Symbolizer::visitInsertValueInst(InsertValueInst &I) {
  IRBuilder<> IRB(&I);
  auto target = I.getAggregateOperand();
  auto insertedValue = I.getInsertedValueOperand();

  if (getSymbolicExpression(target) == nullptr &&
      getSymbolicExpression(insertedValue) == nullptr)
    return;

  // We may have to convert the expression to bit-vector kind...
  auto maybeConversion = convertExprForTypeToBitVectorExpr(
      IRB, insertedValue, getSymbolicExpressionOrNull(insertedValue));

  auto insert = IRB.CreateCall(
      runtime.buildInsert,
      {getSymbolicExpressionOrNull(target),
       // If we had to convert the expression, use the result of the conversion.
       maybeConversion ? maybeConversion->lastInstruction
                       : getSymbolicExpressionOrNull(insertedValue),
       IRB.getInt64(aggregateMemberOffset(target->getType(), I.getIndices())),
       IRB.getInt1(isLittleEndian(insertedValue->getType()) ? 1 : 0)});
  auto insertComputation =
      SymbolicComputation(insert, insert, {Input(target, 0, insert)});

  if (!maybeConversion) {
    // If we didn't have to convert, then the inserted value is first used in
    // the insertion.
    insertComputation.inputs.push_back(Input(insertedValue, 1, insert));
  } else {
    // Otherwise, the full computation consists of the conversion followed by
    // the insertion.
    maybeConversion->merge(insertComputation);
  }

  registerSymbolicComputation(maybeConversion.value_or(insertComputation), &I);
}

void Symbolizer::visitExtractValueInst(ExtractValueInst &I) {
  IRBuilder<> IRB(&I);
  auto target = I.getAggregateOperand();
  auto targetExpr = getSymbolicExpression(target);
  auto resultType = I.getType();

  if (targetExpr == nullptr)
    return;

  auto extractedBits = IRB.CreateCall(
      runtime.buildExtract,
      {targetExpr,
       IRB.getInt64(aggregateMemberOffset(target->getType(), I.getIndices())),
       IRB.getInt64(dataLayout.getTypeStoreSize(resultType)),
       IRB.getInt1(isLittleEndian(resultType) ? 1 : 0)});

  Instruction *result =
      convertBitVectorExprForType(IRB, extractedBits, resultType);
  registerSymbolicComputation(
      {extractedBits, result, {{target, 0, extractedBits}}}, &I);
}

void Symbolizer::visitSwitchInst(SwitchInst &I) {
  // Switch compares a value against a set of integer constants; duplicate
  // constants are not allowed
  // (https://llvm.org/docs/LangRef.html#switch-instruction).

  IRBuilder<> IRB(&I);
  auto *condition = I.getCondition();
  auto bits = condition->getType()->getIntegerBitWidth();
  if (scheduleMemoryTracing && bits <= 64) {
    Value *successor = ConstantInt::get(
        intPtrType, symcc::stableSiteId(*I.getDefaultDest()));
    for (const auto &caseHandle : I.cases()) {
      Value *matches = IRB.CreateICmpEQ(
          condition, caseHandle.getCaseValue());
      successor = IRB.CreateSelect(
          matches,
          ConstantInt::get(
              intPtrType,
              symcc::stableSiteId(*caseHandle.getCaseSuccessor())),
          successor);
    }
    IRB.CreateCall(
        runtime.notifyScheduleBranch,
        {
            getTargetPreferredInt(&I),
            IRB.CreateZExtOrTrunc(condition, IRB.getInt64Ty()),
            successor,
        });
  }
  if (bits <= 64 && I.getNumCases() != 0 &&
      I.getNumCases() <= 65536) {
    std::vector<uint64_t> caseValues;
    caseValues.reserve(I.getNumCases());
    for (const auto &caseHandle : I.cases())
      caseValues.push_back(caseHandle.getCaseValue()->getZExtValue());
    std::sort(caseValues.begin(), caseValues.end());
    std::vector<Constant *> constants;
    constants.reserve(caseValues.size());
    for (uint64_t value : caseValues)
      constants.push_back(IRB.getInt64(value));
    ArrayType *arrayType =
        ArrayType::get(IRB.getInt64Ty(), constants.size());
    auto *caseArray = new GlobalVariable(
        *I.getModule(), arrayType, true, GlobalValue::PrivateLinkage,
        ConstantArray::get(arrayType, constants),
        "__sym_data_switch_" +
            std::to_string(symcc::stableSiteId(I)));
    caseArray->setUnnamedAddr(GlobalValue::UnnamedAddr::Global);
    Value *zero = IRB.getInt32(0);
    Value *casePointer = IRB.CreateInBoundsGEP(
        arrayType, caseArray, {zero, zero});
    IRB.CreateCall(
        runtime.notifyDataSwitch,
        {
            getTargetPreferredInt(&I),
            IRB.CreateZExtOrTrunc(condition, IRB.getInt64Ty()),
            casePointer,
            ConstantInt::get(intPtrType, caseValues.size()),
            IRB.getInt8(bits),
        });
  }
  auto *conditionExpr = getSymbolicExpression(condition);
  if (conditionExpr == nullptr)
    return;

  // Build a check whether we have a symbolic condition, to be used later.
  auto *haveSymbolicCondition = IRB.CreateICmpNE(
      conditionExpr, ConstantPointerNull::get(IRB.getInt8Ty()->getPointerTo()));
  auto *constraintBlock = SplitBlockAndInsertIfThen(haveSymbolicCondition, &I,
                                                    /* unreachable */ false);

  // In the constraint block, we push one path constraint per case.
  IRB.SetInsertPoint(constraintBlock);
  if (bits <= 64 && I.getNumCases() != 0)
    IRB.CreateCall(runtime.notifyValueProfile,
                   {getTargetPreferredInt(&I),
                    IRB.CreateZExtOrTrunc(condition, IRB.getInt64Ty()),
                    IRB.getInt8(bits), conditionExpr});
  for (auto &caseHandle : I.cases()) {
    auto *caseTaken = IRB.CreateICmpEQ(condition, caseHandle.getCaseValue());
    auto *caseConstraint = IRB.CreateCall(
        runtime.comparisonHandlers[CmpInst::ICMP_EQ],
        {conditionExpr, createValueExpression(caseHandle.getCaseValue(), IRB)});
    IRB.CreateCall(runtime.pushPathConstraint,
                   {caseConstraint, caseTaken, getTargetPreferredInt(&I)});
  }
}

void Symbolizer::visitUnreachableInst(UnreachableInst & /*unused*/) {
  // Nothing to do here...
}

void Symbolizer::visitInstruction(Instruction &I) {
  // Some instructions are only used in the context of exception handling, which
  // we ignore for now.
  if (isa<LandingPadInst>(I) || isa<ResumeInst>(I))
    return;

  errs() << "Warning: unknown instruction " << I
         << "; the result will be concretized\n";
}

Instruction *Symbolizer::createValueExpression(Value *V, IRBuilder<> &IRB) {
  auto *valueType = V->getType();

  if (isa<ConstantPointerNull>(V)) {
    return IRB.CreateCall(runtime.buildNullPointer, {});
  }

  if (valueType->isIntegerTy()) {
    auto bits = valueType->getPrimitiveSizeInBits();
    if (bits == 1) {
      // Special case: LLVM uses the type i1 to represent Boolean values, but
      // for Z3 we have to create expressions of a separate sort.
      return IRB.CreateCall(runtime.buildBool, {V});
    } else if (bits <= 64) {
      return IRB.CreateCall(runtime.buildInteger,
                            {IRB.CreateZExtOrBitCast(V, IRB.getInt64Ty()),
                             IRB.getInt8(valueType->getPrimitiveSizeInBits())});
    } else if (bits <= 128) {
      // Anything up to the maximum supported 128 bits. Those integers are a bit
      // tricky because the symbolic backends don't support them per se. We have
      // a special function in the run-time library that handles them, usually
      // by assembling expressions from smaller chunks.
      return IRB.CreateCall(
          runtime.buildInteger128,
          {IRB.CreateTrunc(IRB.CreateLShr(V, ConstantInt::get(valueType, 64)),
                           IRB.getInt64Ty()),
           IRB.CreateTrunc(V, IRB.getInt64Ty())});
    } else {
      auto *storage = IRB.CreateAlloca(valueType);
      IRB.CreateStore(V, storage);
      auto *rawStorage =
          IRB.CreateBitCast(storage, IRB.getInt8Ty()->getPointerTo());
      return IRB.CreateCall(runtime.buildIntegerFromBuffer,
                            {rawStorage, IRB.getInt32(bits)});
    }
  }

  if (valueType->isFloatingPointTy()) {
    return IRB.CreateCall(runtime.buildFloat,
                          {IRB.CreateFPCast(V, IRB.getDoubleTy()),
                           IRB.getInt1(valueType->isDoubleTy())});
  }

  if (valueType->isPointerTy()) {
    return IRB.CreateCall(
        runtime.buildInteger,
        {IRB.CreatePtrToInt(V, IRB.getInt64Ty()), IRB.getInt8(ptrBits)});
  }

  if (auto structType = dyn_cast<StructType>(valueType)) {
    // In unoptimized code we may see structures in SSA registers. What we
    // want is a single bit-vector expression describing their contents, but
    // unfortunately we can't take the address of a register. What we do instead
    // is to build the expression recursively by iterating over the elements of
    // the structure.
    //
    // An alternative would be to change the representation of structures in
    // SSA registers to "shadow structures" that contain one expression per
    // member. However, this would put an additional burden on the handling of
    // cast instructions, because expressions would have to be converted
    // between different representations according to the type.

    if (isa<UndefValue>(V)) {
      // This is just an optimization for completely undefined structs; we
      // create an all-zeros expression without iterating over the elements.
      return IRB.CreateCall(
          runtime.buildZeroBytes,
          {ConstantInt::get(intPtrType,
                            dataLayout.getTypeStoreSize(valueType))});
    } else {
      // Iterate over the elements of the struct and concatenate the
      // corresponding expressions (along with any padding that might be
      // needed).

      auto structLayout = dataLayout.getStructLayout(structType);
      auto constantStructValue = dyn_cast<ConstantStruct>(V);
      size_t offset = 0; // The end of the expressed portion in bytes.
      Instruction *expr = nullptr;
      auto append = [&](Instruction *newExpr) {
        expr = expr ? IRB.CreateCall(runtime.buildConcat, {expr, newExpr})
                    : newExpr;
      };

      for (size_t i = 0; i < structType->getNumElements(); i++) {
        // Build an expression for any padding preceding the current element.
        if (auto padding = structLayout->getElementOffset(i) - offset;
            padding > 0) {
          append(IRB.CreateCall(runtime.buildZeroBytes,
                                {ConstantInt::get(intPtrType, padding)}));
        }

        // Build the expression for the current element. If the struct is not a
        // constant, we need to read the element with extractvalue.
        auto element = constantStructValue
                           ? constantStructValue->getAggregateElement(i)
                           : IRB.CreateExtractValue(V, i);
        auto elementExpr = createValueExpression(element, IRB);

        // The expression may be of a different kind than bit vector; in this
        // case, we need to convert it.
        if (auto conversion =
                convertExprForTypeToBitVectorExpr(IRB, element, elementExpr)) {
          elementExpr = conversion->lastInstruction;
        }

        // If the element is represented in little-endian byte order in memory,
        // swap the bytes.
        auto elementType = structType->getElementType(i);
        if (isLittleEndian(elementType) &&
            dataLayout.getTypeStoreSize(elementType) > 1) {
          elementExpr = IRB.CreateCall(runtime.buildBswap, {elementExpr});
        }

        append(elementExpr);

        offset = structLayout->getElementOffset(i) +
                 dataLayout.getTypeStoreSize(structType->getElementType(i));
      }

      // Insert padding at the end, if any.
      if (auto finalPadding = dataLayout.getTypeStoreSize(structType) - offset;
          finalPadding > 0) {
        append(IRB.CreateCall(runtime.buildZeroBytes,
                              {ConstantInt::get(intPtrType, finalPadding)}));
      }

      return expr;
    }
  }

  llvm_unreachable("Unhandled type for constant expression");
}

bool Symbolizer::canUseValueAt(Value *value, Instruction *insertBefore,
                               DominatorTree &dominators) const {
  if (value == nullptr)
    return false;
  if (isa<Constant>(value) || isa<Argument>(value))
    return true;
  if (auto *instruction = dyn_cast<Instruction>(value))
    return dominators.dominates(instruction, insertBefore);
  return false;
}

bool Symbolizer::trySynthesizeRegionLoad(
    LoadInst &load, Instruction *insertBefore, BasicBlock *regionEntry,
    DominatorTree &dominators, RegionValue &result) {
  if (aliasAnalysis == nullptr || memorySSA == nullptr || !load.isSimple() ||
      !isSupportedMergedType(load.getType()) ||
      !canUseValueAt(load.getPointerOperand(), insertBefore, dominators) ||
      !isSafeToSpeculativelyExecute(
          &load, insertBefore, nullptr, &dominators))
    return false;

  auto *loadAccess = memorySSA->getMemoryAccess(&load);
  if (loadAccess == nullptr || !isa<MemoryUse>(loadAccess))
    return false;

  SmallPtrSet<BasicBlock *, kMaxVeritestingRegionBlocks> regionBlocks;
  BasicBlock *merge = insertBefore->getParent();
  if (!verifyAcyclicEasyRegion(regionEntry, merge, &regionBlocks) ||
      regionBlocks.count(load.getParent()) == 0)
    return false;

  MemoryLocation location = MemoryLocation::get(&load);
  for (BasicBlock *block : regionBlocks) {
    // Instructions before the controller terminator execute before every arm;
    // their effects are already visible to both the original and snapshot
    // loads. Only writes after the region forks can invalidate the snapshot.
    if (block == regionEntry)
      continue;
    for (Instruction &instruction : *block) {
      if (&instruction == &load ||
          originalInstructions.count(&instruction) == 0 ||
          !instruction.mayWriteToMemory())
        continue;
      if (memorySSA->getMemoryAccess(&instruction) == nullptr ||
          mayModifyLocationConservatively(
              *aliasAnalysis, instruction, location))
        return false;
    }
  }

  IRBuilder<> IRB(insertBefore);
  auto *concrete =
      IRB.CreateLoad(load.getType(), load.getPointerOperand(), "sym.region.ld");
  concrete->setAlignment(load.getAlign());
  auto *data = IRB.CreateCall(
      runtime.readMemory,
      {IRB.CreatePtrToInt(load.getPointerOperand(), intPtrType),
       ConstantInt::get(
           intPtrType, dataLayout.getTypeStoreSize(load.getType())),
       IRB.getInt1(isLittleEndian(load.getType()) ? 1 : 0)});
  Instruction *expression =
      convertBitVectorExprForType(IRB, data, load.getType());

  result.concreteValue = concrete;
  result.expressionValue = expression;
  result.computation = SymbolicComputation(data, expression, {});
  return true;
}

bool Symbolizer::trySynthesizeRegionValue(Value *value,
                                          Instruction *insertBefore,
                                          BasicBlock *regionEntry,
                                          DominatorTree &dominators,
                                          unsigned depth,
                                          RegionValue &result) {
  if (value == nullptr || depth > kMaxVeritestingRegionDepth ||
      !isSupportedMergedType(value->getType()) ||
      isa<UndefValue, PoisonValue>(value))
    return false;

  Value *expression = getSymbolicExpression(value);
  if (canUseValueAt(value, insertBefore, dominators)) {
    if (expression != nullptr &&
        !canUseValueAt(expression, insertBefore, dominators))
      return false;
    result.concreteValue = value;
    result.expressionValue = expression;
    return true;
  }

  auto *instruction = dyn_cast<Instruction>(value);
  if (instruction == nullptr || regionEntry == nullptr ||
      !dominators.dominates(regionEntry, instruction->getParent()) ||
      instruction->isTerminator())
    return false;

  if (auto *load = dyn_cast<LoadInst>(instruction))
    return trySynthesizeRegionLoad(
        *load, insertBefore, regionEntry, dominators, result);
  if (instruction->mayReadOrWriteMemory())
    return false;

  IRBuilder<> IRB(insertBefore);

  auto mergeIfAny = [](SymbolicComputation &target,
                       const RegionValue &source) {
    if (source.computation.firstInstruction != nullptr)
      target.merge(source.computation);
  };
  auto regionArg = [](const RegionValue &source) {
    return RegionRuntimeArg{
        source.concreteValue, source.expressionValue, true,
        source.computation.firstInstruction == nullptr ||
            source.expressionValue == nullptr};
  };
  auto hoistConcreteBeforeComputation =
      [](Value *concrete, const SymbolicComputation &computation) {
        auto *instruction = dyn_cast<Instruction>(concrete);
        Instruction *first = computation.firstInstruction;
        if (instruction == nullptr || first == nullptr ||
            instruction->getParent() != first->getParent())
          return;

        SmallPtrSet<Instruction *, 16> visited;
        std::function<void(Instruction *)> hoist =
            [&](Instruction *current) {
              if (!visited.insert(current).second)
                return;
              for (Value *operand : current->operand_values()) {
                auto *dependency = dyn_cast<Instruction>(operand);
                if (dependency != nullptr &&
                    dependency->getParent() == first->getParent() &&
                    first->comesBefore(dependency))
                  hoist(dependency);
              }
              if (first->comesBefore(current))
                current->moveBefore(first);
            };
        hoist(instruction);
      };

  if (auto *phi = dyn_cast<PHINode>(instruction)) {
    if (phi->getNumIncomingValues() != 2)
      return false;

    BasicBlock *left = phi->getIncomingBlock(0);
    BasicBlock *right = phi->getIncomingBlock(1);
    BasicBlock *controller =
        dominators.findNearestCommonDominator(left, right);
    BasicBlock *merge = phi->getParent();
    if (controller == nullptr || controller == merge ||
        !dominators.dominates(regionEntry, controller))
      return false;

    auto *branch = dyn_cast<BranchInst>(controller->getTerminator());
    if (branch == nullptr || !branch->isConditional() ||
        !verifyAcyclicEasyRegion(branch->getSuccessor(0), merge) ||
        !verifyAcyclicEasyRegion(branch->getSuccessor(1), merge))
      return false;

    bool trueSelectsLeft =
        dominators.dominates(branch->getSuccessor(0), left);
    bool falseSelectsLeft =
        dominators.dominates(branch->getSuccessor(1), left);
    bool trueSelectsRight =
        dominators.dominates(branch->getSuccessor(0), right);
    bool falseSelectsRight =
        dominators.dominates(branch->getSuccessor(1), right);

    Value *trueValue = nullptr;
    Value *falseValue = nullptr;
    if (trueSelectsLeft && !falseSelectsLeft && falseSelectsRight &&
        !trueSelectsRight) {
      trueValue = phi->getIncomingValue(0);
      falseValue = phi->getIncomingValue(1);
    } else if (trueSelectsRight && !falseSelectsRight && falseSelectsLeft &&
               !trueSelectsLeft) {
      trueValue = phi->getIncomingValue(1);
      falseValue = phi->getIncomingValue(0);
    } else {
      return false;
    }

    RegionValue condition;
    RegionValue trueArm;
    RegionValue falseArm;
    if (!trySynthesizeRegionValue(branch->getCondition(), insertBefore,
                                  regionEntry, dominators, depth + 1,
                                  condition) ||
        !trySynthesizeRegionValue(trueValue, insertBefore, controller,
                                  dominators, depth + 1, trueArm) ||
        !trySynthesizeRegionValue(falseValue, insertBefore, controller,
                                  dominators, depth + 1, falseArm))
      return false;

    Value *concrete =
        IRB.CreateSelect(condition.concreteValue, trueArm.concreteValue,
                         falseArm.concreteValue);
    SymbolicComputation computation;
    mergeIfAny(computation, condition);
    mergeIfAny(computation, trueArm);
    mergeIfAny(computation, falseArm);
    auto expr = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildIte,
        {regionArg(condition), regionArg(trueArm), regionArg(falseArm)});
    computation.merge(expr);
    hoistConcreteBeforeComputation(concrete, computation);

    result.concreteValue = concrete;
    result.expressionValue = expr.lastInstruction;
    result.computation = computation;
    return true;
  }

  if (auto *binary = dyn_cast<BinaryOperator>(instruction)) {
    if (!isSupportedRegionBinaryOpcode(binary->getOpcode()))
      return false;

    RegionValue left;
    RegionValue right;
    if (!trySynthesizeRegionValue(binary->getOperand(0), insertBefore,
                                  regionEntry, dominators, depth + 1, left) ||
        !trySynthesizeRegionValue(binary->getOperand(1), insertBefore,
                                  regionEntry, dominators, depth + 1, right))
      return false;

    Value *concrete =
        IRB.CreateBinOp(binary->getOpcode(), left.concreteValue,
                        right.concreteValue);
    SymFnT handler = runtime.binaryOperatorHandlers.at(binary->getOpcode());
    if (binary->getOperand(0)->getType()->isIntegerTy(1)) {
      switch (binary->getOpcode()) {
      case Instruction::And:
        handler = runtime.buildBoolAnd;
        break;
      case Instruction::Or:
        handler = runtime.buildBoolOr;
        break;
      case Instruction::Xor:
        handler = runtime.buildBoolXor;
        break;
      default:
        return false;
      }
    }

    SymbolicComputation computation;
    mergeIfAny(computation, left);
    mergeIfAny(computation, right);
    auto expr = forceBuildRuntimeCallWithExpressions(
        IRB, handler,
        {regionArg(left), regionArg(right)});
    computation.merge(expr);
    hoistConcreteBeforeComputation(concrete, computation);

    result.concreteValue = concrete;
    result.expressionValue = expr.lastInstruction;
    result.computation = computation;
    return true;
  }

  if (auto *unary = dyn_cast<UnaryOperator>(instruction)) {
    if (unary->getOpcode() != Instruction::FNeg)
      return false;

    RegionValue operand;
    if (!trySynthesizeRegionValue(unary->getOperand(0), insertBefore,
                                  regionEntry, dominators, depth + 1, operand))
      return false;

    Value *concrete = IRB.CreateFNeg(operand.concreteValue);
    auto expr = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.unaryOperatorHandlers.at(unary->getOpcode()),
        {regionArg(operand)});

    SymbolicComputation computation;
    mergeIfAny(computation, operand);
    computation.merge(expr);
    hoistConcreteBeforeComputation(concrete, computation);

    result.concreteValue = concrete;
    result.expressionValue = expr.lastInstruction;
    result.computation = computation;
    return true;
  }

  if (auto *comparison = dyn_cast<CmpInst>(instruction)) {
    RegionValue left;
    RegionValue right;
    if (!trySynthesizeRegionValue(comparison->getOperand(0), insertBefore,
                                  regionEntry, dominators, depth + 1, left) ||
        !trySynthesizeRegionValue(comparison->getOperand(1), insertBefore,
                                  regionEntry, dominators, depth + 1, right))
      return false;

    Value *concrete = IRB.CreateCmp(comparison->getPredicate(),
                                    left.concreteValue, right.concreteValue);
    auto expr = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.comparisonHandlers.at(comparison->getPredicate()),
        {regionArg(left), regionArg(right)});

    SymbolicComputation computation;
    mergeIfAny(computation, left);
    mergeIfAny(computation, right);
    computation.merge(expr);
    hoistConcreteBeforeComputation(concrete, computation);

    result.concreteValue = concrete;
    result.expressionValue = expr.lastInstruction;
    result.computation = computation;
    return true;
  }

  if (auto *cast = dyn_cast<CastInst>(instruction)) {
    if (!isSupportedRegionCastOpcode(cast->getOpcode()))
      return false;

    RegionValue operand;
    if (!trySynthesizeRegionValue(cast->getOperand(0), insertBefore,
                                  regionEntry, dominators, depth + 1, operand))
      return false;

    Value *concrete =
        IRB.CreateCast(cast->getOpcode(), operand.concreteValue,
                       cast->getDestTy());
    SymbolicComputation computation;
    mergeIfAny(computation, operand);
    Value *resultExpression = operand.expressionValue;

    auto mergeExpr = [&](SymbolicComputation expr) {
      computation.merge(expr);
      resultExpression = expr.lastInstruction;
    };

    switch (cast->getOpcode()) {
    case Instruction::SExt:
    case Instruction::ZExt: {
      SymFnT target =
          cast->getOpcode() == Instruction::SExt ? runtime.buildSExt
                                                 : runtime.buildZExt;
      auto *sourceType = cast->getSrcTy();
      auto *destType = cast->getDestTy();
      if (!sourceType->isIntegerTy() || !destType->isIntegerTy())
        return false;
      if (sourceType->getIntegerBitWidth() == 1) {
        auto bit = forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildBoolToBit, {regionArg(operand)});
        computation.merge(bit);
        auto ext = forceBuildRuntimeCallWithExpressions(
            IRB, target,
            {{bit.lastInstruction, nullptr, false},
             {IRB.getInt8(destType->getIntegerBitWidth() - 1), nullptr,
              false}});
        mergeExpr(ext);
      } else {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, target,
            {regionArg(operand),
             {IRB.getInt8(destType->getIntegerBitWidth() -
                          sourceType->getIntegerBitWidth()),
              nullptr, false}}));
      }
      break;
    }
    case Instruction::Trunc: {
      auto *destType = cast->getDestTy();
      if (!destType->isIntegerTy())
        return false;
      auto trunc = forceBuildRuntimeCallWithExpressions(
          IRB, runtime.buildTrunc,
          {regionArg(operand),
           {IRB.getInt8(destType->getIntegerBitWidth()), nullptr, false}});
      computation.merge(trunc);
      resultExpression = trunc.lastInstruction;
      if (destType->getIntegerBitWidth() == 1)
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildBitToBool,
            {{trunc.lastInstruction, nullptr, false}}));
      break;
    }
    case Instruction::BitCast:
      if (cast->getSrcTy()->isIntegerTy() &&
          cast->getDestTy()->isFloatingPointTy()) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildBitsToFloat,
            {regionArg(operand),
             {IRB.getInt1(cast->getDestTy()->isDoubleTy()), nullptr, false}}));
      } else if (cast->getSrcTy()->isFloatingPointTy() &&
                 cast->getDestTy()->isIntegerTy()) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildFloatToBits,
            {regionArg(operand)}));
      } else if (!(cast->getSrcTy()->isPointerTy() &&
                   cast->getDestTy()->isPointerTy())) {
        return false;
      }
      break;
    case Instruction::PtrToInt: {
      auto destinationBits = cast->getDestTy()->getIntegerBitWidth();
      if (destinationBits < ptrBits) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildTrunc,
            {regionArg(operand),
             {IRB.getInt8(destinationBits), nullptr, false}}));
      } else if (destinationBits > ptrBits) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildZExt,
            {regionArg(operand),
             {IRB.getInt8(destinationBits - ptrBits), nullptr, false}}));
      }
      break;
    }
    case Instruction::IntToPtr: {
      auto sourceBits = cast->getSrcTy()->getIntegerBitWidth();
      if (sourceBits < ptrBits) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildZExt,
            {regionArg(operand),
             {IRB.getInt8(ptrBits - sourceBits), nullptr, false}}));
      } else if (sourceBits > ptrBits) {
        mergeExpr(forceBuildRuntimeCallWithExpressions(
            IRB, runtime.buildTrunc,
            {regionArg(operand),
             {IRB.getInt8(ptrBits), nullptr, false}}));
      }
      break;
    }
    case Instruction::SIToFP:
    case Instruction::UIToFP:
      mergeExpr(forceBuildRuntimeCallWithExpressions(
          IRB, runtime.buildIntToFloat,
          {regionArg(operand),
           {IRB.getInt1(cast->getDestTy()->isDoubleTy()), nullptr, false},
           {IRB.getInt1(cast->getOpcode() == Instruction::SIToFP), nullptr,
            false}}));
      break;
    case Instruction::FPExt:
    case Instruction::FPTrunc:
      mergeExpr(forceBuildRuntimeCallWithExpressions(
          IRB, runtime.buildFloatToFloat,
          {regionArg(operand),
           {IRB.getInt1(cast->getDestTy()->isDoubleTy()), nullptr, false}}));
      break;
    case Instruction::FPToSI:
    case Instruction::FPToUI:
      mergeExpr(forceBuildRuntimeCallWithExpressions(
          IRB,
          cast->getOpcode() == Instruction::FPToSI
              ? runtime.buildFloatToSignedInt
              : runtime.buildFloatToUnsignedInt,
          {regionArg(operand),
           {IRB.getInt8(cast->getDestTy()->getIntegerBitWidth()), nullptr,
            false}}));
      break;
    default:
      return false;
    }

    hoistConcreteBeforeComputation(concrete, computation);
    result.concreteValue = concrete;
    result.expressionValue = resultExpression;
    result.computation = computation;
    return true;
  }

  if (auto *select = dyn_cast<SelectInst>(instruction)) {
    RegionValue condition;
    RegionValue trueArm;
    RegionValue falseArm;
    if (!trySynthesizeRegionValue(select->getCondition(), insertBefore,
                                  regionEntry, dominators, depth + 1,
                                  condition) ||
        !trySynthesizeRegionValue(select->getTrueValue(), insertBefore,
                                  regionEntry, dominators, depth + 1,
                                  trueArm) ||
        !trySynthesizeRegionValue(select->getFalseValue(), insertBefore,
                                  regionEntry, dominators, depth + 1,
                                  falseArm))
      return false;

    Value *concrete =
        IRB.CreateSelect(condition.concreteValue, trueArm.concreteValue,
                         falseArm.concreteValue);
    SymbolicComputation computation;
    mergeIfAny(computation, condition);
    mergeIfAny(computation, trueArm);
    mergeIfAny(computation, falseArm);
    auto expr = forceBuildRuntimeCallWithExpressions(
        IRB, runtime.buildIte,
        {regionArg(condition), regionArg(trueArm), regionArg(falseArm)});
    computation.merge(expr);
    hoistConcreteBeforeComputation(concrete, computation);

    result.concreteValue = concrete;
    result.expressionValue = expr.lastInstruction;
    result.computation = computation;
    return true;
  }

  if (auto *freeze = dyn_cast<FreezeInst>(instruction)) {
    RegionValue operand;
    if (!trySynthesizeRegionValue(freeze->getOperand(0), insertBefore,
                                  regionEntry, dominators, depth + 1, operand))
      return false;
    result.concreteValue = IRB.CreateFreeze(operand.concreteValue);
    result.expressionValue = operand.expressionValue;
    result.computation = operand.computation;
    hoistConcreteBeforeComputation(
        result.concreteValue, result.computation);
    return true;
  }

  return false;
}

Symbolizer::SymbolicComputation
Symbolizer::forceBuildRuntimeCallWithExpressions(
    IRBuilder<> &IRB, SymFnT function, ArrayRef<RegionRuntimeArg> args) const {
  auto *nullExpression =
      ConstantPointerNull::get(IRB.getInt8Ty()->getPointerTo());

  std::vector<Value *> functionArgs;
  functionArgs.reserve(args.size());
  for (const auto &arg : args) {
    functionArgs.push_back(arg.symbolic ? (arg.expressionValue != nullptr
                                               ? arg.expressionValue
                                               : nullExpression)
                                        : arg.concreteValue);
  }

  auto *call = IRB.CreateCall(function, functionArgs);

  std::vector<Input> inputs;
  for (unsigned i = 0; i < args.size(); i++) {
    if (args[i].symbolic && args[i].trackInput)
      inputs.push_back(Input(args[i].concreteValue, i, call));
  }

  return SymbolicComputation(call, call, inputs);
}

Symbolizer::SymbolicComputation Symbolizer::forceBuildRuntimeCall(
    IRBuilder<> &IRB, SymFnT function,
    ArrayRef<std::pair<Value *, bool>> args) const {
  std::vector<Value *> functionArgs;
  for (const auto &[arg, symbolic] : args) {
    functionArgs.push_back(symbolic ? getSymbolicExpressionOrNull(arg) : arg);
  }
  auto *call = IRB.CreateCall(function, functionArgs);

  std::vector<Input> inputs;
  for (unsigned i = 0; i < args.size(); i++) {
    const auto &[arg, symbolic] = args[i];
    if (symbolic)
      inputs.push_back(Input(arg, i, call));
  }

  return SymbolicComputation(call, call, inputs);
}

void Symbolizer::tryAlternative(IRBuilder<> &IRB, Value *V) {
  auto *destExpr = getSymbolicExpression(V);
  if (destExpr != nullptr) {
    auto *concreteDestExpr = createValueExpression(V, IRB);
    auto *destAssertion =
        IRB.CreateCall(runtime.comparisonHandlers[CmpInst::ICMP_EQ],
                       {destExpr, concreteDestExpr});
    auto *pushAssertion = IRB.CreateCall(
        runtime.pushPathConstraint,
        {destAssertion, IRB.getInt1(true), getTargetPreferredInt(V)});
    registerSymbolicComputation(SymbolicComputation(
        concreteDestExpr, pushAssertion, {Input(V, 0, destAssertion)}));
  }
}

uint64_t Symbolizer::aggregateMemberOffset(Type *aggregateType,
                                           ArrayRef<unsigned> indices) const {
  uint64_t offset = 0;
  auto *indexedType = aggregateType;
  for (auto index : indices) {
    // All indices in an extractvalue instruction are constant:
    // https://llvm.org/docs/LangRef.html#extractvalue-instruction

    if (auto *structType = dyn_cast<StructType>(indexedType)) {
      offset += dataLayout.getStructLayout(structType)->getElementOffset(index);
      indexedType = structType->getElementType(index);
    } else {
      auto *arrayType = cast<ArrayType>(indexedType);
      unsigned elementSize =
          dataLayout.getTypeAllocSize(arrayType->getArrayElementType());
      offset += elementSize * index;
      indexedType = arrayType->getArrayElementType();
    }
  }

  return offset;
}

Instruction *Symbolizer::convertBitVectorExprForType(llvm::IRBuilder<> &IRB,
                                                     Instruction *I,
                                                     Type *T) const {
  Instruction *result = I;

  if (T->isFloatingPointTy()) {
    result = IRB.CreateCall(runtime.buildBitsToFloat,
                            {I, IRB.getInt1(T->isDoubleTy())});
  } else if (T->isIntegerTy() && T->getIntegerBitWidth() == 1) {
    result = IRB.CreateCall(runtime.buildTrunc,
                            {I, ConstantInt::get(IRB.getInt8Ty(), 1)});
    result = IRB.CreateCall(runtime.buildBitToBool, {result});
  }

  return result;
}

std::optional<Symbolizer::SymbolicComputation>
Symbolizer::convertExprForTypeToBitVectorExpr(IRBuilder<> &IRB, Value *V,
                                              Value *Expr) const {
  if (Expr == nullptr)
    return {};

  auto T = V->getType();

  if (T->isFloatingPointTy()) {
    auto floatBits = IRB.CreateCall(runtime.buildFloatToBits, {Expr});
    return SymbolicComputation(floatBits, floatBits, {Input(V, 0, floatBits)});
  } else if (T->isIntegerTy() && T->getIntegerBitWidth() == 1) {
    auto bitExpr = IRB.CreateCall(runtime.buildBoolToBit, {Expr});
    auto bitVectorExpr = IRB.CreateCall(runtime.buildZExt,
                                        {bitExpr, IRB.getInt8(7 /* 1 byte */)});
    return SymbolicComputation(bitExpr, bitVectorExpr, {Input(V, 0, bitExpr)});
  } else {
    return {};
  }
}
