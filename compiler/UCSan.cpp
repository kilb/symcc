// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.

#include "UCSan.h"

#include <llvm/ADT/ArrayRef.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/DataLayout.h>
#include <llvm/IR/DerivedTypes.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Verifier.h>
#include <llvm/Transforms/Utils/BasicBlockUtils.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

using namespace llvm;

namespace {

enum class ExternalPolicy { Passthrough, Pure, Arbitrary };

constexpr unsigned kUCSanArgumentCount = 256;

struct UCSanConfig {
  std::string entry;
  std::unordered_set<std::string> scope;
  ExternalPolicy defaultExternal = ExternalPolicy::Pure;
  std::unordered_map<std::string, ExternalPolicy> externalPolicies;
  std::unordered_map<std::string, std::string> wrappers;
};

struct UCSanRuntime {
  FunctionCallee initialize;
  FunctionCallee rootValue;
  FunctionCallee rootShadow;
  FunctionCallee check;
  FunctionCallee pushFrame;
  FunctionCallee popFrame;
  FunctionCallee registerExplicit;
  FunctionCallee releaseExplicit;
  FunctionCallee validateReallocate;
  FunctionCallee reallocateExplicit;
  FunctionCallee loadShadow;
  FunctionCallee loadStoredShadow;
  FunctionCallee storeShadow;
  FunctionCallee storeShadowConditional;
  FunctionCallee clearShadow;
  FunctionCallee copyShadow;
  FunctionCallee loadUninitialized;
  FunctionCallee storeUninitialized;
  FunctionCallee checkInitialized;
  FunctionCallee invalidate;
  FunctionCallee setArgumentShadow;
  FunctionCallee getArgumentShadow;
  FunctionCallee setArgumentUninitialized;
  FunctionCallee getArgumentUninitialized;
  FunctionCallee setReturnShadow;
  FunctionCallee getReturnShadow;
  FunctionCallee setReturnUninitialized;
  FunctionCallee getReturnUninitialized;
  IntegerType *int8Type = nullptr;
  IntegerType *intPtrType = nullptr;
  PointerType *bytePtrType = nullptr;

  explicit UCSanRuntime(Module &M) {
    LLVMContext &C = M.getContext();
    const DataLayout &DL = M.getDataLayout();
    IRBuilder<> IRB(C);
    intPtrType = DL.getIntPtrType(C);
    int8Type = IRB.getInt8Ty();
#if LLVM_VERSION_MAJOR >= 15
    bytePtrType = IRB.getPtrTy();
#else
    bytePtrType = IRB.getInt8PtrTy();
#endif
    Type *voidType = IRB.getVoidTy();
    Type *int32Type = IRB.getInt32Ty();
    Type *int64Type = IRB.getInt64Ty();

    initialize =
        M.getOrInsertFunction("_sym_ucsan_initialize", voidType);
    rootValue = M.getOrInsertFunction("_sym_ucsan_root_value", voidType,
                                      int64Type, bytePtrType, intPtrType);
    rootShadow = M.getOrInsertFunction("_sym_ucsan_root_shadow", intPtrType,
                                       int64Type, bytePtrType);
    check = M.getOrInsertFunction("_sym_ucsan_check", bytePtrType, bytePtrType,
                                  intPtrType, intPtrType);
    pushFrame =
        M.getOrInsertFunction("_sym_ucsan_push_frame", int64Type);
    popFrame = M.getOrInsertFunction("_sym_ucsan_pop_frame", voidType,
                                     int64Type);
    registerExplicit = M.getOrInsertFunction(
        "_sym_ucsan_register_explicit", intPtrType, bytePtrType, intPtrType,
        intPtrType, int32Type, int64Type, int8Type);
    releaseExplicit = M.getOrInsertFunction(
        "_sym_ucsan_release_explicit", voidType, bytePtrType, intPtrType);
    validateReallocate = M.getOrInsertFunction(
        "_sym_ucsan_validate_reallocate", voidType, bytePtrType, intPtrType);
    reallocateExplicit = M.getOrInsertFunction(
        "_sym_ucsan_reallocate_explicit", intPtrType, bytePtrType, intPtrType,
        bytePtrType, intPtrType, intPtrType);
    loadShadow = M.getOrInsertFunction("_sym_ucsan_load_shadow", intPtrType,
                                       bytePtrType, bytePtrType);
    loadStoredShadow = M.getOrInsertFunction(
        "_sym_ucsan_load_stored_shadow", intPtrType, bytePtrType, intPtrType);
    storeShadow = M.getOrInsertFunction("_sym_ucsan_store_shadow", voidType,
                                        bytePtrType, intPtrType, intPtrType);
    storeShadowConditional = M.getOrInsertFunction(
        "_sym_ucsan_store_shadow_conditional", voidType, bytePtrType,
        intPtrType, intPtrType, int8Type);
    clearShadow = M.getOrInsertFunction("_sym_ucsan_clear_shadow", voidType,
                                        bytePtrType, intPtrType);
    copyShadow = M.getOrInsertFunction("_sym_ucsan_copy_shadow", voidType,
                                       bytePtrType, bytePtrType, intPtrType);
    loadUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_load_uninitialized", int8Type, bytePtrType, intPtrType);
    storeUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_store_uninitialized", voidType, bytePtrType, intPtrType,
        int8Type);
    checkInitialized = M.getOrInsertFunction(
        "_sym_ucsan_check_initialized", voidType, int8Type, int32Type);
    invalidate =
        M.getOrInsertFunction("_sym_ucsan_invalidate", voidType, intPtrType);
    setArgumentShadow = M.getOrInsertFunction(
        "_sym_ucsan_set_argument_shadow", voidType, int32Type, intPtrType);
    getArgumentShadow = M.getOrInsertFunction(
        "_sym_ucsan_get_argument_shadow", intPtrType, int32Type);
    setArgumentUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_set_argument_uninitialized", voidType, int32Type,
        int8Type);
    getArgumentUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_get_argument_uninitialized", int8Type, int32Type);
    setReturnShadow = M.getOrInsertFunction("_sym_ucsan_set_return_shadow",
                                            voidType, intPtrType);
    getReturnShadow =
        M.getOrInsertFunction("_sym_ucsan_get_return_shadow", intPtrType);
    setReturnUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_set_return_uninitialized", voidType, int8Type);
    getReturnUninitialized = M.getOrInsertFunction(
        "_sym_ucsan_get_return_uninitialized", int8Type);
  }
};

std::string trim(StringRef input) {
  size_t start = 0;
  while (start < input.size() &&
         std::isspace(static_cast<unsigned char>(input[start])))
    ++start;
  size_t end = input.size();
  while (end > start &&
         std::isspace(static_cast<unsigned char>(input[end - 1])))
    --end;
  StringRef result = input.substr(start, end - start);
  if (result.size() >= 2 &&
      ((result.front() == '"' && result.back() == '"') ||
       (result.front() == '\'' && result.back() == '\'')))
    result = result.substr(1, result.size() - 2);
  return result.str();
}

std::vector<std::string> splitList(const char *raw) {
  std::vector<std::string> result;
  if (raw == nullptr)
    return result;
  std::stringstream input(raw);
  std::string item;
  while (std::getline(input, item, ',')) {
    std::string value = trim(item);
    if (!value.empty())
      result.push_back(std::move(value));
  }
  return result;
}

ExternalPolicy parsePolicy(StringRef value, ExternalPolicy fallback) {
  std::string normalized = trim(value);
  std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                 [](unsigned char ch) { return std::tolower(ch); });
  if (normalized == "passthrough" || normalized == "native" ||
      normalized == "keep")
    return ExternalPolicy::Passthrough;
  if (normalized == "arbitrary" || normalized == "havoc")
    return ExternalPolicy::Arbitrary;
  if (normalized == "pure" || normalized == "stub")
    return ExternalPolicy::Pure;
  return fallback;
}

void parseConfigFile(StringRef path, UCSanConfig &config) {
  std::ifstream input(path.str());
  if (!input)
    return;
  enum class Section { None, Scope, Externals, Wrappers, Custom };
  Section section = Section::None;
  std::string customFunction;
  std::string line;
  while (std::getline(input, line)) {
    size_t comment = line.find('#');
    if (comment != std::string::npos)
      line.erase(comment);
    size_t indentation = 0;
    while (indentation < line.size() &&
           std::isspace(static_cast<unsigned char>(line[indentation])))
      ++indentation;
    std::string value = trim(line);
    if (value.empty())
      continue;

    if (indentation == 0) {
      customFunction.clear();
      size_t colon = value.find(':');
      std::string key =
          colon == std::string::npos ? value : trim(value.substr(0, colon));
      std::string rest = colon == std::string::npos
                             ? ""
                             : trim(value.substr(colon + 1));
      if (key == "entry") {
        config.entry = rest;
        section = Section::None;
      } else if (key == "scope") {
        section = Section::Scope;
      } else if (key == "externals") {
        section = Section::Externals;
      } else if (key == "wrappers") {
        section = Section::Wrappers;
      } else if (key == "custom") {
        section = Section::Custom;
      } else if (key == "external") {
        config.defaultExternal =
            parsePolicy(rest, config.defaultExternal);
        section = Section::None;
      }
      continue;
    }

    if (section == Section::Scope) {
      if (value.front() == '-')
        value = trim(StringRef(value).drop_front());
      if (!value.empty())
        config.scope.insert(value);
      continue;
    }

    size_t colon = value.find(':');
    if (colon == std::string::npos)
      continue;
    std::string key = trim(value.substr(0, colon));
    if (!key.empty() && key.front() == '-')
      key = trim(StringRef(key).drop_front());
    std::string rest = trim(value.substr(colon + 1));
    if (section == Section::Externals && !key.empty()) {
      config.externalPolicies[key] =
          parsePolicy(rest, config.defaultExternal);
    } else if (section == Section::Wrappers && !key.empty() &&
               !rest.empty()) {
      config.wrappers[key] = rest;
    } else if (section == Section::Custom) {
      if (indentation <= 2 && !key.empty()) {
        customFunction = key;
        if (!rest.empty())
          config.wrappers[key] = rest;
      } else if (key == "ref_name" && !customFunction.empty() &&
                 !rest.empty()) {
        config.wrappers[customFunction] = rest;
      }
    }
  }
}

UCSanConfig loadConfig() {
  UCSanConfig config;
  if (const char *path = std::getenv("SYMCC_UCSAN_CONFIG"))
    parseConfigFile(path, config);
  if (const char *entry = std::getenv("SYMCC_UCSAN_ENTRY"))
    config.entry = trim(entry);
  for (std::string &function : splitList(std::getenv("SYMCC_UCSAN_SCOPE")))
    config.scope.insert(std::move(function));
  if (const char *external = std::getenv("SYMCC_UCSAN_EXTERNAL"))
    config.defaultExternal =
        parsePolicy(external, config.defaultExternal);
  if (!config.entry.empty())
    config.scope.insert(config.entry);
  return config;
}

uint64_t stableHash(StringRef value) {
  uint64_t hash = 1469598103934665603ULL;
  for (char ch : value) {
    hash ^= static_cast<unsigned char>(ch);
    hash *= 1099511628211ULL;
  }
  return hash;
}

bool startsWith(StringRef value, StringRef prefix) {
  return value.size() >= prefix.size() &&
         value.substr(0, prefix.size()) == prefix;
}

uint64_t typeSize(const DataLayout &DL, Type *type) {
  if (!type->isSized())
    return 0;
#if LLVM_VERSION_MAJOR >= 11
  TypeSize size = DL.getTypeStoreSize(type);
  return size.isScalable() ? 0 : size.getFixedValue();
#else
  return DL.getTypeStoreSize(type);
#endif
}

Value *asBytePointer(IRBuilder<> &IRB, Value *pointer,
                     PointerType *bytePtrType) {
  if (pointer->getType() == bytePtrType)
    return pointer;
  return IRB.CreatePointerCast(pointer, bytePtrType);
}

Value *asIntPtr(IRBuilder<> &IRB, Value *value, IntegerType *intPtrType) {
  if (value->getType() == intPtrType)
    return value;
  return IRB.CreateZExtOrTrunc(value, intPtrType);
}

bool isUCSanRuntimeCall(const CallBase &call) {
  const Function *callee = call.getCalledFunction();
  return callee != nullptr && startsWith(callee->getName(), "_sym_ucsan_");
}

bool isNativeExternal(StringRef name) {
  static const char *const names[] = {
      "malloc",       "calloc",       "realloc",     "reallocarray",
      "free",
      "memcpy",       "memmove",      "memset",      "memcmp",
      "abort",        "exit",         "_Exit",       "__assert_fail",
      "__stack_chk_fail", "__cxa_atexit", "__cxa_finalize"};
  for (const char *candidate : names)
    if (name == candidate)
      return true;
  static const char *const cxxAllocators[] = {
      "_Znwm", "_Znam", "_Znwj", "_Znaj", "_ZdlPv", "_ZdaPv",
      "_ZdlPvm", "_ZdaPvm", "_ZdlPvj", "_ZdaPvj",
      "_ZnwmSt11align_val_t", "_ZnamSt11align_val_t",
      "_ZnwjSt11align_val_t", "_ZnajSt11align_val_t",
      "_ZdlPvSt11align_val_t", "_ZdaPvSt11align_val_t",
      "_ZdlPvmSt11align_val_t", "_ZdaPvmSt11align_val_t",
      "_ZdlPvjSt11align_val_t", "_ZdaPvjSt11align_val_t"};
  for (const char *candidate : cxxAllocators)
    if (name == candidate)
      return true;
  return startsWith(name, "llvm.") || startsWith(name, "_sym_") ||
         startsWith(name, "__symcc_ucsan_");
}

Value *buildRootValue(IRBuilder<> &IRB, Type *type, uint64_t rootId,
                      const DataLayout &DL, UCSanRuntime &runtime) {
  uint64_t size = typeSize(DL, type);
  if (size == 0)
    return UndefValue::get(type);
  AllocaInst *storage = IRB.CreateAlloca(type);
  IRB.CreateCall(runtime.rootValue,
                 {IRB.getInt64(rootId),
                  asBytePointer(IRB, storage, runtime.bytePtrType),
                  ConstantInt::get(runtime.intPtrType, size)});
  return IRB.CreateLoad(type, storage);
}

Function *createExternalStub(Module &M, FunctionType *type, StringRef name,
                             uint64_t rootId, ExternalPolicy policy,
                             UCSanRuntime &runtime) {
  std::string stubName =
      "__symcc_ucsan_stub." + std::to_string(stableHash(name));
  if (Function *existing = M.getFunction(stubName))
    return existing;
  Function *stub =
      Function::Create(type, GlobalValue::InternalLinkage, stubName, M);
  BasicBlock *entry = BasicBlock::Create(M.getContext(), "entry", stub);
  IRBuilder<> IRB(entry);
  if (policy == ExternalPolicy::Arbitrary) {
    for (Argument &argument : stub->args()) {
      if (!argument.getType()->isPointerTy())
        continue;
      Value *shadow = IRB.CreateCall(
          runtime.getArgumentShadow, IRB.getInt32(argument.getArgNo()));
      IRB.CreateCall(runtime.invalidate, shadow);
    }
  }
  Type *returnType = type->getReturnType();
  if (returnType->isVoidTy()) {
    IRB.CreateRetVoid();
  } else {
    Value *value =
        buildRootValue(IRB, returnType, rootId, M.getDataLayout(), runtime);
    if (returnType->isPointerTy()) {
      Value *shadow = IRB.CreateCall(
          runtime.rootShadow,
          {IRB.getInt64(rootId),
           asBytePointer(IRB, value, runtime.bytePtrType)});
      IRB.CreateCall(runtime.setReturnShadow, shadow);
    }
    IRB.CreateRet(value);
  }
  return stub;
}

void rewriteExternalCalls(Module &M,
                          const std::unordered_set<Function *> &scope,
                          const UCSanConfig &config, UCSanRuntime &runtime) {
  uint64_t callIndex = 0;
  for (Function *function : scope) {
    SmallVector<CallBase *, 16> calls;
    for (Instruction &instruction : instructions(function))
      if (auto *call = dyn_cast<CallBase>(&instruction))
        calls.push_back(call);
    for (CallBase *call : calls) {
      ++callIndex;
      Function *callee = call->getCalledFunction();
      if (callee == nullptr || callee->isIntrinsic() ||
          scope.count(callee) != 0 || isUCSanRuntimeCall(*call))
        continue;

      auto wrapper = config.wrappers.find(callee->getName().str());
      if (wrapper != config.wrappers.end()) {
        FunctionCallee replacement =
            M.getOrInsertFunction(wrapper->second, call->getFunctionType());
        call->setCalledFunction(replacement);
        continue;
      }

      ExternalPolicy policy = config.defaultExternal;
      auto configured =
          config.externalPolicies.find(callee->getName().str());
      if (configured != config.externalPolicies.end())
        policy = configured->second;
      else if (isNativeExternal(callee->getName()))
        policy = ExternalPolicy::Passthrough;
      if (policy == ExternalPolicy::Passthrough)
        continue;

      std::string identity =
          function->getName().str() + ":" + callee->getName().str() + ":" +
          std::to_string(callIndex);
      uint64_t rootId = stableHash(identity);
      Function *stub = createExternalStub(M, call->getFunctionType(), identity,
                                          rootId, policy, runtime);
      call->setCalledFunction(stub);
    }
  }
}

Function *createHarness(Module &M, Function &entry, UCSanRuntime &runtime) {
  if (entry.isVarArg()) {
    errs() << "SymCC UCSan: variadic entry functions are unsupported: "
           << entry.getName() << "\n";
    return nullptr;
  }
  FunctionType *mainType = FunctionType::get(
      Type::getInt32Ty(M.getContext()), false);
  Function *main = Function::Create(mainType, GlobalValue::ExternalLinkage,
                                    "main", M);
  BasicBlock *block = BasicBlock::Create(M.getContext(), "entry", main);
  IRBuilder<> IRB(block);
  IRB.CreateCall(runtime.initialize);

  SmallVector<Value *, 8> arguments;
  for (Argument &argument : entry.args()) {
    uint64_t rootId = argument.getArgNo();
    Value *value = buildRootValue(IRB, argument.getType(), rootId,
                                  M.getDataLayout(), runtime);
    arguments.push_back(value);
    IRB.CreateCall(runtime.setArgumentUninitialized,
                   {IRB.getInt32(argument.getArgNo()), IRB.getInt8(0)});
    if (argument.getType()->isPointerTy()) {
      Value *shadow = IRB.CreateCall(
          runtime.rootShadow,
          {IRB.getInt64(rootId),
           asBytePointer(IRB, value, runtime.bytePtrType)});
      IRB.CreateCall(runtime.setArgumentShadow,
                     {IRB.getInt32(argument.getArgNo()), shadow});
    }
  }
  IRB.CreateCall(&entry, arguments);
  IRB.CreateRet(IRB.getInt32(0));
  return main;
}

Value *zeroShadow(UCSanRuntime &runtime) {
  return ConstantInt::get(runtime.intPtrType, 0);
}

bool hasShadow(Value *value, const DenseMap<Value *, Value *> &shadows) {
  return shadows.find(value) != shadows.end();
}

Value *lookupShadow(Value *value, DenseMap<Value *, Value *> &shadows,
                    UCSanRuntime &runtime) {
  auto found = shadows.find(value);
  return found == shadows.end() ? zeroShadow(runtime) : found->second;
}

Value *zeroUninitialized(LLVMContext &context) {
  return ConstantInt::getFalse(context);
}

Value *lookupUninitialized(Value *value,
                           DenseMap<Value *, Value *> &uninitialized) {
  auto found = uninitialized.find(value);
  return found == uninitialized.end()
             ? zeroUninitialized(value->getContext())
             : found->second;
}

Value *combineUninitialized(IRBuilder<> &IRB, ArrayRef<Value *> values,
                            DenseMap<Value *, Value *> &uninitialized) {
  Value *combined = zeroUninitialized(IRB.getContext());
  for (Value *value : values)
    combined = IRB.CreateOr(combined, lookupUninitialized(value, uninitialized));
  return combined;
}

Value *asUninitializedByte(IRBuilder<> &IRB, Value *value,
                           UCSanRuntime &runtime) {
  return IRB.CreateZExt(value, runtime.int8Type);
}

void checkInitialized(IRBuilder<> &IRB, Value *uninitialized, uint32_t sink,
                      UCSanRuntime &runtime) {
  if (auto *constant = dyn_cast<ConstantInt>(uninitialized))
    if (constant->isZero())
      return;
  IRB.CreateCall(runtime.checkInitialized,
                 {asUninitializedByte(IRB, uninitialized, runtime),
                  IRB.getInt32(sink)});
}

Value *translatePointer(IRBuilder<> &IRB, Value *pointer, Value *shadow,
                        Value *uninitialized, Value *size,
                        UCSanRuntime &runtime) {
  Value *normalizedSize = asIntPtr(IRB, size, runtime.intPtrType);
  Value *accessesMemory =
      IRB.CreateICmpNE(normalizedSize, ConstantInt::get(runtime.intPtrType, 0));
  checkInitialized(IRB, IRB.CreateAnd(uninitialized, accessesMemory), 1,
                   runtime);
  if (auto *constant = dyn_cast<ConstantInt>(shadow))
    if (constant->isZero())
      return pointer;
  Value *translated = IRB.CreateCall(
      runtime.check,
      {asBytePointer(IRB, pointer, runtime.bytePtrType), shadow,
       normalizedSize});
  return IRB.CreatePointerCast(translated, pointer->getType());
}

Value *translatePointer(IRBuilder<> &IRB, Value *pointer, Value *shadow,
                        Value *uninitialized, uint64_t size,
                        UCSanRuntime &runtime) {
  return translatePointer(IRB, pointer, shadow, uninitialized,
                          ConstantInt::get(runtime.intPtrType, size), runtime);
}

Instruction *callReturnPoint(CallBase &call) {
  if (auto *plainCall = dyn_cast<CallInst>(&call))
    return plainCall->getNextNode();
  auto &invoke = cast<InvokeInst>(call);
  BasicBlock *edge = SplitCriticalEdge(invoke.getParent(),
                                       invoke.getNormalDest());
  return edge != nullptr ? &*edge->getFirstInsertionPt()
                         : &*invoke.getNormalDest()->getFirstInsertionPt();
}

bool isDirectCall(const CallBase &call, StringRef name) {
  const Function *callee = call.getCalledFunction();
  return callee != nullptr && callee->getName() == name;
}

bool isOneOf(const CallBase &call,
             std::initializer_list<StringRef> names) {
  const Function *callee = call.getCalledFunction();
  if (callee == nullptr)
    return false;
  for (StringRef name : names)
    if (callee->getName() == name)
      return true;
  return false;
}

void instrumentScopedFunction(Function &function, UCSanRuntime &runtime,
                              const std::unordered_set<Function *> &scope) {
  if (function.arg_size() > kUCSanArgumentCount)
    report_fatal_error("UCSan scoped function exceeds argument-channel bound");
  const DataLayout &DL = function.getParent()->getDataLayout();
  for (Instruction &instruction : instructions(function)) {
    if (auto *call = dyn_cast<CallInst>(&instruction)) {
      if (call->isMustTailCall()) {
        errs() << "SymCC UCSan: musttail is unsupported in scoped function: "
               << function.getName() << "\n";
        report_fatal_error("unsupported UCSan musttail exit");
      }
    }
    if ((isa<CleanupReturnInst>(instruction) ||
         isa<CatchSwitchInst>(instruction))) {
      errs() << "SymCC UCSan: Windows funclet exits are unsupported in scoped "
                "function: "
             << function.getName() << "\n";
      report_fatal_error("unsupported UCSan funclet exit");
    }
  }
  DenseMap<Value *, Value *> shadows;
  DenseMap<Value *, Value *> uninitialized;
  IRBuilder<> entryBuilder(&*function.getEntryBlock().getFirstInsertionPt());
  Value *frame = entryBuilder.CreateCall(runtime.pushFrame);
  for (Argument &argument : function.args()) {
    if (argument.user_empty())
      continue;
    Value *argumentUninitialized = entryBuilder.CreateCall(
        runtime.getArgumentUninitialized,
        entryBuilder.getInt32(argument.getArgNo()));
    uninitialized[&argument] = entryBuilder.CreateICmpNE(
        argumentUninitialized, entryBuilder.getInt8(0));
    if (argument.getType()->isPointerTy())
      shadows[&argument] = entryBuilder.CreateCall(
          runtime.getArgumentShadow,
          entryBuilder.getInt32(argument.getArgNo()));
  }

  SmallVector<Instruction *, 64> instructionsToProcess;
  for (Instruction &instruction : instructions(function))
    instructionsToProcess.push_back(&instruction);

  DenseMap<PHINode *, PHINode *> shadowPhis;
  DenseMap<PHINode *, PHINode *> uninitializedPhis;
  DenseMap<AtomicCmpXchgInst *, std::pair<Value *, Value *>>
      compareExchangeUninitialized;
  for (Instruction *instruction : instructionsToProcess) {
    auto *phi = dyn_cast<PHINode>(instruction);
    if (phi == nullptr)
      continue;
    if (phi->getType()->isPointerTy()) {
      PHINode *shadowPhi =
          PHINode::Create(runtime.intPtrType, phi->getNumIncomingValues(),
                          phi->getName() + ".ucsan",
                          phi->getParent()->getFirstNonPHI());
      shadows[phi] = shadowPhi;
      shadowPhis[phi] = shadowPhi;
    }
    PHINode *uninitializedPhi =
        PHINode::Create(Type::getInt1Ty(function.getContext()),
                        phi->getNumIncomingValues(), phi->getName() + ".ucsan.ubi",
                        phi->getParent()->getFirstNonPHI());
    uninitialized[phi] = uninitializedPhi;
    uninitializedPhis[phi] = uninitializedPhi;
  }

  for (Instruction *instruction : instructionsToProcess) {
    if (isa<PHINode>(instruction))
      continue;

    if (auto *allocation = dyn_cast<AllocaInst>(instruction)) {
      IRBuilder<> before(allocation);
      checkInitialized(
          before,
          lookupUninitialized(allocation->getArraySize(), uninitialized), 3,
          runtime);
      IRBuilder<> after(allocation->getNextNode());
      Value *count = asIntPtr(after, allocation->getArraySize(),
                              runtime.intPtrType);
      uint64_t elementSize = typeSize(DL, allocation->getAllocatedType());
      shadows[allocation] = after.CreateCall(
          runtime.registerExplicit,
          {asBytePointer(after, allocation, runtime.bytePtrType), count,
           ConstantInt::get(runtime.intPtrType, elementSize),
           after.getInt32(1), frame, after.getInt8(0)});
      uninitialized[allocation] = zeroUninitialized(function.getContext());
      continue;
    }

    if (auto *load = dyn_cast<LoadInst>(instruction)) {
      IRBuilder<> before(load);
      Value *original = load->getPointerOperand();
      Value *translated = translatePointer(
          before, original, lookupShadow(original, shadows, runtime),
          lookupUninitialized(original, uninitialized),
          typeSize(DL, load->getType()), runtime);
      load->setOperand(load->getPointerOperandIndex(), translated);
      IRBuilder<> after(load->getNextNode());
      Value *loadedUninitialized = after.CreateCall(
          runtime.loadUninitialized,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, typeSize(DL, load->getType()))});
      uninitialized[load] =
          after.CreateICmpNE(loadedUninitialized, after.getInt8(0));
      if (load->getType()->isPointerTy()) {
        shadows[load] = after.CreateCall(
            runtime.loadShadow,
            {asBytePointer(after, translated, runtime.bytePtrType),
             asBytePointer(after, load, runtime.bytePtrType)});
      } else if (load->getType()->isIntegerTy()) {
        shadows[load] = after.CreateCall(
            runtime.loadStoredShadow,
            {asBytePointer(after, translated, runtime.bytePtrType),
             ConstantInt::get(runtime.intPtrType,
                              typeSize(DL, load->getType()))});
      }
      continue;
    }

    if (auto *store = dyn_cast<StoreInst>(instruction)) {
      IRBuilder<> before(store);
      Value *original = store->getPointerOperand();
      uint64_t size = typeSize(DL, store->getValueOperand()->getType());
      Value *translated = translatePointer(
          before, original, lookupShadow(original, shadows, runtime),
          lookupUninitialized(original, uninitialized), size, runtime);
      store->setOperand(store->getPointerOperandIndex(), translated);
      IRBuilder<> after(store->getNextNode());
      Value *address =
          asBytePointer(after, translated, runtime.bytePtrType);
      if (store->getValueOperand()->getType()->isPointerTy() ||
          hasShadow(store->getValueOperand(), shadows)) {
        after.CreateCall(
            runtime.storeShadow,
            {address, lookupShadow(store->getValueOperand(), shadows, runtime),
             ConstantInt::get(runtime.intPtrType, size)});
      } else {
        after.CreateCall(runtime.clearShadow,
                         {address,
                          ConstantInt::get(runtime.intPtrType, size)});
      }
      after.CreateCall(
          runtime.storeUninitialized,
          {address, ConstantInt::get(runtime.intPtrType, size),
           asUninitializedByte(
               after,
               lookupUninitialized(store->getValueOperand(), uninitialized),
               runtime)});
      continue;
    }

    if (auto *transfer = dyn_cast<MemTransferInst>(instruction)) {
      IRBuilder<> before(transfer);
      Value *length = asIntPtr(before, transfer->getLength(),
                               runtime.intPtrType);
      checkInitialized(before,
                       lookupUninitialized(transfer->getLength(), uninitialized),
                       3, runtime);
      Value *destination = translatePointer(
          before, transfer->getRawDest(),
          lookupShadow(transfer->getRawDest(), shadows, runtime),
          lookupUninitialized(transfer->getRawDest(), uninitialized), length,
          runtime);
      Value *source = translatePointer(
          before, transfer->getRawSource(),
          lookupShadow(transfer->getRawSource(), shadows, runtime),
          lookupUninitialized(transfer->getRawSource(), uninitialized), length,
          runtime);
      transfer->setDest(destination);
      transfer->setSource(source);
      IRBuilder<> after(transfer->getNextNode());
      after.CreateCall(
          runtime.copyShadow,
          {asBytePointer(after, destination, runtime.bytePtrType),
           asBytePointer(after, source, runtime.bytePtrType), length});
      continue;
    }

    if (auto *memorySet = dyn_cast<MemSetInst>(instruction)) {
      IRBuilder<> before(memorySet);
      Value *length =
          asIntPtr(before, memorySet->getLength(), runtime.intPtrType);
      checkInitialized(before,
                       lookupUninitialized(memorySet->getLength(), uninitialized),
                       3, runtime);
      Value *destination = translatePointer(
          before, memorySet->getRawDest(),
          lookupShadow(memorySet->getRawDest(), shadows, runtime),
          lookupUninitialized(memorySet->getRawDest(), uninitialized), length,
          runtime);
      memorySet->setDest(destination);
      IRBuilder<> after(memorySet->getNextNode());
      after.CreateCall(
          runtime.clearShadow,
          {asBytePointer(after, destination, runtime.bytePtrType), length});
      after.CreateCall(
          runtime.storeUninitialized,
          {asBytePointer(after, destination, runtime.bytePtrType), length,
           asUninitializedByte(
               after,
               lookupUninitialized(memorySet->getValue(), uninitialized),
               runtime)});
      continue;
    }

    if (auto *atomic = dyn_cast<AtomicRMWInst>(instruction)) {
      IRBuilder<> before(atomic);
      Value *original = atomic->getPointerOperand();
      uint64_t size = typeSize(DL, atomic->getValOperand()->getType());
      Value *translated = translatePointer(
          before, original, lookupShadow(original, shadows, runtime),
          lookupUninitialized(original, uninitialized), size, runtime);
      atomic->setOperand(atomic->getPointerOperandIndex(), translated);
      IRBuilder<> after(atomic->getNextNode());
      Value *loadedUninitialized = after.CreateCall(
          runtime.loadUninitialized,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size)});
      Value *loadedFlag =
          after.CreateICmpNE(loadedUninitialized, after.getInt8(0));
      uninitialized[atomic] = loadedFlag;
      shadows[atomic] = after.CreateCall(
          runtime.loadStoredShadow,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size)});
      if (atomic->getValOperand()->getType()->isPointerTy() ||
          hasShadow(atomic->getValOperand(), shadows))
        after.CreateCall(
            runtime.storeShadow,
            {asBytePointer(after, translated, runtime.bytePtrType),
             lookupShadow(atomic->getValOperand(), shadows, runtime),
             ConstantInt::get(runtime.intPtrType, size)});
      else
        after.CreateCall(
            runtime.clearShadow,
            {asBytePointer(after, translated, runtime.bytePtrType),
             ConstantInt::get(runtime.intPtrType, size)});
      Value *storedFlag = lookupUninitialized(atomic->getValOperand(),
                                              uninitialized);
      if (atomic->getOperation() != AtomicRMWInst::Xchg)
        storedFlag = after.CreateOr(storedFlag, loadedFlag);
      after.CreateCall(
          runtime.storeUninitialized,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size),
           asUninitializedByte(after, storedFlag, runtime)});
      continue;
    }

    if (auto *compareExchange = dyn_cast<AtomicCmpXchgInst>(instruction)) {
      IRBuilder<> before(compareExchange);
      Value *original = compareExchange->getPointerOperand();
      uint64_t size =
          typeSize(DL, compareExchange->getCompareOperand()->getType());
      Value *translated = translatePointer(
          before, original, lookupShadow(original, shadows, runtime),
          lookupUninitialized(original, uninitialized), size, runtime);
      compareExchange->setOperand(
          compareExchange->getPointerOperandIndex(), translated);
      IRBuilder<> after(compareExchange->getNextNode());
      Value *loadedUninitialized = after.CreateCall(
          runtime.loadUninitialized,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size)});
      Value *loadedFlag =
          after.CreateICmpNE(loadedUninitialized, after.getInt8(0));
      shadows[compareExchange] = after.CreateCall(
          runtime.loadStoredShadow,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size)});
      Value *successUninitialized = after.CreateOr(
          loadedFlag, lookupUninitialized(compareExchange->getCompareOperand(),
                                          uninitialized));
      uninitialized[compareExchange] = successUninitialized;
      compareExchangeUninitialized[compareExchange] =
          {loadedFlag, successUninitialized};
      Value *success = after.CreateExtractValue(compareExchange, 1);
      Value *newShadow =
          compareExchange->getNewValOperand()->getType()->isPointerTy() ||
                  hasShadow(compareExchange->getNewValOperand(), shadows)
              ? lookupShadow(compareExchange->getNewValOperand(), shadows,
                             runtime)
              : zeroShadow(runtime);
      after.CreateCall(
          runtime.storeShadowConditional,
          {asBytePointer(after, translated, runtime.bytePtrType), newShadow,
           ConstantInt::get(runtime.intPtrType, size),
           asUninitializedByte(after, success, runtime)});
      Value *storedFlag = after.CreateSelect(
          success,
          lookupUninitialized(compareExchange->getNewValOperand(),
                              uninitialized),
          loadedFlag);
      after.CreateCall(
          runtime.storeUninitialized,
          {asBytePointer(after, translated, runtime.bytePtrType),
           ConstantInt::get(runtime.intPtrType, size),
           asUninitializedByte(after, storedFlag, runtime)});
      continue;
    }

    if (auto *getElement = dyn_cast<GetElementPtrInst>(instruction)) {
      shadows[getElement] =
          lookupShadow(getElement->getPointerOperand(), shadows, runtime);
      IRBuilder<> IRB(getElement);
      SmallVector<Value *, 8> inputs;
      for (Use &operand : getElement->operands())
        inputs.push_back(operand.get());
      uninitialized[getElement] =
          combineUninitialized(IRB, inputs, uninitialized);
      continue;
    }
    if (auto *cast = dyn_cast<CastInst>(instruction)) {
      if (cast->getType()->isPointerTy() ||
          cast->getOperand(0)->getType()->isPointerTy() ||
          hasShadow(cast->getOperand(0), shadows))
        shadows[cast] = lookupShadow(cast->getOperand(0), shadows, runtime);
      uninitialized[cast] =
          lookupUninitialized(cast->getOperand(0), uninitialized);
      continue;
    }
    if (auto *freeze = dyn_cast<FreezeInst>(instruction)) {
      if (hasShadow(freeze->getOperand(0), shadows))
        shadows[freeze] =
            lookupShadow(freeze->getOperand(0), shadows, runtime);
      uninitialized[freeze] =
          lookupUninitialized(freeze->getOperand(0), uninitialized);
      continue;
    }
    if (auto *select = dyn_cast<SelectInst>(instruction)) {
      if (select->getType()->isPointerTy() ||
          hasShadow(select->getTrueValue(), shadows) ||
          hasShadow(select->getFalseValue(), shadows)) {
        IRBuilder<> IRB(select);
        shadows[select] = IRB.CreateSelect(
            select->getCondition(),
            lookupShadow(select->getTrueValue(), shadows, runtime),
            lookupShadow(select->getFalseValue(), shadows, runtime));
      }
      IRBuilder<> IRB(select);
      Value *selected = IRB.CreateSelect(
          select->getCondition(),
          lookupUninitialized(select->getTrueValue(), uninitialized),
          lookupUninitialized(select->getFalseValue(), uninitialized));
      uninitialized[select] = IRB.CreateOr(
          lookupUninitialized(select->getCondition(), uninitialized), selected);
      continue;
    }
    if (auto *binary = dyn_cast<BinaryOperator>(instruction)) {
      bool left = hasShadow(binary->getOperand(0), shadows);
      bool right = hasShadow(binary->getOperand(1), shadows);
      if (left || right) {
        Value *leftShadow =
            lookupShadow(binary->getOperand(0), shadows, runtime);
        Value *rightShadow =
            lookupShadow(binary->getOperand(1), shadows, runtime);
        if (left && right) {
          IRBuilder<> shadowBuilder(binary);
          shadows[binary] = shadowBuilder.CreateSelect(
              shadowBuilder.CreateICmpNE(leftShadow, zeroShadow(runtime)),
              leftShadow, rightShadow);
        } else {
          shadows[binary] = left ? leftShadow : rightShadow;
        }
      }
      IRBuilder<> IRB(binary);
      SmallVector<Value *, 2> inputs{binary->getOperand(0),
                                     binary->getOperand(1)};
      uninitialized[binary] =
          combineUninitialized(IRB, inputs, uninitialized);
      continue;
    }
    if (auto *extract = dyn_cast<ExtractValueInst>(instruction)) {
      IRBuilder<> IRB(extract);
      unsigned index = extract->getNumIndices() == 1 ? *extract->idx_begin() : 2;
      if (auto *compareExchange =
              dyn_cast<AtomicCmpXchgInst>(extract->getAggregateOperand())) {
        auto found = compareExchangeUninitialized.find(compareExchange);
        uninitialized[extract] =
            found != compareExchangeUninitialized.end() && index < 2
                ? (index == 0 ? found->second.first : found->second.second)
                : lookupUninitialized(extract->getAggregateOperand(),
                                      uninitialized);
      } else {
        uninitialized[extract] =
            lookupUninitialized(extract->getAggregateOperand(), uninitialized);
      }
      if (hasShadow(extract->getAggregateOperand(), shadows) && index == 0)
        shadows[extract] =
            lookupShadow(extract->getAggregateOperand(), shadows, runtime);
      continue;
    }

    if (auto *branch = dyn_cast<BranchInst>(instruction)) {
      if (branch->isConditional()) {
        IRBuilder<> IRB(branch);
        checkInitialized(
            IRB, lookupUninitialized(branch->getCondition(), uninitialized), 2,
            runtime);
      }
      continue;
    }
    if (auto *switchInstruction = dyn_cast<SwitchInst>(instruction)) {
      IRBuilder<> IRB(switchInstruction);
      checkInitialized(
          IRB,
          lookupUninitialized(switchInstruction->getCondition(), uninitialized),
          2, runtime);
      continue;
    }
    if (auto *indirectBranch = dyn_cast<IndirectBrInst>(instruction)) {
      IRBuilder<> IRB(indirectBranch);
      checkInitialized(
          IRB,
          lookupUninitialized(indirectBranch->getAddress(), uninitialized), 1,
          runtime);
      continue;
    }

    if (!isa<CallBase>(instruction) && !instruction->getType()->isVoidTy() &&
        !instruction->isTerminator()) {
      IRBuilder<> IRB(instruction);
      SmallVector<Value *, 8> inputs;
      for (Use &operand : instruction->operands())
        inputs.push_back(operand.get());
      uninitialized[instruction] =
          combineUninitialized(IRB, inputs, uninitialized);
      continue;
    }

    if (auto *call = dyn_cast<CallBase>(instruction)) {
      if (call->arg_size() > kUCSanArgumentCount)
        report_fatal_error("UCSan call exceeds argument-channel bound");
      if (isUCSanRuntimeCall(*call))
        continue;
      IRBuilder<> before(call);
      if (call->getCalledFunction() == nullptr)
        checkInitialized(
            before,
            lookupUninitialized(call->getCalledOperand(), uninitialized), 1,
            runtime);
      Function *callee = call->getCalledFunction();
      if (callee == nullptr || scope.count(callee) == 0) {
        for (Use &argument : call->args())
          if (argument->getType()->isPointerTy())
            checkInitialized(
                before, lookupUninitialized(argument, uninitialized), 1,
                runtime);
      }
      if (isOneOf(*call,
                  {"__cxa_throw", "__cxa_rethrow", "_Unwind_Resume",
                   "_Unwind_RaiseException"}) &&
          isa<CallInst>(call)) {
        before.CreateCall(runtime.popFrame, frame);
      }
      if (isOneOf(*call,
                  {"free", "__libc_free", "_ZdlPv", "_ZdaPv",
                   "_ZdlPvm", "_ZdaPvm", "_ZdlPvj", "_ZdaPvj",
                   "_ZdlPvSt11align_val_t", "_ZdaPvSt11align_val_t",
                   "_ZdlPvmSt11align_val_t", "_ZdaPvmSt11align_val_t",
                   "_ZdlPvjSt11align_val_t", "_ZdaPvjSt11align_val_t"}) &&
          call->arg_size() >= 1) {
        Value *pointer = call->getArgOperand(0);
        checkInitialized(before, lookupUninitialized(pointer, uninitialized), 1,
                         runtime);
        before.CreateCall(
            runtime.releaseExplicit,
            {asBytePointer(before, pointer, runtime.bytePtrType),
             lookupShadow(pointer, shadows, runtime)});
        continue;
      }
      if (isOneOf(*call, {"realloc", "__libc_realloc"}) &&
          call->arg_size() == 2 && call->getType()->isPointerTy()) {
        Value *oldPointer = call->getArgOperand(0);
        Value *oldShadow = lookupShadow(oldPointer, shadows, runtime);
        checkInitialized(
            before, lookupUninitialized(oldPointer, uninitialized), 1, runtime);
        checkInitialized(
            before,
            lookupUninitialized(call->getArgOperand(1), uninitialized), 3,
            runtime);
        before.CreateCall(
            runtime.validateReallocate,
            {asBytePointer(before, oldPointer, runtime.bytePtrType),
             oldShadow});
        Instruction *returnPoint = callReturnPoint(*call);
        IRBuilder<> after(returnPoint);
        shadows[call] = after.CreateCall(
            runtime.reallocateExplicit,
            {asBytePointer(after, oldPointer, runtime.bytePtrType), oldShadow,
             asBytePointer(after, call, runtime.bytePtrType),
             ConstantInt::get(runtime.intPtrType, 1),
             asIntPtr(after, call->getArgOperand(1), runtime.intPtrType)});
        continue;
      }
      if (isDirectCall(*call, "reallocarray") && call->arg_size() == 3 &&
          call->getType()->isPointerTy()) {
        Value *oldPointer = call->getArgOperand(0);
        Value *oldShadow = lookupShadow(oldPointer, shadows, runtime);
        checkInitialized(
            before, lookupUninitialized(oldPointer, uninitialized), 1, runtime);
        checkInitialized(
            before,
            combineUninitialized(
                before,
                {call->getArgOperand(1), call->getArgOperand(2)},
                uninitialized),
            3, runtime);
        before.CreateCall(
            runtime.validateReallocate,
            {asBytePointer(before, oldPointer, runtime.bytePtrType),
             oldShadow});
        Instruction *returnPoint = callReturnPoint(*call);
        IRBuilder<> after(returnPoint);
        shadows[call] = after.CreateCall(
            runtime.reallocateExplicit,
            {asBytePointer(after, oldPointer, runtime.bytePtrType), oldShadow,
             asBytePointer(after, call, runtime.bytePtrType),
             asIntPtr(after, call->getArgOperand(1), runtime.intPtrType),
             asIntPtr(after, call->getArgOperand(2), runtime.intPtrType)});
        continue;
      }
      if (isOneOf(*call,
                  {"malloc", "__libc_malloc", "aligned_alloc", "_Znwm",
                   "_Znam", "_Znwj", "_Znaj", "_ZnwmSt11align_val_t",
                   "_ZnamSt11align_val_t", "_ZnwjSt11align_val_t",
                   "_ZnajSt11align_val_t"}) &&
          call->getType()->isPointerTy()) {
        unsigned sizeIndex = isDirectCall(*call, "aligned_alloc") ? 1 : 0;
        if (call->arg_size() <= sizeIndex)
          continue;
        checkInitialized(
            before,
            lookupUninitialized(call->getArgOperand(sizeIndex), uninitialized),
            3, runtime);
        Instruction *returnPoint = callReturnPoint(*call);
        IRBuilder<> after(returnPoint);
        shadows[call] = after.CreateCall(
            runtime.registerExplicit,
            {asBytePointer(after, call, runtime.bytePtrType),
             ConstantInt::get(runtime.intPtrType, 1),
             asIntPtr(after, call->getArgOperand(sizeIndex),
                      runtime.intPtrType),
             after.getInt32(2), after.getInt64(0), after.getInt8(0)});
        continue;
      }
      if (isOneOf(*call, {"calloc", "__libc_calloc"}) &&
          call->arg_size() == 2 && call->getType()->isPointerTy()) {
        checkInitialized(
            before,
            combineUninitialized(
                before,
                {call->getArgOperand(0), call->getArgOperand(1)},
                uninitialized),
            3, runtime);
        Instruction *returnPoint = callReturnPoint(*call);
        IRBuilder<> after(returnPoint);
        shadows[call] = after.CreateCall(
            runtime.registerExplicit,
            {asBytePointer(after, call, runtime.bytePtrType),
             asIntPtr(after, call->getArgOperand(0), runtime.intPtrType),
             asIntPtr(after, call->getArgOperand(1), runtime.intPtrType),
             after.getInt32(2), after.getInt64(0), after.getInt8(1)});
        continue;
      }
      for (Use &argument : call->args()) {
        uint32_t index = argument.getOperandNo();
        before.CreateCall(runtime.setArgumentUninitialized,
                          {before.getInt32(index),
                           asUninitializedByte(
                               before,
                               lookupUninitialized(argument, uninitialized),
                               runtime)});
        if (argument->getType()->isPointerTy())
          before.CreateCall(runtime.setArgumentShadow,
                            {before.getInt32(index),
                             lookupShadow(argument, shadows, runtime)});
      }
      if (!call->getType()->isVoidTy()) {
        before.CreateCall(runtime.setReturnUninitialized, before.getInt8(0));
      }
      if (call->getType()->isPointerTy())
        before.CreateCall(runtime.setReturnShadow, zeroShadow(runtime));
      if (!call->getType()->isVoidTy()) {
        Instruction *returnPoint = callReturnPoint(*call);
        IRBuilder<> after(returnPoint);
        Value *returnUninitialized =
            after.CreateCall(runtime.getReturnUninitialized);
        uninitialized[call] =
            after.CreateICmpNE(returnUninitialized, after.getInt8(0));
        if (call->getType()->isPointerTy())
          shadows[call] = after.CreateCall(runtime.getReturnShadow);
      }
      continue;
    }

    if (auto *returnInstruction = dyn_cast<ReturnInst>(instruction)) {
      Value *returnValue = returnInstruction->getReturnValue();
      if (returnValue != nullptr) {
        IRBuilder<> IRB(returnInstruction);
        IRB.CreateCall(
            runtime.setReturnUninitialized,
            asUninitializedByte(
                IRB, lookupUninitialized(returnValue, uninitialized), runtime));
        if (returnValue->getType()->isPointerTy())
          IRB.CreateCall(runtime.setReturnShadow,
                         lookupShadow(returnValue, shadows, runtime));
      }
      IRBuilder<> IRB(returnInstruction);
      IRB.CreateCall(runtime.popFrame, frame);
      continue;
    }
    if (isa<ResumeInst>(instruction)) {
      IRBuilder<> IRB(instruction);
      IRB.CreateCall(runtime.popFrame, frame);
      continue;
    }
  }

  for (const auto &entry : shadowPhis) {
    PHINode *original = entry.first;
    PHINode *shadow = entry.second;
    for (unsigned index = 0; index < original->getNumIncomingValues();
         ++index)
      shadow->addIncoming(
          lookupShadow(original->getIncomingValue(index), shadows, runtime),
          original->getIncomingBlock(index));
  }
  for (const auto &entry : uninitializedPhis) {
    PHINode *original = entry.first;
    PHINode *shadow = entry.second;
    for (unsigned index = 0; index < original->getNumIncomingValues();
         ++index)
      shadow->addIncoming(
          lookupUninitialized(original->getIncomingValue(index), uninitialized),
          original->getIncomingBlock(index));
  }
}

} // namespace

bool instrumentUCSan(Module &M) {
  UCSanConfig config = loadConfig();
  if (config.entry.empty())
    return false;

  Function *entry = M.getFunction(config.entry);
  if (entry == nullptr || entry->isDeclaration()) {
    errs() << "SymCC UCSan: configured entry function was not found: "
           << config.entry << "\n";
    report_fatal_error("invalid UCSan entry function");
  }
  if (entry->isVarArg()) {
    errs() << "SymCC UCSan: variadic entry functions are unsupported: "
           << entry->getName() << "\n";
    report_fatal_error("invalid UCSan entry function");
  }

  Function *originalMain = M.getFunction("main");
  if (originalMain != nullptr) {
    if (originalMain == entry)
      originalMain->setName("__symcc_ucsan_entry_main");
    else
      originalMain->setName("__symcc_ucsan_original_main");
  }

  std::unordered_set<Function *> scope;
  for (Function &function : M) {
    if (function.isDeclaration())
      continue;
    if (&function == entry ||
        config.scope.count(function.getName().str()) != 0)
      scope.insert(&function);
  }
  UCSanRuntime runtime(M);
  rewriteExternalCalls(M, scope, config, runtime);
  if (createHarness(M, *entry, runtime) == nullptr)
    report_fatal_error("failed to create UCSan harness");
  for (Function *function : scope)
    instrumentScopedFunction(*function, runtime, scope);

  if (verifyModule(M, &errs())) {
    errs() << "SymCC UCSan produced invalid bitcode\n";
    report_fatal_error("invalid UCSan instrumentation");
  }
  return true;
}
