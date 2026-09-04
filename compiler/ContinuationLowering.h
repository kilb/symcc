// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.

#ifndef CONTINUATION_LOWERING_H
#define CONTINUATION_LOWERING_H

#include <llvm/IR/PassManager.h>
#include <llvm/Pass.h>

namespace llvm {
class Module;
}

namespace symcc {

bool exportLiveContinuation(llvm::Module &M);

class LiveContinuationExportLegacyPass : public llvm::ModulePass {
public:
  static char ID;

  LiveContinuationExportLegacyPass() : ModulePass(ID) {}
  bool runOnModule(llvm::Module &M) override;
};

#if LLVM_VERSION_MAJOR >= 13
class LiveContinuationExportPass
    : public llvm::PassInfoMixin<LiveContinuationExportPass> {
public:
  llvm::PreservedAnalyses run(llvm::Module &M,
                              llvm::ModuleAnalysisManager &);
  static bool isRequired() { return true; }
};
#endif

} // namespace symcc

#endif
