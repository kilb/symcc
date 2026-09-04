// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.

#ifndef HYDRA_TRANSFORMATION_H
#define HYDRA_TRANSFORMATION_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Module;
}

namespace symcc {

bool transformHydra(llvm::Module &module);

class HydraTransformationLegacyPass : public llvm::ModulePass {
public:
  static char ID;

  HydraTransformationLegacyPass() : ModulePass(ID) {}
  bool runOnModule(llvm::Module &module) override;
};

#if LLVM_VERSION_MAJOR >= 13
class HydraTransformationPass
    : public llvm::PassInfoMixin<HydraTransformationPass> {
public:
  llvm::PreservedAnalyses run(llvm::Module &module,
                              llvm::ModuleAnalysisManager &);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif
