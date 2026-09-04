// This file is part of SymCC.

#ifndef SYMCC_IFSS_EXIT_LOWERING_H
#define SYMCC_IFSS_EXIT_LOWERING_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Module;
}

namespace symcc {

bool lowerIFSSReturnExits(llvm::Module &module);

class IFSSExitLoweringLegacyPass : public llvm::ModulePass {
public:
  static char ID;

  IFSSExitLoweringLegacyPass() : ModulePass(ID) {}
  bool runOnModule(llvm::Module &module) override;
};

#if LLVM_VERSION_MAJOR >= 13
class IFSSExitLoweringPass
    : public llvm::PassInfoMixin<IFSSExitLoweringPass> {
public:
  llvm::PreservedAnalyses run(llvm::Module &module,
                              llvm::ModuleAnalysisManager &);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif
