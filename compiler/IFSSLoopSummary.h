// This file is part of SymCC.

#ifndef SYMCC_IFSS_LOOP_SUMMARY_H
#define SYMCC_IFSS_LOOP_SUMMARY_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Module;
}

namespace symcc {

bool summarizeIFSSLoops(llvm::Module &module);

class IFSSLoopSummaryLegacyPass : public llvm::ModulePass {
public:
  static char ID;

  IFSSLoopSummaryLegacyPass() : ModulePass(ID) {}
  bool runOnModule(llvm::Module &module) override;
};

#if LLVM_VERSION_MAJOR >= 13
class IFSSLoopSummaryPass
    : public llvm::PassInfoMixin<IFSSLoopSummaryPass> {
public:
  llvm::PreservedAnalyses run(llvm::Module &module,
                              llvm::ModuleAnalysisManager &);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif

