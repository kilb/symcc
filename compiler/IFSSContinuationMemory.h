// This file is part of SymCC.

#ifndef SYMCC_IFSS_CONTINUATION_MEMORY_H
#define SYMCC_IFSS_CONTINUATION_MEMORY_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Function;
}

namespace symcc {

class IFSSContinuationMemoryLegacyPass : public llvm::FunctionPass {
public:
  static char ID;

  IFSSContinuationMemoryLegacyPass() : FunctionPass(ID) {}
  bool runOnFunction(llvm::Function &function) override;
  void getAnalysisUsage(llvm::AnalysisUsage &usage) const override;
};

#if LLVM_VERSION_MAJOR >= 13
class IFSSContinuationMemoryPass
    : public llvm::PassInfoMixin<IFSSContinuationMemoryPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &function,
                              llvm::FunctionAnalysisManager &analyses);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif
