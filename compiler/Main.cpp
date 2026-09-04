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

#include <llvm/ADT/StringRef.h>
#include <llvm/IR/LegacyPassManager.h>
#if LLVM_VERSION_MAJOR <= 15
#include <llvm/Transforms/IPO/PassManagerBuilder.h>
#endif
#include <llvm/Transforms/Scalar.h>
#include <llvm/Transforms/Scalar/Scalarizer.h>

#if LLVM_VERSION_MAJOR >= 13
#include <llvm/Passes/PassBuilder.h>
#include <llvm/Passes/PassPlugin.h>

#if LLVM_VERSION_MAJOR >= 14
#include <llvm/Passes/OptimizationLevel.h>
#else
using OptimizationLevel = llvm::PassBuilder::OptimizationLevel;
#endif
#endif

#if LLVM_VERSION_MAJOR >= 15
#include <llvm/Transforms/Scalar/LowerAtomicPass.h>
#else
#include <llvm/Transforms/Scalar/LowerAtomic.h>
#endif

#include "Pass.h"
#include "ContinuationLowering.h"
#include "HydraTransformation.h"
#include "IFSSContinuationLowering.h"
#include "IFSSContinuationMemory.h"
#include "IFSSExitLowering.h"
#include "IFSSLoopSummary.h"
#include "IFSSSwitchLowering.h"

#include <cstdlib>

using namespace llvm;

namespace {

bool scheduleOnlyEnabled() {
  const char *raw = std::getenv("SYMCC_DPOR_SCHEDULE_ONLY");
  if (raw == nullptr || *raw == '\0')
    return false;
  StringRef value(raw);
  return !value.equals_insensitive("0") &&
         !value.equals_insensitive("false") &&
         !value.equals_insensitive("off") &&
         !value.equals_insensitive("no");
}

} // namespace

//
// Legacy pass registration (up to LLVM 13)
//

#if LLVM_VERSION_MAJOR <= 15

void addSymbolizeLegacyPass(const PassManagerBuilder & /* unused */,
                            legacy::PassManagerBase &PM) {
  PM.add(new symcc::IFSSSwitchLoweringLegacyPass());
  PM.add(new symcc::IFSSLoopSummaryLegacyPass());
  PM.add(new symcc::IFSSExitLoweringLegacyPass());
  PM.add(new symcc::IFSSContinuationLoweringLegacyPass());
  PM.add(new symcc::IFSSContinuationMemoryLegacyPass());
  PM.add(new symcc::HydraTransformationLegacyPass());
  PM.add(new symcc::LiveContinuationExportLegacyPass());
  PM.add(createScalarizerPass());
  if (!scheduleOnlyEnabled())
    PM.add(createLowerAtomicPass());
  PM.add(new SymbolizeLegacyPass());
}

// Make the pass known to opt.
static RegisterPass<SymbolizeLegacyPass> X("symbolize", "Symbolization Pass");
static RegisterPass<symcc::LiveContinuationExportLegacyPass>
    LiveContinuationExportX("live-continuation-export",
                            "LLVM to SymCC live continuation IR");
static RegisterPass<symcc::HydraTransformationLegacyPass>
    HydraTransformationX("hydra-transform",
                         "Hydra targeted control-flow transformation");
static RegisterPass<symcc::IFSSExitLoweringLegacyPass>
    IFSSExitLoweringX("ifss-exit-lowering",
                      "Bounded IFSS return-exit state lowering");
static RegisterPass<symcc::IFSSSwitchLoweringLegacyPass>
    IFSSSwitchLoweringX("ifss-switch-lowering",
                        "Bounded IFSS switch-chain lowering");
static RegisterPass<symcc::IFSSLoopSummaryLegacyPass>
    IFSSLoopSummaryX("ifss-loop-summary",
                     "Bounded affine IFSS loop summary");
static RegisterPass<symcc::IFSSContinuationLoweringLegacyPass>
    IFSSContinuationLoweringX(
        "ifss-continuation-lowering",
        "Bounded IFSS continuation tuple lowering");
static RegisterPass<symcc::IFSSContinuationMemoryLegacyPass>
    IFSSContinuationMemoryX(
        "ifss-continuation-memory",
        "MemorySSA-proven IFSS continuation memory tuple");
// Tell frontends to run the pass automatically.
static struct RegisterStandardPasses Y(PassManagerBuilder::EP_VectorizerStart,
                                       addSymbolizeLegacyPass);
static struct RegisterStandardPasses
    Z(PassManagerBuilder::EP_EnabledOnOptLevel0, addSymbolizeLegacyPass);

#endif

//
// New pass registration (LLVM 13 and above)
//

#if LLVM_VERSION_MAJOR >= 13

PassPluginLibraryInfo getSymbolizePluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "Symbolization Pass", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            // We need to act on the entire module as well as on each function.
            // Those actions are independent from each other, so we register a
            // module pass at the start of the pipeline and a function pass just
            // before the vectorizer. (There doesn't seem to be a way to run
            // module passes at the start of the vectorizer, hence the split.)
            PB.registerPipelineStartEPCallback(
                [](ModulePassManager &PM, OptimizationLevel) {
                  PM.addPass(symcc::IFSSSwitchLoweringPass());
                  PM.addPass(symcc::IFSSLoopSummaryPass());
                  PM.addPass(symcc::IFSSExitLoweringPass());
                  PM.addPass(symcc::IFSSContinuationLoweringPass());
                  PM.addPass(createModuleToFunctionPassAdaptor(
                      symcc::IFSSContinuationMemoryPass()));
                  PM.addPass(symcc::HydraTransformationPass());
                  PM.addPass(symcc::LiveContinuationExportPass());
                  PM.addPass(SymbolizePass());
                });
            PB.registerPipelineParsingCallback(
                [](StringRef name, ModulePassManager &PM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (name == "ifss-switch-lowering") {
                    PM.addPass(symcc::IFSSSwitchLoweringPass());
                    return true;
                  }
                  if (name == "ifss-loop-summary") {
                    PM.addPass(symcc::IFSSLoopSummaryPass());
                    return true;
                  }
                  if (name == "ifss-exit-lowering") {
                    PM.addPass(symcc::IFSSExitLoweringPass());
                    return true;
                  }
                  if (name == "ifss-continuation-lowering") {
                    PM.addPass(symcc::IFSSContinuationLoweringPass());
                    return true;
                  }
                  if (name == "ifss-continuation-memory") {
                    PM.addPass(createModuleToFunctionPassAdaptor(
                        symcc::IFSSContinuationMemoryPass()));
                    return true;
                  }
                  if (name == "hydra-transform") {
                    PM.addPass(symcc::HydraTransformationPass());
                    return true;
                  }
                  if (name != "live-continuation-export")
                    return false;
                  PM.addPass(symcc::LiveContinuationExportPass());
                  return true;
                });
            PB.registerPipelineParsingCallback(
                [](StringRef name, FunctionPassManager &PM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (name != "ifss-continuation-memory")
                    return false;
                  PM.addPass(symcc::IFSSContinuationMemoryPass());
                  return true;
                });
            PB.registerVectorizerStartEPCallback(
                [](FunctionPassManager &PM, OptimizationLevel) {
                  PM.addPass(ScalarizerPass());
                  if (!scheduleOnlyEnabled())
                    PM.addPass(LowerAtomicPass());
                  PM.addPass(SymbolizePass());
                });
          }};
}

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return getSymbolizePluginInfo();
}

#endif
