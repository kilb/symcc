// This file is part of SymCC.

#ifndef SYMCC_IFSS_CONTINUATION_LOWERING_H
#define SYMCC_IFSS_CONTINUATION_LOWERING_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Module;
}

namespace symcc {

bool lowerIFSSContinuations(llvm::Module &module);

class IFSSContinuationLoweringLegacyPass : public llvm::ModulePass {
public:
  static char ID;

  IFSSContinuationLoweringLegacyPass() : ModulePass(ID) {}
  bool runOnModule(llvm::Module &module) override;
};

#if LLVM_VERSION_MAJOR >= 13
class IFSSContinuationLoweringPass
    : public llvm::PassInfoMixin<IFSSContinuationLoweringPass> {
public:
  llvm::PreservedAnalyses run(llvm::Module &module,
                              llvm::ModuleAnalysisManager &);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif
