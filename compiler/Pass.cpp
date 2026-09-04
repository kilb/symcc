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

#include "Pass.h"

#include <llvm/ADT/SmallString.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/Analysis/AliasAnalysis.h>
#include <llvm/Analysis/MemorySSA.h>
#include <llvm/CodeGen/IntrinsicLowering.h>
#include <llvm/CodeGen/TargetLowering.h>
#include <llvm/CodeGen/TargetSubtargetInfo.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Operator.h>
#include <llvm/IR/Verifier.h>
#include <llvm/Target/TargetMachine.h>
#include <llvm/Target/TargetOptions.h>
#include <llvm/Transforms/Utils/ModuleUtils.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <limits>
#include <queue>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#if LLVM_VERSION_MAJOR < 14
#include <llvm/Support/TargetRegistry.h>
#else
#include <llvm/MC/TargetRegistry.h>
#endif

#include "Runtime.h"
#include "SiteId.h"
#include "Symbolizer.h"
#include "UCSan.h"

using namespace llvm;

#ifndef NDEBUG
#define DEBUG(X)                                                               \
  do {                                                                         \
    X;                                                                         \
  } while (false)
#else
#define DEBUG(X) ((void)0)
#endif

char SymbolizeLegacyPass::ID = 0;

namespace {

static constexpr char kSymCtorName[] = "__sym_ctor";
static constexpr unsigned kStaticDependencyLimit = 128;
static constexpr char kScheduleAtomicMetadata[] =
    "symcc.schedule.atomic.instrumented";

std::string sanitizeSummaryField(std::string value);

struct StaticInputRegion {
  Value *base = nullptr;
  int64_t memoryOffset = 0;
  int64_t inputOffset = 0;
  uint64_t size = 0;
  bool bounded = false;
};

using StaticDependency = std::pair<int64_t, int64_t>;

bool addStaticDependency(SmallVectorImpl<StaticDependency> &target,
                         int64_t lower, int64_t upper) {
  if (lower < 0 || upper < lower || target.size() >= kStaticDependencyLimit)
    return false;
  for (const auto &existing : target)
    if (existing.first == lower && existing.second == upper)
      return false;
  target.emplace_back(lower, upper);
  return true;
}

bool mergeStaticDependencies(SmallVectorImpl<StaticDependency> &target,
                             ArrayRef<StaticDependency> source) {
  bool changed = false;
  for (const auto &interval : source)
    changed |= addStaticDependency(target, interval.first, interval.second);
  return changed;
}

std::pair<Value *, std::optional<int64_t>>
decomposeStaticPointer(Value *pointer, const DataLayout &DL) {
  __int128 offset = 0;
  Value *current = pointer;
  while (current != nullptr) {
    if (auto *gep = dyn_cast<GEPOperator>(current)) {
      APInt constantOffset(
          DL.getPointerSizeInBits(gep->getPointerAddressSpace()), 0, true);
      if (!gep->accumulateConstantOffset(DL, constantOffset) ||
          !constantOffset.isSignedIntN(64))
        return {gep->getPointerOperand()->stripPointerCasts(), std::nullopt};
      offset += static_cast<__int128>(constantOffset.getSExtValue());
      if (offset < std::numeric_limits<int64_t>::min() ||
          offset > std::numeric_limits<int64_t>::max())
        return {gep->getPointerOperand()->stripPointerCasts(), std::nullopt};
      current = gep->getPointerOperand();
      continue;
    }
    Value *stripped = current->stripPointerCasts();
    if (stripped == current)
      break;
    current = stripped;
  }
  return {current, static_cast<int64_t>(offset)};
}

std::optional<uint64_t> constantSize(Value *value) {
  auto *constant = dyn_cast_or_null<ConstantInt>(value);
  if (constant == nullptr || constant->isNegative() ||
      constant->getValue().getActiveBits() > 64)
    return std::nullopt;
  return constant->getZExtValue();
}

void appendPointerDependencies(
    Value *pointer, std::optional<uint64_t> accessSize,
    const DataLayout &DL, ArrayRef<StaticInputRegion> regions,
    SmallVectorImpl<StaticDependency> &dependencies) {
  auto [base, pointerOffset] = decomposeStaticPointer(pointer, DL);
  for (const StaticInputRegion &region : regions) {
    if (base != region.base)
      continue;
    if (!pointerOffset || !region.bounded) {
      __int128 inputUpper = static_cast<__int128>(region.inputOffset) +
                            static_cast<__int128>(region.size) - 1;
      if (region.bounded && region.size != 0 &&
          inputUpper <= std::numeric_limits<int64_t>::max())
        addStaticDependency(
            dependencies, region.inputOffset,
            static_cast<int64_t>(inputUpper));
      continue;
    }
    __int128 accessLower = *pointerOffset;
    uint64_t width = accessSize.value_or(region.size);
    if (width == 0 || region.size == 0)
      continue;
    __int128 accessUpper =
        accessLower + static_cast<__int128>(width) - 1;
    __int128 regionLower = region.memoryOffset;
    __int128 regionUpper =
        regionLower + static_cast<__int128>(region.size) - 1;
    __int128 overlapLower = std::max(accessLower, regionLower);
    __int128 overlapUpper = std::min(accessUpper, regionUpper);
    if (overlapLower <= overlapUpper) {
      __int128 inputLower = static_cast<__int128>(region.inputOffset) +
                            overlapLower - regionLower;
      __int128 inputUpper = static_cast<__int128>(region.inputOffset) +
                            overlapUpper - regionLower;
      if (inputLower < 0 ||
          inputUpper > std::numeric_limits<int64_t>::max())
        continue;
      addStaticDependency(
          dependencies, static_cast<int64_t>(inputLower),
          static_cast<int64_t>(inputUpper));
    }
  }
}

void emitStaticInputDependencies(Module &M) {
  const char *outputPath = std::getenv("SYMCC_STATIC_DEPENDENCE_OUT");
  if (outputPath == nullptr || *outputPath == '\0')
    return;
  const DataLayout &DL = M.getDataLayout();
  std::ofstream output(outputPath, std::ios::app);
  if (!output)
    return;
  output << "# symcc-static-input-dependence-v1 module="
         << M.getModuleIdentifier() << "\n";

  for (Function &function : M) {
    if (function.isDeclaration())
      continue;
    SmallVector<StaticInputRegion, 8> regions;
    for (Instruction &instruction : instructions(function)) {
      auto *call = dyn_cast<CallBase>(&instruction);
      Function *callee = call == nullptr ? nullptr : call->getCalledFunction();
      if (callee == nullptr)
        continue;
      StringRef name = callee->getName();
      Value *buffer = nullptr;
      std::optional<uint64_t> size;
      int64_t inputOffset = 0;
      if ((name == "read" || name == "recv" || name == "recvfrom") &&
          call->arg_size() >= 3) {
        buffer = call->getArgOperand(1);
        size = constantSize(call->getArgOperand(2));
      } else if (name == "pread" && call->arg_size() >= 4) {
        buffer = call->getArgOperand(1);
        size = constantSize(call->getArgOperand(2));
        if (auto offset = constantSize(call->getArgOperand(3)))
          inputOffset = static_cast<int64_t>(
              std::min<uint64_t>(*offset, INT64_MAX));
      } else if (name == "fread" && call->arg_size() >= 3) {
        buffer = call->getArgOperand(0);
        auto element = constantSize(call->getArgOperand(1));
        auto count = constantSize(call->getArgOperand(2));
        if (element && count &&
            (*count == 0 || *element <= UINT64_MAX / *count))
          size = *element * *count;
      } else if ((name == "fgets" || name == "gets") &&
                 call->arg_size() >= 1) {
        buffer = call->getArgOperand(0);
        if (name == "fgets" && call->arg_size() >= 2)
          size = constantSize(call->getArgOperand(1));
      } else if (name == "_sym_make_symbolic" && call->arg_size() >= 3) {
        buffer = call->getArgOperand(0);
        size = constantSize(call->getArgOperand(1));
        if (auto offset = constantSize(call->getArgOperand(2)))
          inputOffset = static_cast<int64_t>(
              std::min<uint64_t>(*offset, INT64_MAX));
      }
      if (buffer == nullptr)
        continue;
      auto [base, memoryOffset] = decomposeStaticPointer(buffer, DL);
      regions.push_back({
          base,
          memoryOffset.value_or(0),
          inputOffset,
          size.value_or(0),
          memoryOffset.has_value() && size.has_value(),
      });
    }
    if (regions.empty())
      continue;

    DenseMap<Value *, SmallVector<StaticDependency, 4>> dependencies;
    for (unsigned round = 0; round < 8; ++round) {
      bool changed = false;
      for (Instruction &instruction : instructions(function)) {
        SmallVector<StaticDependency, 8> inferred;
        for (Value *operand : instruction.operands()) {
          auto found = dependencies.find(operand);
          if (found != dependencies.end())
            mergeStaticDependencies(inferred, found->second);
        }
        if (auto *load = dyn_cast<LoadInst>(&instruction)) {
#if LLVM_VERSION_MAJOR >= 11
          uint64_t width =
              DL.getTypeStoreSize(load->getType()).getFixedValue();
#else
          uint64_t width = DL.getTypeStoreSize(load->getType());
#endif
          appendPointerDependencies(
              load->getPointerOperand(), width, DL, regions, inferred);
        } else if (auto *call = dyn_cast<CallBase>(&instruction)) {
          Function *callee = call->getCalledFunction();
          StringRef name = callee == nullptr ? StringRef() : callee->getName();
          bool inputSource =
              name == "read" || name == "pread" || name == "fread" ||
              name == "recv" || name == "recvfrom" || name == "fgets" ||
              name == "gets" || name == "_sym_make_symbolic";
          if (!inputSource)
            for (Value *argument : call->args())
              if (argument->getType()->isPointerTy())
                appendPointerDependencies(
                    argument, std::nullopt, DL, regions, inferred);
        }
        changed |= mergeStaticDependencies(
            dependencies[&instruction], inferred);
      }
      if (!changed)
        break;
    }

    for (Instruction &instruction : instructions(function)) {
      Value *condition = nullptr;
      if (auto *branch = dyn_cast<BranchInst>(&instruction)) {
        if (branch->isConditional())
          condition = branch->getCondition();
      } else if (auto *switchInstruction = dyn_cast<SwitchInst>(&instruction)) {
        condition = switchInstruction->getCondition();
      } else if (auto *select = dyn_cast<SelectInst>(&instruction)) {
        condition = select->getCondition();
      }
      auto found = dependencies.find(condition);
      if (condition == nullptr || found == dependencies.end())
        continue;
      for (const auto &interval : found->second)
        output << symcc::stableSiteId(instruction) << " "
               << interval.first << " " << interval.second << " "
               << sanitizeSummaryField(function.getName().str()) << "\n";
    }
  }
}

std::vector<std::string> splitTargetList(const char *raw) {
  std::vector<std::string> result;
  if (raw == nullptr)
    return result;
  std::stringstream stream(raw);
  std::string item;
  while (std::getline(stream, item, ',')) {
    size_t start = 0;
    while (start < item.size() &&
           std::isspace(static_cast<unsigned char>(item[start])))
      start++;
    size_t end = item.size();
    while (end > start && std::isspace(static_cast<unsigned char>(item[end - 1])))
      end--;
    if (start < end)
      result.push_back(item.substr(start, end - start));
  }
  return result;
}

bool parseUnsigned(StringRef text, uint64_t &value) {
  if (text.empty())
    return false;
  auto startsWith = [](StringRef value, StringRef prefix) {
    return value.size() >= prefix.size() &&
           value.substr(0, prefix.size()) == prefix;
  };
  int base = startsWith(text, "0x") || startsWith(text, "0X") ? 16 : 10;
  return !text.getAsInteger(base, value);
}

std::string basename(StringRef path) {
  size_t slash = path.find_last_of("/\\");
  if (slash == StringRef::npos)
    return path.str();
  return path.substr(slash + 1).str();
}

bool sourceMatches(StringRef debugFile, StringRef requestedFile) {
  auto endsWith = [](StringRef value, StringRef suffix) {
    return value.size() >= suffix.size() &&
           value.substr(value.size() - suffix.size()) == suffix;
  };
  if (requestedFile.empty())
    return true;
  if (debugFile == requestedFile)
    return true;
  if (basename(debugFile) == requestedFile)
    return true;
  return endsWith(debugFile, requestedFile);
}

bool instructionMatchesSourceLine(const Instruction &I, StringRef file,
                                  unsigned line) {
  const DebugLoc &Loc = I.getDebugLoc();
  if (!Loc)
    return false;
  return Loc.getLine() == line && sourceMatches(Loc->getFilename(), file);
}

bool isDistanceSiteInstruction(const Instruction &I) {
  if (const auto *BI = dyn_cast<BranchInst>(&I))
    return BI->isConditional();
  return isa<SwitchInst>(I) || isa<SelectInst>(I);
}

bool envFlag(const char *name, bool defaultValue = false) {
  const char *raw = std::getenv(name);
  if (raw == nullptr)
    return defaultValue;
  StringRef value(raw);
  return value != "0" && value != "false" && value != "FALSE";
}

uint8_t atomicOrderCode(AtomicOrdering ordering) {
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
  default:
    return 0;
  }
}

uint64_t fixedTypeStoreBytes(const DataLayout &layout, Type *type) {
#if LLVM_VERSION_MAJOR >= 11
  return layout.getTypeStoreSize(type).getFixedValue();
#else
  return layout.getTypeStoreSize(type);
#endif
}

bool normalizeAtomicTraceValue(IRBuilder<> &builder, const DataLayout &layout,
                               Value *source, Value *&normalized,
                               uint8_t &bits) {
  Type *type = source->getType();
  unsigned width = 0;
  if (auto *integer = dyn_cast<IntegerType>(type)) {
    width = integer->getBitWidth();
    if (width == 0 || width > 64)
      return false;
    normalized = builder.CreateZExtOrTrunc(source, builder.getInt64Ty());
  } else if (auto *pointer = dyn_cast<PointerType>(type)) {
    width = layout.getPointerSizeInBits(pointer->getAddressSpace());
    if (width == 0 || width > 64)
      return false;
    normalized = builder.CreatePtrToInt(source, builder.getInt64Ty());
  } else {
    return false;
  }
  bits = static_cast<uint8_t>(width);
  return true;
}

void instrumentAtomicScheduleEvents(Module &M) {
  if (!envFlag("SYMCC_DPOR_MEMORY") &&
      !envFlag("SYMCC_DPOR_ATOMIC_ONLY"))
    return;
  Runtime runtime(M);
  const DataLayout &layout = M.getDataLayout();
  Type *intPtrType = layout.getIntPtrType(M.getContext());
  SmallVector<Instruction *, 32> atomics;
  for (Function &function : M)
    for (Instruction &instruction : instructions(function))
      if (isa<AtomicRMWInst>(instruction) ||
          isa<AtomicCmpXchgInst>(instruction) ||
          isa<FenceInst>(instruction) ||
          (isa<LoadInst>(instruction) &&
           cast<LoadInst>(instruction).isAtomic()) ||
          (isa<StoreInst>(instruction) &&
           cast<StoreInst>(instruction).isAtomic()))
        atomics.push_back(&instruction);

  for (Instruction *instruction : atomics) {
    IRBuilder<> before(instruction);
    Type *bytePtrType = before.getInt8Ty()->getPointerTo();
    Value *address = nullptr;
    uint64_t byteWidth = 0;
    uint8_t kind = 0;
    uint8_t successOrder = 0;
    uint8_t failureOrder = 0;
    uint8_t operation = 0;
    if (auto *load = dyn_cast<LoadInst>(instruction)) {
      address = before.CreatePointerCast(
          load->getPointerOperand(), bytePtrType);
      byteWidth = fixedTypeStoreBytes(layout, load->getType());
      successOrder = atomicOrderCode(load->getOrdering());
    } else if (auto *store = dyn_cast<StoreInst>(instruction)) {
      kind = 1;
      address = before.CreatePointerCast(
          store->getPointerOperand(), bytePtrType);
      byteWidth = fixedTypeStoreBytes(
          layout, store->getValueOperand()->getType());
      successOrder = atomicOrderCode(store->getOrdering());
    } else if (auto *rmw = dyn_cast<AtomicRMWInst>(instruction)) {
      kind = 2;
      address = before.CreatePointerCast(
          rmw->getPointerOperand(), bytePtrType);
      byteWidth = fixedTypeStoreBytes(
          layout, rmw->getValOperand()->getType());
      successOrder = atomicOrderCode(rmw->getOrdering());
      operation = static_cast<uint8_t>(rmw->getOperation());
    } else if (auto *compare =
                   dyn_cast<AtomicCmpXchgInst>(instruction)) {
      kind = 3;
      address = before.CreatePointerCast(
          compare->getPointerOperand(), bytePtrType);
      byteWidth = fixedTypeStoreBytes(
          layout, compare->getCompareOperand()->getType());
      successOrder = atomicOrderCode(compare->getSuccessOrdering());
      failureOrder = atomicOrderCode(compare->getFailureOrdering());
    } else {
      kind = 4;
      address = before.CreateIntToPtr(
          ConstantInt::get(
              intPtrType, symcc::stableSiteId(*instruction)),
          bytePtrType);
      successOrder = atomicOrderCode(
          cast<FenceInst>(instruction)->getOrdering());
    }
    Value *group = before.CreateCall(
        runtime.notifyScheduleAtomic,
        {
            address,
            ConstantInt::get(intPtrType, byteWidth),
            before.getInt8(kind),
            before.getInt8(successOrder),
            before.getInt8(failureOrder),
            before.getInt8(operation),
        });
    auto emitValue = [&](IRBuilder<> &builder, Value *value, uint8_t role) {
      Value *normalized = nullptr;
      uint8_t bits = 0;
      if (normalizeAtomicTraceValue(
              builder, layout, value, normalized, bits))
        builder.CreateCall(
            runtime.notifyScheduleAtomicValue,
            {group, address, normalized, builder.getInt8(bits),
             builder.getInt8(role)});
    };
    if (auto *rmw = dyn_cast<AtomicRMWInst>(instruction)) {
      emitValue(before, rmw->getValOperand(), 2);
    } else if (auto *compare = dyn_cast<AtomicCmpXchgInst>(instruction)) {
      emitValue(before, compare->getCompareOperand(), 3);
      emitValue(before, compare->getNewValOperand(), 4);
    }
    IRBuilder<> after(instruction->getNextNode());
    if (kind == 1) {
      emitValue(
          after, cast<StoreInst>(instruction)->getValueOperand(), 1);
    } else if (kind == 0 || kind == 2 || kind == 3) {
      Value *readValue = instruction;
      if (kind == 3)
        readValue = after.CreateExtractValue(instruction, 0);
      emitValue(after, readValue, 0);
      if (kind == 3) {
        Value *success = after.CreateExtractValue(instruction, 1);
        after.CreateCall(
            runtime.notifyScheduleAtomicResult,
            {group, address, success});
      }
    }
    after.CreateCall(
        runtime.notifyScheduleAtomicCommit, {group, address});
    instruction->setMetadata(
        kScheduleAtomicMetadata, MDNode::get(M.getContext(), {}));
  }
}

unsigned envUnsigned(const char *name, unsigned defaultValue) {
  const char *raw = std::getenv(name);
  uint64_t parsed = 0;
  if (raw == nullptr || !parseUnsigned(StringRef(raw), parsed) ||
      parsed > std::numeric_limits<unsigned>::max())
    return defaultValue;
  return static_cast<unsigned>(parsed);
}

bool callTypesCompatible(const CallBase &CB, const Function &Candidate) {
  FunctionType *CallType = CB.getFunctionType();
  FunctionType *TargetType = Candidate.getFunctionType();
  if (CallType->getReturnType() != TargetType->getReturnType())
    return false;
  if (!TargetType->isVarArg() &&
      CallType->getNumParams() != TargetType->getNumParams())
    return false;
  if (TargetType->isVarArg() &&
      CallType->getNumParams() < TargetType->getNumParams())
    return false;
  unsigned sharedParams = std::min(
      CallType->getNumParams(), TargetType->getNumParams());
  for (unsigned i = 0; i < sharedParams; ++i)
    if (CallType->getParamType(i) != TargetType->getParamType(i))
      return false;
  return true;
}

bool targetMatchesInstruction(const Instruction &I,
                              ArrayRef<std::string> targets) {
  for (const std::string &target : targets) {
    StringRef spec(target);
    uint64_t numeric = 0;
    if (parseUnsigned(spec, numeric) &&
        symcc::stableSiteId(I) == numeric)
      return true;

    size_t colon = spec.rfind(':');
    if (colon != StringRef::npos && colon + 1 < spec.size()) {
      uint64_t line = 0;
      if (parseUnsigned(spec.substr(colon + 1), line) && line > 0 &&
          line <= std::numeric_limits<unsigned>::max() &&
          instructionMatchesSourceLine(I, spec.substr(0, colon),
                                       static_cast<unsigned>(line)))
        return true;
    }
  }
  return false;
}

bool targetMatchesFunction(const Function &F, ArrayRef<std::string> targets) {
  for (const std::string &target : targets) {
    StringRef spec(target);
    uint64_t ignored = 0;
    if (parseUnsigned(spec, ignored))
      continue;
    if (spec.rfind(':') != StringRef::npos)
      continue;
    if (F.getName() == spec)
      return true;
  }
  return false;
}

std::string instructionLocation(const Instruction &I) {
  const DebugLoc &Loc = I.getDebugLoc();
  if (!Loc)
    return "-";
  std::string loc;
  raw_string_ostream out(loc);
  out << Loc->getFilename() << ":" << Loc.getLine();
  return out.str();
}

std::string sanitizeSummaryField(std::string value) {
  for (char &ch : value) {
    if (std::isspace(static_cast<unsigned char>(ch)))
      ch = '_';
  }
  return value;
}

std::string functionTypeSignature(FunctionType *Type) {
  std::string text;
  raw_string_ostream out(text);
  Type->print(out);
  out.flush();
  return sanitizeSummaryField(text);
}

bool stringRefStartsWith(StringRef value, StringRef prefix) {
  return value.size() >= prefix.size() &&
         value.substr(0, prefix.size()) == prefix;
}

std::string normalizedCalleeName(StringRef name) {
  std::string value = name.str();
  const char *prefixes[] = {
      "__interceptor_",
      "__wrap_",
      "symcc_",
  };
  bool changed = true;
  while (changed) {
    changed = false;
    for (StringRef prefix : prefixes) {
      if (stringRefStartsWith(StringRef(value), prefix)) {
        value = value.substr(prefix.size());
        changed = true;
      }
    }
  }
  return value;
}

bool concurrencyInstructionKind(const Instruction &I, std::string &kind,
                                std::string &detail) {
  if (const auto *RMW = dyn_cast<AtomicRMWInst>(&I)) {
    kind = "atomicrmw";
    detail = RMW->getOperationName(RMW->getOperation()).str();
    return true;
  }
  if (isa<AtomicCmpXchgInst>(&I)) {
    kind = "cmpxchg";
    detail = "atomic";
    return true;
  }
  if (isa<FenceInst>(&I)) {
    kind = "fence";
    detail = "atomic";
    return true;
  }
  if (const auto *LI = dyn_cast<LoadInst>(&I)) {
    if (LI->isAtomic()) {
      kind = "atomic_load";
      detail = "atomic";
      return true;
    }
  }
  if (const auto *SI = dyn_cast<StoreInst>(&I)) {
    if (SI->isAtomic()) {
      kind = "atomic_store";
      detail = "atomic";
      return true;
    }
  }
  const auto *CB = dyn_cast<CallBase>(&I);
  if (CB == nullptr)
    return false;
  const Function *Callee = CB->getCalledFunction();
  if (Callee == nullptr || Callee->getName().empty())
    return false;
  std::string normalized = normalizedCalleeName(Callee->getName());
  StringRef name(normalized);
  detail = sanitizeSummaryField(normalized);
  if (name == "pthread_create" || name == "thrd_create") {
    kind = "thread_create";
    return true;
  }
  if (name == "pthread_join" || name == "pthread_detach" ||
      name == "thrd_join" || name == "thrd_detach") {
    kind = "thread_lifecycle";
    return true;
  }
  if (stringRefStartsWith(name, "pthread_mutex_") ||
      stringRefStartsWith(name, "pthread_rwlock_") ||
      stringRefStartsWith(name, "mtx_")) {
    kind = "lock";
    return true;
  }
  if (stringRefStartsWith(name, "pthread_cond_") ||
      stringRefStartsWith(name, "cnd_")) {
    kind = "condition";
    return true;
  }
  if (stringRefStartsWith(name, "pthread_barrier_") ||
      stringRefStartsWith(name, "sem_")) {
    kind = "sync";
    return true;
  }
  return false;
}

std::string moduleFragmentId(const Module &M) {
  return std::to_string(symcc::stableModuleId(M));
}

std::string blockSummaryId(const Module &M, const BasicBlock *BB) {
  (void)M;
  return std::to_string(symcc::stableSiteId(*BB));
}

void emitDirectedDistanceMap(Module &M) {
  const char *colorationPath = std::getenv("SYMCC_COLORATION_OUT");
  const char *concurrencyPath = std::getenv("SYMCC_CONCURRENCY_OUT");
  const char *taskGraphPath = std::getenv("SYMCC_TASK_GRAPH_OUT");
  std::vector<std::string> targets =
      splitTargetList(std::getenv("SYMCC_COLOR_TARGETS"));
  const bool emitColoration = colorationPath != nullptr && !targets.empty();
  const bool emitConcurrency = concurrencyPath != nullptr;
  const bool emitTaskGraph = taskGraphPath != nullptr;
  if (!emitColoration && !emitConcurrency && !emitTaskGraph)
    return;

  std::vector<BasicBlock *> blocks;
  std::unordered_map<BasicBlock *, std::vector<BasicBlock *>> graph;
  std::unordered_map<BasicBlock *, std::vector<BasicBlock *>> reverseGraph;
  std::unordered_set<BasicBlock *> targetBlocks;
  std::unordered_set<BasicBlock *> concurrencyBlocks;
  std::unordered_map<Function *, std::vector<BasicBlock *>> callers;
  std::vector<Function *> addressTakenFunctions;
  std::vector<std::pair<BasicBlock *, std::string>> externalDirectCalls;
  std::vector<std::pair<BasicBlock *, std::string>> externalIndirectCalls;
  struct ConcurrencySite {
    const Instruction *instruction;
    BasicBlock *block;
    std::string kind;
    std::string detail;
  };
  std::vector<ConcurrencySite> concurrencySites;
  const bool includeIndirectCalls = envFlag("SYMCC_COLOR_INDIRECT", true);
  const unsigned indirectLimit = envUnsigned("SYMCC_COLOR_INDIRECT_LIMIT", 64);

  for (Function &F : M) {
    if (F.isDeclaration())
      continue;
    if (includeIndirectCalls && !F.empty() && F.hasAddressTaken())
      addressTakenFunctions.push_back(&F);
    for (BasicBlock &BB : F) {
      blocks.push_back(&BB);
      graph[&BB];
      reverseGraph[&BB];
      if (targetMatchesFunction(F, targets))
        targetBlocks.insert(&BB);
      for (Instruction &I : BB) {
        if (targetMatchesInstruction(I, targets))
          targetBlocks.insert(&BB);
        std::string concurrencyKind;
        std::string concurrencyDetail;
        if (concurrencyInstructionKind(I, concurrencyKind, concurrencyDetail)) {
          concurrencyBlocks.insert(&BB);
          concurrencySites.push_back(
              {&I, &BB, concurrencyKind, concurrencyDetail});
        }
        if (auto *CB = dyn_cast<CallBase>(&I)) {
          Function *Callee = CB->getCalledFunction();
          if (Callee != nullptr) {
            if (!Callee->isDeclaration() && !Callee->empty())
              callers[Callee].push_back(&BB);
            else if (!Callee->getName().empty())
              externalDirectCalls.push_back(
                  {&BB, sanitizeSummaryField(Callee->getName().str())});
          } else {
            externalIndirectCalls.push_back(
                {&BB, functionTypeSignature(CB->getFunctionType())});
          }
        }
      }
    }
  }

  auto addEdge = [&](BasicBlock *From, BasicBlock *To) {
    if (From == nullptr || To == nullptr)
      return;
    graph[From].push_back(To);
    reverseGraph[To].push_back(From);
  };

  for (BasicBlock *BB : blocks) {
    for (BasicBlock *Succ : successors(BB))
      addEdge(BB, Succ);
    for (Instruction &I : *BB) {
      auto *CB = dyn_cast<CallBase>(&I);
      if (CB == nullptr)
        continue;
      Function *Callee = CB->getCalledFunction();
      if (Callee != nullptr && !Callee->isDeclaration() && !Callee->empty()) {
        addEdge(BB, &Callee->getEntryBlock());
        continue;
      }
      if (!includeIndirectCalls || indirectLimit == 0)
        continue;
      unsigned matched = 0;
      for (Function *Candidate : addressTakenFunctions) {
        if (!callTypesCompatible(*CB, *Candidate))
          continue;
        addEdge(BB, &Candidate->getEntryBlock());
        callers[Candidate].push_back(BB);
        if (++matched >= indirectLimit)
          break;
      }
    }
  }

  for (auto &Item : callers) {
    Function *Callee = Item.first;
    for (BasicBlock &BB : *Callee) {
      if (BB.getTerminator()->getNumSuccessors() != 0)
        continue;
      for (BasicBlock *Caller : Item.second)
        addEdge(&BB, Caller);
    }
  }

  auto computeDistance = [&](const std::unordered_set<BasicBlock *> &targets) {
    std::unordered_map<BasicBlock *, unsigned> distance;
    std::queue<BasicBlock *> queue;
    for (BasicBlock *Target : targets) {
      distance[Target] = 0;
      queue.push(Target);
    }
    while (!queue.empty()) {
      BasicBlock *Current = queue.front();
      queue.pop();
      unsigned nextDistance = distance[Current] + 1;
      for (BasicBlock *Pred : reverseGraph[Current]) {
        auto found = distance.find(Pred);
        if (found != distance.end() && found->second <= nextDistance)
          continue;
        distance[Pred] = nextDistance;
        queue.push(Pred);
      }
    }
    return distance;
  };
  auto colorDistance = computeDistance(targetBlocks);
  auto concurrencyDistance = computeDistance(concurrencyBlocks);

  auto writeSummary = [&](const char *path, bool includeDistances,
                          StringRef kind) {
    std::ofstream out(path, std::ios::app);
    if (!out)
      return;
    out << "# " << kind.str() << " module=" << M.getModuleIdentifier();
    if (includeDistances) {
      out << " targets=";
      for (size_t i = 0; i < targets.size(); ++i) {
        if (i != 0)
          out << ",";
        out << targets[i];
      }
    }
    out << "\n";
    out << "#M " << moduleFragmentId(M) << " "
        << sanitizeSummaryField(M.getModuleIdentifier()) << "\n";
    for (BasicBlock *BB : blocks) {
      Function *F = BB->getParent();
      out << "#N " << blockSummaryId(M, BB) << " "
          << sanitizeSummaryField(F->getName().str()) << "\n";
      if (BB == &F->getEntryBlock()) {
        out << "#ENTRY " << sanitizeSummaryField(F->getName().str()) << " "
            << functionTypeSignature(F->getFunctionType()) << " "
            << blockSummaryId(M, BB) << "\n";
        if (F->hasAddressTaken())
          out << "#ADDR " << sanitizeSummaryField(F->getName().str()) << " "
              << functionTypeSignature(F->getFunctionType()) << " "
              << blockSummaryId(M, BB) << "\n";
      }
      if (BB->getTerminator()->getNumSuccessors() == 0)
        out << "#EXIT " << sanitizeSummaryField(F->getName().str()) << " "
            << blockSummaryId(M, BB) << "\n";
      if (targetBlocks.find(BB) != targetBlocks.end())
        out << "#TARGET " << blockSummaryId(M, BB) << "\n";
      for (Instruction &I : *BB) {
        if (!isDistanceSiteInstruction(I))
          continue;
        out << "#SITE " << symcc::stableSiteId(I) << " "
            << blockSummaryId(M, BB) << " "
            << sanitizeSummaryField(F->getName().str()) << " "
            << I.getOpcodeName() << " " << instructionLocation(I) << "\n";
        if (auto *BI = dyn_cast<BranchInst>(&I)) {
          if (BI->isConditional())
            out << "#BRANCH " << symcc::stableSiteId(I) << " "
                << blockSummaryId(M, BB) << " "
                << blockSummaryId(M, BI->getSuccessor(0)) << " "
                << blockSummaryId(M, BI->getSuccessor(1)) << "\n";
        } else if (auto *SI = dyn_cast<SwitchInst>(&I)) {
          out << "#SWITCH " << symcc::stableSiteId(I) << " "
              << blockSummaryId(M, BB) << " "
              << blockSummaryId(M, SI->getDefaultDest()) << " "
              << SI->getNumCases();
          for (const auto &Case : SI->cases()) {
            SmallString<32> caseValue;
            Case.getCaseValue()->getValue().toString(
                caseValue, 10, true);
            out << " " << caseValue.c_str() << ":"
                << blockSummaryId(M, Case.getCaseSuccessor());
          }
          out << "\n";
        }
      }
    }
    for (const auto &Item : graph) {
      for (BasicBlock *Succ : Item.second)
        out << "#E " << blockSummaryId(M, Item.first) << " "
            << blockSummaryId(M, Succ) << "\n";
    }
    for (const auto &Call : externalDirectCalls)
      out << "#X " << blockSummaryId(M, Call.first) << " " << Call.second
          << "\n";
    for (const auto &Call : externalIndirectCalls)
      out << "#IX " << blockSummaryId(M, Call.first) << " " << Call.second
          << "\n";

    if (!includeDistances)
      return;
    for (BasicBlock *BB : blocks) {
      auto found = colorDistance.find(BB);
      if (found == colorDistance.end())
        continue;
      for (Instruction &I : *BB) {
        if (!isDistanceSiteInstruction(I))
          continue;
        out << symcc::stableSiteId(I) << " " << found->second
            << " # " << I.getFunction()->getName().str() << " "
            << I.getOpcodeName() << " " << instructionLocation(I) << "\n";
      }
    }
  };

  auto writeConcurrencySummary = [&]() {
    if (!emitConcurrency)
      return;
    std::ofstream out(concurrencyPath, std::ios::app);
    if (!out)
      return;
    out << "# symcc-concurrency-guidance-v1 module="
        << M.getModuleIdentifier() << "\n";
    out << "#M " << moduleFragmentId(M) << " "
        << sanitizeSummaryField(M.getModuleIdentifier()) << "\n";
    for (const ConcurrencySite &Site : concurrencySites) {
      const Function *F = Site.instruction->getFunction();
      out << "#CONC " << symcc::stableSiteId(*Site.instruction) << " "
          << Site.kind << " " << Site.detail << " "
          << sanitizeSummaryField(F->getName().str()) << " "
          << instructionLocation(*Site.instruction) << " "
          << blockSummaryId(M, Site.block) << "\n";
    }
    for (BasicBlock *BB : blocks) {
      auto found = concurrencyDistance.find(BB);
      if (found == concurrencyDistance.end())
        continue;
      for (Instruction &I : *BB) {
        if (!isDistanceSiteInstruction(I))
          continue;
        out << symcc::stableSiteId(I) << " " << found->second
            << " # concurrency " << I.getFunction()->getName().str() << " "
            << I.getOpcodeName() << " " << instructionLocation(I) << "\n";
      }
    }
  };

  if (emitColoration && emitTaskGraph &&
      std::string(colorationPath) == std::string(taskGraphPath)) {
    writeSummary(colorationPath, true, "symcc-directed-distance-v2");
  } else {
    if (emitColoration)
      writeSummary(colorationPath, true, "symcc-directed-distance-v2");
    if (emitTaskGraph)
      writeSummary(taskGraphPath, false, "symcc-structural-task-graph-v1");
  }
  writeConcurrencySummary();
}

bool instrumentModule(Module &M) {
  DEBUG(errs() << "Symbolizer module instrumentation\n");

  symcc::initializeStableSiteIds(M);
  emitStaticInputDependencies(M);

  // Under-constrained execution must run before regular symbolic
  // instrumentation: the latter should observe real pointers only at the
  // immediate memory operations inserted by UCSan.
  instrumentUCSan(M);
  instrumentAtomicScheduleEvents(M);

  // Native schedule replay needs the stable atomic probes without the
  // symbolic shadow-memory runtime.  Keeping this as a module-level mode also
  // prevents libc wrapper redirection and runtime construction below.
  if (envFlag("SYMCC_DPOR_SCHEDULE_ONLY"))
    return true;

  // Emit the shared interprocedural graph used by ColorGo-style static
  // coloration and DynamiQ-style structural task allocation.
  emitDirectedDistanceMap(M);

  // Redirect calls to external functions to the corresponding wrappers and
  // rename internal functions.
  for (auto &function : M.functions()) {
    auto name = function.getName();
    if (isInterceptedFunction(function))
      function.setName(name + "_symbolized");
  }

  // Insert a constructor that initializes the runtime and any globals.
  Function *ctor;
  std::tie(ctor, std::ignore) = createSanitizerCtorAndInitFunctions(
      M, kSymCtorName, "_sym_initialize", {}, {});
  {
    IRBuilder<> IRB(ctor->getEntryBlock().getTerminator());
    Type *bytePointer = IRB.getInt8Ty()->getPointerTo();
    Type *intPtrType = M.getDataLayout().getIntPtrType(M.getContext());
    unsigned intPtrBits = cast<IntegerType>(intPtrType)->getBitWidth();
    FunctionCallee registerRegion = M.getOrInsertFunction(
        "_sym_register_data_region", IRB.getVoidTy(), bytePointer,
        intPtrType, IRB.getInt64Ty());
    for (GlobalVariable &global : M.globals()) {
      if (global.isDeclaration() || !global.hasInitializer() ||
          !global.isConstant() || global.isThreadLocal() ||
          global.getAddressSpace() != 0)
        continue;
#if LLVM_VERSION_MAJOR >= 11
      uint64_t byteLength = M.getDataLayout()
                                .getTypeAllocSize(global.getValueType())
                                .getFixedValue();
#else
      uint64_t byteLength =
          M.getDataLayout().getTypeAllocSize(global.getValueType());
#endif
      if (byteLength == 0 ||
          (intPtrBits < 64 &&
           byteLength >= (uint64_t{1} << intPtrBits)))
        continue;
      uint64_t objectId = symcc::mixSiteIdText(
          symcc::stableModuleId(M), global.getName());
      if (objectId == 0)
        objectId = 1;
      IRB.CreateCall(
          registerRegion,
          {
              IRB.CreatePointerCast(&global, bytePointer),
              ConstantInt::get(intPtrType, byteLength),
              IRB.getInt64(objectId),
          });
    }
  }
  appendToGlobalCtors(M, ctor, 0);

  return true;
}

bool canLower(const CallInst *CI) {
  const Function *Callee = CI->getCalledFunction();
  if (!Callee)
    return false;

  switch (Callee->getIntrinsicID()) {
  case Intrinsic::expect:
  case Intrinsic::ctpop:
  case Intrinsic::ctlz:
  case Intrinsic::cttz:
  case Intrinsic::prefetch:
  case Intrinsic::pcmarker:
  case Intrinsic::dbg_declare:
  case Intrinsic::dbg_label:
  case Intrinsic::annotation:
  case Intrinsic::ptr_annotation:
  case Intrinsic::assume:
#if LLVM_VERSION_MAJOR > 11
  case Intrinsic::experimental_noalias_scope_decl:
#endif
  case Intrinsic::var_annotation:
  case Intrinsic::sqrt:
  case Intrinsic::log:
  case Intrinsic::log2:
  case Intrinsic::log10:
  case Intrinsic::exp:
  case Intrinsic::exp2:
  case Intrinsic::pow:
  case Intrinsic::sin:
  case Intrinsic::cos:
  case Intrinsic::floor:
  case Intrinsic::ceil:
  case Intrinsic::trunc:
  case Intrinsic::round:
#if LLVM_VERSION_MAJOR > 10
  case Intrinsic::roundeven:
#endif
  case Intrinsic::copysign:
#if LLVM_VERSION_MAJOR < 16
  case Intrinsic::flt_rounds:
#else
  case Intrinsic::get_rounding:
#endif
  case Intrinsic::invariant_start:
  case Intrinsic::lifetime_start:
  case Intrinsic::invariant_end:
  case Intrinsic::lifetime_end:
    return true;
  default:
    return false;
  }

  llvm_unreachable("Control cannot reach here");
}

void liftInlineAssembly(CallInst *CI) {
  // TODO When we don't have to worry about the old pass manager anymore, move
  // the initialization to the pass constructor. (Currently there are two
  // passes, but only if we're on a recent enough LLVM...)

  Function *F = CI->getFunction();
  Module *M = F->getParent();
  auto triple = M->getTargetTriple();

  std::string error;
  auto target = TargetRegistry::lookupTarget(triple, error);
  if (!target) {
    errs() << "Warning: can't get target info to lift inline assembly\n";
    return;
  }

  auto cpu = F->getFnAttribute("target-cpu").getValueAsString();
  auto features = F->getFnAttribute("target-features").getValueAsString();

  std::unique_ptr<TargetMachine> TM(
      target->createTargetMachine(triple, cpu, features, TargetOptions(), {}));
  auto subTarget = TM->getSubtargetImpl(*F);
  if (subTarget == nullptr)
    return;

  auto targetLowering = subTarget->getTargetLowering();
  if (targetLowering == nullptr)
    return;

  targetLowering->ExpandInlineAsm(CI);
}

bool instrumentFunction(Function &F, AAResults *aliasAnalysis = nullptr,
                        MemorySSA *memorySSA = nullptr) {
  if (envFlag("SYMCC_DPOR_SCHEDULE_ONLY"))
    return false;

  auto functionName = F.getName();
  if (functionName == kSymCtorName)
    return false;

  DEBUG(errs() << "Symbolizing function ");
  DEBUG(errs().write_escaped(functionName) << '\n');

  SmallVector<Instruction *, 0> allInstructions;
  allInstructions.reserve(F.getInstructionCount());
  for (auto &I : instructions(F))
    allInstructions.push_back(&I);

  IntrinsicLowering IL(F.getParent()->getDataLayout());
  for (auto *I : allInstructions) {
    if (auto *CI = dyn_cast<CallInst>(I)) {
      if (canLower(CI)) {
        IL.LowerIntrinsicCall(CI);
      } else if (isa<InlineAsm>(CI->getCalledOperand())) {
        liftInlineAssembly(CI);
      }
    }
  }

  allInstructions.clear();
  for (auto &I : instructions(F))
    allInstructions.push_back(&I);

  Symbolizer symbolizer(*F.getParent(), F, aliasAnalysis, memorySSA);
  symbolizer.symbolizeFunctionArguments(F);

  for (auto &basicBlock : F)
    symbolizer.insertBasicBlockNotification(basicBlock);

  for (auto *instPtr : allInstructions)
    symbolizer.visit(instPtr);

  symbolizer.finalizePHINodes();
  symbolizer.shortCircuitExpressionUses();

  // DEBUG(errs() << F << '\n');
  assert(!verifyFunction(F, &errs()) &&
         "SymbolizePass produced invalid bitcode");

  return true;
}

} // namespace

bool SymbolizeLegacyPass::doInitialization(Module &M) {
  return instrumentModule(M);
}

bool SymbolizeLegacyPass::runOnFunction(Function &F) {
  auto &aliasAnalysis = getAnalysis<AAResultsWrapperPass>().getAAResults();
  auto &memorySSA = getAnalysis<MemorySSAWrapperPass>().getMSSA();
  return instrumentFunction(F, &aliasAnalysis, &memorySSA);
}

void SymbolizeLegacyPass::getAnalysisUsage(AnalysisUsage &AU) const {
  AU.addRequired<AAResultsWrapperPass>();
  AU.addRequired<MemorySSAWrapperPass>();
}

#if LLVM_VERSION_MAJOR >= 13

PreservedAnalyses SymbolizePass::run(Function &F,
                                     FunctionAnalysisManager &FAM) {
  auto &aliasAnalysis = FAM.getResult<AAManager>(F);
  auto &memorySSA = FAM.getResult<MemorySSAAnalysis>(F).getMSSA();
  return instrumentFunction(F, &aliasAnalysis, &memorySSA)
             ? PreservedAnalyses::none()
             : PreservedAnalyses::all();
}

PreservedAnalyses SymbolizePass::run(Module &M, ModuleAnalysisManager &) {
  return instrumentModule(M) ? PreservedAnalyses::none()
                             : PreservedAnalyses::all();
}

#endif
