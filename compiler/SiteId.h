// This file is part of SymCC.

#ifndef SYMCC_SITE_ID_H
#define SYMCC_SITE_ID_H

#include <llvm/IR/Argument.h>
#include <llvm/IR/BasicBlock.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/GlobalValue.h>
#include <llvm/IR/Instruction.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>

#include <cstdint>
#include <string>

namespace symcc {

inline uint64_t mixSiteIdByte(uint64_t hash, unsigned char value) {
  hash ^= static_cast<uint64_t>(value);
  return hash * 1099511628211ULL;
}

inline uint64_t mixSiteIdText(uint64_t hash, llvm::StringRef value) {
  for (unsigned char byte : value)
    hash = mixSiteIdByte(hash, byte);
  return mixSiteIdByte(hash, 0xff);
}

inline uint64_t mixSiteIdInteger(uint64_t hash, uint64_t value) {
  for (unsigned shift = 0; shift < 64; shift += 8)
    hash = mixSiteIdByte(
        hash, static_cast<unsigned char>((value >> shift) & 0xff));
  return hash;
}

inline llvm::StringRef stableModuleIdentity(const llvm::Module &module) {
  llvm::StringRef source = module.getSourceFileName();
  return source.empty() ? llvm::StringRef(module.getModuleIdentifier())
                        : source;
}

inline uint64_t stableModuleId(const llvm::Module &module) {
  return mixSiteIdText(
      1469598103934665603ULL, stableModuleIdentity(module));
}

inline uint64_t maskSiteId(const llvm::Module &module, uint64_t value) {
  const unsigned bits = module.getDataLayout().getPointerSizeInBits();
  if (bits > 0 && bits < 64)
    value &= (uint64_t{1} << bits) - 1;
  return value == 0 ? 1 : value;
}

inline uint64_t computeStableSiteId(const llvm::Value &value) {
  const llvm::Module *module = nullptr;
  const llvm::Function *function = nullptr;
  uint64_t blockIndex = 0;
  uint64_t valueIndex = 0;
  uint64_t tag = 0;
  uint64_t detail = 0;

  if (const auto *instruction = llvm::dyn_cast<llvm::Instruction>(&value)) {
    function = instruction->getFunction();
    module = instruction->getModule();
    tag = 1;
    detail = instruction->getOpcode();
    for (const llvm::BasicBlock &block : *function) {
      if (&block == instruction->getParent())
        break;
      ++blockIndex;
    }
    for (const llvm::Instruction &candidate : *instruction->getParent()) {
      if (&candidate == instruction)
        break;
      ++valueIndex;
    }
  } else if (const auto *block = llvm::dyn_cast<llvm::BasicBlock>(&value)) {
    function = block->getParent();
    module = function == nullptr ? nullptr : function->getParent();
    tag = 2;
    if (function != nullptr) {
      for (const llvm::BasicBlock &candidate : *function) {
        if (&candidate == block)
          break;
        ++blockIndex;
      }
    }
  } else if (const auto *argument = llvm::dyn_cast<llvm::Argument>(&value)) {
    function = argument->getParent();
    module = function == nullptr ? nullptr : function->getParent();
    tag = 3;
    valueIndex = argument->getArgNo();
  } else if (const auto *global = llvm::dyn_cast<llvm::GlobalValue>(&value)) {
    module = global->getParent();
    tag = 4;
  } else {
    tag = 5;
  }

  uint64_t hash = 1469598103934665603ULL;
  if (module != nullptr)
    hash = mixSiteIdText(hash, stableModuleIdentity(*module));
  hash = mixSiteIdInteger(hash, tag);
  if (function != nullptr)
    hash = mixSiteIdText(hash, function->getName());
  else if (const auto *global = llvm::dyn_cast<llvm::GlobalValue>(&value))
    hash = mixSiteIdText(hash, global->getName());
  hash = mixSiteIdInteger(hash, blockIndex);
  hash = mixSiteIdInteger(hash, valueIndex);
  hash = mixSiteIdInteger(hash, detail);
  return module == nullptr ? (hash == 0 ? 1 : hash)
                           : maskSiteId(*module, hash);
}

inline uint64_t stableSiteId(const llvm::Value &value) {
  if (const auto *instruction = llvm::dyn_cast<llvm::Instruction>(&value)) {
    if (const llvm::MDNode *metadata =
            instruction->getMetadata("symcc.site_id")) {
      if (metadata->getNumOperands() == 1) {
        if (const auto *constant =
                llvm::mdconst::dyn_extract<llvm::ConstantInt>(
                    metadata->getOperand(0)))
          return constant->getZExtValue();
      }
    }
  }
  return computeStableSiteId(value);
}

inline void initializeStableSiteIds(llvm::Module &module) {
  llvm::LLVMContext &context = module.getContext();
  for (llvm::Function &function : module) {
    if (function.isDeclaration())
      continue;
    for (llvm::BasicBlock &block : function) {
      for (llvm::Instruction &instruction : block) {
        if (instruction.getMetadata("symcc.site_id") != nullptr)
          continue;
        const uint64_t id = computeStableSiteId(instruction);
        llvm::Metadata *operand = llvm::ConstantAsMetadata::get(
            llvm::ConstantInt::get(llvm::Type::getInt64Ty(context), id));
        instruction.setMetadata(
            "symcc.site_id", llvm::MDNode::get(context, operand));
      }
    }
  }
}

} // namespace symcc

#endif
