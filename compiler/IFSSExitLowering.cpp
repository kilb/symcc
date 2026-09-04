// This file is part of SymCC.
//
// Lower a strictly bounded multi-return region to one return-state PHI before
// symbolic instrumentation. The ordinary IFSS machinery can then reconstruct
// an ITE that remains visible to callers.

#include "IFSSExitLowering.h"

#include "SiteId.h"

#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>

#include <cstdlib>
#include <functional>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr unsigned kMaxExitArms = 8;
constexpr unsigned kMaxExitRegionBlocks = 32;
constexpr unsigned kMaxExitRegionPaths = 64;
constexpr char kExitSchema[] = "bounded-return-exit-state-v1";

struct ReturnExitRegion {
  BranchInst *controller = nullptr;
  SmallVector<ReturnInst *, kMaxExitArms> returns;
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

Metadata *integerMetadata(LLVMContext &context, unsigned bits, uint64_t value) {
  return ConstantAsMetadata::get(
      ConstantInt::get(IntegerType::get(context, bits), value));
}

bool hasMustTailCall(const Function &function) {
  for (const Instruction &instruction : instructions(function))
    if (const auto *call = dyn_cast<CallInst>(&instruction))
      if (call->isMustTailCall())
        return true;
  return false;
}

bool isSupportedReturnType(Type *type) {
  return type->isIntegerTy() || type->isFloatingPointTy() ||
         type->isPointerTy();
}

bool collectReturnExitRegion(Function &function, ReturnExitRegion &result) {
  if (function.isDeclaration() || function.empty() ||
      !isSupportedReturnType(function.getReturnType()) ||
      hasMustTailCall(function))
    return false;

  for (Instruction &instruction : instructions(function)) {
    auto *returnInstruction = dyn_cast<ReturnInst>(&instruction);
    if (returnInstruction == nullptr)
      continue;
    Value *value = returnInstruction->getReturnValue();
    if (value == nullptr || isa<UndefValue, PoisonValue>(value) ||
        returnInstruction->getMetadata("symcc.ifss_exit") != nullptr)
      return false;
    result.returns.push_back(returnInstruction);
    if (result.returns.size() > kMaxExitArms)
      return false;
  }
  if (result.returns.size() < 2)
    return false;
  Value *firstReturnValue = result.returns.front()->getReturnValue();
  bool hasDistinctReturnState = false;
  for (ReturnInst *returnInstruction :
       ArrayRef<ReturnInst *>(result.returns).drop_front())
    hasDistinctReturnState |=
        returnInstruction->getReturnValue() != firstReturnValue;
  if (!hasDistinctReturnState)
    return false;

  DominatorTree dominators(function);
  BasicBlock *controller = result.returns.front()->getParent();
  for (ReturnInst *returnInstruction :
       ArrayRef<ReturnInst *>(result.returns).drop_front()) {
    controller = dominators.findNearestCommonDominator(
        controller, returnInstruction->getParent());
    if (controller == nullptr)
      return false;
  }
  auto *rootBranch = dyn_cast<BranchInst>(controller->getTerminator());
  if (rootBranch == nullptr || !rootBranch->isConditional() ||
      rootBranch->getSuccessor(0) == rootBranch->getSuccessor(1))
    return false;

  SmallPtrSet<BasicBlock *, kMaxExitArms> returnBlocks;
  for (ReturnInst *returnInstruction : result.returns)
    if (!returnBlocks.insert(returnInstruction->getParent()).second)
      return false;

  SmallPtrSet<BasicBlock *, kMaxExitRegionBlocks> regionBlocks;
  SmallPtrSet<BasicBlock *, kMaxExitRegionBlocks> visiting;
  SmallPtrSet<BasicBlock *, kMaxExitArms> reachedReturns;
  unsigned pathCount = 0;
  std::function<bool(BasicBlock *)> enumerate = [&](BasicBlock *block) {
    if (block == nullptr || !dominators.dominates(controller, block) ||
        block->hasAddressTaken() || block->isEHPad() ||
        !visiting.insert(block).second)
      return false;

    if (block != controller) {
      regionBlocks.insert(block);
      if (regionBlocks.size() > kMaxExitRegionBlocks) {
        visiting.erase(block);
        return false;
      }
    }

    if (returnBlocks.count(block) != 0) {
      reachedReturns.insert(block);
      bool valid = ++pathCount <= kMaxExitRegionPaths;
      visiting.erase(block);
      return valid;
    }

    auto *branch = dyn_cast<BranchInst>(block->getTerminator());
    if (branch == nullptr) {
      visiting.erase(block);
      return false;
    }
    for (unsigned index = 0; index < branch->getNumSuccessors(); ++index)
      if (!enumerate(branch->getSuccessor(index))) {
        visiting.erase(block);
        return false;
      }
    visiting.erase(block);
    return true;
  };

  if (!enumerate(controller) ||
      reachedReturns.size() != result.returns.size())
    return false;

  result.controller = rootBranch;
  result.blocks = regionBlocks.size();
  result.paths = pathCount;
  return true;
}

void attachArmMetadata(Instruction &instruction, unsigned ordinal,
                       uint64_t returnSite) {
  LLVMContext &context = instruction.getContext();
  Metadata *operands[] = {
      MDString::get(context, kExitSchema),
      integerMetadata(context, 32, ordinal),
      integerMetadata(context, 64, returnSite),
  };
  instruction.setMetadata("symcc.ifss_exit_arm",
                          MDNode::get(context, operands));
}

bool lowerReturnExitRegion(ReturnExitRegion &region) {
  Function &function = *region.controller->getFunction();
  LLVMContext &context = function.getContext();
  BasicBlock *dispatch =
      BasicBlock::Create(context, "ifss.exit.dispatch", &function);
  PHINode *returnState = PHINode::Create(
      function.getReturnType(), region.returns.size(), "ifss.exit.state",
      dispatch);

  SmallVector<uint64_t, kMaxExitArms> returnSites;
  returnSites.reserve(region.returns.size());
  for (unsigned ordinal = 0; ordinal < region.returns.size(); ++ordinal) {
    ReturnInst *returnInstruction = region.returns[ordinal];
    BasicBlock *source = returnInstruction->getParent();
    uint64_t returnSite = stableSiteId(*returnInstruction);
    returnSites.push_back(returnSite);
    returnState->addIncoming(returnInstruction->getReturnValue(), source);

    auto *branch = BranchInst::Create(dispatch, returnInstruction);
    branch->setDebugLoc(returnInstruction->getDebugLoc());
    attachArmMetadata(*branch, ordinal, returnSite);
    returnInstruction->eraseFromParent();
  }

  auto *unifiedReturn = ReturnInst::Create(context, returnState, dispatch);
  Metadata *returnMetadata[] = {
      MDString::get(context, kExitSchema),
      integerMetadata(context, 64, stableSiteId(*region.controller)),
      integerMetadata(context, 32, region.returns.size()),
      integerMetadata(context, 32, region.blocks),
      integerMetadata(context, 32, region.paths),
  };
  unifiedReturn->setMetadata("symcc.ifss_exit",
                             MDNode::get(context, returnMetadata));

  SmallVector<Metadata *, 24> stateMetadata;
  stateMetadata.push_back(MDString::get(context, kExitSchema));
  stateMetadata.push_back(
      integerMetadata(context, 64, stableSiteId(*region.controller)));
  stateMetadata.push_back(
      integerMetadata(context, 32, region.returns.size()));
  stateMetadata.push_back(integerMetadata(context, 32, region.blocks));
  stateMetadata.push_back(integerMetadata(context, 32, region.paths));
  for (unsigned ordinal = 0; ordinal < returnSites.size(); ++ordinal) {
    stateMetadata.push_back(integerMetadata(context, 32, ordinal));
    stateMetadata.push_back(integerMetadata(context, 64, returnSites[ordinal]));
  }
  returnState->setMetadata("symcc.ifss_exit",
                           MDNode::get(context, stateMetadata));
  return true;
}

} // namespace

bool lowerIFSSReturnExits(Module &module) {
  if (!enabled(std::getenv("SYMCC_IFSS_EXIT_STATE")))
    return false;

  std::vector<ReturnExitRegion> regions;
  for (Function &function : module) {
    ReturnExitRegion region;
    if (collectReturnExitRegion(function, region))
      regions.push_back(std::move(region));
  }
  if (regions.empty())
    return false;

  // Freeze every original identity before the first CFG rewrite so metadata
  // for later functions cannot depend on which earlier regions were accepted.
  initializeStableSiteIds(module);
  for (ReturnExitRegion &region : regions)
    lowerReturnExitRegion(region);
  return true;
}

char IFSSExitLoweringLegacyPass::ID = 0;

bool IFSSExitLoweringLegacyPass::runOnModule(Module &module) {
  return lowerIFSSReturnExits(module);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses IFSSExitLoweringPass::run(
    Module &module, ModuleAnalysisManager &) {
  return lowerIFSSReturnExits(module) ? PreservedAnalyses::none()
                                     : PreservedAnalyses::all();
}
#endif

} // namespace symcc
