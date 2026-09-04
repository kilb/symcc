// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.

#include "ContinuationLowering.h"

#include "SiteId.h"

#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/MapVector.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/Analysis/AliasAnalysis.h>
#include <llvm/Analysis/ConstantFolding.h>
#include <llvm/Analysis/LoopInfo.h>
#include <llvm/Analysis/MemorySSA.h>
#include <llvm/Analysis/ValueTracking.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Attributes.h>
#include <llvm/IR/ConstantRange.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Operator.h>
#include <llvm/IR/Module.h>
#include <llvm/Support/ErrorHandling.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/FormatVariadic.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/MathExtras.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Transforms/Utils/PromoteMemToReg.h>

#include <algorithm>
#include <array>
#include <cstdlib>
#include <deque>
#include <functional>
#include <iterator>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr char kProgramSchema[] = "symcc-live-program-v1";
constexpr char kReportSchema[] = "symcc-llvm-continuation-lowering-v1";

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

unsigned integerBits(Type *type) {
  auto *integer = dyn_cast_or_null<IntegerType>(type);
  if (integer == nullptr)
    return 0;
  unsigned bits = integer->getBitWidth();
  return bits <= 64 ? bits : 0;
}

json::Object variableOperand(StringRef name) {
  json::Object result;
  result["var"] = name.str();
  return result;
}

json::Object constantOperand(const ConstantInt &constant) {
  json::Object result;
  result["const"] = constant.getValue().getSExtValue();
  result["bits"] = static_cast<int64_t>(constant.getBitWidth());
  return result;
}

json::Object integerConstant(int64_t value, unsigned bits) {
  json::Object result;
  result["const"] = value;
  result["bits"] = static_cast<int64_t>(bits);
  return result;
}

uint64_t fixedAllocBytes(const DataLayout &layout, Type *type) {
#if LLVM_VERSION_MAJOR >= 11
  return layout.getTypeAllocSize(type).getFixedValue();
#else
  return layout.getTypeAllocSize(type);
#endif
}

uint64_t fixedStoreBytes(const DataLayout &layout, Type *type) {
#if LLVM_VERSION_MAJOR >= 11
  return layout.getTypeStoreSize(type).getFixedValue();
#else
  return layout.getTypeStoreSize(type);
#endif
}

__int128 floorSignedDivision(__int128 dividend, __int128 divisor) {
  __int128 quotient = dividend / divisor;
  __int128 remainder = dividend % divisor;
  if (remainder != 0 && ((remainder < 0) != (divisor < 0)))
    --quotient;
  return quotient;
}

__int128 ceilSignedDivision(__int128 dividend, __int128 divisor) {
  return -floorSignedDivision(-dividend, divisor);
}

std::string bytesToHex(ArrayRef<uint8_t> bytes) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result;
  result.reserve(bytes.size() * 2);
  for (uint8_t byte : bytes) {
    result.push_back(digits[byte >> 4]);
    result.push_back(digits[byte & 0x0f]);
  }
  return result;
}

struct StaticMemoryObject {
  uint64_t address = 0;
  uint64_t size = 0;
  bool readOnly = false;
};

struct HeapPool {
  std::string site;
  std::string allocator;
  uint64_t objectSize = 0;
  unsigned sizeBits = 0;
  bool nullable = false;
  std::vector<StaticMemoryObject> slots;
};

struct StaticPointer {
  uint64_t address = 0;
  uint64_t objectSize = 0;
  int64_t objectOffset = 0;
  bool readOnly = false;
  const AllocaInst *stackObject = nullptr;
  const CallBase *heapObject = nullptr;
  Value *dynamicIndex = nullptr;
  int64_t dynamicScale = 0;
  unsigned dynamicIndexBits = 0;
  uint64_t dynamicObjectAddress = 0;
  int64_t dynamicMinimumIndex = INT64_MIN;
  int64_t dynamicMaximumIndex = INT64_MAX;
  bool interprocedural = false;
  const CallBase *reallocationObject = nullptr;
};

struct MemoryAliases {
  std::vector<uint64_t> addresses;
  std::vector<int64_t> indices;
  int64_t minimumIndex = 0;
  int64_t maximumIndex = 0;
};

struct PointerGuard {
  const Value *condition = nullptr;
  std::string variable;
  int64_t expected = 0;
  unsigned bits = 1;
};

struct PointerAlternative {
  StaticPointer pointer;
  std::vector<PointerGuard> guards;
};

struct GuardedHeapInitializationDecision {
  const BranchInst *branch = nullptr;
  bool expected = false;
};

struct GuardedHeapInitializationPath {
  std::vector<const BasicBlock *> blocks;
  std::vector<GuardedHeapInitializationDecision> decisions;
  const BasicBlock *predecessor = nullptr;
  const StoreInst *store = nullptr;
  uint64_t base = 0;
  uint64_t loadAddress = 0;
  uint64_t storeAddress = 0;
  uint64_t storeBytes = 0;
  unsigned storeOrdinal = 0;
};

struct GuardedHeapInitializationCertificate {
  const BasicBlock *root = nullptr;
  const BasicBlock *merge = nullptr;
  unsigned depth = 0;
  std::vector<GuardedHeapInitializationPath> paths;
};

struct MemorySSAHeapInitializationSkip {
  const Instruction *instruction = nullptr;
  std::string proof;
  unsigned ordinal = 0;
};

struct MemorySSAHeapInitializationIncoming {
  const BasicBlock *block = nullptr;
  unsigned node = 0;
};

struct MemorySSAHeapInitializationNode {
  enum class Kind { Phi, Store };

  Kind kind = Kind::Store;
  const BasicBlock *block = nullptr;
  std::vector<MemorySSAHeapInitializationIncoming> incoming;
  const StoreInst *store = nullptr;
  uint64_t base = 0;
  uint64_t loadAddress = 0;
  uint64_t storeAddress = 0;
  uint64_t storeBytes = 0;
  unsigned storeOrdinal = 0;
  std::vector<MemorySSAHeapInitializationSkip> skipped;
};

struct MemorySSAHeapInitializationCertificate {
  const BasicBlock *merge = nullptr;
  unsigned rootNode = 0;
  uint64_t loadBytes = 0;
  std::vector<MemorySSAHeapInitializationNode> nodes;
};

struct InterproceduralHeapEffectCertificate {
  enum class Kind { ReturnedAllocation, ArgumentInitializer };

  Kind kind = Kind::ArgumentInitializer;
  const CallInst *call = nullptr;
  const Function *callee = nullptr;
  const CallBase *allocation = nullptr;
  const StoreInst *store = nullptr;
  const ReturnInst *returnInstruction = nullptr;
  unsigned parameterIndex = 0;
  uint64_t base = 0;
  uint64_t loadAddress = 0;
  uint64_t loadBytes = 0;
  uint64_t storeAddress = 0;
  uint64_t storeBytes = 0;
  bool zeroInitialize = false;
  std::vector<MemorySSAHeapInitializationSkip> skipped;
};

struct DynamicByteLaneCoverLane {
  uint64_t address = 0;
  unsigned lane = 0;
  uint64_t regionOffset = 0;
};

struct DynamicByteLaneCoverCertificate {
  const CallBase *writer = nullptr;
  std::string operation;
  uint64_t base = 0;
  uint64_t writerAddress = 0;
  uint64_t maximumBytes = 0;
  unsigned lengthBits = 0;
  const Value *length = nullptr;
  uint64_t loadBytes = 0;
  unsigned indexBits = 0;
  int64_t minimumIndex = 0;
  int64_t maximumIndex = 0;
  std::vector<uint64_t> loadAddresses;
  std::vector<int64_t> indexValues;
  std::vector<DynamicByteLaneCoverLane> lanes;
  std::vector<MemorySSAHeapInitializationSkip> skipped;
};

struct LoopMemoryPhiByteLaneWitness {
  uint64_t loadAddress = 0;
  unsigned lane = 0;
  uint64_t writerAddress = 0;
  unsigned writerLane = 0;
  int64_t inductionValue = 0;
};

struct LoopMemoryPhiByteLaneCertificate {
  const BasicBlock *header = nullptr;
  const BasicBlock *preheader = nullptr;
  const BasicBlock *body = nullptr;
  const BasicBlock *writerBlock = nullptr;
  const BasicBlock *skipBlock = nullptr;
  const BasicBlock *latch = nullptr;
  const BasicBlock *exit = nullptr;
  const PHINode *induction = nullptr;
  const BinaryOperator *step = nullptr;
  const ICmpInst *guard = nullptr;
  const ICmpInst *writerGuard = nullptr;
  bool writerGuardExpected = true;
  const Value *bound = nullptr;
  const StoreInst *writer = nullptr;
  uint64_t base = 0;
  uint64_t loadBytes = 0;
  unsigned inductionBits = 0;
  int64_t seed = 0;
  int64_t stepValue = 1;
  unsigned writerOrdinal = 0;
  unsigned writerBytes = 0;
  int64_t writerScale = 1;
  uint64_t writerAddressStride = 1;
  int64_t writerMinimumIndex = 0;
  int64_t writerMaximumIndex = 0;
  std::vector<uint64_t> writerAddresses;
  std::vector<int64_t> writerIndexValues;
  std::vector<uint64_t> reachableWriterAddresses;
  std::vector<int64_t> reachableWriterIndexValues;
  unsigned loadIndexBits = 0;
  int64_t loadMinimumIndex = 0;
  int64_t loadMaximumIndex = 0;
  std::vector<uint64_t> loadAddresses;
  std::vector<int64_t> loadIndexValues;
  std::vector<LoopMemoryPhiByteLaneWitness> witnesses;

  bool isStrided() const {
    return stepValue != 1 || writerScale != 1 || writerBytes != 1;
  }

  bool isConditional() const { return writerGuard != nullptr; }
};

struct MultiLatchLoopMemoryPhiGuard {
  const ICmpInst *comparison = nullptr;
  bool expected = false;
};

struct NestedLoopMemoryPhiWriterInstance {
  uint64_t address = 0;
  int64_t indexValue = 0;
  int64_t outerInductionValue = 0;
  int64_t innerInductionValue = 0;
};

struct NestedLoopMemoryPhiAffineWriterValue {
  const Value *operand = nullptr;
  const Argument *input = nullptr;
  unsigned bits = 0;
  uint64_t inputOffset = 0;
  unsigned inputBytes = 0;
  int64_t constant = 0;
  int64_t outerScale = 0;
  int64_t innerScale = 0;
  int64_t inputScale = 0;
};

struct NestedLoopMemoryPhiPiecewiseWriterValue {
  const SelectInst *selection = nullptr;
  const ICmpInst *guard = nullptr;
  bool guardUsesOuterInduction = false;
  bool guardConstantOnLeft = false;
  int64_t guardConstant = 0;
  NestedLoopMemoryPhiAffineWriterValue whenTrue;
  NestedLoopMemoryPhiAffineWriterValue whenFalse;
};

struct NestedLoopMemoryPhiDecisionDagNode {
  enum class Kind {
    Affine,
    Guard,
  };

  Kind kind = Kind::Affine;
  const Value *operand = nullptr;
  NestedLoopMemoryPhiAffineWriterValue affine;
  const ICmpInst *guard = nullptr;
  bool guardUsesOuterInduction = false;
  bool guardConstantOnLeft = false;
  int64_t guardConstant = 0;
  unsigned whenTrue = 0;
  unsigned whenFalse = 0;
};

struct NestedLoopMemoryPhiDecisionDagWriterValue {
  const Value *rootOperand = nullptr;
  unsigned root = 0;
  unsigned depth = 0;
  std::vector<NestedLoopMemoryPhiDecisionDagNode> nodes;
};

struct MultiLatchLoopMemoryPhiWriter {
  StoreInst *instruction = nullptr;
  unsigned ordinal = 0;
  unsigned bytes = 0;
  unsigned constantValueBits = 0;
  int64_t constantValue = 0;
  std::vector<uint8_t> constantValueBytes;
  const Value *affineValue = nullptr;
  const Argument *affineValueInput = nullptr;
  unsigned affineValueBits = 0;
  uint64_t affineValueInputOffset = 0;
  unsigned affineValueInputBytes = 0;
  int64_t affineValueConstant = 0;
  int64_t affineValueOuterScale = 0;
  int64_t affineValueInnerScale = 0;
  int64_t affineValueInputScale = 0;
  std::optional<NestedLoopMemoryPhiPiecewiseWriterValue> piecewiseValue;
  std::optional<NestedLoopMemoryPhiDecisionDagWriterValue> decisionDagValue;
  int64_t scale = 1;
  uint64_t addressStride = 1;
  int64_t minimumIndex = 0;
  int64_t maximumIndex = 0;
  std::vector<uint64_t> addresses;
  std::vector<int64_t> indexValues;
  std::vector<uint64_t> reachableAddresses;
  std::vector<int64_t> reachableIndexValues;
  const Value *affineIndex = nullptr;
  unsigned affineIndexBits = 0;
  int64_t affineConstant = 0;
  int64_t affineOuterScale = 0;
  int64_t affineInnerScale = 0;
  int64_t pointerScale = 0;
  uint64_t pointerBase = 0;
  std::vector<NestedLoopMemoryPhiWriterInstance> affineInstances;

  bool isStrided(int64_t stepValue) const {
    if (affineIndex != nullptr)
      return stepValue != 1 || pointerScale != 1 ||
             affineInnerScale != 1 || bytes != 1;
    return stepValue != 1 || scale != 1 || bytes != 1;
  }
};

struct MultiLatchLoopMemoryPhiTransfer {
  BasicBlock *latch = nullptr;
  BinaryOperator *step = nullptr;
  std::vector<MultiLatchLoopMemoryPhiWriter> writers;
  std::vector<MultiLatchLoopMemoryPhiGuard> guards;

  bool isWriter() const { return !writers.empty(); }
  bool isStrided(int64_t stepValue) const {
    return std::any_of(
        writers.begin(), writers.end(),
        [&](const MultiLatchLoopMemoryPhiWriter &writer) {
          return writer.isStrided(stepValue);
        });
  }
};

struct MultiLatchLoopMemoryPhiAlternative {
  unsigned transfer = 0;
  unsigned writer = 0;
  uint64_t writerAddress = 0;
  unsigned writerLane = 0;
  int64_t inductionValue = 0;
  int64_t outerInductionValue = 0;
};

struct MultiLatchLoopMemoryPhiWitness {
  uint64_t loadAddress = 0;
  unsigned lane = 0;
  std::vector<MultiLatchLoopMemoryPhiAlternative> alternatives;
};

struct MultiLatchLoopMemoryPhiCertificate {
  bool nestedSummary = false;
  bool nestedValueSummary = false;
  bool nestedTwoDimensionalSummary = false;
  bool nestedSymbolicValueSummary = false;
  bool nestedPiecewiseValueSummary = false;
  bool nestedDecisionDagValueSummary = false;
  bool executableMemoryTransfer = false;
  const AllocaInst *executableStackObject = nullptr;
  const BasicBlock *header = nullptr;
  const BasicBlock *preheader = nullptr;
  const BasicBlock *root = nullptr;
  const BasicBlock *exit = nullptr;
  const PHINode *induction = nullptr;
  const ICmpInst *guard = nullptr;
  const Value *bound = nullptr;
  const BasicBlock *innerPreheader = nullptr;
  const BasicBlock *innerHeader = nullptr;
  const BasicBlock *innerBody = nullptr;
  const BasicBlock *outerLatch = nullptr;
  const PHINode *innerInduction = nullptr;
  const BinaryOperator *outerStep = nullptr;
  const BinaryOperator *innerStep = nullptr;
  const ICmpInst *innerGuard = nullptr;
  const Value *innerBound = nullptr;
  uint64_t base = 0;
  uint64_t loadBytes = 0;
  unsigned inductionBits = 0;
  unsigned innerInductionBits = 0;
  int64_t seed = 0;
  int64_t stepValue = 1;
  int64_t innerStepValue = 1;
  unsigned loadIndexBits = 0;
  int64_t loadMinimumIndex = 0;
  int64_t loadMaximumIndex = 0;
  std::vector<const ICmpInst *> decisions;
  std::vector<MultiLatchLoopMemoryPhiTransfer> transfers;
  std::vector<int64_t> closureDomain;
  std::vector<std::pair<int64_t, int64_t>> closurePairs;
  std::vector<unsigned> roundNewLanes;
  std::vector<unsigned> roundTotalLanes;
  std::vector<uint64_t> finalLanes;
  std::vector<uint64_t> loadAddresses;
  std::vector<int64_t> loadIndexValues;
  std::vector<MultiLatchLoopMemoryPhiWitness> witnesses;

  bool isStrided() const {
    int64_t writerStep = nestedSummary ? innerStepValue : stepValue;
    return std::any_of(
        transfers.begin(), transfers.end(),
        [&](const MultiLatchLoopMemoryPhiTransfer &transfer) {
          return transfer.isStrided(writerStep);
        });
  }

  bool hasOrderedWriterTransfer() const {
    return std::any_of(
        transfers.begin(), transfers.end(),
        [](const MultiLatchLoopMemoryPhiTransfer &transfer) {
          return transfer.writers.size() > 1;
        });
  }
};

struct ExecutableNestedLoopMemoryTransfer {
  MultiLatchLoopMemoryPhiCertificate certificate;
  std::string sourceLoad;
};

struct SymbolicRegionEffect {
  std::string operation;
  Value *destination = nullptr;
  Value *sourceOrValue = nullptr;
  Value *length = nullptr;
};

struct ConstantScalarMemoryRegionGuard {
  const Value *condition = nullptr;
  bool expected = false;
  const BasicBlock *phiBlock = nullptr;
  const BasicBlock *phiPredecessor = nullptr;
};

struct ConstantScalarMemoryRegion {
  const Value *base = nullptr;
  int64_t offset = 0;
  int64_t maximumOffset = 0;
  std::vector<ConstantScalarMemoryRegionGuard> guards;
};

struct FunctionAlternative {
  Function *function = nullptr;
  std::vector<PointerGuard> guards;
};

struct FunctionContext {
  struct MemoryDefinednessTreeNode {
    enum class Kind {
      Store,
      Carry,
      Select,
    };

    Kind kind = Kind::Carry;
    const StoreInst *store = nullptr;
    const Value *condition = nullptr;
    int trueNode = -1;
    int falseNode = -1;

    bool operator==(
        const MemoryDefinednessTreeNode &other) const {
      return kind == other.kind && store == other.store &&
             condition == other.condition &&
             trueNode == other.trueNode &&
             falseNode == other.falseNode;
    }
  };

  struct MemoryPoisonIncoming {
    enum class Kind {
      Store,
      Initial,
      Carry,
      ConditionalStoreCarry,
      NestedConditionalStoresCarry,
      RecursiveConditionalTree,
    };

    const BasicBlock *block = nullptr;
    const StoreInst *store = nullptr;
    Kind kind = Kind::Store;
    const Value *condition = nullptr;
    bool storeWhenTrue = false;
    bool conditionalForwarded = false;
    bool multiArmConditional = false;
    bool equivalentDefinedStores = false;
    bool sharedPoisonStores = false;
    std::vector<const StoreInst *> equivalentStores;
    const StoreInst *secondaryStore = nullptr;
    const Value *innerCondition = nullptr;
    bool firstStoreWhenTrue = false;
    bool nestedConditional = false;
    std::vector<MemoryDefinednessTreeNode> conditionTree;
    int conditionTreeRoot = -1;
    unsigned conditionTreeDepth = 0;
    bool groupedRecursiveConditional = false;
    bool repeatedSourceRecursiveConditional = false;
    bool multiCarryRecursiveConditional = false;
  };

  struct MemoryPoisonMerge {
    const LoadInst *load = nullptr;
    std::string defined;
    bool interprocedural = false;
    bool multilevel = false;
    bool cyclic = false;
    bool initial = false;
    bool initialSubobject = false;
    bool carry = false;
    bool conditionalCarry = false;
    bool forwardedConditionalCarry = false;
    bool multiArmConditionalCarry = false;
    bool equivalentDefinedStoreCarry = false;
    bool sharedPoisonStoreCarry = false;
    bool nestedConditionalCarry = false;
    bool recursiveConditionalCarry = false;
    unsigned conditionTreeDepth = 0;
    unsigned conditionTreeLeaves = 0;
    unsigned conditionTreeCarryLeaves = 0;
    bool groupedRecursiveConditionalCarry = false;
    bool repeatedSourceRecursiveConditionalCarry = false;
    bool multiCarryRecursiveConditionalCarry = false;
    bool multiCell = false;
    bool identifiedObjectMultiCell = false;
    bool fixedHeapObjectMultiCell = false;
    bool finitePointerDomainMultiCell = false;
    bool guardCorrelatedPointerDomainMultiCell = false;
    bool phiCorrelatedPointerDomainMultiCell = false;
    bool symbolicIndexIntervalMultiCell = false;
    std::vector<const LoadInst *> aliasGraphNeighbors;
    std::vector<MemoryPoisonIncoming> incoming;
  };

  struct ByteLaneMemorySource {
    const StoreInst *store = nullptr;
    unsigned storeByte = 0;
  };

  struct ByteLaneMemoryComposition {
    const LoadInst *load = nullptr;
    std::string defined;
    uint64_t bytes = 0;
    bool initial = false;
    bool crossBlock = false;
    std::vector<ByteLaneMemorySource> lanes;
  };

  struct ByteLaneMemoryIncoming {
    const BasicBlock *block = nullptr;
    bool initial = false;
    std::vector<ByteLaneMemorySource> lanes;
  };

  struct ByteLaneMemoryPhi {
    const LoadInst *load = nullptr;
    std::string defined;
    uint64_t bytes = 0;
    std::vector<ByteLaneMemoryIncoming> incoming;
  };

  struct CyclicByteLaneMemorySource {
    enum class Kind {
      Store,
      Initial,
      Carry,
    };

    Kind kind = Kind::Initial;
    const StoreInst *store = nullptr;
    unsigned storeByte = 0;
  };

  struct CyclicByteLaneMemoryIncoming {
    const BasicBlock *block = nullptr;
    std::vector<CyclicByteLaneMemorySource> lanes;
  };

  struct ConditionalCyclicByteLaneTransfer {
    const BranchInst *branch = nullptr;
    const BasicBlock *storeArm = nullptr;
    const BasicBlock *carryArm = nullptr;
    const BasicBlock *storeSuccessor = nullptr;
    const BasicBlock *carrySuccessor = nullptr;
    const BasicBlock *join = nullptr;
    const StoreInst *store = nullptr;
    bool storeWhenTrue = false;
    bool forwarded = false;
    std::vector<CyclicByteLaneMemorySource> lanes;
  };

  struct MultiArmCyclicByteLaneArm {
    enum class Route {
      Root,
      InnerTrue,
      InnerFalse,
    };

    Route route = Route::Root;
    const BasicBlock *block = nullptr;
    const BasicBlock *successor = nullptr;
    const StoreInst *store = nullptr;
    std::vector<CyclicByteLaneMemorySource> lanes;
  };

  struct MultiArmCyclicByteLaneTransfer {
    const BranchInst *rootBranch = nullptr;
    const BranchInst *innerBranch = nullptr;
    const BasicBlock *join = nullptr;
    bool forwarded = false;
    std::vector<MultiArmCyclicByteLaneArm> arms;
  };

  struct RecursiveCyclicByteLaneBranch {
    const BranchInst *branch = nullptr;
  };

  struct RecursiveCyclicByteLaneTransfer {
    const BranchInst *rootBranch = nullptr;
    const BasicBlock *join = nullptr;
    unsigned depth = 0;
    bool forwarded = false;
    bool grouped = false;
    bool repeatedSource = false;
    bool composedRepeatedSource = false;
    bool multiCarry = false;
    unsigned carryLeaves = 0;
    bool multipleGroups = false;
    bool tripleGroups = false;
    unsigned groupCount = 0;
    bool mixedGroups = false;
    bool doubleComposedGroups = false;
    bool composedTripleGroups = false;
    unsigned composedGroupCount = 0;
    std::vector<RecursiveCyclicByteLaneBranch>
        branches;
    std::vector<MultiArmCyclicByteLaneArm> leaves;
  };

  struct CyclicByteLaneMemoryPhi {
    const LoadInst *load = nullptr;
    std::string defined;
    uint64_t bytes = 0;
    bool conditional = false;
    bool forwardedConditional = false;
    bool multiArmConditional = false;
    bool forwardedMultiArmConditional = false;
    bool recursiveConditional = false;
    bool forwardedRecursiveConditional = false;
    bool groupedRecursiveConditional = false;
    bool forwardedGroupedRecursiveConditional = false;
    bool repeatedSourceRecursiveConditional = false;
    bool forwardedRepeatedSourceRecursiveConditional = false;
    bool composedRepeatedSourceRecursiveConditional = false;
    bool forwardedComposedRepeatedSourceRecursiveConditional = false;
    bool multiCarryRecursiveConditional = false;
    bool forwardedMultiCarryRecursiveConditional = false;
    bool multipleGroupsRecursiveConditional = false;
    bool forwardedMultipleGroupsRecursiveConditional = false;
    bool tripleGroupsRecursiveConditional = false;
    bool forwardedTripleGroupsRecursiveConditional = false;
    bool mixedGroupsRecursiveConditional = false;
    bool forwardedMixedGroupsRecursiveConditional = false;
    bool doubleComposedGroupsRecursiveConditional = false;
    bool forwardedDoubleComposedGroupsRecursiveConditional = false;
    bool composedTripleGroupsRecursiveConditional = false;
    bool forwardedComposedTripleGroupsRecursiveConditional = false;
    std::vector<std::string> laneDefined;
    std::vector<CyclicByteLaneMemoryIncoming> incoming;
    std::vector<ConditionalCyclicByteLaneTransfer>
        conditionalTransfers;
    std::vector<MultiArmCyclicByteLaneTransfer>
        multiArmTransfers;
    std::vector<RecursiveCyclicByteLaneTransfer>
        recursiveTransfers;
  };

  Function &function;
  DominatorTree dominators;
  AAResults *aliasAnalysis = nullptr;
  MemorySSA *memorySSA = nullptr;
  DenseMap<const Value *, std::string> values;
  DenseMap<const BasicBlock *, std::string> blocks;
  DenseMap<const BasicBlock *, unsigned> blockIds;
  std::map<std::pair<const BasicBlock *, const BasicBlock *>, std::string>
      edgeBlocks;
  DenseMap<const PHINode *, std::string> pointerTags;
  DenseMap<const BasicBlock *, std::string> pointerBlockTags;
  DenseMap<const Value *, std::string> poisonConditions;
  DenseMap<const LoadInst *, unsigned> memoryPoisonMergeIndices;
  std::vector<MemoryPoisonMerge> memoryPoisonMerges;
  DenseMap<const LoadInst *, unsigned> byteLaneCompositionIndices;
  std::vector<ByteLaneMemoryComposition> byteLaneCompositions;
  DenseMap<const StoreInst *, std::string> byteLaneStoreIds;
  DenseMap<const StoreInst *, std::string> byteLaneStoreDefinedNames;
  DenseMap<const LoadInst *, unsigned> byteLanePhiIndices;
  std::vector<ByteLaneMemoryPhi> byteLanePhis;
  DenseMap<const LoadInst *, unsigned>
      cyclicByteLanePhiIndices;
  std::vector<CyclicByteLaneMemoryPhi>
      cyclicByteLanePhis;
  std::vector<ExecutableNestedLoopMemoryTransfer>
      executableNestedLoopMemoryTransfers;
  unsigned temporary = 0;

  explicit FunctionContext(
      Function &F, AAResults *AA = nullptr, MemorySSA *MSSA = nullptr)
      : function(F), dominators(F), aliasAnalysis(AA), memorySSA(MSSA) {
    unsigned argumentIndex = 0;
    for (Argument &argument : F.args())
      values[&argument] = "arg" + std::to_string(argumentIndex++);
    unsigned blockIndex = 0;
    unsigned valueIndex = 0;
    for (BasicBlock &block : F) {
      blockIds[&block] = blockIndex;
      blocks[&block] = "bb" + std::to_string(blockIndex++);
      for (Instruction &instruction : block)
        if (!instruction.getType()->isVoidTy())
          values[&instruction] = "v" + std::to_string(valueIndex++);
    }
    for (BasicBlock &block : F) {
      if (block.empty() || !isa<PHINode>(block.front()))
        continue;
      std::string pointerBlockTag;
      for (Instruction &instruction : block) {
        auto *phi = dyn_cast<PHINode>(&instruction);
        if (phi == nullptr)
          break;
        if (phi->getType()->isPointerTy()) {
          if (pointerBlockTag.empty()) {
            pointerBlockTag =
                blocks.lookup(&block) + "_pointer_edge_tag";
            pointerBlockTags[&block] = pointerBlockTag;
          }
          pointerTags[phi] = pointerBlockTag;
        }
      }
      for (BasicBlock *predecessor : predecessors(&block)) {
        std::string name =
            "edge_" + blocks.lookup(predecessor) + "_" + blocks.lookup(&block);
        edgeBlocks[{predecessor, &block}] = std::move(name);
      }
    }
  }

  std::string temporaryName(StringRef prefix) {
    return (prefix + Twine(temporary++)).str();
  }

  std::string edgeTarget(const BasicBlock *from,
                         const BasicBlock *to) const {
    auto found = edgeBlocks.find({from, to});
    return found == edgeBlocks.end() ? blocks.lookup(to) : found->second;
  }

  void ensureEdgeBlock(
      const BasicBlock *from, const BasicBlock *to) {
    if (edgeBlocks.count({from, to}) != 0)
      return;
    std::string name =
        "edge_" + blocks.lookup(from) + "_" + blocks.lookup(to);
    edgeBlocks[{from, to}] = std::move(name);
  }
};

struct LiveFunctionAnalyses {
  AAResults *aliasAnalysis = nullptr;
  MemorySSA *memorySSA = nullptr;
};

using LiveFunctionAnalysisProvider =
    std::function<LiveFunctionAnalyses(Function &)>;

bool promoteLiveScalarAllocas(Module &module) {
  bool changed = false;
  for (Function &function : module) {
    if (function.isDeclaration())
      continue;
    SmallVector<AllocaInst *, 16> allocas;
    for (Instruction &instruction : function.getEntryBlock()) {
      auto *alloca = dyn_cast<AllocaInst>(&instruction);
      if (alloca != nullptr && isAllocaPromotable(alloca))
        allocas.push_back(alloca);
    }
    if (allocas.empty())
      continue;
    DominatorTree dominators(function);
    PromoteMemToReg(allocas, dominators);
    changed = true;
  }
  return changed;
}

class ContinuationLowerer {
public:
  ContinuationLowerer(
      Module &module, StringRef entryName,
      LiveFunctionAnalysisProvider analysisProvider = {},
      bool promoteAllocas = true)
      : M(module), entryName(entryName.str()),
        analysisProvider(std::move(analysisProvider)),
        promoteAllocas(promoteAllocas) {
    const char *rawLimit = std::getenv("SYMCC_LIVE_MEMORY_LIMIT");
    if (rawLimit != nullptr && *rawLimit != '\0') {
      char *end = nullptr;
      unsigned long long parsed = std::strtoull(rawLimit, &end, 10);
      if (end != rawLimit && *end == '\0')
        memoryLimit = std::max<uint64_t>(
            64, std::min<uint64_t>(4 * 1024 * 1024, parsed));
    }
    const char *rawInputLimit =
        std::getenv("SYMCC_LIVE_INPUT_BUFFER_LIMIT");
    if (rawInputLimit != nullptr && *rawInputLimit != '\0' &&
        *rawInputLimit != '-') {
      char *end = nullptr;
      unsigned long long parsed = std::strtoull(rawInputLimit, &end, 10);
      if (end != rawInputLimit && *end == '\0' && parsed != 0)
        inputBufferLimit = std::min<uint64_t>(
            4 * 1024 * 1024, parsed);
    }
    const char *rawHeapLimit =
        std::getenv("SYMCC_LIVE_HEAP_OBJECT_LIMIT");
    if (rawHeapLimit != nullptr && *rawHeapLimit != '\0' &&
        *rawHeapLimit != '-') {
      char *end = nullptr;
      unsigned long long parsed = std::strtoull(rawHeapLimit, &end, 10);
      if (end != rawHeapLimit && *end == '\0' && parsed != 0)
        heapObjectLimit = std::min<uint64_t>(
            4 * 1024 * 1024, parsed);
    }
    const char *rawHeapCapacity =
        std::getenv("SYMCC_LIVE_HEAP_SITE_CAPACITY");
    if (rawHeapCapacity != nullptr && *rawHeapCapacity != '\0' &&
        *rawHeapCapacity != '-') {
      char *end = nullptr;
      unsigned long long parsed =
          std::strtoull(rawHeapCapacity, &end, 10);
      if (end != rawHeapCapacity && *end == '\0' && parsed != 0)
        heapSiteCapacity = std::min<uint64_t>(64, parsed);
    }
    dynamicHeapObjectLimit = std::min(
        dynamicHeapObjectLimit, heapObjectLimit);
    const char *rawDynamicHeapLimit =
        std::getenv("SYMCC_LIVE_DYNAMIC_HEAP_LIMIT");
    if (rawDynamicHeapLimit != nullptr &&
        *rawDynamicHeapLimit != '\0' &&
        *rawDynamicHeapLimit != '-') {
      char *end = nullptr;
      unsigned long long parsed =
          std::strtoull(rawDynamicHeapLimit, &end, 10);
      if (end != rawDynamicHeapLimit && *end == '\0' && parsed != 0)
        dynamicHeapObjectLimit = std::min<uint64_t>(
            heapObjectLimit, parsed);
    }
    const char *rawAliasLimit =
        std::getenv("SYMCC_LIVE_ALIAS_LIMIT");
    if (rawAliasLimit != nullptr && *rawAliasLimit != '\0' &&
        *rawAliasLimit != '-') {
      char *end = nullptr;
      unsigned long long parsed = std::strtoull(rawAliasLimit, &end, 10);
      if (end != rawAliasLimit && *end == '\0' && parsed != 0)
        aliasLimit = std::min<uint64_t>(256, parsed);
    }
  }

  bool run(json::Object &program, json::Object &report) {
    if (promoteAllocas)
      promoteLiveScalarAllocas(M);
    Function *entry = M.getFunction(entryName);
    if (entry == nullptr || entry->isDeclaration()) {
      errors.push_back("entry function '" + entryName +
                       "' is missing or is only a declaration");
      writeReport(report);
      return false;
    }
    collectReachable(*entry);
    for (Function *function : reachable) {
      for (Instruction &instruction : instructions(function)) {
        if (auto *call = dyn_cast<CallBase>(&instruction)) {
          if (isCxaThrowSummary(*call)) {
            programUsesExceptionObjectArena = true;
          } else if (isContinuationThrowSummary(*call) ||
                     isContinuationTypedThrowSummary(*call)) {
            programUsesScalarExceptionSummary = true;
          }
        }
      }
    }
    if (programUsesExceptionObjectArena &&
        programUsesScalarExceptionSummary) {
      errors.push_back(
          "reachable program mixes scalar exception summaries with "
          "exception-object throws");
      writeReport(report);
      return false;
    }
    std::set<Function *> activeCalls;
    std::set<Function *> completedCalls;
    if (hasReachableRecursion(*entry, activeCalls, completedCalls)) {
      errors.push_back(
          "reachable recursive call graph exceeds the portable continuation "
          "contract");
      writeReport(report);
      return false;
    }
    configureEntryInputAbi(*entry);
    json::Object functions;
    for (Function *function : reachable)
      lowerFunction(*function, function == entry, functions);

    if (!errors.empty()) {
      writeReport(report);
      return false;
    }

    program["schema"] = kProgramSchema;
    program["entry"] = entryName;
    program["input_size"] = static_cast<int64_t>(inputSize);
    program["memory_size"] = static_cast<int64_t>(initialMemory.size());
    if (!initialMemory.empty())
      program["memory_hex"] = bytesToHex(initialMemory);
    program["endianness"] =
        M.getDataLayout().isLittleEndian() ? "little" : "big";
    if (!memoryObjectsMetadata.empty())
      program["memory_objects"] = std::move(memoryObjectsMetadata);
    if (inputBufferAbi) {
      json::Object inputBuffer;
      inputBuffer["schema"] = "symcc-live-input-buffer-v1";
      inputBuffer["address"] =
          static_cast<int64_t>(inputMemoryObject.address);
      inputBuffer["capacity"] =
          static_cast<int64_t>(inputMemoryObject.size);
      inputBuffer["size_bits"] =
          static_cast<int64_t>(integerBits(inputSizeArgument->getType()));
      program["input_buffer"] = std::move(inputBuffer);
    }
    program["functions"] = std::move(functions);
    json::Object lowering;
    lowering["schema"] = kReportSchema;
    lowering["status"] = "lowered";
    lowering["source_module"] = M.getModuleIdentifier();
    lowering["entry"] = entryName;
    lowering["input_abi"] = inputBufferAbi
                                ? "pointer-size-symbolic-buffer"
                                : "integer-arguments-little-endian";
    lowering["llvm_version"] = LLVM_VERSION_STRING;
    json::Array capabilities;
    for (StringRef capability :
         {"integer-ssa", "scalar-mem2reg", "phi-edge-copies", "switch-chain",
          "direct-internal-calls", "bounded-bitvectors",
          "static-global-memory", "constant-gep", "multi-byte-memory"})
      capabilities.push_back(capability);
    if (inputBufferAbi)
      capabilities.push_back("pointer-size-input-buffer");
    if (!stackObjects.empty())
      capabilities.push_back("frame-local-stack");
    if (!heapObjects.empty())
      capabilities.push_back("bounded-heap-lifetime");
    if (usesHeapLifetimePointerUnions)
      capabilities.push_back("bounded-heap-lifetime-pointer-union");
    if (usesCollectiveHeapUnionInitialization)
      capabilities.push_back(
          "bounded-collective-heap-union-initialization");
    if (usesGuardCorrelatedHeapUnionInitialization)
      capabilities.push_back(
          "bounded-guard-correlated-heap-union-initialization");
    if (usesMemorySSAHeapInitialization)
      capabilities.push_back(
          "bounded-memoryssa-aa-heap-initialization");
    if (usesInterproceduralHeapEffects)
      capabilities.push_back(
          "bounded-interprocedural-heap-effect-summary");
    if (usesSymbolicRegionEffects)
      capabilities.push_back(
          "bounded-symbolic-length-region-effect");
    if (usesDynamicByteLaneCover)
      capabilities.push_back(
          "bounded-symbolic-length-byte-lane-cover");
    if (usesLoopMemoryPhiByteLaneInduction)
      capabilities.push_back(
          "bounded-loop-memoryphi-byte-lane-induction");
    if (usesStridedLoopMemoryPhiByteLaneInduction)
      capabilities.push_back(
          "bounded-strided-loop-memoryphi-byte-lane-induction");
    if (usesConditionalLoopMemoryPhiByteLaneInduction)
      capabilities.push_back(
          "bounded-conditional-loop-memoryphi-byte-lane-induction");
    if (usesMultiLatchLoopMemoryPhiFixedPoint)
      capabilities.push_back(
          "bounded-multilatch-loop-memoryphi-byte-lane-fixed-point");
    if (usesOrderedMultiLatchLoopMemoryPhiTransfer)
      capabilities.push_back(
          "bounded-multilatch-loop-memoryphi-ordered-writer-transfer");
    if (usesNestedLoopMemoryPhiSummaryComposition)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-summary-composition");
    if (usesNestedLoopMemoryPhiLastWriteValueSummary)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-last-write-value-summary");
    if (usesNestedLoopMemoryPhiTwoDimensionalAffineSummary)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-two-dimensional-affine-summary");
    if (usesNestedLoopMemoryPhiAffineSymbolicValueSummary)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-affine-symbolic-value-summary");
    if (usesNestedLoopMemoryPhiPiecewiseAffineValueSummary)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-piecewise-affine-value-summary");
    if (usesNestedLoopMemoryPhiDecisionDagValueSummary)
      capabilities.push_back(
          "bounded-nested-loop-memoryphi-decision-dag-value-summary");
    if (usesExecutableNestedLoopMemoryTransfer)
      capabilities.push_back(
          "refinement-verified-nested-loop-memory-summary-transfer");
    if (!heapObjects.empty())
      capabilities.push_back("bounded-multi-instance-heap");
    if (usesNullableHeap) {
      capabilities.push_back("bounded-nullable-heap");
      capabilities.push_back("runtime-sized-heap");
    }
    if (usesReallocHeap)
      capabilities.push_back("bounded-in-place-realloc");
    if (usesBoundedAliases)
      capabilities.push_back("bounded-symbolic-alias");
    if (usesPointerUnions)
      capabilities.push_back("bounded-pointer-union");
    if (usesSharedPhiEdgeDiscriminator)
      capabilities.push_back("bounded-shared-phi-edge-discriminator");
    if (usesPointerMemory)
      capabilities.push_back("bounded-pointer-memory");
    if (usesFunctionPointerMemory)
      capabilities.push_back("bounded-function-pointer-memory");
    if (usesPointerTables)
      capabilities.push_back("bounded-pointer-table");
    if (usesPointerMemoryMerges)
      capabilities.push_back("bounded-pointer-memory-merge");
    if (usesSymbolicPointerMemory)
      capabilities.push_back("bounded-symbolic-pointer-memory");
    if (usesPointerInitialDefinitionMerge)
      capabilities.push_back(
          "pointer-initial-definition-merge");
    if (usesAcyclicPointerMemorySsa)
      capabilities.push_back("acyclic-pointer-memory-ssa");
    if (usesCyclicPointerMemorySsa)
      capabilities.push_back("bounded-cyclic-pointer-memory-ssa");
    if (usesCanonicalPointerCells)
      capabilities.push_back("canonical-pointer-cell-identity");
    if (usesCrossFunctionPointers)
      capabilities.push_back("bounded-cross-function-pointer");
    if (usesCrossFunctionDomains)
      capabilities.push_back("caller-domain-pointer-certificate");
    if (usesIndirectCalls)
      capabilities.push_back("bounded-indirect-call-dispatch");
    if (usesNoUnwindInvokes)
      capabilities.push_back("bounded-nounwind-invoke");
    if (usesCleanupExceptions)
      capabilities.push_back("bounded-cleanup-exception-unwind");
    if (usesTypedExceptions)
      capabilities.push_back("bounded-typed-exception-matching");
    if (usesExceptionCatchLifecycle)
      capabilities.push_back("bounded-exception-catch-lifecycle");
    if (usesTrivialScalarCatchObjects)
      capabilities.push_back("bounded-trivial-scalar-catch-object");
    if (usesExceptionObjectArena)
      capabilities.push_back("bounded-exception-object-arena");
    if (usesExceptionObjectFields)
      capabilities.push_back("bounded-exception-object-fields");
    if (usesDeclarativePureExternalSummaries)
      capabilities.push_back("declarative-pure-external-summary");
    if (usesGuardedLoads)
      capabilities.push_back("guarded-memory-access");
    if (usesGuardedStores)
      capabilities.push_back("guarded-memory-write");
    if (usesStringSummaries)
      capabilities.push_back("bounded-nul-string-summary");
    if (usesPointerSearchSummaries)
      capabilities.push_back("bounded-pointer-search-summary");
    if (usesStringCopySummaries)
      capabilities.push_back("bounded-string-copy-summary");
    if (usesUbGuards)
      capabilities.push_back("llvm-defined-value-guards");
    if (usesNondeterministicFreeze)
      capabilities.push_back("stable-nondeterministic-freeze");
    if (usesDeferredPoisonFreeze)
      capabilities.push_back("bounded-deferred-poison-freeze");
    if (usesTransitiveDeferredPoison)
      capabilities.push_back("bounded-transitive-deferred-poison");
    if (usesSelectDeferredPoison)
      capabilities.push_back("bounded-select-deferred-poison");
    if (usesPhiDeferredPoison)
      capabilities.push_back("bounded-phi-deferred-poison");
    if (usesMemoryDeferredPoison)
      capabilities.push_back("bounded-memory-deferred-poison");
    if (usesCanonicalMemoryDeferredPoison)
      capabilities.push_back(
          "canonical-address-memory-deferred-poison");
    if (usesCrossBlockMemoryDeferredPoison)
      capabilities.push_back(
          "bounded-cross-block-memory-deferred-poison");
    if (usesCrossFunctionDeferredPoison)
      capabilities.push_back(
          "bounded-cross-function-deferred-poison");
    if (usesCrossFunctionArgumentPoison)
      capabilities.push_back(
          "bounded-cross-function-argument-poison");
    if (usesMultiCallsiteDeferredPoison)
      capabilities.push_back(
          "bounded-multicallsite-deferred-poison");
    if (usesTransitiveCallDeferredPoison)
      capabilities.push_back(
          "bounded-transitive-call-deferred-poison");
    if (usesMultiConsumerDeferredPoison)
      capabilities.push_back(
          "bounded-multiconsumer-deferred-poison");
    if (usesMultiAccessMemoryDeferredPoison)
      capabilities.push_back(
          "bounded-multiaccess-memory-deferred-poison");
    if (usesBranchMemoryDeferredPoison)
      capabilities.push_back(
          "bounded-branch-memory-deferred-poison");
    if (usesMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-memory-definedness-phi");
    if (usesMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-multicell-memory-definedness-phi");
    if (usesIdentifiedObjectMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-identified-object-multicell-memory-definedness-phi");
    if (usesFixedHeapObjectMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-fixed-heap-object-multicell-memory-definedness-phi");
    if (usesFinitePointerDomainMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-finite-pointer-domain-multicell-memory-definedness-phi");
    if (usesGuardCorrelatedPointerDomainMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi");
    if (usesPhiCorrelatedPointerDomainMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-phi-correlated-pointer-domain-multicell-memory-definedness-phi");
    if (usesSymbolicIndexIntervalMultiCellMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-symbolic-index-interval-multicell-memory-definedness-phi");
    if (usesMultiCellAliasGraph)
      capabilities.push_back("bounded-multicell-alias-graph");
    if (usesByteLaneMemoryDefinedness)
      capabilities.push_back(
          "bounded-byte-lane-memory-definedness");
    if (usesByteLaneWriterGraph)
      capabilities.push_back(
          "bounded-byte-lane-writer-graph");
    if (usesByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-byte-lane-memory-definedness-phi");
    if (usesByteLanePhiWriterGraph)
      capabilities.push_back(
          "bounded-byte-lane-phi-writer-graph");
    if (usesCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-cyclic-byte-lane-memory-definedness-phi");
    if (usesCyclicByteLaneWriterGraph)
      capabilities.push_back(
          "bounded-cyclic-byte-lane-writer-graph");
    if (
        usesConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (usesConditionalCyclicByteLaneWriterGraph)
      capabilities.push_back(
          "bounded-conditional-cyclic-byte-lane-writer-graph");
    if (
        usesMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (usesMultiArmCyclicByteLaneWriterGraph)
      capabilities.push_back(
          "bounded-multiarm-cyclic-byte-lane-writer-graph");
    if (usesRecursiveCyclicByteLaneWriterGraph)
      capabilities.push_back(
          "bounded-recursive-cyclic-byte-lane-writer-graph");
    if (
        usesRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (
        usesForwardedComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi");
    if (usesInterproceduralMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-interprocedural-memory-definedness-phi");
    if (usesMultilevelMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-multilevel-memory-definedness-phi");
    if (usesCyclicMemoryDefinednessPhi)
      capabilities.push_back(
          "bounded-cyclic-memory-definedness-phi");
    if (usesCyclicMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-cyclic-memory-definedness-carry");
    if (usesConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-conditional-memory-definedness-carry");
    if (usesForwardedConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-forwarded-conditional-memory-definedness-carry");
    if (usesMultiArmConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-multiarm-conditional-memory-definedness-carry");
    if (usesEquivalentDefinedStoreMemoryCarry)
      capabilities.push_back(
          "bounded-equivalent-defined-store-memory-carry");
    if (usesSharedPoisonStoreMemoryCarry)
      capabilities.push_back(
          "bounded-shared-poison-store-memory-carry");
    if (usesNestedConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-nested-conditional-memory-definedness-carry");
    if (usesRecursiveConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-recursive-memory-definedness-condition-tree");
    if (usesGroupedRecursiveConditionalMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-grouped-recursive-memory-definedness-condition-tree");
    if (usesRepeatedSourceRecursiveMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-repeated-source-recursive-memory-definedness-condition-tree");
    if (usesMultiCarryRecursiveMemoryDefinednessCarry)
      capabilities.push_back(
          "bounded-multicarry-recursive-memory-definedness-condition-tree");
    if (usesInitialMemoryDefinednessMerge)
      capabilities.push_back(
          "bounded-initial-memory-definedness-merge");
    if (usesInitialSubobjectDefinednessMerge)
      capabilities.push_back(
          "bounded-initial-subobject-definedness-merge");
    if (usesExternalSummaries)
      capabilities.push_back("bounded-external-effect-summary");
    if (usesScalarExternalSummaries)
      capabilities.push_back("bounded-scalar-external-summary");
    if (usesBitCountIntrinsics)
      capabilities.push_back("bounded-bitcount-intrinsic");
    if (usesBitPermutationIntrinsics)
      capabilities.push_back("bounded-bit-permutation-intrinsic");
    if (usesSaturatingArithmeticIntrinsics)
      capabilities.push_back("bounded-saturating-arithmetic-intrinsic");
    if (usesScalarSelectionIntrinsics)
      capabilities.push_back("bounded-scalar-selection-intrinsic");
    if (usesOptimizationHintIntrinsics)
      capabilities.push_back("llvm-optimization-hint-identity");
    if (usesObjectSizeIntrinsics)
      capabilities.push_back("bounded-objectsize-intrinsic");
    if (usesDynamicObjectSizeIntrinsics)
      capabilities.push_back("bounded-dynamic-objectsize-intrinsic");
    if (usesOverflowArithmeticIntrinsics)
      capabilities.push_back("bounded-overflow-arithmetic-intrinsic");
    if (usesSsaCopyIntrinsics)
      capabilities.push_back("bounded-ssa-copy-intrinsic");
    lowering["capabilities"] = std::move(capabilities);
    program["lowering"] = std::move(lowering);
    return true;
  }

private:
  Module &M;
  std::string entryName;
  LiveFunctionAnalysisProvider analysisProvider;
  bool promoteAllocas = true;
  std::vector<Function *> reachable;
  std::set<Function *> seenFunctions;
  std::vector<std::string> errors;
  uint64_t inputSize = 0;
  uint64_t memoryCursor = 64;
  uint64_t memoryLimit = 1024 * 1024;
  uint64_t inputBufferLimit = 64 * 1024;
  uint64_t heapObjectLimit = 64 * 1024;
  uint64_t dynamicHeapObjectLimit = 4096;
  uint64_t heapSiteCapacity = 4;
  uint64_t aliasLimit = 16;
  bool usesBoundedAliases = false;
  bool usesPointerUnions = false;
  bool usesHeapLifetimePointerUnions = false;
  bool usesCollectiveHeapUnionInitialization = false;
  bool usesGuardCorrelatedHeapUnionInitialization = false;
  bool usesMemorySSAHeapInitialization = false;
  bool usesInterproceduralHeapEffects = false;
  bool usesSymbolicRegionEffects = false;
  bool usesDynamicByteLaneCover = false;
  bool usesLoopMemoryPhiByteLaneInduction = false;
  bool usesStridedLoopMemoryPhiByteLaneInduction = false;
  bool usesConditionalLoopMemoryPhiByteLaneInduction = false;
  bool usesMultiLatchLoopMemoryPhiFixedPoint = false;
  bool usesOrderedMultiLatchLoopMemoryPhiTransfer = false;
  bool usesNestedLoopMemoryPhiSummaryComposition = false;
  bool usesNestedLoopMemoryPhiLastWriteValueSummary = false;
  bool usesNestedLoopMemoryPhiTwoDimensionalAffineSummary = false;
  bool usesNestedLoopMemoryPhiAffineSymbolicValueSummary = false;
  bool usesNestedLoopMemoryPhiPiecewiseAffineValueSummary = false;
  bool usesNestedLoopMemoryPhiDecisionDagValueSummary = false;
  bool usesExecutableNestedLoopMemoryTransfer = false;
  bool usesSharedPhiEdgeDiscriminator = false;
  bool usesPointerMemory = false;
  bool usesFunctionPointerMemory = false;
  bool usesPointerTables = false;
  bool usesPointerMemoryMerges = false;
  bool usesSymbolicPointerMemory = false;
  bool usesPointerInitialDefinitionMerge = false;
  bool usesAcyclicPointerMemorySsa = false;
  bool usesCyclicPointerMemorySsa = false;
  bool usesCanonicalPointerCells = false;
  bool usesCrossFunctionPointers = false;
  bool usesCrossFunctionDomains = false;
  bool usesIndirectCalls = false;
  bool usesNoUnwindInvokes = false;
  bool usesCleanupExceptions = false;
  bool usesTypedExceptions = false;
  bool usesExceptionCatchLifecycle = false;
  bool usesTrivialScalarCatchObjects = false;
  bool usesExceptionObjectArena = false;
  bool usesExceptionObjectFields = false;
  bool programUsesExceptionObjectArena = false;
  bool programUsesScalarExceptionSummary = false;
  bool usesDeclarativePureExternalSummaries = false;
  std::map<uint32_t, const GlobalValue *> exceptionTypeIdentities;
  std::map<uint64_t, const Instruction *> declarativePureExternalSites;
  bool usesGuardedLoads = false;
  bool usesGuardedStores = false;
  bool usesStringSummaries = false;
  bool usesPointerSearchSummaries = false;
  bool usesStringCopySummaries = false;
  bool usesUbGuards = false;
  bool usesNondeterministicFreeze = false;
  bool usesDeferredPoisonFreeze = false;
  bool usesTransitiveDeferredPoison = false;
  bool usesSelectDeferredPoison = false;
  bool usesPhiDeferredPoison = false;
  bool usesMemoryDeferredPoison = false;
  bool usesCanonicalMemoryDeferredPoison = false;
  bool usesCrossBlockMemoryDeferredPoison = false;
  bool usesCrossFunctionDeferredPoison = false;
  bool usesCrossFunctionArgumentPoison = false;
  bool usesMultiCallsiteDeferredPoison = false;
  bool usesTransitiveCallDeferredPoison = false;
  bool usesMultiConsumerDeferredPoison = false;
  bool usesMultiAccessMemoryDeferredPoison = false;
  bool usesBranchMemoryDeferredPoison = false;
  bool usesMemoryDefinednessPhi = false;
  bool usesMultiCellMemoryDefinednessPhi = false;
  bool usesIdentifiedObjectMultiCellMemoryDefinednessPhi = false;
  bool usesFixedHeapObjectMultiCellMemoryDefinednessPhi = false;
  bool usesFinitePointerDomainMultiCellMemoryDefinednessPhi = false;
  bool usesGuardCorrelatedPointerDomainMultiCellMemoryDefinednessPhi = false;
  bool usesPhiCorrelatedPointerDomainMultiCellMemoryDefinednessPhi = false;
  bool usesSymbolicIndexIntervalMultiCellMemoryDefinednessPhi = false;
  bool usesMultiCellAliasGraph = false;
  bool usesByteLaneMemoryDefinedness = false;
  bool usesByteLaneWriterGraph = false;
  bool usesByteLaneMemoryDefinednessPhi = false;
  bool usesByteLanePhiWriterGraph = false;
  bool usesCyclicByteLaneMemoryDefinednessPhi = false;
  bool usesCyclicByteLaneWriterGraph = false;
  bool usesConditionalCyclicByteLaneWriterGraph = false;
  bool usesMultiArmCyclicByteLaneWriterGraph = false;
  bool usesRecursiveCyclicByteLaneWriterGraph = false;
  bool
      usesConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool
      usesForwardedComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
          false;
  bool usesInterproceduralMemoryDefinednessPhi = false;
  bool usesMultilevelMemoryDefinednessPhi = false;
  bool usesCyclicMemoryDefinednessPhi = false;
  bool usesCyclicMemoryDefinednessCarry = false;
  bool usesConditionalMemoryDefinednessCarry = false;
  bool usesForwardedConditionalMemoryDefinednessCarry = false;
  bool usesMultiArmConditionalMemoryDefinednessCarry = false;
  bool usesEquivalentDefinedStoreMemoryCarry = false;
  bool usesSharedPoisonStoreMemoryCarry = false;
  bool usesNestedConditionalMemoryDefinednessCarry = false;
  bool usesRecursiveConditionalMemoryDefinednessCarry = false;
  bool usesGroupedRecursiveConditionalMemoryDefinednessCarry = false;
  bool usesRepeatedSourceRecursiveMemoryDefinednessCarry = false;
  bool usesMultiCarryRecursiveMemoryDefinednessCarry = false;
  bool usesInitialMemoryDefinednessMerge = false;
  bool usesInitialSubobjectDefinednessMerge = false;
  bool usesExternalSummaries = false;
  bool usesScalarExternalSummaries = false;
  bool usesBitCountIntrinsics = false;
  bool usesBitPermutationIntrinsics = false;
  bool usesSaturatingArithmeticIntrinsics = false;
  bool usesScalarSelectionIntrinsics = false;
  bool usesOptimizationHintIntrinsics = false;
  bool usesObjectSizeIntrinsics = false;
  bool usesDynamicObjectSizeIntrinsics = false;
  bool usesOverflowArithmeticIntrinsics = false;
  bool usesSsaCopyIntrinsics = false;
  bool usesNullableHeap = false;
  bool usesReallocHeap = false;
  bool inputBufferAbi = false;
  Argument *inputPointerArgument = nullptr;
  Argument *inputSizeArgument = nullptr;
  StaticMemoryObject inputMemoryObject;
  DenseMap<const GlobalVariable *, StaticMemoryObject> memoryObjects;
  DenseMap<const AllocaInst *, StaticMemoryObject> stackObjects;
  DenseMap<const CallBase *, HeapPool> heapObjects;
  DenseMap<const Function *, uint64_t> functionIds;
  DenseMap<const StoreInst *, std::vector<const LoadInst *>>
      byteLanePoisonLoads;
  std::set<const StoreInst *>
      cyclicByteLanePoisonStores;
  std::set<Function *> indirectTargets;
  std::vector<uint8_t> initialMemory;
  json::Array memoryObjectsMetadata;

  void reject(const Instruction &instruction, StringRef reason) {
    std::string message;
    raw_string_ostream output(message);
    output << instruction.getFunction()->getName() << ":"
           << instruction.getParent()->getName() << ": "
           << reason << " [" << instruction.getOpcodeName() << "]";
    errors.push_back(output.str());
  }

  std::optional<std::vector<Constant *>>
  globalPointerInitializers(Value *slot) const {
    slot = slot->stripPointerCasts();
    if (auto *getElement = dyn_cast<GEPOperator>(slot))
      slot = getElement->getPointerOperand()->stripPointerCasts();
    auto *global = dyn_cast<GlobalVariable>(slot);
    if (global == nullptr || !global->hasInitializer())
      return std::nullopt;
    Constant *initializer = global->getInitializer();
    if (global->getValueType()->isPointerTy())
      return std::vector<Constant *>{initializer};
    auto *array = dyn_cast<ConstantArray>(initializer);
    if (
        array == nullptr ||
        !array->getType()->getElementType()->isPointerTy())
      return std::nullopt;
    std::vector<Constant *> elements;
    elements.reserve(array->getNumOperands());
    for (Value *element : array->operands())
      elements.push_back(cast<Constant>(element));
    return elements;
  }

  struct ReachingPointerStores {
    std::vector<StoreInst *> stores;
    std::string error;
    bool traversedPredecessor = false;
    bool traversedCycle = false;
    bool canonicalizedCell = false;
    bool initialDefinitionReaches = false;
  };

  struct PointerCellIdentity {
    const Value *base = nullptr;
    uint64_t offset = 0;
    bool exact = false;
  };

  PointerCellIdentity pointerCellIdentity(const Value *value) const {
    const Value *original = value->stripPointerCasts();
    const Value *current = original;
    unsigned addressSpace =
        cast<PointerType>(value->getType())->getAddressSpace();
    unsigned bits =
        M.getDataLayout().getPointerSizeInBits(addressSpace);
    if (bits == 0 || bits > 64)
      return PointerCellIdentity{original, 0, false};
    uint64_t totalOffset = 0;
    bool sawGetElement = false;
    while (auto *getElement = dyn_cast<GEPOperator>(current)) {
      APInt offset(bits, 0);
      if (!getElement->accumulateConstantOffset(
              M.getDataLayout(), offset))
        return PointerCellIdentity{original, 0, false};
      totalOffset += offset.getZExtValue();
      if (bits < 64)
        totalOffset &= (UINT64_C(1) << bits) - 1;
      sawGetElement = true;
      current =
          getElement->getPointerOperand()->stripPointerCasts();
    }
    return PointerCellIdentity{
        current, totalOffset, sawGetElement || current == original};
  }

  ReachingPointerStores
  computeReachingPointerStores(
      const LoadInst &load,
      bool hasInitialDefinition = false) const {
    ReachingPointerStores result;
    PointerCellIdentity slot =
        pointerCellIdentity(load.getPointerOperand());
    auto sameCell = [&](const Value *candidate) {
      PointerCellIdentity identity = pointerCellIdentity(candidate);
      if (slot.exact && identity.exact) {
        bool equal =
            slot.base == identity.base &&
            slot.offset == identity.offset;
        if (
            equal &&
            candidate->stripPointerCasts() !=
                load.getPointerOperand()->stripPointerCasts())
          result.canonicalizedCell = true;
        return equal;
      }
      return candidate->stripPointerCasts() ==
             load.getPointerOperand()->stripPointerCasts();
    };
    auto lastWriter = [&](const BasicBlock *block,
                          const Instruction *before) {
      StoreInst *writer = nullptr;
      for (const Instruction &candidate : *block) {
        if (&candidate == before)
          break;
        auto *store = dyn_cast<StoreInst>(&candidate);
        if (
            store != nullptr &&
            sameCell(store->getPointerOperand()))
          writer = const_cast<StoreInst *>(store);
      }
      return writer;
    };
    auto validate = [&](StoreInst *writer) {
      if (writer->isVolatile() || writer->isAtomic()) {
        result.error =
            "pointer cell has an atomic or volatile reaching writer";
        return false;
      }
      if (!writer->getValueOperand()->getType()->isPointerTy()) {
        result.error =
            "pointer cell is overwritten by a non-pointer value";
        return false;
      }
      return true;
    };

    if (StoreInst *writer = lastWriter(load.getParent(), &load)) {
      if (validate(writer))
        result.stores.push_back(writer);
      return result;
    }
    result.traversedPredecessor = true;

    const Function *function = load.getFunction();
    std::set<const BasicBlock *> live;
    std::deque<const BasicBlock *> livePending = {
        &function->getEntryBlock()};
    while (!livePending.empty()) {
      const BasicBlock *block = livePending.front();
      livePending.pop_front();
      if (!live.insert(block).second)
        continue;
      for (const BasicBlock *successor : successors(block))
        livePending.push_back(successor);
    }
    struct DefinitionState {
      std::set<StoreInst *> stores;
      bool uninitialized = false;
      bool initial = false;
    };
    std::map<const BasicBlock *, DefinitionState> input;
    std::map<const BasicBlock *, DefinitionState> output;
    auto equal = [](const DefinitionState &left,
                    const DefinitionState &right) {
      return left.uninitialized == right.uninitialized &&
             left.initial == right.initial &&
             left.stores == right.stores;
    };
    bool changed = true;
    uint64_t visits = 0;
    while (changed) {
      changed = false;
      for (const BasicBlock &block : *function) {
        if (live.count(&block) == 0)
          continue;
        if (++visits > 4096) {
          result.error =
              "pointer memory reaching-definition graph exceeds limit";
          return result;
        }
        DefinitionState nextInput;
        if (&block == &function->getEntryBlock()) {
          nextInput.uninitialized =
              !hasInitialDefinition;
          nextInput.initial = hasInitialDefinition;
        } else {
          bool hasLivePredecessor = false;
          for (const BasicBlock *predecessor : predecessors(&block)) {
            if (live.count(predecessor) == 0)
              continue;
            hasLivePredecessor = true;
            const DefinitionState &incoming = output[predecessor];
            nextInput.uninitialized |= incoming.uninitialized;
            nextInput.initial |= incoming.initial;
            nextInput.stores.insert(
                incoming.stores.begin(), incoming.stores.end());
          }
          if (!hasLivePredecessor)
            nextInput.uninitialized = true;
        }
        DefinitionState nextOutput = nextInput;
        if (StoreInst *writer = lastWriter(&block, nullptr)) {
          nextOutput.stores = {writer};
          nextOutput.uninitialized = false;
          nextOutput.initial = false;
        }
        if (!equal(input[&block], nextInput)) {
          input[&block] = std::move(nextInput);
          changed = true;
        }
        if (!equal(output[&block], nextOutput)) {
          output[&block] = std::move(nextOutput);
          changed = true;
        }
      }
    }

    const DefinitionState &atLoad = input[load.getParent()];
    result.initialDefinitionReaches = atLoad.initial;
    if (atLoad.uninitialized) {
      result.error =
          "pointer memory merge does not cover every predecessor";
      return result;
    }
    if (atLoad.stores.size() > 256) {
      result.error =
          "pointer memory reaching-definition set exceeds limit";
      return result;
    }
    for (const BasicBlock &block : *function)
      for (const Instruction &instruction : block)
        if (
            auto *store = const_cast<StoreInst *>(
                dyn_cast<StoreInst>(&instruction));
            store != nullptr && atLoad.stores.count(store) != 0) {
          if (!validate(store)) {
            result.stores.clear();
            return result;
          }
          result.stores.push_back(store);
        }
    if (
        result.stores.empty() &&
        !hasInitialDefinition) {
      result.error =
          "pointer load has no exact reaching pointer store";
      return result;
    }

    std::set<const BasicBlock *> ancestors;
    std::deque<const BasicBlock *> pending = {load.getParent()};
    while (!pending.empty()) {
      const BasicBlock *block = pending.front();
      pending.pop_front();
      if (!ancestors.insert(block).second)
        continue;
      for (const BasicBlock *predecessor : predecessors(block))
        if (live.count(predecessor) != 0)
          pending.push_back(predecessor);
    }
    std::map<const BasicBlock *, unsigned> colors;
    std::function<void(const BasicBlock *)> detectCycle =
        [&](const BasicBlock *block) {
          colors[block] = 1;
          for (const BasicBlock *successor : successors(block)) {
            if (ancestors.count(successor) == 0)
              continue;
            if (colors[successor] == 1) {
              result.traversedCycle = true;
            } else if (colors[successor] == 0) {
              detectCycle(successor);
            }
          }
          colors[block] = 2;
        };
    for (const BasicBlock *block : ancestors)
      if (colors[block] == 0)
        detectCycle(block);
    return result;
  }

  bool discoverFunctionTargets(
      Value *value, std::set<const Value *> &active,
      std::vector<Function *> &targets) const {
    value = value->stripPointerCasts();
    if (auto *function = dyn_cast<Function>(value)) {
      if (std::find(targets.begin(), targets.end(), function) ==
          targets.end())
        targets.push_back(function);
      return true;
    }
    if (isa<ConstantPointerNull>(value))
      return true;
    if (!active.insert(value).second)
      return false;
    bool complete = false;
    if (auto *select = dyn_cast<SelectInst>(value)) {
      complete = discoverFunctionTargets(
                     select->getTrueValue(), active, targets) &&
                 discoverFunctionTargets(
                     select->getFalseValue(), active, targets);
    } else if (auto *phi = dyn_cast<PHINode>(value)) {
      complete = true;
      for (Value *incoming : phi->incoming_values())
        complete = discoverFunctionTargets(
                       incoming, active, targets) &&
                   complete;
    } else if (auto *load = dyn_cast<LoadInst>(value)) {
      Value *slot = load->getPointerOperand()->stripPointerCasts();
      auto initializers = globalPointerInitializers(slot);
      auto reaching = computeReachingPointerStores(
          *load, initializers.has_value());
      complete = true;
      if (
          initializers &&
          reaching.initialDefinitionReaches) {
        for (Constant *initializer : *initializers)
          complete = discoverFunctionTargets(
                         initializer, active, targets) &&
                     complete;
      }
      complete =
          reaching.error.empty() &&
          (reaching.initialDefinitionReaches ||
           !reaching.stores.empty()) &&
          complete;
      for (StoreInst *store : reaching.stores)
        complete = discoverFunctionTargets(
                       store->getValueOperand(), active, targets) &&
                   complete;
    } else if (auto *call = dyn_cast<CallBase>(value)) {
      Function *callee = call->getCalledFunction();
      if (
          callee != nullptr && callee->isIntrinsic() &&
          callee->getIntrinsicID() == Intrinsic::ssa_copy &&
          call->arg_size() == 1)
        complete = discoverFunctionTargets(
            call->getArgOperand(0), active, targets);
    }
    active.erase(value);
    return complete;
  }

  bool discoverFunctionTargets(
      Value *value, std::vector<Function *> &targets) const {
    std::set<const Value *> active;
    return discoverFunctionTargets(value, active, targets);
  }

  bool isFunctionPointerValue(Value *value) const {
    std::vector<Function *> targets;
    return discoverFunctionTargets(value, targets) && !targets.empty();
  }

  bool isProvablyNoUnwind(const CallBase &call) const {
    if (call.hasFnAttr(Attribute::NoUnwind))
      return true;
    Function *callee = call.getCalledFunction();
    if (callee != nullptr)
      return callee->hasFnAttribute(Attribute::NoUnwind);
    std::vector<Function *> targets;
    if (!discoverFunctionTargets(
            call.getCalledOperand(), targets) ||
        targets.empty())
      return false;
    return std::all_of(
        targets.begin(), targets.end(), [](const Function *target) {
          return target != nullptr &&
                 target->hasFnAttribute(Attribute::NoUnwind);
        });
  }

  bool isContinuationThrowSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__symcc_continuation_throw_if" &&
           !callee->isVarArg() && callee->arg_size() == 2 &&
           callee->getReturnType()->isVoidTy() &&
           callee->getArg(0)->getType()->isIntegerTy(1) &&
           integerBits(callee->getArg(1)->getType()) > 0 &&
           integerBits(callee->getArg(1)->getType()) <= 64 &&
           call.getType()->isVoidTy() && call.arg_size() == 2 &&
           call.getArgOperand(0)->getType()->isIntegerTy(1) &&
           integerBits(call.getArgOperand(1)->getType()) > 0 &&
           integerBits(call.getArgOperand(1)->getType()) <= 64;
  }

  bool isCxaAllocateExceptionSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    unsigned sizeBits =
        call.arg_size() == 1
            ? integerBits(call.getArgOperand(0)->getType())
            : 0;
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__cxa_allocate_exception" &&
           callee->getCallingConv() == CallingConv::C &&
           call.getCallingConv() == CallingConv::C &&
           !callee->isVarArg() && callee->arg_size() == 1 &&
           callee->getReturnType()->isPointerTy() &&
           callee->getReturnType()->getPointerAddressSpace() == 0 &&
           integerBits(callee->getArg(0)->getType()) == sizeBits &&
           sizeBits > 0 && sizeBits <= 64 &&
           call.getType()->isPointerTy() &&
           call.getType()->getPointerAddressSpace() == 0 &&
           call.arg_size() == 1 && call.getNumOperandBundles() == 0 &&
           isProvablyNoUnwind(call);
  }

  bool isCxaThrowSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    const Instruction *normalTerminator = nullptr;
    if (auto *invoke = dyn_cast<InvokeInst>(&call))
      normalTerminator = invoke->getNormalDest()->getTerminator();
    else
      normalTerminator = call.getParent()->getTerminator();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__cxa_throw" &&
           callee->getCallingConv() == CallingConv::C &&
           call.getCallingConv() == CallingConv::C &&
           !callee->isVarArg() && callee->arg_size() == 3 &&
           callee->getReturnType()->isVoidTy() &&
           std::all_of(
               callee->arg_begin(), callee->arg_end(),
               [](const Argument &argument) {
                 return argument.getType()->isPointerTy() &&
                        argument.getType()->getPointerAddressSpace() == 0;
               }) &&
           call.getType()->isVoidTy() && call.arg_size() == 3 &&
           std::all_of(
               call.arg_begin(), call.arg_end(),
               [](const Use &argument) {
                 return argument->getType()->isPointerTy() &&
                        argument->getType()->getPointerAddressSpace() == 0;
               }) &&
           call.getNumOperandBundles() == 0 && call.doesNotReturn() &&
           isa_and_nonnull<UnreachableInst>(normalTerminator);
  }

  bool hasOnlyBoundedExceptionAllocationUses(
      const CallBase &allocation) const {
    auto *size = dyn_cast<ConstantInt>(allocation.getArgOperand(0));
    if (size == nullptr || size->isZero() ||
        size->getValue().getActiveBits() > 64)
      return false;
    uint64_t objectSize = size->getZExtValue();
    bool sawThrow = false;
    std::set<const Value *> visited;
    std::deque<const Value *> pending = {&allocation};
    while (!pending.empty()) {
      const Value *pointer = pending.front();
      pending.pop_front();
      if (!visited.insert(pointer).second)
        continue;
      for (const User *user : pointer->users()) {
        if (auto *store = dyn_cast<StoreInst>(user)) {
          unsigned bits = integerBits(store->getValueOperand()->getType());
          uint64_t bytes = fixedStoreBytes(
              M.getDataLayout(), store->getValueOperand()->getType());
          auto offset = constantPointerOffsetFrom(
              store->getPointerOperand(), allocation);
          if (store->getPointerOperand() == pointer && offset &&
              bits > 0 && bits <= 64 && bytes > 0 && bytes <= 8 &&
              *offset <= objectSize && bytes <= objectSize - *offset &&
              !store->isAtomic() && !store->isVolatile())
            continue;
          return false;
        }
        if (auto *getElement = dyn_cast<GetElementPtrInst>(user)) {
          auto offset = constantPointerOffsetFrom(getElement, allocation);
          if (getElement->getPointerOperand() != pointer || !offset ||
              *offset > objectSize || getElement->use_empty())
            return false;
          pending.push_back(getElement);
          continue;
        }
        auto *throwCall = dyn_cast<CallBase>(user);
        if (pointer != &allocation || throwCall == nullptr ||
            !isCxaThrowSummary(*throwCall) ||
            throwCall->getArgOperand(0)->stripPointerCasts() != &allocation)
          return false;
        sawThrow = true;
      }
    }
    return sawThrow;
  }

  std::optional<uint32_t> exceptionTypeId(Value *value) {
    if (value == nullptr || !value->getType()->isPointerTy())
      return std::nullopt;
    Value *stripped = value->stripPointerCasts();
    auto *global = dyn_cast<GlobalValue>(stripped);
    if (global == nullptr)
      return std::nullopt;
    uint64_t stable = stableSiteId(*global);
    uint32_t folded = static_cast<uint32_t>(stable) ^
                      static_cast<uint32_t>(stable >> 32);
    folded &= 0x7fffffffU;
    if (folded == 0)
      folded = 1;
    auto [position, inserted] =
        exceptionTypeIdentities.emplace(folded, global);
    if (!inserted && position->second != global)
      return std::nullopt;
    return folded;
  }

  bool isContinuationTypedThrowSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() ==
               "__symcc_continuation_throw_typed_if" &&
           !callee->isVarArg() && callee->arg_size() == 3 &&
           callee->getReturnType()->isVoidTy() &&
           callee->getArg(0)->getType()->isIntegerTy(1) &&
           integerBits(callee->getArg(1)->getType()) > 0 &&
           integerBits(callee->getArg(1)->getType()) <= 64 &&
           callee->getArg(2)->getType()->isPointerTy() &&
           call.getType()->isVoidTy() && call.arg_size() == 3 &&
           call.getArgOperand(0)->getType()->isIntegerTy(1) &&
           integerBits(call.getArgOperand(1)->getType()) > 0 &&
           integerBits(call.getArgOperand(1)->getType()) <= 64 &&
           call.getArgOperand(2)->getType()->isPointerTy();
  }

  bool isCxaBeginCatchSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__cxa_begin_catch" &&
           callee->getCallingConv() == CallingConv::C &&
           call.getCallingConv() == CallingConv::C &&
           !callee->isVarArg() && callee->arg_size() == 1 &&
           callee->getReturnType()->isPointerTy() &&
           callee->getArg(0)->getType()->isPointerTy() &&
           callee->getReturnType()->getPointerAddressSpace() == 0 &&
           callee->getArg(0)->getType()->getPointerAddressSpace() == 0 &&
           call.getType()->isPointerTy() && call.arg_size() == 1 &&
           call.getArgOperand(0)->getType()->isPointerTy() &&
           call.getType()->getPointerAddressSpace() == 0 &&
           call.getArgOperand(0)->getType()->getPointerAddressSpace() == 0 &&
           call.getNumOperandBundles() == 0 &&
           isProvablyNoUnwind(call);
  }

  bool isCxaEndCatchSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__cxa_end_catch" &&
           callee->getCallingConv() == CallingConv::C &&
           call.getCallingConv() == CallingConv::C &&
           !callee->isVarArg() && callee->arg_size() == 0 &&
           callee->getReturnType()->isVoidTy() &&
           call.getType()->isVoidTy() && call.arg_size() == 0 &&
           call.getNumOperandBundles() == 0;
  }

  std::optional<uint64_t> constantPointerOffsetFrom(
      const Value *pointer, const Value &root) const {
    uint64_t total = 0;
    std::set<const Value *> visited;
    while (pointer != &root) {
      if (!visited.insert(pointer).second)
        return std::nullopt;
      auto *getElement = dyn_cast<GEPOperator>(pointer);
      if (getElement == nullptr)
        return std::nullopt;
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          getElement->getPointerAddressSpace());
      if (pointerBits == 0 || pointerBits > 64)
        return std::nullopt;
      APInt offset(pointerBits, 0, true);
      if (!getElement->accumulateConstantOffset(M.getDataLayout(), offset) ||
          offset.isNegative() || offset.getActiveBits() > 64)
        return std::nullopt;
      uint64_t delta = offset.getZExtValue();
      if (total > UINT64_MAX - delta)
        return std::nullopt;
      total += delta;
      pointer = getElement->getPointerOperand();
    }
    return total;
  }

  std::optional<uint64_t> exceptionCatchObjectOffset(
      const Value *pointer) const {
    const Value *cursor = pointer;
    std::set<const Value *> visited;
    while (true) {
      if (!visited.insert(cursor).second)
        return std::nullopt;
      if (auto *call = dyn_cast<CallBase>(cursor)) {
        if (!isCxaBeginCatchSummary(*call))
          return std::nullopt;
        auto offset = constantPointerOffsetFrom(pointer, *call);
        if (!offset || (!programUsesExceptionObjectArena && *offset != 0))
          return std::nullopt;
        return offset;
      }
      auto *getElement = dyn_cast<GEPOperator>(cursor);
      if (getElement == nullptr)
        return std::nullopt;
      cursor = getElement->getPointerOperand();
    }
  }

  bool isTrivialScalarCatchObjectLoad(const LoadInst &load) const {
    auto offset = exceptionCatchObjectOffset(load.getPointerOperand());
    unsigned bits = integerBits(load.getType());
    uint64_t bytes = fixedStoreBytes(M.getDataLayout(), load.getType());
    uint64_t offsetLimit = std::min<uint64_t>(heapObjectLimit, 65535);
    return offset && *offset <= offsetLimit && bytes > 0 && bytes <= 8 &&
           bytes <= heapObjectLimit - *offset && bits > 0 && bits <= 64 &&
           !load.isAtomic() && !load.isVolatile() &&
           load.getType()->isIntegerTy();
  }

  bool hasOnlyTrivialScalarCatchObjectLoads(const CallBase &call) const {
    if (call.use_empty())
      return false;
    std::set<const Value *> visited;
    std::deque<const Value *> pending = {&call};
    while (!pending.empty()) {
      const Value *pointer = pending.front();
      pending.pop_front();
      if (!visited.insert(pointer).second)
        continue;
      for (const User *user : pointer->users()) {
        if (auto *load = dyn_cast<LoadInst>(user)) {
          if (load->getPointerOperand() != pointer ||
              !isTrivialScalarCatchObjectLoad(*load))
            return false;
          continue;
        }
        auto *getElement = dyn_cast<GetElementPtrInst>(user);
        if (!programUsesExceptionObjectArena || getElement == nullptr ||
            getElement->getPointerOperand() != pointer ||
            !exceptionCatchObjectOffset(getElement) ||
            getElement->use_empty())
          return false;
        pending.push_back(getElement);
      }
    }
    return true;
  }

  bool isCxaRethrowSummary(const CallBase &call) const {
    Function *callee = call.getCalledFunction();
    return callee != nullptr && callee->isDeclaration() &&
           callee->getName() == "__cxa_rethrow" &&
           callee->getCallingConv() == CallingConv::C &&
           call.getCallingConv() == CallingConv::C &&
           !callee->isVarArg() && callee->arg_size() == 0 &&
           callee->getReturnType()->isVoidTy() &&
           call.getType()->isVoidTy() && call.arg_size() == 0 &&
           call.getNumOperandBundles() == 0 && call.doesNotReturn();
  }

  bool isExceptionTokenExtract(const ExtractValueInst &extract) const {
    auto *landing = dyn_cast<LandingPadInst>(
        extract.getAggregateOperand());
    ArrayRef<unsigned> indices = extract.getIndices();
    if (landing == nullptr || indices.size() != 1 ||
        indices.front() != 0 || !extract.getType()->isPointerTy() ||
        !extract.hasOneUse())
      return false;
    auto *call = dyn_cast<CallBase>(*extract.user_begin());
    return call != nullptr && isa<CallInst>(call) &&
           isCxaBeginCatchSummary(*call) &&
           (call->use_empty() ||
            hasOnlyTrivialScalarCatchObjectLoads(*call)) &&
           call->getArgOperand(0)->stripPointerCasts() == &extract;
  }

  std::set<const BasicBlock *>
  continuationReachableBlocks(Function &function) const {
    std::set<const BasicBlock *> live;
    std::deque<const BasicBlock *> pending = {
        &function.getEntryBlock()};
    while (!pending.empty()) {
      const BasicBlock *block = pending.front();
      pending.pop_front();
      if (!live.insert(block).second)
        continue;
      const Instruction *terminator = block->getTerminator();
      if (auto *invoke = dyn_cast<InvokeInst>(terminator)) {
        if (isCxaRethrowSummary(*invoke)) {
          pending.push_back(invoke->getUnwindDest());
          continue;
        }
        if (isCxaEndCatchSummary(*invoke)) {
          pending.push_back(invoke->getNormalDest());
          continue;
        }
        if (isProvablyNoUnwind(*invoke)) {
          pending.push_back(invoke->getNormalDest());
          continue;
        }
      }
      for (const BasicBlock *successor : successors(block))
        pending.push_back(successor);
    }
    return live;
  }

  void collectReachable(Function &entry) {
    std::deque<Function *> pending = {&entry};
    while (!pending.empty()) {
      Function *function = pending.front();
      pending.pop_front();
      if (!seenFunctions.insert(function).second)
        continue;
      reachable.push_back(function);
      const auto liveBlocks =
          continuationReachableBlocks(*function);
      for (BasicBlock &block : *function) {
        if (liveBlocks.count(&block) == 0)
          continue;
        for (Instruction &instruction : block) {
          auto *call = dyn_cast<CallBase>(&instruction);
          if (call == nullptr)
            continue;
          Function *callee = call->getCalledFunction();
          if (callee != nullptr && !callee->isDeclaration() &&
              !callee->isIntrinsic())
            pending.push_back(callee);
          else if (callee == nullptr) {
            std::vector<Function *> targets;
            if (discoverFunctionTargets(
                    call->getCalledOperand(), targets)) {
              for (Function *target : targets) {
                if (!target->isDeclaration() && !target->isIntrinsic()) {
                  indirectTargets.insert(target);
                  pending.push_back(target);
                }
              }
            }
          }
        }
      }
    }
    std::vector<Function *> ordered(
        indirectTargets.begin(), indirectTargets.end());
    llvm::sort(
        ordered, [](const Function *left, const Function *right) {
          return left->getName() < right->getName();
        });
    uint64_t identifier = 1;
    for (Function *function : ordered)
      functionIds[function] = identifier++;
  }

  bool hasReachableRecursion(Function &function,
                             std::set<Function *> &active,
                             std::set<Function *> &completed) const {
    if (active.count(&function) != 0)
      return true;
    if (completed.count(&function) != 0)
      return false;
    active.insert(&function);
    const auto liveBlocks =
        continuationReachableBlocks(function);
    for (BasicBlock &block : function) {
      if (liveBlocks.count(&block) == 0)
        continue;
      for (Instruction &instruction : block) {
        auto *call = dyn_cast<CallBase>(&instruction);
        if (call == nullptr)
          continue;
        Function *callee = call->getCalledFunction();
        if (callee != nullptr && !callee->isDeclaration() &&
            !callee->isIntrinsic() &&
            hasReachableRecursion(*callee, active, completed))
          return true;
        if (callee == nullptr) {
          std::vector<Function *> targets;
          if (discoverFunctionTargets(
                  call->getCalledOperand(), targets))
            for (Function *target : targets)
              if (!target->isDeclaration() && !target->isIntrinsic() &&
                  hasReachableRecursion(*target, active, completed))
                return true;
        }
      }
    }
    active.erase(&function);
    completed.insert(&function);
    return false;
  }

  void rejectGlobal(const GlobalVariable &global, StringRef reason) {
    errors.push_back(
        ("global @" + global.getName() + ": " + reason).str());
  }

  void configureEntryInputAbi(Function &entry) {
    if (entry.arg_size() != 2)
      return;
    auto argument = entry.arg_begin();
    Argument *pointer = &*argument++;
    Argument *size = &*argument;
    if (!pointer->getType()->isPointerTy() ||
        pointer->getType()->getPointerAddressSpace() != 0 ||
        integerBits(size->getType()) == 0)
      return;
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits();
    if (pointerBits == 0 || pointerBits > 64) {
      errors.push_back(
          entry.getName().str() +
          ": input-buffer pointer width exceeds 64 bits");
      return;
    }
    uint64_t address = alignTo(memoryCursor, 16);
    if (address >= memoryLimit) {
      errors.push_back(
          entry.getName().str() +
          ": input-buffer object exceeds continuation memory limit");
      return;
    }
    unsigned sizeBits = integerBits(size->getType());
    uint64_t representableSize =
        sizeBits == 64 ? UINT64_MAX : (uint64_t{1} << sizeBits) - 1;
    uint64_t capacity = std::min({
        inputBufferLimit,
        memoryLimit - address,
        representableSize,
    });
    if (capacity == 0) {
      errors.push_back(
          entry.getName().str() +
          ": input-buffer capacity is zero");
      return;
    }
    inputBufferAbi = true;
    inputPointerArgument = pointer;
    inputSizeArgument = size;
    inputMemoryObject = StaticMemoryObject{
        address, capacity, false,
    };
    memoryCursor = address + capacity;
    initialMemory.resize(memoryCursor, 0);

    json::Object metadata;
    metadata["name"] = "$input";
    metadata["kind"] = "input";
    metadata["address"] = static_cast<int64_t>(address);
    metadata["size"] = static_cast<int64_t>(capacity);
    metadata["read_only"] = false;
    metadata["logical_size"] = "input-length";
    memoryObjectsMetadata.push_back(std::move(metadata));
  }

  std::optional<StaticMemoryObject> layoutStack(AllocaInst &allocation) {
    auto existing = stackObjects.find(&allocation);
    if (existing != stackObjects.end())
      return existing->second;
    auto *count = dyn_cast<ConstantInt>(allocation.getArraySize());
    if (!allocation.isStaticAlloca() || count == nullptr ||
        count->isZero() || count->getValue().getActiveBits() > 64 ||
        allocation.getAddressSpace() != 0) {
      reject(allocation,
             "stack allocation must have a fixed nonzero bounded size");
      return std::nullopt;
    }
    if (!allocation.getAllocatedType()->isSized()) {
      reject(allocation, "stack allocation has an unsized element type");
      return std::nullopt;
    }
#if LLVM_VERSION_MAJOR >= 11
    TypeSize allocationSize =
        M.getDataLayout().getTypeAllocSize(allocation.getAllocatedType());
    if (allocationSize.isScalable()) {
      reject(allocation, "scalable stack allocation is unsupported");
      return std::nullopt;
    }
    uint64_t elementSize = allocationSize.getFixedValue();
#else
    uint64_t elementSize =
        M.getDataLayout().getTypeAllocSize(allocation.getAllocatedType());
#endif
    uint64_t elementCount = count->getZExtValue();
    if (elementSize == 0 || elementCount > memoryLimit / elementSize) {
      reject(allocation, "stack allocation exceeds continuation memory limit");
      return std::nullopt;
    }
    uint64_t size = elementSize * elementCount;
    uint64_t alignment = std::max<uint64_t>(
        M.getDataLayout()
            .getABITypeAlign(allocation.getAllocatedType())
            .value(),
        allocation.getAlign().value());
    uint64_t address = alignTo(memoryCursor, alignment);
    if (address > memoryLimit || size > memoryLimit - address) {
      reject(allocation, "stack memory image exceeds continuation memory limit");
      return std::nullopt;
    }
    StaticMemoryObject object{address, size, false};
    stackObjects[&allocation] = object;
    memoryCursor = address + size;
    initialMemory.resize(memoryCursor, 0);

    json::Object metadata;
    std::string name = allocation.getFunction()->getName().str() + ":";
    name += allocation.hasName()
                ? allocation.getName().str()
                : "stack@" + std::to_string(address);
    metadata["name"] = std::move(name);
    metadata["kind"] = "stack";
    metadata["function"] = allocation.getFunction()->getName().str();
    metadata["address"] = static_cast<int64_t>(address);
    metadata["size"] = static_cast<int64_t>(size);
    metadata["read_only"] = false;
    metadata["initialization"] = "runtime-write-tracked";
    memoryObjectsMetadata.push_back(std::move(metadata));
    return object;
  }

  std::optional<HeapPool> layoutHeap(CallBase &allocation) {
    auto existing = heapObjects.find(&allocation);
    if (existing != heapObjects.end())
      return existing->second;
    Function *callee = allocation.getCalledFunction();
    StringRef allocatorName =
        callee == nullptr ? StringRef() : callee->getName();
    bool isMalloc = allocatorName == "malloc";
    bool isCalloc = allocatorName == "calloc";
    bool isException =
        allocatorName == "__cxa_allocate_exception";
    Value *sizeValue =
        (isMalloc || isException) && allocation.arg_size() == 1
            ? allocation.getArgOperand(0)
            : nullptr;
    Value *countValue =
        isCalloc && allocation.arg_size() == 2
            ? allocation.getArgOperand(0)
            : nullptr;
    Value *elementSizeValue =
        isCalloc && allocation.arg_size() == 2
            ? allocation.getArgOperand(1)
            : nullptr;
    if (isCalloc)
      sizeValue = elementSizeValue;
    auto *sizeConstant = dyn_cast_or_null<ConstantInt>(sizeValue);
    unsigned sizeBits =
        sizeValue == nullptr ? 0 : integerBits(sizeValue->getType());
    if (callee == nullptr || !callee->isDeclaration() ||
        (!isMalloc && !isCalloc && !isException) ||
        (isException &&
         !isCxaAllocateExceptionSummary(allocation)) ||
        !allocation.getType()->isPointerTy() ||
        allocation.getType()->getPointerAddressSpace() != 0 ||
        sizeValue == nullptr ||
        (isCalloc && countValue == nullptr) ||
        sizeBits == 0 || sizeBits > 64 ||
        (isCalloc &&
         (integerBits(countValue->getType()) != sizeBits ||
          integerBits(elementSizeValue->getType()) != sizeBits)) ||
        (sizeConstant != nullptr &&
         sizeConstant->getValue().getActiveBits() > 64)) {
      reject(allocation,
             "allocator requires bounded integer size operands");
      return std::nullopt;
    }
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
        allocation.getType()->getPointerAddressSpace());
    if (pointerBits == 0 || pointerBits > 64) {
      reject(allocation, "malloc pointer width exceeds 64 bits");
      return std::nullopt;
    }
    if (isException &&
        (sizeConstant == nullptr || sizeConstant->isZero())) {
      reject(
          allocation,
          "__cxa_allocate_exception requires a fixed non-zero size");
      return std::nullopt;
    }
    if (isException &&
        !hasOnlyBoundedExceptionAllocationUses(allocation)) {
      reject(
          allocation,
          "exception allocation must be written and consumed by one or "
          "more direct __cxa_throw calls");
      return std::nullopt;
    }
    bool nullable =
        !isException &&
        (isCalloc || sizeConstant == nullptr || sizeConstant->isZero());
    uint64_t size =
        nullable ? dynamicHeapObjectLimit : sizeConstant->getZExtValue();
    if (!nullable && size > heapObjectLimit) {
      reject(allocation, "allocation exceeds heap object limit");
      return std::nullopt;
    }
    uint64_t maximumAddress =
        pointerBits == 64 ? UINT64_MAX : (uint64_t{1} << pointerBits) - 1;
    HeapPool pool;
    pool.site = std::to_string(stableSiteId(allocation));
    pool.allocator = allocatorName.str();
    pool.objectSize = size;
    pool.sizeBits = sizeBits;
    pool.nullable = nullable;
    usesNullableHeap |= nullable;
    pool.slots.reserve(heapSiteCapacity);
    for (uint64_t slot = 0; slot < heapSiteCapacity; ++slot) {
      uint64_t address = alignTo(memoryCursor, 16);
      if (address > memoryLimit || size > memoryLimit - address) {
        reject(allocation,
               "heap pool memory image exceeds continuation memory limit");
        return std::nullopt;
      }
      if (address > maximumAddress ||
          size - 1 > maximumAddress - address) {
        reject(allocation, "heap pool exceeds target pointer range");
        return std::nullopt;
      }
      pool.slots.push_back(StaticMemoryObject{address, size, false});
      memoryCursor = address + size;
      initialMemory.resize(memoryCursor, 0);

      json::Object metadata;
      metadata["name"] =
          ((isException ? "exception:" : "heap:") +
           Twine(pool.site) + ":" + Twine(slot)).str();
      metadata["kind"] = "heap";
      metadata["site"] = pool.site;
      metadata["slot"] = static_cast<int64_t>(slot);
      metadata["capacity"] = static_cast<int64_t>(heapSiteCapacity);
      metadata["address"] = static_cast<int64_t>(address);
      metadata["size"] = static_cast<int64_t>(size);
      metadata["read_only"] = false;
      metadata["lifetime"] =
          isException ? "exception-lifecycle" : "runtime-alloc-free";
      metadata["allocation"] =
          isException
              ? "bounded-exception-arena"
              : (nullable ? "bounded-pool-nullable"
                          : "bounded-pool-infallible");
      if (nullable)
        metadata["logical_size"] = "runtime-allocation-size";
      memoryObjectsMetadata.push_back(std::move(metadata));
    }
    heapObjects[&allocation] = pool;
    return pool;
  }

  bool writeInitializer(const Constant &constant, Type *type, uint64_t address,
                        const GlobalVariable &global) {
    if (constant.isNullValue())
      return true;
    if (isa<UndefValue>(constant) || isa<PoisonValue>(constant)) {
      rejectGlobal(global, "undef or poison initializer is unsupported");
      return false;
    }
    if (auto *integer = dyn_cast<ConstantInt>(&constant)) {
      unsigned bits = integerBits(type);
      uint64_t bytes = fixedStoreBytes(M.getDataLayout(), type);
      if (bits == 0 || bytes == 0 || bytes > 8) {
        rejectGlobal(global, "initializer integer exceeds 64 bits");
        return false;
      }
      APInt value = integer->getValue().zextOrTrunc(bytes * 8);
      for (uint64_t memoryByte = 0; memoryByte < bytes; ++memoryByte) {
        uint64_t valueByte = M.getDataLayout().isLittleEndian()
                                 ? memoryByte
                                 : bytes - memoryByte - 1;
        initialMemory[address + memoryByte] = static_cast<uint8_t>(
            value.extractBits(8, valueByte * 8).getZExtValue());
      }
      return true;
    }
    if (isa<ConstantPointerNull>(constant))
      return true;
    if (type->isPointerTy()) {
      Value *stripped =
          const_cast<Constant &>(constant).stripPointerCasts();
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          type->getPointerAddressSpace());
      uint64_t bytes = fixedStoreBytes(M.getDataLayout(), type);
      if (auto *function = dyn_cast<Function>(stripped)) {
        auto identifier = functionIds.find(function);
        if (
            identifier == functionIds.end() || pointerBits == 0 ||
            pointerBits > 64 || bytes == 0 || bytes > 8) {
          rejectGlobal(
              global,
              "function-pointer initializer has no bounded target ID");
          return false;
        }
        APInt value(bytes * 8, identifier->second);
        for (uint64_t memoryByte = 0; memoryByte < bytes; ++memoryByte) {
          uint64_t valueByte = M.getDataLayout().isLittleEndian()
                                   ? memoryByte
                                   : bytes - memoryByte - 1;
          initialMemory[address + memoryByte] = static_cast<uint8_t>(
              value.extractBits(8, valueByte * 8).getZExtValue());
        }
        usesPointerMemory = true;
        usesFunctionPointerMemory = true;
        return true;
      }
      auto *target = dyn_cast<GlobalVariable>(stripped);
      if (
          target == nullptr || pointerBits == 0 || pointerBits > 64 ||
          bytes == 0 || bytes > 8) {
        rejectGlobal(
            global,
            "pointer initializer is not a direct bounded data object");
        return false;
      }
      auto targetObject = layoutGlobal(*target);
      if (!targetObject)
        return false;
      APInt value(bytes * 8, targetObject->address);
      for (uint64_t memoryByte = 0; memoryByte < bytes; ++memoryByte) {
        uint64_t valueByte = M.getDataLayout().isLittleEndian()
                                 ? memoryByte
                                 : bytes - memoryByte - 1;
        initialMemory[address + memoryByte] = static_cast<uint8_t>(
            value.extractBits(8, valueByte * 8).getZExtValue());
      }
      usesPointerMemory = true;
      return true;
    }
    if (auto *data = dyn_cast<ConstantDataSequential>(&constant)) {
      Type *elementType = data->getElementType();
      uint64_t stride = fixedAllocBytes(M.getDataLayout(), elementType);
      for (unsigned index = 0; index < data->getNumElements(); ++index) {
        Constant *element = data->getElementAsConstant(index);
        if (!writeInitializer(
                *element, elementType, address + index * stride, global))
          return false;
      }
      return true;
    }
    if (auto *array = dyn_cast<ConstantArray>(&constant)) {
      Type *elementType = array->getType()->getElementType();
      uint64_t stride = fixedAllocBytes(M.getDataLayout(), elementType);
      for (unsigned index = 0; index < array->getNumOperands(); ++index)
        if (!writeInitializer(
                *cast<Constant>(array->getOperand(index)), elementType,
                address + index * stride, global))
          return false;
      return true;
    }
    if (auto *structure = dyn_cast<ConstantStruct>(&constant)) {
      StructType *structType = structure->getType();
      const StructLayout *layout =
          M.getDataLayout().getStructLayout(structType);
      for (unsigned index = 0; index < structure->getNumOperands(); ++index)
        if (!writeInitializer(
                *cast<Constant>(structure->getOperand(index)),
                structType->getElementType(index),
                address + layout->getElementOffset(index), global))
          return false;
      return true;
    }
    rejectGlobal(global, "initializer is outside the bounded memory subset");
    return false;
  }

  std::optional<StaticMemoryObject>
  layoutGlobal(GlobalVariable &global) {
    auto existing = memoryObjects.find(&global);
    if (existing != memoryObjects.end())
      return existing->second;
    if (global.isDeclaration() || !global.hasInitializer()) {
      rejectGlobal(global, "external or uninitialized global is unsupported");
      return std::nullopt;
    }
    if (global.isThreadLocal() || global.getAddressSpace() != 0) {
      rejectGlobal(global, "TLS or nonzero address space is unsupported");
      return std::nullopt;
    }
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits();
    if (pointerBits == 0 || pointerBits > 64) {
      rejectGlobal(global, "module pointer width exceeds 64 bits");
      return std::nullopt;
    }
    uint64_t size = fixedAllocBytes(
        M.getDataLayout(), global.getValueType());
    uint64_t alignment =
        M.getDataLayout().getABITypeAlign(global.getValueType()).value();
    if (MaybeAlign explicitAlignment = global.getAlign())
      alignment = std::max<uint64_t>(
          alignment, explicitAlignment->value());
    if (size == 0 || size > memoryLimit || alignment > memoryLimit) {
      rejectGlobal(global, "object size or alignment exceeds memory limit");
      return std::nullopt;
    }
    uint64_t address = alignTo(memoryCursor, alignment);
    if (address > memoryLimit || size > memoryLimit - address) {
      rejectGlobal(global, "static memory image exceeds memory limit");
      return std::nullopt;
    }
    initialMemory.resize(address + size, 0);
    StaticMemoryObject object{
        address, size, global.isConstant(),
    };
    memoryObjects[&global] = object;
    memoryCursor = address + size;
    json::Object metadata;
    metadata["name"] = global.getName().str();
    metadata["kind"] = "static";
    metadata["address"] = static_cast<int64_t>(address);
    metadata["size"] = static_cast<int64_t>(size);
    metadata["read_only"] = global.isConstant();
    memoryObjectsMetadata.push_back(std::move(metadata));
    if (!writeInitializer(
            *global.getInitializer(), global.getValueType(), address, global))
      return std::nullopt;
    return object;
  }

  std::optional<StaticPointer>
  applyGetElementPtr(const GEPOperator &getElement, StaticPointer base,
                     const Instruction &user) {
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
        getElement.getPointerAddressSpace());
    if (pointerBits == 0 || pointerBits > 64) {
      reject(user, "GEP pointer width exceeds 64 bits");
      return std::nullopt;
    }
    APInt offset(pointerBits, 0, true);
    if (!getElement.accumulateConstantOffset(M.getDataLayout(), offset)) {
      if (base.dynamicIndex != nullptr) {
        reject(user, "nested symbolic GEP is unsupported");
        return std::nullopt;
      }
      MapVector<Value *, APInt> variableOffsets;
      APInt constantOffset(pointerBits, 0, true);
      if (!getElement.collectOffset(
              M.getDataLayout(), pointerBits, variableOffsets,
              constantOffset) ||
          variableOffsets.size() != 1 ||
          !constantOffset.isSignedIntN(64)) {
        reject(user, "GEP requires one bounded symbolic offset");
        return std::nullopt;
      }
      Value *index = variableOffsets.begin()->first;
      const APInt &scaleValue = variableOffsets.begin()->second;
      unsigned indexBits = integerBits(index->getType());
      unsigned addressIndexBits = M.getDataLayout().getIndexSizeInBits(
          getElement.getPointerAddressSpace());
      if (addressIndexBits != pointerBits) {
        reject(user, "GEP pointer/index width mismatch is unsupported");
        return std::nullopt;
      }
      if (indexBits == 0 || !scaleValue.isSignedIntN(64) ||
          scaleValue.isZero()) {
        reject(user, "GEP symbolic offset has an unsupported scale");
        return std::nullopt;
      }
      int64_t constantDelta = constantOffset.getSExtValue();
      if ((constantDelta > 0 &&
           base.objectOffset > INT64_MAX - constantDelta) ||
          (constantDelta < 0 &&
           base.objectOffset < INT64_MIN - constantDelta)) {
        reject(user, "GEP offset overflows the bounded address model");
        return std::nullopt;
      }
      APInt address(pointerBits, base.address);
      address += constantOffset;
      uint64_t objectAddress =
          base.address - static_cast<uint64_t>(base.objectOffset);
      StaticPointer result{
          address.getZExtValue(),
          base.objectSize,
          base.objectOffset + constantDelta,
          base.readOnly,
          base.stackObject,
          base.heapObject,
          index,
          scaleValue.getSExtValue(),
          indexBits,
          objectAddress,
          indexBits == 64
              ? INT64_MIN
              : -(INT64_C(1) << (indexBits - 1)),
          indexBits == 64
              ? INT64_MAX
              : (INT64_C(1) << (indexBits - 1)) - 1,
      };
      if (!constrainDynamicInBounds(result, getElement, user))
        return std::nullopt;
      return result;
    }
    if (!offset.isSignedIntN(64)) {
      reject(user, "GEP constant offset exceeds 64 bits");
      return std::nullopt;
    }
    int64_t delta = offset.getSExtValue();
    if ((delta > 0 && base.objectOffset > INT64_MAX - delta) ||
        (delta < 0 && base.objectOffset < INT64_MIN - delta)) {
      reject(user, "GEP offset overflows the bounded address model");
      return std::nullopt;
    }
    if (getElement.isInBounds() &&
        !constrainDynamicObjectRange(base, user))
      return std::nullopt;
    int64_t objectOffset = base.objectOffset + delta;
    if (base.dynamicIndex == nullptr &&
        (objectOffset < 0 ||
         static_cast<uint64_t>(objectOffset) > base.objectSize)) {
      reject(user, "GEP escapes its static memory object");
      return std::nullopt;
    }
    APInt address(pointerBits, base.address);
    address += offset;
    uint64_t objectAddress =
        base.dynamicIndex == nullptr
            ? base.address - static_cast<uint64_t>(base.objectOffset)
            : base.dynamicObjectAddress;
    StaticPointer result{
        address.getZExtValue(),
        base.objectSize,
        objectOffset,
        base.readOnly,
        base.stackObject,
        base.heapObject,
        base.dynamicIndex,
        base.dynamicScale,
        base.dynamicIndexBits,
        objectAddress,
        base.dynamicMinimumIndex,
        base.dynamicMaximumIndex,
    };
    if (!constrainDynamicInBounds(result, getElement, user))
      return std::nullopt;
    return result;
  }

  std::optional<StaticPointer> staticPointer(Value *value,
                                             const Instruction &user) {
    if (!value->getType()->isPointerTy() ||
        value->getType()->getPointerAddressSpace() != 0) {
      reject(user, "pointer address space is unsupported");
      return std::nullopt;
    }
    if (isa<ConstantPointerNull>(value))
      return StaticPointer{};
    if (inputBufferAbi && value == inputPointerArgument)
      return StaticPointer{
          inputMemoryObject.address,
          inputMemoryObject.size,
          0,
          inputMemoryObject.readOnly,
          nullptr,
          nullptr,
      };
    if (auto *allocation = dyn_cast<AllocaInst>(value)) {
      auto object = layoutStack(*allocation);
      if (!object)
        return std::nullopt;
      return StaticPointer{
          object->address,
          object->size,
          0,
          object->readOnly,
          allocation,
          nullptr,
      };
    }
    if (auto *call = dyn_cast<CallBase>(value)) {
      Function *callee = call->getCalledFunction();
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "malloc" ||
           callee->getName() == "calloc" ||
           callee->getName() == "__cxa_allocate_exception")) {
        auto pool = layoutHeap(*call);
        if (!pool)
          return std::nullopt;
        if (pool->slots.size() != 1) {
          reject(user,
                 "multi-instance heap pointer requires provenance alternatives");
          return std::nullopt;
        }
        const StaticMemoryObject &object = pool->slots.front();
        return StaticPointer{
            object.address,
            object.size,
            0,
            object.readOnly,
            nullptr,
            call,
        };
      }
    }
    if (auto *global = dyn_cast<GlobalVariable>(value)) {
      auto object = layoutGlobal(*global);
      if (!object)
        return std::nullopt;
      return StaticPointer{
          object->address, object->size, 0, object->readOnly, nullptr, nullptr,
      };
    }
    if (auto *getElement = dyn_cast<GEPOperator>(value)) {
      auto base = staticPointer(getElement->getPointerOperand(), user);
      if (!base)
        return std::nullopt;
      return applyGetElementPtr(*getElement, *base, user);
    }
    Value *stripped = value->stripPointerCasts();
    if (stripped != value)
      return staticPointer(stripped, user);
    reject(user, "pointer is not rooted in a supported memory object");
    return std::nullopt;
  }

  bool constrainDynamicInBounds(StaticPointer &pointer,
                                const GEPOperator &getElement,
                                const Instruction &user) {
    if (pointer.dynamicIndex == nullptr || !getElement.isInBounds())
      return true;
    return constrainDynamicObjectRange(pointer, user);
  }

  bool constrainDynamicObjectRange(StaticPointer &pointer,
                                   const Instruction &user) {
    if (pointer.dynamicIndex == nullptr)
      return true;
    __int128 constant = static_cast<__int128>(pointer.objectOffset);
    __int128 scale = static_cast<__int128>(pointer.dynamicScale);
    __int128 extent = static_cast<__int128>(pointer.objectSize);
    __int128 first =
        scale > 0 ? ceilSignedDivision(-constant, scale)
                  : ceilSignedDivision(extent - constant, scale);
    __int128 second =
        scale > 0 ? floorSignedDivision(extent - constant, scale)
                  : floorSignedDivision(-constant, scale);
    __int128 minimum = std::max(
        first, static_cast<__int128>(pointer.dynamicMinimumIndex));
    __int128 maximum = std::min(
        second, static_cast<__int128>(pointer.dynamicMaximumIndex));
    if (minimum > maximum) {
      reject(user, "inbounds symbolic GEP has no defined index domain");
      return false;
    }
    pointer.dynamicMinimumIndex = static_cast<int64_t>(minimum);
    pointer.dynamicMaximumIndex = static_cast<int64_t>(maximum);
    return true;
  }

  bool validMemoryAccess(const StaticPointer &pointer, uint64_t bytes,
                         const Instruction &instruction, bool write) {
    if (pointer.address == 0 || pointer.objectOffset < 0 ||
        static_cast<uint64_t>(pointer.objectOffset) > pointer.objectSize ||
        bytes > pointer.objectSize -
                    static_cast<uint64_t>(pointer.objectOffset)) {
      reject(instruction, "memory access is outside its declared object");
      return false;
    }
    if (write && pointer.readOnly) {
      reject(instruction, "store targets a constant global object");
      return false;
    }
    return true;
  }

  std::optional<MemoryAliases>
  memoryAliases(const StaticPointer &pointer, uint64_t bytes,
                const Instruction &instruction, bool write) {
    if (pointer.dynamicIndex == nullptr) {
      if (!validMemoryAccess(pointer, bytes, instruction, write))
        return std::nullopt;
      return MemoryAliases{{pointer.address}, {}, 0, 0};
    }
    if (bytes == 0 || bytes > pointer.objectSize) {
      reject(instruction,
             "symbolic memory access exceeds its declared object");
      return std::nullopt;
    }
    if (write && pointer.readOnly) {
      reject(instruction, "store targets a constant global object");
      return std::nullopt;
    }
    __int128 minimumIndex =
        pointer.dynamicIndexBits == 64
            ? static_cast<__int128>(INT64_MIN)
            : -(
                  static_cast<__int128>(1)
                  << (pointer.dynamicIndexBits - 1));
    __int128 maximumIndex =
        pointer.dynamicIndexBits == 64
            ? static_cast<__int128>(INT64_MAX)
            : (
                  static_cast<__int128>(1)
                  << (pointer.dynamicIndexBits - 1)) -
                  1;
    minimumIndex = std::max(
        minimumIndex,
        static_cast<__int128>(pointer.dynamicMinimumIndex));
    maximumIndex = std::min(
        maximumIndex,
        static_cast<__int128>(pointer.dynamicMaximumIndex));
    __int128 scale = static_cast<__int128>(pointer.dynamicScale);
    MemoryAliases aliases;
    bool firstIndex = true;
    for (uint64_t offset = 0; offset <= pointer.objectSize - bytes;
         ++offset) {
      __int128 difference =
          static_cast<__int128>(offset) -
          static_cast<__int128>(pointer.objectOffset);
      if (difference % scale != 0)
        continue;
      __int128 index = difference / scale;
      if (index < minimumIndex || index > maximumIndex)
        continue;
      int64_t boundedIndex = static_cast<int64_t>(index);
      aliases.addresses.push_back(pointer.dynamicObjectAddress + offset);
      aliases.indices.push_back(boundedIndex);
      if (firstIndex) {
        aliases.minimumIndex = boundedIndex;
        aliases.maximumIndex = boundedIndex;
        firstIndex = false;
      } else {
        aliases.minimumIndex =
            std::min(aliases.minimumIndex, boundedIndex);
        aliases.maximumIndex =
            std::max(aliases.maximumIndex, boundedIndex);
      }
      if (aliases.addresses.size() > aliasLimit) {
        reject(instruction,
               "symbolic memory alias set exceeds continuation limit");
        return std::nullopt;
      }
    }
    if (aliases.addresses.empty()) {
      reject(instruction,
             "symbolic memory access has no in-object aliases");
      return std::nullopt;
    }
    usesBoundedAliases = true;
    return aliases;
  }

  static json::Array aliasArray(ArrayRef<uint64_t> aliases) {
    json::Array result;
    for (uint64_t address : aliases)
      result.push_back(static_cast<int64_t>(address));
    return result;
  }

  static json::Array aliasIndexArray(ArrayRef<int64_t> indices) {
    json::Array result;
    for (int64_t index : indices)
      result.push_back(index);
    return result;
  }

  std::optional<json::Object>
  pointerOperand(Value *value, FunctionContext &context,
                 const Instruction &user) {
    if (!value->getType()->isPointerTy() ||
        value->getType()->getPointerAddressSpace() != 0) {
      reject(user, "pointer operand has an unsupported type");
      return std::nullopt;
    }
    auto *call = dyn_cast<CallBase>(value);
    bool isHeapAllocation =
        call != nullptr && call->getCalledFunction() != nullptr &&
        call->getCalledFunction()->isDeclaration() &&
        (call->getCalledFunction()->getName() == "malloc" ||
         call->getCalledFunction()->getName() == "calloc" ||
         call->getCalledFunction()->getName() ==
             "__cxa_allocate_exception" ||
         call->getCalledFunction()->getName() == "realloc");
    bool isInternalPointerCall =
        call != nullptr && call->getType()->isPointerTy() &&
        (call->getCalledFunction() == nullptr ||
         !call->getCalledFunction()->isDeclaration());
    bool isRegionEffectCall =
        call != nullptr && call->getCalledFunction() != nullptr &&
        call->getCalledFunction()->isDeclaration() &&
        call->getType()->isPointerTy() &&
        (call->getCalledFunction()->getName() == "memcpy" ||
         call->getCalledFunction()->getName() == "memmove" ||
         call->getCalledFunction()->getName() == "memset" ||
         call->getCalledFunction()->getName() == "strcpy" ||
         call->getCalledFunction()->getName() == "strncpy");
    bool isPointerSearchCall =
        call != nullptr && call->getCalledFunction() != nullptr &&
        call->getCalledFunction()->isDeclaration() &&
        call->getType()->isPointerTy() &&
        (call->getCalledFunction()->getName() == "memchr" ||
         call->getCalledFunction()->getName() == "strchr");
    bool isSsaCopy =
        call != nullptr && call->getCalledFunction() != nullptr &&
        call->getCalledFunction()->isIntrinsic() &&
        call->getCalledFunction()->getIntrinsicID() ==
            Intrinsic::ssa_copy;
    if (isa<GetElementPtrInst>(value) || isa<LoadInst>(value) ||
        isa<SelectInst>(value) ||
        isa<PHINode>(value) || isa<Argument>(value) ||
        isHeapAllocation || isInternalPointerCall ||
        isRegionEffectCall || isPointerSearchCall || isSsaCopy) {
      auto found = context.values.find(value);
      if (found == context.values.end()) {
        reject(user, "pointer SSA value has no continuation binding");
        return std::nullopt;
      }
      return variableOperand(found->second);
    }
    auto pointer = staticPointer(value, user);
    if (!pointer)
      return std::nullopt;
    if (pointer->dynamicIndex != nullptr) {
      auto found = context.values.find(value);
      if (found == context.values.end()) {
        reject(user, "dynamic pointer has no continuation binding");
        return std::nullopt;
      }
      return variableOperand(found->second);
    }
    return integerConstant(
        static_cast<int64_t>(pointer->address),
        M.getDataLayout().getPointerSizeInBits());
  }

  std::optional<json::Object>
  functionPointerOperand(Value *value, FunctionContext &context,
                         const Instruction &user) {
    unsigned bits = M.getDataLayout().getPointerSizeInBits(
        value->getType()->getPointerAddressSpace());
    if (bits == 0 || bits > 64) {
      reject(user, "function pointer width is unsupported");
      return std::nullopt;
    }
    Value *stripped = value->stripPointerCasts();
    if (auto *function = dyn_cast<Function>(stripped)) {
      auto found = functionIds.find(function);
      if (found == functionIds.end()) {
        reject(user, "function pointer target has no stable identifier");
        return std::nullopt;
      }
      return integerConstant(
          static_cast<int64_t>(found->second), bits);
    }
    if (isa<ConstantPointerNull>(stripped))
      return integerConstant(0, bits);
    auto found = context.values.find(value);
    if (found == context.values.end() && stripped != value)
      found = context.values.find(stripped);
    if (found == context.values.end()) {
      reject(user, "function pointer has no continuation binding");
      return std::nullopt;
    }
    return variableOperand(found->second);
  }

  std::optional<std::vector<FunctionAlternative>>
  functionPointerAlternatives(
      Value *value, FunctionContext &context,
      const Instruction &user) {
    std::set<const Value *> active;
    return functionPointerAlternatives(
        value, context, user, active);
  }

  std::optional<std::vector<FunctionAlternative>>
  functionPointerAlternatives(
      Value *value, FunctionContext &context,
      const Instruction &user,
      std::set<const Value *> &active) {
    value = value->stripPointerCasts();
    if (auto *function = dyn_cast<Function>(value)) {
      if (functionIds.find(function) == functionIds.end()) {
        reject(user, "function pointer target is outside the reachable set");
        return std::nullopt;
      }
      return std::vector<FunctionAlternative>{
          FunctionAlternative{function, {}}};
    }
    if (isa<ConstantPointerNull>(value))
      return std::vector<FunctionAlternative>{};
    if (!active.insert(value).second) {
      reject(user, "cyclic function-pointer PHI is unsupported");
      return std::nullopt;
    }
    auto finish = [&active, value](
                      std::optional<std::vector<FunctionAlternative>>
                          result) {
      active.erase(value);
      return result;
    };
    if (auto *call = dyn_cast<CallBase>(value)) {
      Function *callee = call->getCalledFunction();
      if (
          callee != nullptr && callee->isIntrinsic() &&
          callee->getIntrinsicID() == Intrinsic::ssa_copy &&
          call->arg_size() == 1) {
        auto copied = functionPointerAlternatives(
            call->getArgOperand(0), context, user, active);
        return finish(std::move(copied));
      }
    }
    if (auto *load = dyn_cast<LoadInst>(value)) {
      unsigned bits = M.getDataLayout().getPointerSizeInBits(
          load->getType()->getPointerAddressSpace());
      std::string variable = context.values.lookup(load);
      if (bits == 0 || bits > 64 || variable.empty()) {
        reject(user, "function-pointer load has no bounded SSA binding");
        return finish(std::nullopt);
      }
      Value *slot = load->getPointerOperand()->stripPointerCasts();
      auto initializers = globalPointerInitializers(slot);
      std::vector<FunctionAlternative> combined;
      bool initialDefinitionReaches = false;
      auto stores = reachingPointerStores(
          *load, context, user, initializers.has_value(),
          &initialDefinitionReaches);
      if (!stores)
        return finish(std::nullopt);
      if (initializers && initialDefinitionReaches) {
        for (Constant *initializer : *initializers) {
          auto item = functionPointerAlternatives(
              initializer, context, user, active);
          if (!item)
            return finish(std::nullopt);
          for (FunctionAlternative &alternative : *item)
            if (std::none_of(
                    combined.begin(), combined.end(),
                    [&](const FunctionAlternative &existing) {
                      return existing.function == alternative.function;
                    }))
              combined.push_back(std::move(alternative));
        }
        usesPointerTables |= initializers->size() > 1;
      }
      usesPointerInitialDefinitionMerge |=
          initialDefinitionReaches && !stores->empty();
      for (StoreInst *store : *stores) {
        auto item = functionPointerAlternatives(
            store->getValueOperand(), context, user, active);
        if (!item)
          return finish(std::nullopt);
        for (FunctionAlternative &alternative : *item)
          if (std::none_of(
                  combined.begin(), combined.end(),
                  [&](const FunctionAlternative &existing) {
                    return existing.function ==
                           alternative.function;
                  }))
            combined.push_back(std::move(alternative));
      }
      if (combined.size() > 64) {
        reject(user, "function-pointer table exceeds target limit");
        return finish(std::nullopt);
      }
      if (combined.empty())
        return finish(std::nullopt);
      for (FunctionAlternative &alternative : combined) {
        auto identifier = functionIds.find(alternative.function);
        if (identifier == functionIds.end()) {
          reject(user, "loaded function target has no stable ID");
          return finish(std::nullopt);
        }
        alternative.guards.push_back(PointerGuard{
            nullptr,
            variable,
            static_cast<int64_t>(identifier->second),
            bits,
        });
      }
      usesPointerMemory = true;
      usesFunctionPointerMemory = true;
      return finish(std::move(combined));
    }
    if (auto *select = dyn_cast<SelectInst>(value)) {
      auto whenTrue = functionPointerAlternatives(
          select->getTrueValue(), context, user, active);
      auto whenFalse = functionPointerAlternatives(
          select->getFalseValue(), context, user, active);
      if (!whenTrue || !whenFalse)
        return finish(std::nullopt);
      PointerGuard trueGuard{
          select->getCondition(), "", 1, 1,
      };
      PointerGuard falseGuard{
          select->getCondition(), "", 0, 1,
      };
      for (FunctionAlternative &alternative : *whenTrue)
        alternative.guards.push_back(trueGuard);
      for (FunctionAlternative &alternative : *whenFalse)
        alternative.guards.push_back(falseGuard);
      whenTrue->insert(
          whenTrue->end(),
          std::make_move_iterator(whenFalse->begin()),
          std::make_move_iterator(whenFalse->end()));
      if (whenTrue->size() > 64) {
        reject(user, "indirect-call target set exceeds dispatch limit");
        return finish(std::nullopt);
      }
      return finish(std::move(whenTrue));
    }
    if (auto *phi = dyn_cast<PHINode>(value)) {
      std::string tag = context.pointerTags.lookup(phi);
      if (tag.empty()) {
        reject(user, "function-pointer PHI has no discriminator");
        return finish(std::nullopt);
      }
      std::vector<FunctionAlternative> result;
      for (unsigned index = 0;
           index < phi->getNumIncomingValues(); ++index) {
        auto incoming = functionPointerAlternatives(
            phi->getIncomingValue(index), context, user, active);
        if (!incoming)
          return finish(std::nullopt);
        PointerGuard guard{
            nullptr, tag,
            static_cast<int64_t>(context.blockIds.lookup(
                phi->getIncomingBlock(index))),
            32,
        };
        for (FunctionAlternative &alternative : *incoming) {
          alternative.guards.push_back(guard);
          result.push_back(std::move(alternative));
        }
        if (result.size() > 64) {
          reject(user, "indirect-call target set exceeds dispatch limit");
          return finish(std::nullopt);
        }
      }
      return finish(std::move(result));
    }
    reject(user, "function pointer provenance is not finite");
    return finish(std::nullopt);
  }

  static bool sameStaticPointer(const StaticPointer &left,
                                const StaticPointer &right) {
    return left.address == right.address &&
           left.objectSize == right.objectSize &&
           left.objectOffset == right.objectOffset &&
           left.readOnly == right.readOnly &&
           left.stackObject == right.stackObject &&
           left.heapObject == right.heapObject &&
           left.dynamicIndex == right.dynamicIndex &&
           left.dynamicScale == right.dynamicScale &&
           left.dynamicIndexBits == right.dynamicIndexBits &&
           left.dynamicObjectAddress ==
               right.dynamicObjectAddress &&
           left.dynamicMinimumIndex ==
               right.dynamicMinimumIndex &&
           left.dynamicMaximumIndex ==
               right.dynamicMaximumIndex &&
           left.interprocedural == right.interprocedural &&
           left.reallocationObject == right.reallocationObject;
  }

  static std::string pointerDomainParameter(unsigned argumentIndex) {
    return "__ptr_domain_" + std::to_string(argumentIndex);
  }

  std::optional<std::vector<StaticPointer>>
  expandInterproceduralPointers(
      ArrayRef<PointerAlternative> alternatives,
      const Instruction &user) {
    std::vector<StaticPointer> expanded;
    auto append = [&](StaticPointer pointer) {
      pointer.dynamicIndex = nullptr;
      pointer.dynamicScale = 0;
      pointer.dynamicIndexBits = 0;
      pointer.dynamicObjectAddress = 0;
      pointer.dynamicMinimumIndex = INT64_MIN;
      pointer.dynamicMaximumIndex = INT64_MAX;
      pointer.interprocedural = true;
      if (std::none_of(
              expanded.begin(), expanded.end(),
              [&](const StaticPointer &existing) {
                return sameStaticPointer(existing, pointer);
              }))
        expanded.push_back(std::move(pointer));
    };
    for (const PointerAlternative &alternative : alternatives) {
      const StaticPointer &pointer = alternative.pointer;
      if (pointer.dynamicIndex == nullptr) {
        append(pointer);
      } else {
        if (pointer.dynamicScale == 0 ||
            pointer.dynamicObjectAddress == 0) {
          reject(user,
                 "cross-function symbolic pointer domain is malformed");
          return std::nullopt;
        }
        __int128 scale = static_cast<__int128>(
            pointer.dynamicScale);
        for (uint64_t offset = 0; offset < pointer.objectSize;
             ++offset) {
          __int128 difference =
              static_cast<__int128>(offset) -
              static_cast<__int128>(pointer.objectOffset);
          if (difference % scale != 0)
            continue;
          __int128 index = difference / scale;
          if (index < pointer.dynamicMinimumIndex ||
              index > pointer.dynamicMaximumIndex)
            continue;
          StaticPointer concrete = pointer;
          concrete.address = pointer.dynamicObjectAddress + offset;
          concrete.objectOffset = static_cast<int64_t>(offset);
          append(std::move(concrete));
          if (expanded.size() > 256) {
            reject(
                user,
                "cross-function symbolic pointer domain exceeds union "
                "limit");
            return std::nullopt;
          }
        }
      }
      if (expanded.size() > 256) {
        reject(user,
               "cross-function pointer summary exceeds union limit");
        return std::nullopt;
      }
    }
    if (expanded.empty()) {
      reject(user, "cross-function pointer summary is empty");
      return std::nullopt;
    }
    return expanded;
  }

  std::optional<std::vector<PointerAlternative>>
  pointerAlternatives(Value *value, FunctionContext &context,
                      const Instruction &user) {
    std::set<const Value *> active;
    return pointerAlternatives(value, context, user, active);
  }

  std::optional<std::vector<StoreInst *>> reachingPointerStores(
      LoadInst &load, FunctionContext &,
      const Instruction &user,
      bool hasInitialDefinition = false,
      bool *initialDefinitionReaches = nullptr) {
    auto reaching = computeReachingPointerStores(
        load, hasInitialDefinition);
    if (!reaching.error.empty()) {
      reject(user, reaching.error);
      return std::nullopt;
    }
    if (reaching.stores.size() > 1)
      usesPointerMemoryMerges = true;
    if (reaching.traversedPredecessor && !reaching.traversedCycle)
      usesAcyclicPointerMemorySsa = true;
    if (reaching.traversedCycle)
      usesCyclicPointerMemorySsa = true;
    if (reaching.canonicalizedCell)
      usesCanonicalPointerCells = true;
    if (initialDefinitionReaches != nullptr)
      *initialDefinitionReaches =
          reaching.initialDefinitionReaches;
    return reaching.stores;
  }

  std::optional<std::vector<PointerAlternative>>
  pointerAlternatives(Value *value, FunctionContext &context,
                      const Instruction &user,
                      std::set<const Value *> &active) {
    if (!value->getType()->isPointerTy() ||
        value->getType()->getPointerAddressSpace() != 0) {
      reject(user, "pointer union has an unsupported type");
      return std::nullopt;
    }
    if (!active.insert(value).second) {
      reject(user, "cyclic pointer PHI provenance is unsupported");
      return std::nullopt;
    }
    auto finish = [&active, value](
                      std::optional<std::vector<PointerAlternative>> result) {
      active.erase(value);
      return result;
    };
    if (auto *call = dyn_cast<CallBase>(value)) {
      Function *callee = call->getCalledFunction();
      if (
          callee != nullptr && callee->isIntrinsic() &&
          callee->getIntrinsicID() == Intrinsic::ssa_copy &&
          call->arg_size() == 1) {
        auto copied = pointerAlternatives(
            call->getArgOperand(0), context, user, active);
        return finish(std::move(copied));
      }
    }
    if (auto *load = dyn_cast<LoadInst>(value)) {
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          load->getType()->getPointerAddressSpace());
      std::string variable = context.values.lookup(load);
      if (pointerBits == 0 || pointerBits > 64 || variable.empty()) {
        reject(user, "pointer load has no bounded SSA binding");
        return finish(std::nullopt);
      }
      Value *slot = load->getPointerOperand()->stripPointerCasts();
      auto initializers = globalPointerInitializers(slot);
      std::vector<PointerAlternative> alternatives;
      bool initialDefinitionReaches = false;
      auto stores = reachingPointerStores(
          *load, context, user, initializers.has_value(),
          &initialDefinitionReaches);
      if (!stores)
        return finish(std::nullopt);
      if (initializers && initialDefinitionReaches) {
        for (Constant *initializer : *initializers) {
          if (
              !isa<ConstantPointerNull>(initializer) &&
              isa<Function>(initializer->stripPointerCasts())) {
            reject(user, "function-pointer load used as a data pointer");
            return finish(std::nullopt);
          }
          auto item = pointerAlternatives(
              initializer, context, user, active);
          if (!item)
            return finish(std::nullopt);
          for (PointerAlternative &alternative : *item)
            if (std::none_of(
                    alternatives.begin(), alternatives.end(),
                    [&](const PointerAlternative &existing) {
                      return sameStaticPointer(
                          existing.pointer, alternative.pointer);
                    }))
              alternatives.push_back(std::move(alternative));
        }
        usesPointerTables |= initializers->size() > 1;
      }
      usesPointerInitialDefinitionMerge |=
          initialDefinitionReaches && !stores->empty();
      for (StoreInst *store : *stores) {
        if (isFunctionPointerValue(store->getValueOperand())) {
          reject(user, "function-pointer load used as a data pointer");
          return finish(std::nullopt);
        }
        auto item = pointerAlternatives(
            store->getValueOperand(), context, user, active);
        if (!item)
          return finish(std::nullopt);
        for (PointerAlternative &alternative : *item)
          if (std::none_of(
                  alternatives.begin(), alternatives.end(),
                  [&](const PointerAlternative &existing) {
                    return sameStaticPointer(
                        existing.pointer, alternative.pointer);
                  }))
            alternatives.push_back(std::move(alternative));
      }
      if (alternatives.size() > 256) {
        reject(user, "data-pointer table exceeds provenance limit");
        return finish(std::nullopt);
      }
      if (alternatives.empty())
        return finish(std::nullopt);
      std::vector<PointerAlternative> concreteAlternatives;
      for (PointerAlternative &alternative : alternatives) {
        const StaticPointer &pointer = alternative.pointer;
        if (pointer.dynamicIndex == nullptr) {
          concreteAlternatives.push_back(
              std::move(alternative));
          continue;
        }
        auto aliases = memoryAliases(
            pointer, 1, user, false);
        if (!aliases)
          return finish(std::nullopt);
        for (uint64_t address : aliases->addresses) {
          StaticPointer concrete = pointer;
          concrete.address = address;
          concrete.objectOffset = static_cast<int64_t>(
              address - pointer.dynamicObjectAddress);
          concrete.dynamicIndex = nullptr;
          concrete.dynamicScale = 0;
          concrete.dynamicIndexBits = 0;
          concrete.dynamicObjectAddress = 0;
          concrete.dynamicMinimumIndex = INT64_MIN;
          concrete.dynamicMaximumIndex = INT64_MAX;
          if (std::none_of(
                  concreteAlternatives.begin(),
                  concreteAlternatives.end(),
                  [&](const PointerAlternative &existing) {
                    return sameStaticPointer(
                        existing.pointer, concrete);
                  }))
            concreteAlternatives.push_back(PointerAlternative{
                std::move(concrete), alternative.guards});
        }
        usesSymbolicPointerMemory = true;
      }
      alternatives = std::move(concreteAlternatives);
      if (alternatives.empty()) {
        reject(user, "symbolic pointer-memory domain is empty");
        return finish(std::nullopt);
      }
      for (PointerAlternative &alternative : alternatives) {
        alternative.guards.push_back(PointerGuard{
            nullptr,
            variable,
            static_cast<int64_t>(alternative.pointer.address),
            pointerBits,
        });
      }
      usesPointerMemory = true;
      usesPointerUnions = true;
      return finish(std::move(alternatives));
    }
    if (auto *getElement = dyn_cast<GEPOperator>(value)) {
      auto alternatives = pointerAlternatives(
          getElement->getPointerOperand(), context, user, active);
      if (!alternatives)
        return finish(std::nullopt);
      for (PointerAlternative &alternative : *alternatives) {
        auto pointer = applyGetElementPtr(
            *getElement, alternative.pointer, user);
        if (!pointer)
          return finish(std::nullopt);
        alternative.pointer = *pointer;
      }
      if (alternatives->size() > 256) {
        reject(user, "GEP pointer provenance exceeds union limit");
        return finish(std::nullopt);
      }
      if (alternatives->size() != 1 ||
          !alternatives->front().guards.empty())
        usesPointerUnions = true;
      return finish(std::move(alternatives));
    }
    if (auto *argument = dyn_cast<Argument>(value)) {
      Function *owner = argument->getParent();
      std::vector<StaticPointer> summary;
      bool hasReachableCall = false;
      for (Function *caller : reachable) {
        for (Instruction &instruction : instructions(caller)) {
          auto *call = dyn_cast<CallBase>(&instruction);
          if (call == nullptr)
            continue;
          bool callsOwner = call->getCalledFunction() == owner;
          if (call->getCalledFunction() == nullptr) {
            FunctionContext callerContext(*caller);
            auto targets = functionPointerAlternatives(
                call->getCalledOperand(), callerContext, user);
            if (!targets)
              return finish(std::nullopt);
            callsOwner = std::any_of(
                targets->begin(), targets->end(),
                [owner](const FunctionAlternative &alternative) {
                  return alternative.function == owner;
                });
          }
          if (!callsOwner)
            continue;
          hasReachableCall = true;
          if (argument->getArgNo() >= call->arg_size()) {
            reject(user,
                   "cross-function pointer call arity is inconsistent");
            return finish(std::nullopt);
          }
          Value *actual = call->getArgOperand(argument->getArgNo());
          if (!actual->getType()->isPointerTy()) {
            reject(user,
                   "cross-function pointer argument has a non-pointer actual");
            return finish(std::nullopt);
          }
          FunctionContext callerContext(*caller);
          auto alternatives = pointerAlternatives(
              actual, callerContext, user, active);
          if (!alternatives)
            return finish(std::nullopt);
          auto expanded =
              expandInterproceduralPointers(*alternatives, user);
          if (!expanded)
            return finish(std::nullopt);
          for (StaticPointer &pointer : *expanded) {
            if (std::none_of(
                    summary.begin(), summary.end(),
                    [&](const StaticPointer &existing) {
                      return sameStaticPointer(existing, pointer);
                    }))
              summary.push_back(std::move(pointer));
          }
          if (summary.size() > 256) {
            reject(
                user,
                "cross-function pointer parameter exceeds union limit");
            return finish(std::nullopt);
          }
        }
      }
      if (hasReachableCall) {
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            argument->getType()->getPointerAddressSpace());
        std::string variable = context.values.lookup(argument);
        if (pointerBits == 0 || pointerBits > 64 || variable.empty()) {
          reject(user,
                 "cross-function pointer parameter has no bounded binding");
          return finish(std::nullopt);
        }
        std::vector<PointerAlternative> result;
        result.reserve(summary.size());
        for (StaticPointer &pointer : summary) {
          PointerGuard addressGuard{
              nullptr,
              variable,
              static_cast<int64_t>(pointer.address),
              pointerBits,
          };
          PointerGuard domainGuard{
              nullptr,
              pointerDomainParameter(argument->getArgNo()),
              1,
              1,
          };
          result.push_back(PointerAlternative{
              std::move(pointer),
              {std::move(addressGuard), std::move(domainGuard)},
          });
        }
        usesPointerUnions = true;
        usesCrossFunctionPointers = true;
        return finish(std::move(result));
      }
    }
    if (auto *call = dyn_cast<CallBase>(value)) {
      Function *callee = call->getCalledFunction();
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "strcpy" ||
           callee->getName() == "strncpy")) {
        if (
            call->arg_size() !=
                (callee->getName() == "strcpy" ? 2U : 3U) ||
            !call->getArgOperand(0)->getType()->isPointerTy()) {
          reject(user, "string copy has an unsupported signature");
          return finish(std::nullopt);
        }
        auto result = pointerAlternatives(
            call->getArgOperand(0), context, user, active);
        return finish(std::move(result));
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "memcpy" ||
           callee->getName() == "memmove" ||
           callee->getName() == "memset")) {
        if (call->arg_size() != 3 ||
            !call->getArgOperand(0)->getType()->isPointerTy()) {
          reject(user, "region effect has an unsupported signature");
          return finish(std::nullopt);
        }
        auto result = pointerAlternatives(
            call->getArgOperand(0), context, user, active);
        return finish(std::move(result));
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "memchr" ||
           callee->getName() == "strchr")) {
        bool isMemorySearch = callee->getName() == "memchr";
        if (
            call->arg_size() != (isMemorySearch ? 3U : 2U) ||
            !call->getArgOperand(0)->getType()->isPointerTy()) {
          reject(user, "pointer search has an unsupported signature");
          return finish(std::nullopt);
        }
        uint64_t requested = 64;
        if (isMemorySearch) {
          auto *count = dyn_cast<ConstantInt>(call->getArgOperand(2));
          if (count == nullptr ||
              count->getValue().getActiveBits() > 64 ||
              count->getZExtValue() > 64) {
            reject(
                user,
                "memchr summary requires a constant length at most 64 bytes");
            return finish(std::nullopt);
          }
          requested = count->getZExtValue();
        }
        auto sources = pointerAlternatives(
            call->getArgOperand(0), context, user, active);
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getType()->getPointerAddressSpace());
        std::string resultVariable = context.values.lookup(call);
        if (!sources || pointerBits == 0 || pointerBits > 64 ||
            resultVariable.empty())
          return finish(std::nullopt);
        std::vector<PointerAlternative> results;
        PointerGuard nullGuard{
            nullptr, resultVariable, 0, pointerBits,
        };
        results.push_back(
            PointerAlternative{StaticPointer{}, {std::move(nullGuard)}});
        for (const PointerAlternative &source : *sources) {
          std::vector<StaticPointer> concreteSources;
          if (source.pointer.dynamicIndex == nullptr) {
            concreteSources.push_back(source.pointer);
          } else {
            auto aliases = memoryAliases(
                source.pointer, 1, user, false);
            if (!aliases)
              return finish(std::nullopt);
            for (uint64_t address : aliases->addresses) {
              StaticPointer concrete = source.pointer;
              concrete.address = address;
              concrete.objectOffset = static_cast<int64_t>(
                  address - source.pointer.dynamicObjectAddress);
              concrete.dynamicIndex = nullptr;
              concrete.dynamicScale = 0;
              concrete.dynamicIndexBits = 0;
              concrete.dynamicObjectAddress = 0;
              concreteSources.push_back(std::move(concrete));
            }
          }
          for (const StaticPointer &base : concreteSources) {
            if (base.address == 0 || base.objectOffset < 0 ||
                static_cast<uint64_t>(base.objectOffset) >=
                    base.objectSize)
              continue;
            uint64_t remaining =
                base.objectSize -
                static_cast<uint64_t>(base.objectOffset);
            uint64_t extent = std::min(requested, remaining);
            for (uint64_t offset = 0; offset < extent; ++offset) {
              StaticPointer pointer = base;
              pointer.address += offset;
              pointer.objectOffset += static_cast<int64_t>(offset);
              std::vector<PointerGuard> guards = source.guards;
              guards.push_back(PointerGuard{
                  nullptr,
                  resultVariable,
                  static_cast<int64_t>(pointer.address),
                  pointerBits,
              });
              results.push_back(PointerAlternative{
                  std::move(pointer), std::move(guards)});
              if (results.size() > 256) {
                reject(
                    user,
                    "pointer search result exceeds union limit");
                return finish(std::nullopt);
              }
            }
          }
        }
        usesPointerUnions = true;
        usesPointerSearchSummaries = true;
        return finish(std::move(results));
      }
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "realloc") {
        if (call->arg_size() != 2 ||
            !call->getArgOperand(0)->getType()->isPointerTy()) {
          reject(user, "realloc has an unsupported signature");
          return finish(std::nullopt);
        }
        auto source = pointerAlternatives(
            call->getArgOperand(0), context, user, active);
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getType()->getPointerAddressSpace());
        std::string pointerVariable = context.values.lookup(call);
        if (!source || source->empty() || pointerBits == 0 ||
            pointerBits > 64 || pointerVariable.empty()) {
          reject(user, "realloc result has no bounded pointer binding");
          return finish(std::nullopt);
        }
        std::vector<PointerAlternative> alternatives;
        for (PointerAlternative &alternative : *source) {
          StaticPointer &pointer = alternative.pointer;
          if (pointer.address == 0 || pointer.heapObject == nullptr ||
              pointer.dynamicIndex != nullptr ||
              pointer.objectOffset != 0) {
            reject(
                user,
                "realloc requires a non-null supported heap object base");
            return finish(std::nullopt);
          }
          alternative.guards.push_back(PointerGuard{
              nullptr,
              pointerVariable,
              static_cast<int64_t>(pointer.address),
              pointerBits,
          });
          pointer.reallocationObject = call;
          alternatives.push_back(std::move(alternative));
        }
        usesPointerUnions = true;
        usesReallocHeap = true;
        return finish(std::move(alternatives));
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "malloc" ||
           callee->getName() == "calloc" ||
           callee->getName() == "__cxa_allocate_exception")) {
        auto pool = layoutHeap(*call);
        if (!pool)
          return finish(std::nullopt);
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getType()->getPointerAddressSpace());
        std::string pointerVariable = context.values.lookup(call);
        if (pointerBits == 0 || pointerBits > 64 ||
            pointerVariable.empty()) {
          reject(user, "heap pool pointer has no bounded SSA binding");
          return finish(std::nullopt);
        }
        std::vector<PointerAlternative> alternatives;
        alternatives.reserve(pool->slots.size());
        for (const StaticMemoryObject &slot : pool->slots) {
          StaticPointer pointer{
              slot.address,
              slot.size,
              0,
              slot.readOnly,
              nullptr,
              call,
          };
          PointerGuard guard{
              nullptr,
              pointerVariable,
              static_cast<int64_t>(slot.address),
              pointerBits,
          };
          alternatives.push_back(
              PointerAlternative{pointer, {std::move(guard)}});
        }
        usesPointerUnions = true;
        return finish(std::move(alternatives));
      }
      if ((callee == nullptr ||
           (callee != nullptr && !callee->isDeclaration())) &&
          call->getType()->isPointerTy()) {
        std::vector<Function *> returnTargets;
        if (callee != nullptr) {
          returnTargets.push_back(callee);
        } else {
          auto targets = functionPointerAlternatives(
              call->getCalledOperand(), context, user);
          if (!targets || targets->empty())
            return finish(std::nullopt);
          for (const FunctionAlternative &alternative : *targets)
            if (std::find(
                    returnTargets.begin(), returnTargets.end(),
                    alternative.function) == returnTargets.end())
              returnTargets.push_back(alternative.function);
        }
        std::vector<StaticPointer> summary;
        for (Function *target : returnTargets) {
          if (target == nullptr || target->isDeclaration() ||
              !target->getReturnType()->isPointerTy()) {
            reject(
                user,
                "indirect pointer-return target is inconsistent");
            return finish(std::nullopt);
          }
          FunctionContext calleeContext(*target);
          for (Instruction &instruction : instructions(target)) {
            auto *returnInstruction = dyn_cast<ReturnInst>(&instruction);
            if (returnInstruction == nullptr)
              continue;
            Value *returned = returnInstruction->getReturnValue();
            if (returned == nullptr ||
                !returned->getType()->isPointerTy()) {
              reject(user,
                     "cross-function pointer return is inconsistent");
              return finish(std::nullopt);
            }
            auto alternatives = pointerAlternatives(
                returned, calleeContext, user, active);
            if (!alternatives)
              return finish(std::nullopt);
            auto expanded =
                expandInterproceduralPointers(*alternatives, user);
            if (!expanded)
              return finish(std::nullopt);
            for (StaticPointer &pointer : *expanded) {
              if (pointer.stackObject != nullptr &&
                  pointer.stackObject->getFunction() == target) {
                reject(
                    user,
                    "pointer return escapes the callee stack frame");
                return finish(std::nullopt);
              }
              if (std::none_of(
                      summary.begin(), summary.end(),
                      [&](const StaticPointer &existing) {
                        return sameStaticPointer(existing, pointer);
                      }))
                summary.push_back(std::move(pointer));
            }
            if (summary.size() > 256) {
              reject(
                  user,
                  "cross-function pointer return exceeds union limit");
              return finish(std::nullopt);
            }
          }
        }
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getType()->getPointerAddressSpace());
        std::string variable = context.values.lookup(call);
        if (summary.empty() || pointerBits == 0 || pointerBits > 64 ||
            variable.empty()) {
          reject(user,
                 "cross-function pointer return has no bounded binding");
          return finish(std::nullopt);
        }
        std::vector<PointerAlternative> result;
        result.reserve(summary.size());
        for (StaticPointer &pointer : summary) {
          PointerGuard guard{
              nullptr,
              variable,
              static_cast<int64_t>(pointer.address),
              pointerBits,
          };
          PointerGuard domainGuard{
              nullptr,
              variable + "__domain",
              1,
              1,
          };
          result.push_back(PointerAlternative{
              std::move(pointer),
              {std::move(guard), std::move(domainGuard)},
          });
        }
        usesPointerUnions = true;
        usesCrossFunctionPointers = true;
        return finish(std::move(result));
      }
    }
    if (auto *select = dyn_cast<SelectInst>(value)) {
      if (!select->getCondition()->getType()->isIntegerTy(1)) {
        reject(user, "pointer select condition is not i1");
        return finish(std::nullopt);
      }
      auto whenTrue = pointerAlternatives(
          select->getTrueValue(), context, user, active);
      auto whenFalse = pointerAlternatives(
          select->getFalseValue(), context, user, active);
      if (!whenTrue || !whenFalse)
        return finish(std::nullopt);
      PointerGuard trueGuard{
          select->getCondition(), "", 1, 1,
      };
      PointerGuard falseGuard{
          select->getCondition(), "", 0, 1,
      };
      for (PointerAlternative &alternative : *whenTrue)
        alternative.guards.push_back(trueGuard);
      for (PointerAlternative &alternative : *whenFalse)
        alternative.guards.push_back(falseGuard);
      whenTrue->insert(
          whenTrue->end(),
          std::make_move_iterator(whenFalse->begin()),
          std::make_move_iterator(whenFalse->end()));
      if (whenTrue->size() > 256) {
        reject(user, "pointer select provenance exceeds union limit");
        return finish(std::nullopt);
      }
      usesPointerUnions = true;
      return finish(std::move(whenTrue));
    }
    if (auto *phi = dyn_cast<PHINode>(value)) {
      std::string tag = context.pointerTags.lookup(phi);
      if (tag.empty()) {
        reject(user, "pointer PHI has no provenance discriminator");
        return finish(std::nullopt);
      }
      std::vector<PointerAlternative> result;
      for (unsigned index = 0; index < phi->getNumIncomingValues(); ++index) {
        auto incoming = pointerAlternatives(
            phi->getIncomingValue(index), context, user, active);
        if (!incoming)
          return finish(std::nullopt);
        PointerGuard guard{
            nullptr, tag,
            static_cast<int64_t>(context.blockIds.lookup(
                phi->getIncomingBlock(index))),
            32,
        };
        for (PointerAlternative &alternative : *incoming) {
          alternative.guards.push_back(guard);
          result.push_back(std::move(alternative));
        }
        if (result.size() > 256) {
          reject(user, "pointer PHI provenance exceeds union limit");
          return finish(std::nullopt);
        }
      }
      usesPointerUnions = true;
      return finish(std::move(result));
    }
    auto pointer = staticPointer(value, user);
    if (!pointer)
      return finish(std::nullopt);
    std::vector<PointerAlternative> result;
    result.push_back(PointerAlternative{*pointer, {}});
    return finish(std::move(result));
  }

  std::optional<json::Array>
  guardArray(ArrayRef<PointerGuard> guards, FunctionContext &context,
             const Instruction &user) {
    json::Array result;
    for (const PointerGuard &guard : guards) {
      json::Object item;
      if (guard.condition != nullptr) {
        auto condition = operand(
            const_cast<Value *>(guard.condition), context, user);
        if (!condition)
          return std::nullopt;
        item["value"] = std::move(*condition);
      } else {
        item["value"] = variableOperand(guard.variable);
      }
      item["equals"] = guard.expected;
      item["bits"] = static_cast<int64_t>(guard.bits);
      result.push_back(std::move(item));
    }
    return result;
  }

  std::optional<json::Object> pointerDomainOperand(
      ArrayRef<PointerAlternative> alternatives,
      FunctionContext &context, const Instruction &user,
      json::Array &output) {
    std::vector<json::Object> alternativeConditions;
    for (const PointerAlternative &alternative : alternatives) {
      std::vector<json::Object> conditions;
      for (const PointerGuard &guard : alternative.guards) {
        std::optional<json::Object> value;
        if (guard.condition != nullptr) {
          value = operand(
              const_cast<Value *>(guard.condition), context, user);
        } else {
          value = variableOperand(guard.variable);
        }
        if (!value)
          return std::nullopt;
        std::string comparison =
            context.temporaryName("ptr_domain_guard_");
        json::Object instruction;
        instruction["op"] = "binary";
        instruction["operator"] = "eq";
        instruction["dst"] = comparison;
        instruction["left"] = std::move(*value);
        instruction["right"] =
            integerConstant(guard.expected, guard.bits);
        instruction["bits"] = 1;
        output.push_back(std::move(instruction));
        conditions.push_back(variableOperand(comparison));
      }
      const StaticPointer &pointer = alternative.pointer;
      if (pointer.dynamicIndex != nullptr) {
        auto index = operand(pointer.dynamicIndex, context, user);
        if (!index)
          return std::nullopt;
        for (auto bound : {
                 std::make_pair(
                     StringRef("sge"), pointer.dynamicMinimumIndex),
                 std::make_pair(
                     StringRef("sle"), pointer.dynamicMaximumIndex),
             }) {
          std::string comparison =
              context.temporaryName("ptr_domain_index_");
          json::Object instruction;
          instruction["op"] = "binary";
          instruction["operator"] = bound.first;
          instruction["dst"] = comparison;
          instruction["left"] = variableOperand(
              context.values.lookup(pointer.dynamicIndex));
          instruction["right"] = integerConstant(
              bound.second, pointer.dynamicIndexBits);
          instruction["bits"] = 1;
          output.push_back(std::move(instruction));
          conditions.push_back(variableOperand(comparison));
        }
      }
      if (conditions.empty())
        return integerConstant(1, 1);
      json::Object combined = std::move(conditions.front());
      for (size_t index = 1; index < conditions.size(); ++index) {
        std::string conjunction =
            context.temporaryName("ptr_domain_and_");
        json::Object instruction;
        instruction["op"] = "binary";
        instruction["operator"] = "and";
        instruction["dst"] = conjunction;
        instruction["left"] = std::move(combined);
        instruction["right"] = std::move(conditions[index]);
        instruction["bits"] = 1;
        output.push_back(std::move(instruction));
        combined = variableOperand(conjunction);
      }
      alternativeConditions.push_back(std::move(combined));
    }
    if (alternativeConditions.empty()) {
      reject(user, "pointer domain certificate is empty");
      return std::nullopt;
    }
    json::Object result = std::move(alternativeConditions.front());
    for (size_t index = 1;
         index < alternativeConditions.size(); ++index) {
      std::string disjunction =
          context.temporaryName("ptr_domain_or_");
      json::Object instruction;
      instruction["op"] = "binary";
      instruction["operator"] = "or";
      instruction["dst"] = disjunction;
      instruction["left"] = std::move(result);
      instruction["right"] =
          std::move(alternativeConditions[index]);
      instruction["bits"] = 1;
      output.push_back(std::move(instruction));
      result = variableOperand(disjunction);
    }
    return result;
  }

  std::optional<json::Object>
  aliasCase(const PointerAlternative &alternative,
            const MemoryAliases &aliases, FunctionContext &context,
            const Instruction &user) {
    auto guards = guardArray(alternative.guards, context, user);
    if (!guards)
      return std::nullopt;
    json::Object result;
    result["addresses"] = aliasArray(aliases.addresses);
    result["guards"] = std::move(*guards);
    if (alternative.pointer.dynamicIndex != nullptr) {
      auto index = operand(
          alternative.pointer.dynamicIndex, context, user);
      if (!index)
        return std::nullopt;
      result["alias_index"] = std::move(*index);
      result["alias_index_values"] =
          aliasIndexArray(aliases.indices);
      result["alias_index_bits"] =
          static_cast<int64_t>(alternative.pointer.dynamicIndexBits);
      result["alias_index_min"] = aliases.minimumIndex;
      result["alias_index_max"] = aliases.maximumIndex;
    }
    return result;
  }

  bool appendExternalByteLoad(
      Value *basePointer, uint64_t byteOffset,
      StringRef destination, FunctionContext &context,
      const Instruction &user, json::Array &output,
      StringRef guardVariable = "", StringRef regionSite = "",
      StringRef regionKind = "",
      std::optional<uint64_t> regionOffset = std::nullopt,
      uint64_t regionExtent = 0, unsigned regionLengthBits = 0,
      Value *regionLength = nullptr) {
    auto alternatives = pointerAlternatives(
        basePointer, context, user);
    if (!alternatives || alternatives->empty())
      return false;
    for (PointerAlternative &alternative : *alternatives) {
      StaticPointer &pointer = alternative.pointer;
      __int128 adjustedOffset =
          static_cast<__int128>(pointer.objectOffset) +
          static_cast<__int128>(byteOffset);
      if (adjustedOffset < INT64_MIN ||
          adjustedOffset > INT64_MAX) {
        reject(user, "external memory summary offset overflows");
        return false;
      }
      pointer.objectOffset = static_cast<int64_t>(adjustedOffset);
      if (pointer.dynamicIndex == nullptr) {
        if (pointer.address > UINT64_MAX - byteOffset) {
          reject(user, "external memory summary address overflows");
          return false;
        }
        pointer.address += byteOffset;
      }
    }
    bool isUnion = alternatives->size() != 1 ||
                   !alternatives->front().guards.empty();
    json::Object lowered;
    lowered["op"] = "load";
    lowered["dst"] = destination.str();
    if (isUnion) {
      auto address = pointerOperand(
          basePointer, context, user);
      if (!address)
        return false;
      if (byteOffset != 0) {
        std::string adjusted =
            context.temporaryName("external_address_");
        json::Object addition;
        addition["op"] = "binary";
        addition["operator"] = "add";
        addition["dst"] = adjusted;
        addition["left"] = std::move(*address);
        addition["right"] = integerConstant(
            static_cast<int64_t>(byteOffset),
            M.getDataLayout().getPointerSizeInBits());
        addition["bits"] = static_cast<int64_t>(
            M.getDataLayout().getPointerSizeInBits());
        output.push_back(std::move(addition));
        address = variableOperand(adjusted);
      }
      lowered["address"] = std::move(*address);
      json::Array cases;
      uint64_t totalAliases = 0;
      for (const PointerAlternative &alternative : *alternatives) {
        const StaticPointer &pointer = alternative.pointer;
        if ((pointer.address == 0 &&
             pointer.dynamicIndex == nullptr) ||
            (pointer.dynamicIndex == nullptr &&
             (pointer.objectOffset < 0 ||
              static_cast<uint64_t>(pointer.objectOffset) >=
                  pointer.objectSize)))
          continue;
        auto aliases = memoryAliases(
            pointer, 1, user, false);
        if (!aliases)
          return false;
        totalAliases += aliases->addresses.size();
        if (totalAliases > aliasLimit) {
          reject(
              user,
              "external summary alias set exceeds continuation limit");
          return false;
        }
        auto item = aliasCase(
            alternative, *aliases, context, user);
        if (!item)
          return false;
        cases.push_back(std::move(*item));
      }
      if (cases.empty()) {
        reject(user, "external memory summary has no valid alternatives");
        return false;
      }
      lowered["alias_cases"] = std::move(cases);
    } else {
      const StaticPointer &pointer =
          alternatives->front().pointer;
      auto aliases = memoryAliases(
          pointer, 1, user, false);
      if (!aliases)
        return false;
      if (pointer.dynamicIndex == nullptr) {
        lowered["address"] = integerConstant(
            static_cast<int64_t>(pointer.address),
            M.getDataLayout().getPointerSizeInBits());
      } else {
        auto address = pointerOperand(
            basePointer, context, user);
        auto aliasIndex = operand(
            pointer.dynamicIndex, context, user);
        if (!address || !aliasIndex)
          return false;
        if (byteOffset != 0) {
          std::string adjusted =
              context.temporaryName("external_address_");
          json::Object addition;
          addition["op"] = "binary";
          addition["operator"] = "add";
          addition["dst"] = adjusted;
          addition["left"] = std::move(*address);
          addition["right"] = integerConstant(
              static_cast<int64_t>(byteOffset),
              M.getDataLayout().getPointerSizeInBits());
          addition["bits"] = static_cast<int64_t>(
              M.getDataLayout().getPointerSizeInBits());
          output.push_back(std::move(addition));
          address = variableOperand(adjusted);
        }
        lowered["address"] = std::move(*address);
        lowered["aliases"] = aliasArray(aliases->addresses);
        lowered["alias_index_values"] =
            aliasIndexArray(aliases->indices);
        lowered["alias_index"] = std::move(*aliasIndex);
        lowered["alias_index_bits"] =
            static_cast<int64_t>(pointer.dynamicIndexBits);
        lowered["alias_index_min"] = aliases->minimumIndex;
        lowered["alias_index_max"] = aliases->maximumIndex;
      }
    }
    if (!guardVariable.empty()) {
      lowered["guard"] = variableOperand(guardVariable);
      usesGuardedLoads = true;
    }
    if (!regionSite.empty()) {
      auto length = operand(regionLength, context, user);
      if (!length || regionKind.empty() || !regionOffset ||
          *regionOffset >= regionExtent || regionExtent == 0 ||
          regionExtent > 64 || regionLengthBits == 0 ||
          regionLengthBits > 64 || guardVariable.empty()) {
        reject(user, "symbolic region read metadata is invalid");
        return false;
      }
      json::Object region;
      region["schema"] = "symcc-symbolic-length-region-read-v1";
      region["site"] = regionSite.str();
      region["kind"] = regionKind.str();
      region["offset"] = static_cast<int64_t>(*regionOffset);
      region["maximum_bytes"] = static_cast<int64_t>(regionExtent);
      region["length_bits"] = static_cast<int64_t>(regionLengthBits);
      region["length"] = std::move(*length);
      lowered["symbolic_region_read"] = std::move(region);
    }
    lowered["bits"] = 8;
    lowered["bytes"] = 1;
    output.push_back(std::move(lowered));
    return true;
  }

  bool appendExternalByteStore(
      Value *basePointer, uint64_t byteOffset,
      StringRef valueVariable, FunctionContext &context,
      const Instruction &user, json::Array &output,
      StringRef guardVariable = "", StringRef regionSite = "",
      StringRef regionKind = "",
      std::optional<uint64_t> regionOffset = std::nullopt,
      uint64_t regionExtent = 0, unsigned regionLengthBits = 0,
      Value *regionLength = nullptr) {
    auto alternatives = pointerAlternatives(
        basePointer, context, user);
    if (!alternatives || alternatives->empty())
      return false;
    for (PointerAlternative &alternative : *alternatives) {
      StaticPointer &pointer = alternative.pointer;
      __int128 adjustedOffset =
          static_cast<__int128>(pointer.objectOffset) +
          static_cast<__int128>(byteOffset);
      if (adjustedOffset < INT64_MIN ||
          adjustedOffset > INT64_MAX) {
        reject(user, "external memory summary offset overflows");
        return false;
      }
      pointer.objectOffset = static_cast<int64_t>(adjustedOffset);
      if (pointer.dynamicIndex == nullptr) {
        if (pointer.address > UINT64_MAX - byteOffset) {
          reject(user, "external memory summary address overflows");
          return false;
        }
        pointer.address += byteOffset;
      }
    }
    bool isUnion = alternatives->size() != 1 ||
                   !alternatives->front().guards.empty();
    json::Object lowered;
    lowered["op"] = "store";
    if (isUnion) {
      auto address = pointerOperand(
          basePointer, context, user);
      if (!address)
        return false;
      if (byteOffset != 0) {
        std::string adjusted =
            context.temporaryName("external_address_");
        json::Object addition;
        addition["op"] = "binary";
        addition["operator"] = "add";
        addition["dst"] = adjusted;
        addition["left"] = std::move(*address);
        addition["right"] = integerConstant(
            static_cast<int64_t>(byteOffset),
            M.getDataLayout().getPointerSizeInBits());
        addition["bits"] = static_cast<int64_t>(
            M.getDataLayout().getPointerSizeInBits());
        output.push_back(std::move(addition));
        address = variableOperand(adjusted);
      }
      lowered["address"] = std::move(*address);
      json::Array cases;
      uint64_t totalAliases = 0;
      for (const PointerAlternative &alternative : *alternatives) {
        const StaticPointer &pointer = alternative.pointer;
        if ((pointer.address == 0 &&
             pointer.dynamicIndex == nullptr) ||
            pointer.readOnly ||
            (pointer.dynamicIndex == nullptr &&
             (pointer.objectOffset < 0 ||
              static_cast<uint64_t>(pointer.objectOffset) >=
                  pointer.objectSize)))
          continue;
        auto aliases = memoryAliases(
            pointer, 1, user, true);
        if (!aliases)
          return false;
        totalAliases += aliases->addresses.size();
        if (totalAliases > aliasLimit) {
          reject(
              user,
              "external summary alias set exceeds continuation limit");
          return false;
        }
        auto item = aliasCase(
            alternative, *aliases, context, user);
        if (!item)
          return false;
        cases.push_back(std::move(*item));
      }
      if (cases.empty()) {
        reject(user, "external write summary has no valid alternatives");
        return false;
      }
      lowered["alias_cases"] = std::move(cases);
    } else {
      const StaticPointer &pointer =
          alternatives->front().pointer;
      auto aliases = memoryAliases(
          pointer, 1, user, true);
      if (!aliases)
        return false;
      if (pointer.dynamicIndex == nullptr) {
        lowered["address"] = integerConstant(
            static_cast<int64_t>(pointer.address),
            M.getDataLayout().getPointerSizeInBits());
      } else {
        auto address = pointerOperand(
            basePointer, context, user);
        auto aliasIndex = operand(
            pointer.dynamicIndex, context, user);
        if (!address || !aliasIndex)
          return false;
        if (byteOffset != 0) {
          std::string adjusted =
              context.temporaryName("external_address_");
          json::Object addition;
          addition["op"] = "binary";
          addition["operator"] = "add";
          addition["dst"] = adjusted;
          addition["left"] = std::move(*address);
          addition["right"] = integerConstant(
              static_cast<int64_t>(byteOffset),
              M.getDataLayout().getPointerSizeInBits());
          addition["bits"] = static_cast<int64_t>(
              M.getDataLayout().getPointerSizeInBits());
          output.push_back(std::move(addition));
          address = variableOperand(adjusted);
        }
        lowered["address"] = std::move(*address);
        lowered["aliases"] = aliasArray(aliases->addresses);
        lowered["alias_index_values"] =
            aliasIndexArray(aliases->indices);
        lowered["alias_index"] = std::move(*aliasIndex);
        lowered["alias_index_bits"] =
            static_cast<int64_t>(pointer.dynamicIndexBits);
        lowered["alias_index_min"] = aliases->minimumIndex;
        lowered["alias_index_max"] = aliases->maximumIndex;
      }
    }
    lowered["value"] = variableOperand(valueVariable);
    if (!guardVariable.empty()) {
      lowered["guard"] = variableOperand(guardVariable);
      usesGuardedStores = true;
    }
    if (!regionSite.empty()) {
      auto length = operand(regionLength, context, user);
      if (!length || regionKind.empty() || !regionOffset ||
          *regionOffset >= regionExtent || regionExtent == 0 ||
          regionExtent > 64 || regionLengthBits == 0 ||
          regionLengthBits > 64 || guardVariable.empty()) {
        reject(user, "symbolic region write metadata is invalid");
        return false;
      }
      json::Object region;
      region["schema"] = "symcc-symbolic-length-region-write-v1";
      region["site"] = regionSite.str();
      region["kind"] = regionKind.str();
      region["offset"] = static_cast<int64_t>(*regionOffset);
      region["maximum_bytes"] = static_cast<int64_t>(regionExtent);
      region["length_bits"] = static_cast<int64_t>(regionLengthBits);
      region["length"] = std::move(*length);
      lowered["symbolic_region"] = std::move(region);
    }
    lowered["bits"] = 8;
    lowered["bytes"] = 1;
    output.push_back(std::move(lowered));
    return true;
  }

  std::optional<SymbolicRegionEffect>
  symbolicRegionEffect(CallBase &call) const {
    if (auto *transfer = dyn_cast<MemTransferInst>(&call)) {
      Function *callee = call.getCalledFunction();
      if (transfer->isVolatile() || callee == nullptr)
        return std::nullopt;
      return SymbolicRegionEffect{
          callee->getIntrinsicID() == Intrinsic::memmove
              ? "memmove"
              : "memcpy",
          transfer->getRawDest(), transfer->getRawSource(),
          transfer->getLength()};
    }
    if (auto *memorySet = dyn_cast<MemSetInst>(&call)) {
      if (memorySet->isVolatile())
        return std::nullopt;
      return SymbolicRegionEffect{
          "memset", memorySet->getRawDest(), memorySet->getValue(),
          memorySet->getLength()};
    }
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isDeclaration() ||
        (callee->getName() != "memcpy" &&
         callee->getName() != "memmove" &&
         callee->getName() != "memset") ||
        call.arg_size() != 3)
      return std::nullopt;
    bool isSet = callee->getName() == "memset";
    if (!call.getArgOperand(0)->getType()->isPointerTy() ||
        (!isSet &&
         !call.getArgOperand(1)->getType()->isPointerTy()))
      return std::nullopt;
    return SymbolicRegionEffect{
        callee->getName().str(), call.getArgOperand(0),
        call.getArgOperand(1), call.getArgOperand(2)};
  }

  std::optional<uint64_t> boundedSymbolicRegionExtent(
      const SymbolicRegionEffect &effect, FunctionContext &context,
      const Instruction &user) {
    auto boundedSpan = [&](Value *value, bool write)
        -> std::optional<uint64_t> {
      auto alternatives = pointerAlternatives(value, context, user);
      if (!alternatives || alternatives->size() != 1)
        return std::nullopt;
      const StaticPointer &pointer = alternatives->front().pointer;
      if (pointer.address == 0 || pointer.dynamicIndex != nullptr ||
          pointer.objectOffset < 0 ||
          static_cast<uint64_t>(pointer.objectOffset) >= pointer.objectSize ||
          (write && pointer.readOnly))
        return std::nullopt;
      return std::min<uint64_t>(
          64, pointer.objectSize -
                  static_cast<uint64_t>(pointer.objectOffset));
    };
    auto destination = boundedSpan(effect.destination, true);
    if (!destination)
      return std::nullopt;
    uint64_t extent = *destination;
    if (effect.operation != "memset") {
      auto source = boundedSpan(effect.sourceOrValue, false);
      if (!source)
        return std::nullopt;
      extent = std::min(extent, *source);
    }
    return extent == 0 ? std::nullopt
                       : std::optional<uint64_t>(extent);
  }

  bool proveMemcpyDisjoint(
      Value *destination, Value *source, uint64_t bytes,
      FunctionContext &context, const Instruction &user) {
    auto destinations = pointerAlternatives(
        destination, context, user);
    auto sources = pointerAlternatives(
        source, context, user);
    if (!destinations || !sources ||
        destinations->empty() || sources->empty())
      return false;
    std::vector<uint64_t> destinationAddresses;
    std::vector<uint64_t> sourceAddresses;
    auto collect = [&](ArrayRef<PointerAlternative> alternatives,
                       std::vector<uint64_t> &addresses) {
      for (const PointerAlternative &alternative : alternatives) {
        const StaticPointer &pointer = alternative.pointer;
        if (pointer.address == 0 &&
            pointer.dynamicIndex == nullptr)
          continue;
        auto aliases = memoryAliases(
            pointer, bytes, user, false);
        if (!aliases)
          return false;
        addresses.insert(
            addresses.end(),
            aliases->addresses.begin(), aliases->addresses.end());
      }
      return !addresses.empty();
    };
    if (!collect(*destinations, destinationAddresses) ||
        !collect(*sources, sourceAddresses))
      return false;
    for (uint64_t destinationAddress : destinationAddresses)
      for (uint64_t sourceAddress : sourceAddresses)
        if (static_cast<__int128>(destinationAddress) <
                static_cast<__int128>(sourceAddress) + bytes &&
            static_cast<__int128>(sourceAddress) <
                static_cast<__int128>(destinationAddress) + bytes)
          return false;
    return true;
  }

  bool lowerBoundedRegionOperands(
      StringRef operation, Value *destination, Value *second,
      Value *length, StringRef resultDestination,
      unsigned resultBits, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    bool isSet = operation == "memset";
    auto *count = dyn_cast<ConstantInt>(length);
    bool symbolicLength = count == nullptr;
    unsigned lengthBits = integerBits(length->getType());
    uint64_t bytes = 0;
    if (!symbolicLength &&
        (count->getValue().getActiveBits() > 64 ||
         count->getZExtValue() > 64)) {
      reject(
          user,
          "region effect requires a constant length at most 64 bytes");
      return true;
    }
    std::string regionSite;
    if (symbolicLength) {
      SymbolicRegionEffect effect{
          operation.str(), destination, second, length};
      auto extent = boundedSymbolicRegionExtent(
          effect, context, user);
      if (!extent || lengthBits == 0 || lengthBits > 64) {
        reject(
            user,
            "symbolic region effect requires a bounded static object "
            "interval and integer length");
        return true;
      }
      uint64_t maximumRepresentable =
          lengthBits == 64
              ? UINT64_MAX
              : (UINT64_C(1) << lengthBits) - 1;
      bytes = std::min(*extent, maximumRepresentable);
      if (bytes == 0) {
        reject(user, "symbolic region effect has an empty capacity");
        return true;
      }
      regionSite = std::to_string(stableSiteId(user));
      if (maximumRepresentable > bytes) {
        auto lengthOperand = operand(length, context, user);
        if (!lengthOperand)
          return true;
        std::string bounded =
            context.temporaryName("symbolic_region_bound_");
        json::Object comparison;
        comparison["op"] = "binary";
        comparison["operator"] = "ule";
        comparison["dst"] = bounded;
        comparison["left"] = std::move(*lengthOperand);
        comparison["right"] = integerConstant(
            static_cast<int64_t>(bytes), lengthBits);
        comparison["bits"] = 1;
        json::Object comparisonMetadata;
        comparisonMetadata["schema"] =
            "symcc-symbolic-length-region-bound-v1";
        comparisonMetadata["site"] = regionSite;
        comparisonMetadata["maximum_bytes"] =
            static_cast<int64_t>(bytes);
        comparisonMetadata["length_bits"] =
            static_cast<int64_t>(lengthBits);
        comparison["symbolic_region_bound"] =
            std::move(comparisonMetadata);
        output.push_back(std::move(comparison));
        json::Object assume;
        assume["op"] = "assume";
        assume["condition"] = variableOperand(bounded);
        json::Object assumeMetadata;
        assumeMetadata["schema"] =
            "symcc-symbolic-length-region-bound-v1";
        assumeMetadata["site"] = regionSite;
        assumeMetadata["maximum_bytes"] =
            static_cast<int64_t>(bytes);
        assumeMetadata["length_bits"] =
            static_cast<int64_t>(lengthBits);
        assume["symbolic_region_bound"] =
            std::move(assumeMetadata);
        output.push_back(std::move(assume));
      }
    } else {
      bytes = count->getZExtValue();
    }
    if (
        operation == "memcpy" && bytes != 0 &&
        !proveMemcpyDisjoint(
            destination, second,
            bytes, context, user)) {
      reject(
          user,
          "memcpy summary cannot prove source and destination disjoint");
      return true;
    }
    std::vector<std::string> writeGuards(bytes);
    if (symbolicLength) {
      for (uint64_t offset = 0; offset < bytes; ++offset) {
        auto lengthOperand = operand(length, context, user);
        if (!lengthOperand)
          return true;
        std::string guard =
            context.temporaryName("symbolic_region_lane_");
        json::Object comparison;
        comparison["op"] = "binary";
        comparison["operator"] = "ult";
        comparison["dst"] = guard;
        comparison["left"] = integerConstant(
            static_cast<int64_t>(offset), lengthBits);
        comparison["right"] = std::move(*lengthOperand);
        comparison["bits"] = 1;
        json::Object metadata;
        metadata["schema"] =
            "symcc-symbolic-length-region-guard-v1";
        metadata["site"] = regionSite;
        metadata["offset"] = static_cast<int64_t>(offset);
        comparison["symbolic_region_guard"] = std::move(metadata);
        output.push_back(std::move(comparison));
        writeGuards[offset] = std::move(guard);
      }
    }
    std::vector<std::string> values;
    values.reserve(bytes);
    if (isSet && bytes != 0) {
      unsigned valueBits =
          integerBits(second->getType());
      auto value = operand(
          second, context, user);
      if (valueBits == 0 || !value) {
        reject(user, "memset value is not a bounded integer");
        return true;
      }
      std::string byte =
          context.temporaryName("memset_byte_");
      json::Object truncate;
      truncate["op"] = "unary";
      truncate["operator"] =
          valueBits > 8 ? "trunc" :
          (valueBits < 8 ? "zext" : "identity");
      truncate["dst"] = byte;
      truncate["value"] = std::move(*value);
      truncate["bits"] = 8;
      output.push_back(std::move(truncate));
      values.assign(bytes, byte);
    } else if (!isSet) {
      for (uint64_t offset = 0; offset < bytes; ++offset) {
        std::string byte =
            context.temporaryName("region_byte_");
        if (!appendExternalByteLoad(
                second, offset, byte,
                context, user, output,
                symbolicLength ? StringRef(writeGuards[offset])
                               : StringRef(),
                symbolicLength ? StringRef(regionSite) : StringRef(),
                symbolicLength ? operation : StringRef(),
                symbolicLength
                    ? std::optional<uint64_t>(offset)
                    : std::nullopt,
                symbolicLength ? bytes : 0,
                symbolicLength ? lengthBits : 0,
                symbolicLength ? length : nullptr))
          return true;
        values.push_back(std::move(byte));
      }
    }
    for (uint64_t offset = 0; offset < bytes; ++offset)
      if (!appendExternalByteStore(
              destination, offset, values[offset],
              context, user, output,
              symbolicLength ? StringRef(writeGuards[offset])
                             : StringRef(),
              symbolicLength ? StringRef(regionSite) : StringRef(),
              symbolicLength ? operation : StringRef(),
              symbolicLength
                  ? std::optional<uint64_t>(offset)
                  : std::nullopt,
              symbolicLength ? bytes : 0,
              symbolicLength ? lengthBits : 0,
              symbolicLength ? length : nullptr))
        return true;
    if (!resultDestination.empty()) {
      auto result = pointerOperand(
          destination, context, user);
      if (!result || resultBits == 0 || resultBits > 64)
        return true;
      json::Object bind;
      bind["op"] = "unary";
      bind["operator"] = "identity";
      bind["dst"] = resultDestination.str();
      bind["value"] = std::move(*result);
      bind["bits"] = static_cast<int64_t>(resultBits);
      output.push_back(std::move(bind));
    }
    usesExternalSummaries = true;
    usesSymbolicRegionEffects |= symbolicLength;
    return true;
  }

  bool lowerBoundedRegionEffect(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr ||
        (callee->getName() != "memcpy" &&
         callee->getName() != "memmove" &&
         callee->getName() != "memset"))
      return false;
    bool isSet = callee->getName() == "memset";
    if (
        call.arg_size() != 3 ||
        !call.getType()->isPointerTy() ||
        !call.getArgOperand(0)->getType()->isPointerTy() ||
        (!isSet &&
         !call.getArgOperand(1)->getType()->isPointerTy())) {
      reject(user, "region effect has an unsupported signature");
      return true;
    }
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
        call.getType()->getPointerAddressSpace());
    return lowerBoundedRegionOperands(
        callee->getName(), call.getArgOperand(0),
        call.getArgOperand(1), call.getArgOperand(2),
        context.values.lookup(&call), pointerBits,
        context, user, output);
  }

  bool lowerBoundedMemoryCompare(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr ||
        (callee->getName() != "memcmp" &&
         callee->getName() != "bcmp"))
      return false;
    if (
        call.arg_size() != 3 ||
        !call.getArgOperand(0)->getType()->isPointerTy() ||
        !call.getArgOperand(1)->getType()->isPointerTy()) {
      reject(user, "memory compare has an unsupported signature");
      return true;
    }
    unsigned resultBits = integerBits(call.getType());
    auto *count = dyn_cast<ConstantInt>(call.getArgOperand(2));
    if (resultBits < 2 || resultBits > 64 || count == nullptr ||
        count->getValue().getActiveBits() > 64 ||
        count->getZExtValue() > 64) {
      reject(
          user,
          "memory compare requires a constant length at most 64 bytes");
      return true;
    }
    uint64_t bytes = count->getZExtValue();
    if (bytes == 0) {
      json::Object zero;
      zero["op"] = "const";
      zero["dst"] = context.values.lookup(&call);
      zero["value"] = 0;
      zero["bits"] = static_cast<int64_t>(resultBits);
      output.push_back(std::move(zero));
      usesExternalSummaries = true;
      return true;
    }
    std::vector<std::string> leftBytes;
    std::vector<std::string> rightBytes;
    leftBytes.reserve(bytes);
    rightBytes.reserve(bytes);
    for (uint64_t offset = 0; offset < bytes; ++offset) {
      std::string left =
          context.temporaryName("memcmp_left_");
      std::string right =
          context.temporaryName("memcmp_right_");
      if (!appendExternalByteLoad(
              call.getArgOperand(0), offset, left,
              context, user, output) ||
          !appendExternalByteLoad(
              call.getArgOperand(1), offset, right,
              context, user, output))
        return true;
      leftBytes.push_back(std::move(left));
      rightBytes.push_back(std::move(right));
    }
    std::string result =
        context.temporaryName("memcmp_result_");
    json::Object zero;
    zero["op"] = "const";
    zero["dst"] = result;
    zero["value"] = 0;
    zero["bits"] = static_cast<int64_t>(resultBits);
    output.push_back(std::move(zero));
    for (uint64_t index = bytes; index-- > 0;) {
      std::string equal =
          context.temporaryName("memcmp_equal_");
      json::Object equality;
      equality["op"] = "binary";
      equality["operator"] = "eq";
      equality["dst"] = equal;
      equality["left"] = variableOperand(leftBytes[index]);
      equality["right"] = variableOperand(rightBytes[index]);
      equality["bits"] = 1;
      output.push_back(std::move(equality));

      std::string less =
          context.temporaryName("memcmp_less_");
      json::Object ordering;
      ordering["op"] = "binary";
      ordering["operator"] = "ult";
      ordering["dst"] = less;
      ordering["left"] = variableOperand(leftBytes[index]);
      ordering["right"] = variableOperand(rightBytes[index]);
      ordering["bits"] = 1;
      output.push_back(std::move(ordering));

      std::string difference =
          context.temporaryName("memcmp_difference_");
      json::Object sign;
      sign["op"] = "select";
      sign["dst"] = difference;
      sign["condition"] = variableOperand(less);
      sign["true"] = integerConstant(-1, resultBits);
      sign["false"] = integerConstant(1, resultBits);
      sign["bits"] = static_cast<int64_t>(resultBits);
      output.push_back(std::move(sign));

      std::string next =
          context.temporaryName("memcmp_prefix_");
      json::Object prefix;
      prefix["op"] = "select";
      prefix["dst"] = next;
      prefix["condition"] = variableOperand(equal);
      prefix["true"] = variableOperand(result);
      prefix["false"] = variableOperand(difference);
      prefix["bits"] = static_cast<int64_t>(resultBits);
      output.push_back(std::move(prefix));
      result = std::move(next);
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = context.values.lookup(&call);
    bind["value"] = variableOperand(result);
    bind["bits"] = static_cast<int64_t>(resultBits);
    output.push_back(std::move(bind));
    usesExternalSummaries = true;
    return true;
  }

  std::optional<uint64_t> boundedStringExtent(
      Value *value, FunctionContext &context,
      const Instruction &user) {
    auto alternatives = pointerAlternatives(value, context, user);
    if (!alternatives)
      return std::nullopt;
    uint64_t maximum = 0;
    for (const PointerAlternative &alternative : *alternatives) {
      const StaticPointer &pointer = alternative.pointer;
      uint64_t remaining = 0;
      if (pointer.address == 0 && pointer.dynamicIndex == nullptr)
        continue;
      if (pointer.dynamicIndex != nullptr) {
        remaining = pointer.objectSize;
      } else if (
          pointer.objectOffset >= 0 &&
          static_cast<uint64_t>(pointer.objectOffset) <
              pointer.objectSize) {
        remaining =
            pointer.objectSize -
            static_cast<uint64_t>(pointer.objectOffset);
      }
      maximum = std::max(maximum, remaining);
    }
    if (maximum == 0) {
      reject(user, "string summary has no readable object extent");
      return std::nullopt;
    }
    return std::min<uint64_t>(maximum, 64);
  }

  bool lowerBoundedStringSummary(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr ||
        (callee->getName() != "strlen" &&
         callee->getName() != "strcmp" &&
         callee->getName() != "strncmp"))
      return false;
    bool isLength = callee->getName() == "strlen";
    bool isBoundedCompare = callee->getName() == "strncmp";
    if (
        (isLength && call.arg_size() != 1) ||
        (!isLength && !isBoundedCompare && call.arg_size() != 2) ||
        (isBoundedCompare && call.arg_size() != 3) ||
        !call.getArgOperand(0)->getType()->isPointerTy() ||
        (!isLength &&
         !call.getArgOperand(1)->getType()->isPointerTy())) {
      reject(user, "string summary has an unsupported signature");
      return true;
    }
    unsigned resultBits = integerBits(call.getType());
    if (resultBits == 0 || resultBits > 64 ||
        (!isLength && resultBits < 2)) {
      reject(user, "string summary has an unsupported result width");
      return true;
    }
    uint64_t requested = 0;
    if (isBoundedCompare) {
      auto *count = dyn_cast<ConstantInt>(call.getArgOperand(2));
      if (count == nullptr ||
          count->getValue().getActiveBits() > 64 ||
          count->getZExtValue() > 64) {
        reject(
            user,
            "strncmp summary requires a constant length at most 64 bytes");
        return true;
      }
      requested = count->getZExtValue();
      if (requested == 0) {
        json::Object zero;
        zero["op"] = "const";
        zero["dst"] = context.values.lookup(&call);
        zero["value"] = 0;
        zero["bits"] = static_cast<int64_t>(resultBits);
        output.push_back(std::move(zero));
        usesExternalSummaries = true;
        usesStringSummaries = true;
        return true;
      }
    }
    auto leftExtent = boundedStringExtent(
        call.getArgOperand(0), context, user);
    if (!leftExtent)
      return true;
    uint64_t iterations = *leftExtent;
    if (!isLength) {
      auto rightExtent = boundedStringExtent(
          call.getArgOperand(1), context, user);
      if (!rightExtent)
        return true;
      iterations = std::min(iterations, *rightExtent);
    }
    if (isBoundedCompare)
      iterations = std::min(iterations, requested);
    if (iterations == 0) {
      reject(user, "string summary has no bounded search extent");
      return true;
    }
    if (
        isLength && resultBits < 64 &&
        iterations - 1 >= (UINT64_C(1) << resultBits)) {
      reject(user, "strlen result width cannot represent its bounded extent");
      return true;
    }

    std::string active = context.temporaryName("string_active_");
    json::Object initialActive;
    initialActive["op"] = "const";
    initialActive["dst"] = active;
    initialActive["value"] = 1;
    initialActive["bits"] = 1;
    output.push_back(std::move(initialActive));
    std::string result = context.temporaryName("string_result_");
    json::Object initialResult;
    initialResult["op"] = "const";
    initialResult["dst"] = result;
    initialResult["value"] = 0;
    initialResult["bits"] = static_cast<int64_t>(resultBits);
    output.push_back(std::move(initialResult));

    for (uint64_t offset = 0; offset < iterations; ++offset) {
      std::string left = context.temporaryName("string_left_");
      if (!appendExternalByteLoad(
              call.getArgOperand(0), offset, left,
              context, user, output, active))
        return true;
      std::string leftZero =
          context.temporaryName("string_left_zero_");
      json::Object leftZeroInstruction;
      leftZeroInstruction["op"] = "binary";
      leftZeroInstruction["operator"] = "eq";
      leftZeroInstruction["dst"] = leftZero;
      leftZeroInstruction["left"] = variableOperand(left);
      leftZeroInstruction["right"] = integerConstant(0, 8);
      leftZeroInstruction["bits"] = 1;
      output.push_back(std::move(leftZeroInstruction));

      std::string continuationCondition;
      if (isLength) {
        std::string found = context.temporaryName("strlen_found_");
        json::Object foundInstruction;
        foundInstruction["op"] = "binary";
        foundInstruction["operator"] = "and";
        foundInstruction["dst"] = found;
        foundInstruction["left"] = variableOperand(active);
        foundInstruction["right"] = variableOperand(leftZero);
        foundInstruction["bits"] = 1;
        output.push_back(std::move(foundInstruction));

        std::string nextResult =
            context.temporaryName("strlen_result_");
        json::Object selectResult;
        selectResult["op"] = "select";
        selectResult["dst"] = nextResult;
        selectResult["condition"] = variableOperand(found);
        selectResult["true"] = integerConstant(
            static_cast<int64_t>(offset), resultBits);
        selectResult["false"] = variableOperand(result);
        selectResult["bits"] = static_cast<int64_t>(resultBits);
        output.push_back(std::move(selectResult));
        result = std::move(nextResult);

        continuationCondition =
            context.temporaryName("strlen_nonzero_");
        json::Object nonzero;
        nonzero["op"] = "unary";
        nonzero["operator"] = "not";
        nonzero["dst"] = continuationCondition;
        nonzero["value"] = variableOperand(leftZero);
        nonzero["bits"] = 1;
        output.push_back(std::move(nonzero));
      } else {
        std::string right = context.temporaryName("string_right_");
        if (!appendExternalByteLoad(
                call.getArgOperand(1), offset, right,
                context, user, output, active))
          return true;
        std::string equal = context.temporaryName("string_equal_");
        json::Object equality;
        equality["op"] = "binary";
        equality["operator"] = "eq";
        equality["dst"] = equal;
        equality["left"] = variableOperand(left);
        equality["right"] = variableOperand(right);
        equality["bits"] = 1;
        output.push_back(std::move(equality));
        std::string different =
            context.temporaryName("string_different_");
        json::Object difference;
        difference["op"] = "unary";
        difference["operator"] = "not";
        difference["dst"] = different;
        difference["value"] = variableOperand(equal);
        difference["bits"] = 1;
        output.push_back(std::move(difference));
        std::string activeDifference =
            context.temporaryName("string_active_difference_");
        json::Object activeDifferenceInstruction;
        activeDifferenceInstruction["op"] = "binary";
        activeDifferenceInstruction["operator"] = "and";
        activeDifferenceInstruction["dst"] = activeDifference;
        activeDifferenceInstruction["left"] = variableOperand(active);
        activeDifferenceInstruction["right"] =
            variableOperand(different);
        activeDifferenceInstruction["bits"] = 1;
        output.push_back(std::move(activeDifferenceInstruction));
        std::string less = context.temporaryName("string_less_");
        json::Object ordering;
        ordering["op"] = "binary";
        ordering["operator"] = "ult";
        ordering["dst"] = less;
        ordering["left"] = variableOperand(left);
        ordering["right"] = variableOperand(right);
        ordering["bits"] = 1;
        output.push_back(std::move(ordering));
        std::string sign = context.temporaryName("string_sign_");
        json::Object signInstruction;
        signInstruction["op"] = "select";
        signInstruction["dst"] = sign;
        signInstruction["condition"] = variableOperand(less);
        signInstruction["true"] = integerConstant(-1, resultBits);
        signInstruction["false"] = integerConstant(1, resultBits);
        signInstruction["bits"] = static_cast<int64_t>(resultBits);
        output.push_back(std::move(signInstruction));
        std::string nextResult =
            context.temporaryName("string_compare_result_");
        json::Object selectResult;
        selectResult["op"] = "select";
        selectResult["dst"] = nextResult;
        selectResult["condition"] =
            variableOperand(activeDifference);
        selectResult["true"] = variableOperand(sign);
        selectResult["false"] = variableOperand(result);
        selectResult["bits"] = static_cast<int64_t>(resultBits);
        output.push_back(std::move(selectResult));
        result = std::move(nextResult);

        std::string leftNonzero =
            context.temporaryName("string_left_nonzero_");
        json::Object nonzero;
        nonzero["op"] = "unary";
        nonzero["operator"] = "not";
        nonzero["dst"] = leftNonzero;
        nonzero["value"] = variableOperand(leftZero);
        nonzero["bits"] = 1;
        output.push_back(std::move(nonzero));
        continuationCondition =
            context.temporaryName("string_equal_nonzero_");
        json::Object continueInstruction;
        continueInstruction["op"] = "binary";
        continueInstruction["operator"] = "and";
        continueInstruction["dst"] = continuationCondition;
        continueInstruction["left"] = variableOperand(equal);
        continueInstruction["right"] = variableOperand(leftNonzero);
        continueInstruction["bits"] = 1;
        output.push_back(std::move(continueInstruction));
      }
      std::string nextActive =
          context.temporaryName("string_next_active_");
      json::Object updateActive;
      updateActive["op"] = "binary";
      updateActive["operator"] = "and";
      updateActive["dst"] = nextActive;
      updateActive["left"] = variableOperand(active);
      updateActive["right"] =
          variableOperand(continuationCondition);
      updateActive["bits"] = 1;
      output.push_back(std::move(updateActive));
      active = std::move(nextActive);
    }
    bool requireTermination =
        !isBoundedCompare || requested > iterations;
    if (requireTermination) {
      std::string complete =
          context.temporaryName("string_complete_");
      json::Object invert;
      invert["op"] = "unary";
      invert["operator"] = "not";
      invert["dst"] = complete;
      invert["value"] = variableOperand(active);
      invert["bits"] = 1;
      output.push_back(std::move(invert));
      json::Object assume;
      assume["op"] = "assume";
      assume["condition"] = variableOperand(complete);
      output.push_back(std::move(assume));
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = context.values.lookup(&call);
    bind["value"] = variableOperand(result);
    bind["bits"] = static_cast<int64_t>(resultBits);
    output.push_back(std::move(bind));
    usesExternalSummaries = true;
    usesGuardedLoads = true;
    usesStringSummaries = true;
    return true;
  }

  bool lowerBoundedPointerSearch(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr ||
        (callee->getName() != "memchr" &&
         callee->getName() != "strchr"))
      return false;
    bool isMemorySearch = callee->getName() == "memchr";
    if (
        call.arg_size() != (isMemorySearch ? 3U : 2U) ||
        !call.getType()->isPointerTy() ||
        call.getType()->getPointerAddressSpace() != 0 ||
        !call.getArgOperand(0)->getType()->isPointerTy()) {
      reject(user, "pointer search has an unsupported signature");
      return true;
    }
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
        call.getType()->getPointerAddressSpace());
    unsigned needleBits = integerBits(
        call.getArgOperand(1)->getType());
    auto needle = operand(
        call.getArgOperand(1), context, user);
    if (pointerBits == 0 || pointerBits > 64 ||
        needleBits == 0 || !needle)
      return true;
    std::string byteNeedle =
        context.temporaryName("search_needle_");
    json::Object narrowNeedle;
    narrowNeedle["op"] = "unary";
    narrowNeedle["operator"] =
        needleBits > 8 ? "trunc" :
        (needleBits < 8 ? "zext" : "identity");
    narrowNeedle["dst"] = byteNeedle;
    narrowNeedle["value"] = std::move(*needle);
    narrowNeedle["bits"] = 8;
    output.push_back(std::move(narrowNeedle));

    uint64_t iterations = 0;
    if (isMemorySearch) {
      auto *count = dyn_cast<ConstantInt>(call.getArgOperand(2));
      if (count == nullptr ||
          count->getValue().getActiveBits() > 64 ||
          count->getZExtValue() > 64) {
        reject(
            user,
            "memchr summary requires a constant length at most 64 bytes");
        return true;
      }
      iterations = count->getZExtValue();
      if (iterations == 0) {
        json::Object zero;
        zero["op"] = "const";
        zero["dst"] = context.values.lookup(&call);
        zero["value"] = 0;
        zero["bits"] = static_cast<int64_t>(pointerBits);
        output.push_back(std::move(zero));
        usesExternalSummaries = true;
        usesPointerSearchSummaries = true;
        return true;
      }
      auto extent = boundedStringExtent(
          call.getArgOperand(0), context, user);
      if (!extent)
        return true;
      if (*extent < iterations) {
        reject(
            user,
            "memchr length exceeds every finite object extent");
        return true;
      }
    } else {
      auto extent = boundedStringExtent(
          call.getArgOperand(0), context, user);
      if (!extent)
        return true;
      iterations = *extent;
    }

    auto addressAt = [&](uint64_t offset)
        -> std::optional<json::Object> {
      auto address = pointerOperand(
          call.getArgOperand(0), context, user);
      if (!address)
        return std::nullopt;
      if (offset == 0)
        return address;
      std::string adjusted =
          context.temporaryName("search_address_");
      json::Object addition;
      addition["op"] = "binary";
      addition["operator"] = "add";
      addition["dst"] = adjusted;
      addition["left"] = std::move(*address);
      addition["right"] = integerConstant(
          static_cast<int64_t>(offset), pointerBits);
      addition["bits"] = static_cast<int64_t>(pointerBits);
      output.push_back(std::move(addition));
      return variableOperand(adjusted);
    };

    std::string result = context.temporaryName("search_result_");
    json::Object initialResult;
    initialResult["op"] = "const";
    initialResult["dst"] = result;
    initialResult["value"] = 0;
    initialResult["bits"] = static_cast<int64_t>(pointerBits);
    output.push_back(std::move(initialResult));
    if (isMemorySearch) {
      std::vector<std::string> matches;
      std::vector<json::Object> addresses;
      matches.reserve(iterations);
      addresses.reserve(iterations);
      for (uint64_t offset = 0; offset < iterations; ++offset) {
        std::string byte =
            context.temporaryName("memchr_byte_");
        if (!appendExternalByteLoad(
                call.getArgOperand(0), offset, byte,
                context, user, output))
          return true;
        std::string match =
            context.temporaryName("memchr_match_");
        json::Object equality;
        equality["op"] = "binary";
        equality["operator"] = "eq";
        equality["dst"] = match;
        equality["left"] = variableOperand(byte);
        equality["right"] = variableOperand(byteNeedle);
        equality["bits"] = 1;
        output.push_back(std::move(equality));
        auto address = addressAt(offset);
        if (!address)
          return true;
        matches.push_back(std::move(match));
        addresses.push_back(std::move(*address));
      }
      for (uint64_t index = iterations; index-- > 0;) {
        std::string next =
            context.temporaryName("memchr_result_");
        json::Object select;
        select["op"] = "select";
        select["dst"] = next;
        select["condition"] = variableOperand(matches[index]);
        select["true"] = std::move(addresses[index]);
        select["false"] = variableOperand(result);
        select["bits"] = static_cast<int64_t>(pointerBits);
        output.push_back(std::move(select));
        result = std::move(next);
      }
    } else {
      std::string active =
          context.temporaryName("strchr_active_");
      json::Object initialActive;
      initialActive["op"] = "const";
      initialActive["dst"] = active;
      initialActive["value"] = 1;
      initialActive["bits"] = 1;
      output.push_back(std::move(initialActive));
      for (uint64_t offset = 0; offset < iterations; ++offset) {
        std::string byte =
            context.temporaryName("strchr_byte_");
        if (!appendExternalByteLoad(
                call.getArgOperand(0), offset, byte,
                context, user, output, active))
          return true;
        std::string match =
            context.temporaryName("strchr_match_");
        json::Object equality;
        equality["op"] = "binary";
        equality["operator"] = "eq";
        equality["dst"] = match;
        equality["left"] = variableOperand(byte);
        equality["right"] = variableOperand(byteNeedle);
        equality["bits"] = 1;
        output.push_back(std::move(equality));
        std::string selected =
            context.temporaryName("strchr_selected_");
        json::Object selection;
        selection["op"] = "binary";
        selection["operator"] = "and";
        selection["dst"] = selected;
        selection["left"] = variableOperand(active);
        selection["right"] = variableOperand(match);
        selection["bits"] = 1;
        output.push_back(std::move(selection));
        auto address = addressAt(offset);
        if (!address)
          return true;
        std::string nextResult =
            context.temporaryName("strchr_result_");
        json::Object updateResult;
        updateResult["op"] = "select";
        updateResult["dst"] = nextResult;
        updateResult["condition"] = variableOperand(selected);
        updateResult["true"] = std::move(*address);
        updateResult["false"] = variableOperand(result);
        updateResult["bits"] = static_cast<int64_t>(pointerBits);
        output.push_back(std::move(updateResult));
        result = std::move(nextResult);

        std::string zero =
            context.temporaryName("strchr_zero_");
        json::Object zeroTest;
        zeroTest["op"] = "binary";
        zeroTest["operator"] = "eq";
        zeroTest["dst"] = zero;
        zeroTest["left"] = variableOperand(byte);
        zeroTest["right"] = integerConstant(0, 8);
        zeroTest["bits"] = 1;
        output.push_back(std::move(zeroTest));
        std::string noMatch =
            context.temporaryName("strchr_no_match_");
        json::Object invertMatch;
        invertMatch["op"] = "unary";
        invertMatch["operator"] = "not";
        invertMatch["dst"] = noMatch;
        invertMatch["value"] = variableOperand(match);
        invertMatch["bits"] = 1;
        output.push_back(std::move(invertMatch));
        std::string nonzero =
            context.temporaryName("strchr_nonzero_");
        json::Object invertZero;
        invertZero["op"] = "unary";
        invertZero["operator"] = "not";
        invertZero["dst"] = nonzero;
        invertZero["value"] = variableOperand(zero);
        invertZero["bits"] = 1;
        output.push_back(std::move(invertZero));
        std::string continueSearch =
            context.temporaryName("strchr_continue_");
        json::Object continuation;
        continuation["op"] = "binary";
        continuation["operator"] = "and";
        continuation["dst"] = continueSearch;
        continuation["left"] = variableOperand(noMatch);
        continuation["right"] = variableOperand(nonzero);
        continuation["bits"] = 1;
        output.push_back(std::move(continuation));
        std::string nextActive =
            context.temporaryName("strchr_next_active_");
        json::Object updateActive;
        updateActive["op"] = "binary";
        updateActive["operator"] = "and";
        updateActive["dst"] = nextActive;
        updateActive["left"] = variableOperand(active);
        updateActive["right"] = variableOperand(continueSearch);
        updateActive["bits"] = 1;
        output.push_back(std::move(updateActive));
        active = std::move(nextActive);
      }
      std::string complete =
          context.temporaryName("strchr_complete_");
      json::Object invert;
      invert["op"] = "unary";
      invert["operator"] = "not";
      invert["dst"] = complete;
      invert["value"] = variableOperand(active);
      invert["bits"] = 1;
      output.push_back(std::move(invert));
      json::Object assume;
      assume["op"] = "assume";
      assume["condition"] = variableOperand(complete);
      output.push_back(std::move(assume));
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = context.values.lookup(&call);
    bind["value"] = variableOperand(result);
    bind["bits"] = static_cast<int64_t>(pointerBits);
    output.push_back(std::move(bind));
    usesExternalSummaries = true;
    usesPointerSearchSummaries = true;
    usesStringSummaries |= !isMemorySearch;
    return true;
  }

  bool proveDistinctPointerObjects(
      Value *destination, Value *source,
      FunctionContext &context, const Instruction &user) {
    auto destinations = pointerAlternatives(
        destination, context, user);
    auto sources = pointerAlternatives(source, context, user);
    if (!destinations || !sources ||
        destinations->empty() || sources->empty())
      return false;
    auto objectBase = [](const StaticPointer &pointer)
        -> std::optional<uint64_t> {
      if (pointer.address == 0 && pointer.dynamicIndex == nullptr)
        return std::nullopt;
      if (pointer.dynamicIndex != nullptr)
        return pointer.dynamicObjectAddress;
      if (pointer.objectOffset < 0 ||
          pointer.address <
              static_cast<uint64_t>(pointer.objectOffset))
        return std::nullopt;
      return pointer.address -
             static_cast<uint64_t>(pointer.objectOffset);
    };
    for (const PointerAlternative &destinationAlternative :
         *destinations) {
      auto destinationBase =
          objectBase(destinationAlternative.pointer);
      if (!destinationBase)
        return false;
      for (const PointerAlternative &sourceAlternative : *sources) {
        auto sourceBase = objectBase(sourceAlternative.pointer);
        if (!sourceBase || *destinationBase == *sourceBase)
          return false;
      }
    }
    return true;
  }

  bool lowerBoundedStringCopy(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr ||
        (callee->getName() != "strcpy" &&
         callee->getName() != "strncpy"))
      return false;
    bool isBounded = callee->getName() == "strncpy";
    if (
        call.arg_size() != (isBounded ? 3U : 2U) ||
        !call.getType()->isPointerTy() ||
        call.getType()->getPointerAddressSpace() != 0 ||
        !call.getArgOperand(0)->getType()->isPointerTy() ||
        !call.getArgOperand(1)->getType()->isPointerTy()) {
      reject(user, "string copy has an unsupported signature");
      return true;
    }
    unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
        call.getType()->getPointerAddressSpace());
    if (pointerBits == 0 || pointerBits > 64)
      return true;
    uint64_t requested = 0;
    if (isBounded) {
      auto *count = dyn_cast<ConstantInt>(call.getArgOperand(2));
      if (count == nullptr ||
          count->getValue().getActiveBits() > 64 ||
          count->getZExtValue() > 64) {
        reject(
            user,
            "strncpy summary requires a constant length at most 64 bytes");
        return true;
      }
      requested = count->getZExtValue();
      if (requested == 0) {
        auto destination = pointerOperand(
            call.getArgOperand(0), context, user);
        if (!destination)
          return true;
        json::Object bind;
        bind["op"] = "unary";
        bind["operator"] = "identity";
        bind["dst"] = context.values.lookup(&call);
        bind["value"] = std::move(*destination);
        bind["bits"] = static_cast<int64_t>(pointerBits);
        output.push_back(std::move(bind));
        usesExternalSummaries = true;
        usesStringCopySummaries = true;
        return true;
      }
    }
    if (!proveDistinctPointerObjects(
            call.getArgOperand(0), call.getArgOperand(1),
            context, user)) {
      reject(
          user,
          "string copy cannot prove source and destination objects distinct");
      return true;
    }
    auto sourceExtent = boundedStringExtent(
        call.getArgOperand(1), context, user);
    auto destinationExtent = boundedStringExtent(
        call.getArgOperand(0), context, user);
    if (!sourceExtent || !destinationExtent)
      return true;
    uint64_t sourceIterations = *sourceExtent;
    uint64_t stores = 0;
    if (isBounded) {
      stores = requested;
      sourceIterations = std::min(sourceIterations, requested);
      if (*destinationExtent < requested) {
        reject(
            user,
            "strncpy length exceeds every destination object extent");
        return true;
      }
    } else {
      stores = std::min(*sourceExtent, *destinationExtent);
      sourceIterations = stores;
    }
    if (stores == 0 || sourceIterations == 0) {
      reject(user, "string copy has no bounded object extent");
      return true;
    }

    std::string active =
        context.temporaryName("string_copy_active_");
    json::Object initialActive;
    initialActive["op"] = "const";
    initialActive["dst"] = active;
    initialActive["value"] = 1;
    initialActive["bits"] = 1;
    output.push_back(std::move(initialActive));
    std::vector<std::string> values;
    std::vector<std::string> writeGuards;
    values.reserve(stores);
    writeGuards.reserve(stores);
    for (uint64_t offset = 0; offset < sourceIterations; ++offset) {
      std::string byte =
          context.temporaryName("string_copy_byte_");
      writeGuards.push_back(active);
      if (!appendExternalByteLoad(
              call.getArgOperand(1), offset, byte,
              context, user, output, active))
        return true;
      values.push_back(byte);
      std::string zero =
          context.temporaryName("string_copy_zero_");
      json::Object zeroTest;
      zeroTest["op"] = "binary";
      zeroTest["operator"] = "eq";
      zeroTest["dst"] = zero;
      zeroTest["left"] = variableOperand(byte);
      zeroTest["right"] = integerConstant(0, 8);
      zeroTest["bits"] = 1;
      output.push_back(std::move(zeroTest));
      std::string nonzero =
          context.temporaryName("string_copy_nonzero_");
      json::Object invert;
      invert["op"] = "unary";
      invert["operator"] = "not";
      invert["dst"] = nonzero;
      invert["value"] = variableOperand(zero);
      invert["bits"] = 1;
      output.push_back(std::move(invert));
      std::string nextActive =
          context.temporaryName("string_copy_next_active_");
      json::Object update;
      update["op"] = "binary";
      update["operator"] = "and";
      update["dst"] = nextActive;
      update["left"] = variableOperand(active);
      update["right"] = variableOperand(nonzero);
      update["bits"] = 1;
      output.push_back(std::move(update));
      active = std::move(nextActive);
    }
    if (!isBounded || requested > sourceIterations) {
      std::string complete =
          context.temporaryName("string_copy_complete_");
      json::Object invert;
      invert["op"] = "unary";
      invert["operator"] = "not";
      invert["dst"] = complete;
      invert["value"] = variableOperand(active);
      invert["bits"] = 1;
      output.push_back(std::move(invert));
      json::Object assume;
      assume["op"] = "assume";
      assume["condition"] = variableOperand(complete);
      output.push_back(std::move(assume));
    }
    if (isBounded && values.size() < stores) {
      std::string zero =
          context.temporaryName("string_copy_padding_");
      json::Object zeroByte;
      zeroByte["op"] = "const";
      zeroByte["dst"] = zero;
      zeroByte["value"] = 0;
      zeroByte["bits"] = 8;
      output.push_back(std::move(zeroByte));
      values.resize(stores, zero);
    }
    for (uint64_t offset = 0; offset < stores; ++offset) {
      StringRef guard =
          isBounded ? StringRef() : StringRef(writeGuards[offset]);
      if (!appendExternalByteStore(
              call.getArgOperand(0), offset, values[offset],
              context, user, output, guard))
        return true;
    }
    auto destination = pointerOperand(
        call.getArgOperand(0), context, user);
    if (!destination)
      return true;
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = context.values.lookup(&call);
    bind["value"] = std::move(*destination);
    bind["bits"] = static_cast<int64_t>(pointerBits);
    output.push_back(std::move(bind));
    usesExternalSummaries = true;
    usesStringSummaries = true;
    usesStringCopySummaries = true;
    return true;
  }

  static bool pointerIntervalCovers(
      const StaticPointer &stored, uint64_t storeBytes,
      const StaticPointer &loaded, uint64_t loadBytes) {
    if (stored.stackObject != loaded.stackObject ||
        stored.heapObject != loaded.heapObject)
      return false;
    if ((stored.dynamicIndex == nullptr && stored.objectOffset < 0) ||
        (loaded.dynamicIndex == nullptr && loaded.objectOffset < 0))
      return false;
    uint64_t storedBase =
        stored.dynamicIndex == nullptr
            ? stored.address - static_cast<uint64_t>(stored.objectOffset)
            : stored.dynamicObjectAddress;
    uint64_t loadedBase =
        loaded.dynamicIndex == nullptr
            ? loaded.address - static_cast<uint64_t>(loaded.objectOffset)
            : loaded.dynamicObjectAddress;
    if (storedBase != loadedBase)
      return false;
    if (loaded.dynamicIndex != nullptr) {
      return stored.dynamicIndex == loaded.dynamicIndex &&
             stored.dynamicScale == loaded.dynamicScale &&
             stored.dynamicIndexBits == loaded.dynamicIndexBits &&
             stored.objectOffset == loaded.objectOffset &&
             storeBytes >= loadBytes;
    }
    if (stored.dynamicIndex != nullptr || stored.objectOffset < 0 ||
        loaded.objectOffset < 0 || storeBytes < loadBytes)
      return false;
    uint64_t storedOffset = static_cast<uint64_t>(stored.objectOffset);
    uint64_t loadedOffset = static_cast<uint64_t>(loaded.objectOffset);
    return storedOffset <= loadedOffset &&
           loadedOffset - storedOffset <= storeBytes - loadBytes;
  }

  std::optional<GuardedHeapInitializationCertificate>
  guardedHeapUnionInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumPaths = 64;
    constexpr size_t maximumBlocksPerPath = 64;
    constexpr size_t maximumDecisionDepth = 8;
    constexpr size_t maximumReferencedBlocks =
        maximumPaths * maximumBlocksPerPath;
    BasicBlock *merge = load.getParent();
    if (alternatives.size() < 2 || pred_size(merge) < 2 ||
        pred_size(merge) > maximumPaths)
      return std::nullopt;
    if (std::any_of(
            alternatives.begin(), alternatives.end(),
            [](const PointerAlternative &alternative) {
              const StaticPointer &pointer = alternative.pointer;
              return pointer.heapObject == nullptr ||
                     pointer.interprocedural ||
                     pointer.dynamicIndex != nullptr ||
                     pointer.objectOffset < 0;
            }))
      return std::nullopt;

    BasicBlock *root = nullptr;
    for (BasicBlock *predecessor : predecessors(merge))
      root = root == nullptr
                 ? predecessor
                 : context.dominators.findNearestCommonDominator(
                       root, predecessor);
    auto *rootBranch =
        root == nullptr ? nullptr
                        : dyn_cast<BranchInst>(root->getTerminator());
    if (root == nullptr || root == merge || rootBranch == nullptr ||
        !rootBranch->isConditional() ||
        !context.dominators.dominates(root, merge))
      return std::nullopt;

    struct RawPath {
      std::vector<const BasicBlock *> blocks;
      std::vector<GuardedHeapInitializationDecision> decisions;
      const BasicBlock *predecessor = nullptr;
    };
    std::vector<RawPath> rawPaths;
    std::set<const BasicBlock *> uniqueBlocks;
    unsigned maximumDepth = 0;
    std::function<bool(
        const BasicBlock *, std::vector<const BasicBlock *>,
        std::vector<GuardedHeapInitializationDecision>,
        std::set<const BasicBlock *>)>
        enumerate = [&]
        (const BasicBlock *block,
         std::vector<const BasicBlock *> blocks,
         std::vector<GuardedHeapInitializationDecision> decisions,
         std::set<const BasicBlock *> active) -> bool {
      if (block == merge) {
        if (blocks.empty() || rawPaths.size() >= maximumPaths)
          return false;
        const BasicBlock *predecessor = blocks.back();
        maximumDepth = std::max(
            maximumDepth, static_cast<unsigned>(decisions.size()));
        rawPaths.push_back(
            RawPath{std::move(blocks), std::move(decisions),
                    predecessor});
        return true;
      }
      if (blocks.size() >= maximumBlocksPerPath ||
          decisions.size() > maximumDecisionDepth ||
          !active.insert(block).second ||
          !context.dominators.dominates(root, block) ||
          (!block->empty() && isa<PHINode>(block->front())))
        return false;
      uniqueBlocks.insert(block);
      blocks.push_back(block);
      auto *branch = dyn_cast<BranchInst>(block->getTerminator());
      if (branch == nullptr)
        return false;
      if (!branch->isConditional())
        return enumerate(
            branch->getSuccessor(0), std::move(blocks),
            std::move(decisions), std::move(active));
      if (!branch->getCondition()->getType()->isIntegerTy(1) ||
          decisions.size() >= maximumDecisionDepth)
        return false;
      for (unsigned successor = 0; successor < 2; ++successor) {
        auto childDecisions = decisions;
        childDecisions.push_back(GuardedHeapInitializationDecision{
            branch, successor == 0});
        if (!enumerate(
                branch->getSuccessor(successor), blocks,
                std::move(childDecisions), active))
          return false;
      }
      return true;
    };
    if (!enumerate(root, {}, {}, {}) || rawPaths.size() < 2 ||
        rawPaths.size() != pred_size(merge) ||
        uniqueBlocks.size() > maximumReferencedBlocks ||
        maximumDepth == 0 || maximumDepth > maximumDecisionDepth)
      return std::nullopt;
    std::set<const BasicBlock *> pathPredecessors;
    for (const RawPath &path : rawPaths) {
      std::map<const Value *, bool> decisions;
      for (const GuardedHeapInitializationDecision &decision :
           path.decisions) {
        const Value *condition = decision.branch->getCondition();
        auto [position, inserted] =
            decisions.emplace(condition, decision.expected);
        if (!inserted && position->second != decision.expected)
          return std::nullopt;
      }
      pathPredecessors.insert(path.predecessor);
    }
    if (pathPredecessors.size() != pred_size(merge))
      return std::nullopt;
    for (const BasicBlock *predecessor : predecessors(merge))
      if (pathPredecessors.count(predecessor) == 0)
        return std::nullopt;

    std::string mergePointerTag = context.pointerBlockTags.lookup(merge);
    if (!merge->empty() && isa<PHINode>(merge->front()) &&
        mergePointerTag.empty())
      return std::nullopt;
    std::set<std::string> pointerTags;
    for (const auto &item : context.pointerBlockTags)
      pointerTags.insert(item.second);
    GuardedHeapInitializationCertificate certificate;
    certificate.root = root;
    certificate.merge = merge;
    certificate.depth = maximumDepth;
    std::set<const StoreInst *> stores;
    std::set<uint64_t> bases;
    for (RawPath &path : rawPaths) {
      std::vector<size_t> selected;
      for (size_t index = 0; index < alternatives.size(); ++index) {
        const PointerAlternative &alternative = alternatives[index];
        bool pathSpecific = false;
        bool excluded = false;
        bool unresolved = false;
        for (const PointerGuard &guard : alternative.guards) {
          if (guard.condition != nullptr) {
            auto decision = std::find_if(
                path.decisions.begin(), path.decisions.end(),
                [&](const GuardedHeapInitializationDecision &item) {
                  return item.branch->getCondition() == guard.condition;
                });
            if (decision == path.decisions.end()) {
              unresolved = true;
              continue;
            }
            pathSpecific = true;
            if (guard.bits != 1 ||
                guard.expected != static_cast<int64_t>(decision->expected)) {
              excluded = true;
              break;
            }
          } else if (
              !mergePointerTag.empty() &&
              guard.variable == mergePointerTag) {
            pathSpecific = true;
            if (guard.bits != 32 ||
                guard.expected != static_cast<int64_t>(
                    context.blockIds.lookup(path.predecessor))) {
              excluded = true;
              break;
            }
          } else if (pointerTags.count(guard.variable) != 0) {
            unresolved = true;
          }
        }
        if (excluded)
          continue;
        if (unresolved || !pathSpecific)
          return std::nullopt;
        selected.push_back(index);
      }
      if (selected.empty())
        return std::nullopt;
      const StaticPointer &loaded = alternatives[selected.front()].pointer;
      uint64_t base =
          loaded.address - static_cast<uint64_t>(loaded.objectOffset);
      if (std::any_of(
              selected.begin(), selected.end(),
              [&](size_t index) {
                const StaticPointer &candidate = alternatives[index].pointer;
                return candidate.heapObject != loaded.heapObject ||
                       candidate.address != loaded.address ||
                       candidate.objectOffset != loaded.objectOffset;
              }))
        return std::nullopt;

      const StoreInst *witness = nullptr;
      StaticPointer storedPointer;
      uint64_t witnessBytes = 0;
      for (auto block = path.blocks.rbegin();
           block != path.blocks.rend() && witness == nullptr; ++block) {
        if (std::any_of(
                (*block)->begin(), (*block)->end(),
                [](const Instruction &instruction) {
                  return isa<CallBase>(instruction);
                }))
          continue;
        for (auto instruction = (*block)->rbegin();
             instruction != (*block)->rend(); ++instruction) {
          auto *store = dyn_cast<StoreInst>(&*instruction);
          if (store == nullptr || store->isVolatile() || store->isAtomic())
            continue;
          unsigned storeBits =
              store->getValueOperand()->getType()->isPointerTy()
                  ? M.getDataLayout().getPointerSizeInBits(
                        store->getValueOperand()->getType()
                            ->getPointerAddressSpace())
                  : integerBits(store->getValueOperand()->getType());
          uint64_t storeBytes =
              storeBits == 0
                  ? 0
                  : fixedStoreBytes(
                        M.getDataLayout(),
                        store->getValueOperand()->getType());
          if (storeBytes < bytes)
            continue;
          auto storeAlternatives = pointerAlternatives(
              const_cast<Value *>(store->getPointerOperand()),
              context, *store);
          if (!storeAlternatives || storeAlternatives->size() != 1)
            continue;
          const StaticPointer &stored = storeAlternatives->front().pointer;
          if (stored.heapObject == nullptr || stored.interprocedural ||
              stored.dynamicIndex != nullptr || stored.objectOffset < 0 ||
              !pointerIntervalCovers(stored, storeBytes, loaded, bytes))
            continue;
          witness = store;
          storedPointer = stored;
          witnessBytes = storeBytes;
          break;
        }
      }
      if (witness == nullptr || context.dominators.dominates(witness, &load))
        return std::nullopt;
      unsigned storeOrdinal = 0;
      for (const Instruction &instruction : *witness->getParent()) {
        auto *store = dyn_cast<StoreInst>(&instruction);
        if (store == nullptr)
          continue;
        if (store == witness)
          break;
        ++storeOrdinal;
      }
      stores.insert(witness);
      bases.insert(base);
      certificate.paths.push_back(GuardedHeapInitializationPath{
          std::move(path.blocks), std::move(path.decisions),
          path.predecessor, witness, base, loaded.address,
          storedPointer.address, witnessBytes, storeOrdinal});
    }
    if (stores.size() < 2 || bases.size() < 2)
      return std::nullopt;
    return certificate;
  }

  std::optional<MemorySSAHeapInitializationCertificate>
  memorySSAHeapInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumNodes = 128;
    constexpr size_t maximumPhiIncoming = 64;
    constexpr size_t maximumPhiDepth = 8;
    constexpr size_t maximumSkippedDefinitions = 64;
    if (context.aliasAnalysis == nullptr || context.memorySSA == nullptr ||
        alternatives.empty() || alternatives.size() > maximumPhiIncoming ||
        std::any_of(
            alternatives.begin(), alternatives.end(),
            [](const PointerAlternative &alternative) {
              const StaticPointer &pointer = alternative.pointer;
              return pointer.heapObject == nullptr || pointer.interprocedural ||
                     pointer.dynamicIndex != nullptr ||
                     pointer.objectOffset < 0;
            }))
      return std::nullopt;

    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    if (use == nullptr)
      return std::nullopt;
    MemoryAccess *rootAccess = use->getDefiningAccess();
    if (rootAccess == nullptr ||
        rootAccess == context.memorySSA->getLiveOnEntryDef())
      return std::nullopt;

    const bool pointerUnion = alternatives.size() != 1;
    auto *rootPhi = dyn_cast<MemoryPhi>(rootAccess);
    BasicBlock *merge = load.getParent();
    std::string pointerTag = context.pointerBlockTags.lookup(merge);
    if (rootPhi == nullptr || rootPhi->getBlock() != merge ||
        (pointerUnion && pointerTag.empty()))
      return std::nullopt;

    std::set<std::string> pointerTags;
    for (const auto &item : context.pointerBlockTags)
      pointerTags.insert(item.second);
    std::set<size_t> assignedAlternatives;
    auto alternativesForIncoming =
        [&](const BasicBlock *incoming)
        -> std::optional<std::vector<size_t>> {
      if (!pointerUnion) {
        assignedAlternatives.insert(0);
        return std::vector<size_t>{0};
      }
      std::vector<size_t> selected;
      for (size_t index = 0; index < alternatives.size(); ++index) {
        bool sawDiscriminator = false;
        bool excluded = false;
        bool unresolved = false;
        for (const PointerGuard &guard : alternatives[index].guards) {
          if (guard.condition != nullptr) {
            unresolved = true;
          } else if (guard.variable == pointerTag) {
            sawDiscriminator = true;
            if (guard.bits != 32 ||
                guard.expected != static_cast<int64_t>(
                    context.blockIds.lookup(incoming)))
              excluded = true;
          } else if (pointerTags.count(guard.variable) != 0) {
            unresolved = true;
          }
        }
        if (!excluded) {
          if (!sawDiscriminator || unresolved)
            return std::nullopt;
          selected.push_back(index);
          assignedAlternatives.insert(index);
        }
      }
      if (selected.empty())
        return std::nullopt;
      const StaticPointer &first = alternatives[selected.front()].pointer;
      if (std::any_of(
              selected.begin(), selected.end(),
              [&](size_t index) {
                const StaticPointer &candidate = alternatives[index].pointer;
                return candidate.heapObject != first.heapObject ||
                       candidate.address != first.address ||
                       candidate.objectOffset != first.objectOffset;
              }))
        return std::nullopt;
      return selected;
    };

    auto storeOrdinal = [](const StoreInst &store) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *store.getParent()) {
        auto *candidate = dyn_cast<StoreInst>(&instruction);
        if (candidate == nullptr)
          continue;
        if (candidate == &store)
          break;
        ++ordinal;
      }
      return ordinal;
    };
    auto memoryDefinitionOrdinal = [](const Instruction &target) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *target.getParent()) {
        if (!instruction.mayWriteToMemory())
          continue;
        if (&instruction == &target)
          break;
        ++ordinal;
      }
      return ordinal;
    };

    MemorySSAHeapInitializationCertificate certificate;
    certificate.merge = merge;
    certificate.loadBytes = bytes;
    std::set<MemoryAccess *> active;
    std::set<const StoreInst *> witnessedStores;
    std::set<uint64_t> witnessedBases;
    MemoryLocation loadLocation = MemoryLocation::get(&load);
    std::function<std::optional<unsigned>(
        MemoryAccess *, const BasicBlock *, ArrayRef<size_t>, unsigned,
        std::vector<MemorySSAHeapInitializationSkip>)>
        prove = [&]
        (MemoryAccess *access, const BasicBlock *point,
         ArrayRef<size_t> selected, unsigned phiDepth,
         std::vector<MemorySSAHeapInitializationSkip> skipped)
        -> std::optional<unsigned> {
      while (access != context.memorySSA->getLiveOnEntryDef()) {
        if (certificate.nodes.size() >= maximumNodes ||
            skipped.size() > maximumSkippedDefinitions)
          return std::nullopt;
        if (auto *phi = dyn_cast<MemoryPhi>(access)) {
          if (phiDepth >= maximumPhiDepth ||
              phi->getNumIncomingValues() < 2 ||
              phi->getNumIncomingValues() > maximumPhiIncoming ||
              !active.insert(phi).second ||
              !context.dominators.dominates(phi->getBlock(), point))
            return std::nullopt;
          std::set<const BasicBlock *> incomingBlocks;
          for (unsigned index = 0; index < phi->getNumIncomingValues(); ++index)
            if (!incomingBlocks.insert(phi->getIncomingBlock(index)).second) {
              active.erase(phi);
              return std::nullopt;
            }
          unsigned nodeIndex = certificate.nodes.size();
          certificate.nodes.push_back(MemorySSAHeapInitializationNode{});
          certificate.nodes[nodeIndex].kind =
              MemorySSAHeapInitializationNode::Kind::Phi;
          certificate.nodes[nodeIndex].block = phi->getBlock();
          for (unsigned index = 0; index < phi->getNumIncomingValues(); ++index) {
            const BasicBlock *incomingBlock = phi->getIncomingBlock(index);
            std::vector<size_t> childAlternatives(selected.begin(), selected.end());
            if (phi == rootPhi && pointerUnion) {
              auto correlated = alternativesForIncoming(incomingBlock);
              if (!correlated) {
                active.erase(phi);
                return std::nullopt;
              }
              childAlternatives = std::move(*correlated);
            }
            auto child = prove(
                phi->getIncomingValue(index), incomingBlock,
                childAlternatives, phiDepth + 1, skipped);
            if (!child) {
              active.erase(phi);
              return std::nullopt;
            }
            certificate.nodes[nodeIndex].incoming.push_back(
                {incomingBlock, *child});
          }
          active.erase(phi);
          return nodeIndex;
        }

        auto *definition = dyn_cast<MemoryDef>(access);
        if (definition == nullptr)
          return std::nullopt;
        Instruction *instruction = definition->getMemoryInst();
        if (instruction == nullptr)
          return std::nullopt;
        if (auto *store = dyn_cast<StoreInst>(instruction)) {
          if (!store->isSimple())
            return std::nullopt;
          unsigned storeBits =
              store->getValueOperand()->getType()->isPointerTy()
                  ? M.getDataLayout().getPointerSizeInBits(
                        store->getValueOperand()->getType()
                            ->getPointerAddressSpace())
                  : integerBits(store->getValueOperand()->getType());
          uint64_t storeBytes =
              storeBits == 0
                  ? 0
                  : fixedStoreBytes(
                        M.getDataLayout(), store->getValueOperand()->getType());
          auto storedAlternatives = pointerAlternatives(
              store->getPointerOperand(), context, *store);
          if (storedAlternatives && storedAlternatives->size() == 1 &&
              storeBytes >= bytes) {
            const StaticPointer &stored =
                storedAlternatives->front().pointer;
            bool covers = std::all_of(
                selected.begin(), selected.end(),
                [&](size_t index) {
                  return pointerIntervalCovers(
                      stored, storeBytes, alternatives[index].pointer, bytes);
                });
            if (covers &&
                context.dominators.dominates(
                    store, point->getTerminator())) {
              const StaticPointer &loaded = alternatives[selected.front()].pointer;
              uint64_t base =
                  loaded.address - static_cast<uint64_t>(loaded.objectOffset);
              unsigned nodeIndex = certificate.nodes.size();
              MemorySSAHeapInitializationNode node;
              node.kind = MemorySSAHeapInitializationNode::Kind::Store;
              node.block = store->getParent();
              node.store = store;
              node.base = base;
              node.loadAddress = loaded.address;
              node.storeAddress = stored.address;
              node.storeBytes = storeBytes;
              node.storeOrdinal = storeOrdinal(*store);
              node.skipped = std::move(skipped);
              certificate.nodes.push_back(std::move(node));
              witnessedStores.insert(store);
              witnessedBases.insert(base);
              return nodeIndex;
            }
          }
          bool allocationNoAlias =
              storedAlternatives && !storedAlternatives->empty() &&
              std::all_of(
                  storedAlternatives->begin(), storedAlternatives->end(),
                  [&](const PointerAlternative &stored) {
                    return std::all_of(
                        selected.begin(), selected.end(),
                        [&](size_t index) {
                          const StaticPointer &loaded =
                              alternatives[index].pointer;
                          const StaticPointer &candidate = stored.pointer;
                          if (candidate.stackObject != loaded.stackObject ||
                              candidate.heapObject != loaded.heapObject)
                            return true;
                          if (candidate.dynamicIndex != nullptr ||
                              loaded.dynamicIndex != nullptr ||
                              candidate.objectOffset < 0 ||
                              loaded.objectOffset < 0)
                            return false;
                          uint64_t candidateOffset =
                              static_cast<uint64_t>(candidate.objectOffset);
                          uint64_t loadedOffset =
                              static_cast<uint64_t>(loaded.objectOffset);
                          return candidateOffset + storeBytes <= loadedOffset ||
                                 loadedOffset + bytes <= candidateOffset;
                        });
                  });
          bool aaNoAlias =
              context.aliasAnalysis->alias(
                  loadLocation, MemoryLocation::get(store)) ==
              AliasResult::NoAlias;
          if (!allocationNoAlias && !aaNoAlias)
            return std::nullopt;
          skipped.push_back({
              store,
              allocationNoAlias ? "allocation-noalias" : "aa-noalias",
              storeOrdinal(*store)});
        } else {
          auto *call = dyn_cast<CallBase>(instruction);
          Function *callee =
              call == nullptr ? nullptr : call->getCalledFunction();
          // The artifact consumer can bind direct internal calls back to one
          // lowered `call` operation. Other MemoryDefs remain fail-closed
          // until their lowering has an equally precise identity contract.
          if (call == nullptr || !isa<CallInst>(call) || callee == nullptr ||
              callee->isDeclaration() || callee->isIntrinsic())
            return std::nullopt;
          if (isModSet(context.aliasAnalysis->getModRefInfo(
                  instruction, loadLocation)))
            return std::nullopt;
          skipped.push_back({
              instruction, "aa-no-modref",
              memoryDefinitionOrdinal(*instruction)});
        }
        access = definition->getDefiningAccess();
      }
      return std::nullopt;
    };

    std::vector<size_t> allAlternatives;
    if (!pointerUnion) {
      allAlternatives.push_back(0);
      assignedAlternatives.insert(0);
    }
    auto root = prove(
        rootAccess, merge, allAlternatives, 0, {});
    if (!root || certificate.nodes.empty() ||
        assignedAlternatives.size() != alternatives.size())
      return std::nullopt;
    std::set<uint64_t> expectedBases;
    for (const PointerAlternative &alternative : alternatives)
      expectedBases.insert(
          alternative.pointer.address -
          static_cast<uint64_t>(alternative.pointer.objectOffset));
    if (witnessedBases != expectedBases ||
        (pointerUnion &&
         (witnessedBases.size() < 2 || witnessedStores.size() < 2)))
      return std::nullopt;
    certificate.rootNode = *root;
    return certificate;
  }

  json::Object memorySSAHeapInitializationRecord(
      const MemorySSAHeapInitializationCertificate &certificate,
      const FunctionContext &context) const {
    json::Object transcript;
    transcript["schema"] =
        "symcc-memoryssa-aa-heap-initialization-v1";
    transcript["merge"] = context.blocks.lookup(certificate.merge);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);
    transcript["root_node"] =
        static_cast<int64_t>(certificate.rootNode);
    json::Array nodes;
    for (size_t index = 0; index < certificate.nodes.size(); ++index) {
      const MemorySSAHeapInitializationNode &node =
          certificate.nodes[index];
      json::Object item;
      item["id"] = static_cast<int64_t>(index);
      item["kind"] =
          node.kind == MemorySSAHeapInitializationNode::Kind::Phi
              ? "memory-phi"
              : "store";
      item["block"] = context.blocks.lookup(node.block);
      if (node.kind == MemorySSAHeapInitializationNode::Kind::Phi) {
        json::Array incoming;
        for (const MemorySSAHeapInitializationIncoming &edge :
             node.incoming) {
          json::Object value;
          value["block"] = context.blocks.lookup(edge.block);
          value["node"] = static_cast<int64_t>(edge.node);
          incoming.push_back(std::move(value));
        }
        item["incoming"] = std::move(incoming);
      } else {
        item["base"] = static_cast<int64_t>(node.base);
        item["load_address"] =
            static_cast<int64_t>(node.loadAddress);
        json::Object store;
        store["block"] = context.blocks.lookup(node.store->getParent());
        store["ordinal"] =
            static_cast<int64_t>(node.storeOrdinal);
        store["address"] =
            static_cast<int64_t>(node.storeAddress);
        store["bytes"] = static_cast<int64_t>(node.storeBytes);
        item["store"] = std::move(store);
        json::Array skipped;
        for (const MemorySSAHeapInitializationSkip &definition :
             node.skipped) {
          json::Object skippedItem;
          skippedItem["block"] =
              context.blocks.lookup(definition.instruction->getParent());
          skippedItem["ordinal"] =
              static_cast<int64_t>(definition.ordinal);
          skippedItem["kind"] =
              isa<StoreInst>(definition.instruction)
                  ? "store"
                  : isa<CallBase>(definition.instruction) ? "call"
                                                          : "memory-def";
          skippedItem["opcode"] =
              definition.instruction->getOpcodeName();
          skippedItem["proof"] = definition.proof;
          skipped.push_back(std::move(skippedItem));
        }
        item["skipped_defs"] = std::move(skipped);
      }
      nodes.push_back(std::move(item));
    }
    transcript["nodes"] = std::move(nodes);
    return transcript;
  }

  std::optional<DynamicByteLaneCoverCertificate>
  dynamicByteLaneCoverInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumSkippedDefinitions = 64;
    constexpr size_t maximumLanes = 2048;
    if (context.aliasAnalysis == nullptr || context.memorySSA == nullptr ||
        alternatives.size() != 1)
      return std::nullopt;
    const StaticPointer &loaded = alternatives.front().pointer;
    if (loaded.dynamicIndex == nullptr || loaded.dynamicScale == 0 ||
        loaded.dynamicObjectAddress == 0 ||
        (loaded.stackObject == nullptr && loaded.heapObject == nullptr) ||
        loaded.interprocedural)
      return std::nullopt;
    auto aliases = memoryAliases(loaded, bytes, load, false);
    if (!aliases || aliases->addresses.empty() ||
        aliases->addresses.size() != aliases->indices.size() ||
        aliases->addresses.size() * bytes > maximumLanes)
      return std::nullopt;
    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    if (use == nullptr)
      return std::nullopt;

    auto objectBase = [](const StaticPointer &pointer) {
      return pointer.dynamicIndex == nullptr
                 ? pointer.address -
                       static_cast<uint64_t>(pointer.objectOffset)
                 : pointer.dynamicObjectAddress;
    };
    auto sameObject = [&](const StaticPointer &pointer) {
      return pointer.stackObject == loaded.stackObject &&
             pointer.heapObject == loaded.heapObject &&
             objectBase(pointer) == objectBase(loaded);
    };
    auto storeOrdinal = [](const StoreInst &store) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *store.getParent()) {
        auto *candidate = dyn_cast<StoreInst>(&instruction);
        if (candidate == nullptr)
          continue;
        if (candidate == &store)
          break;
        ++ordinal;
      }
      return ordinal;
    };
    MemoryLocation loadLocation = MemoryLocation::get(&load);
    std::vector<MemorySSAHeapInitializationSkip> skipped;
    MemoryAccess *access = use->getDefiningAccess();
    while (access != nullptr &&
           access != context.memorySSA->getLiveOnEntryDef()) {
      if (skipped.size() > maximumSkippedDefinitions)
        return std::nullopt;
      auto *definition = dyn_cast<MemoryDef>(access);
      if (definition == nullptr)
        return std::nullopt;
      Instruction *instruction = definition->getMemoryInst();
      if (instruction == nullptr)
        return std::nullopt;
      auto *call = dyn_cast<CallBase>(instruction);
      auto effect = call == nullptr
                        ? std::nullopt
                        : symbolicRegionEffect(*call);
      if (effect && !isa<ConstantInt>(effect->length) &&
          context.dominators.dominates(call, &load)) {
        unsigned regionEffects = 0;
        for (Instruction &candidate : instructions(*load.getFunction())) {
          auto *candidateCall = dyn_cast<CallBase>(&candidate);
          if (candidateCall != nullptr &&
              symbolicRegionEffect(*candidateCall))
            ++regionEffects;
        }
        if (regionEffects != 1)
          return std::nullopt;
        unsigned lengthBits = integerBits(effect->length->getType());
        auto extent = boundedSymbolicRegionExtent(
            *effect, context, load);
        auto destinations = pointerAlternatives(
            effect->destination, context, load);
        if (lengthBits != 0 && extent && destinations &&
            destinations->size() == 1) {
          const StaticPointer &destination =
              destinations->front().pointer;
          uint64_t maximumRepresentable =
              lengthBits == 64
                  ? UINT64_MAX
                  : (UINT64_C(1) << lengthBits) - 1;
          uint64_t maximumBytes =
              std::min(*extent, maximumRepresentable);
          __int128 writerBegin = destination.address;
          __int128 writerEnd = writerBegin + maximumBytes;
          bool covers =
              maximumBytes != 0 && destination.dynamicIndex == nullptr &&
              destination.objectOffset >= 0 && sameObject(destination) &&
              std::all_of(
                  aliases->addresses.begin(), aliases->addresses.end(),
                  [&](uint64_t address) {
                    __int128 loadBegin = address;
                    __int128 loadEnd = loadBegin + bytes;
                    return writerBegin <= loadBegin &&
                           loadEnd <= writerEnd;
                  });
          std::string lengthName =
              context.values.lookup(effect->length);
          if (covers && !lengthName.empty()) {
            DynamicByteLaneCoverCertificate certificate;
            certificate.writer = call;
            certificate.operation = effect->operation;
            certificate.base = objectBase(loaded);
            certificate.writerAddress = destination.address;
            certificate.maximumBytes = maximumBytes;
            certificate.lengthBits = lengthBits;
            certificate.length = effect->length;
            certificate.loadBytes = bytes;
            certificate.indexBits = loaded.dynamicIndexBits;
            certificate.minimumIndex = aliases->minimumIndex;
            certificate.maximumIndex = aliases->maximumIndex;
            certificate.loadAddresses = aliases->addresses;
            certificate.indexValues = aliases->indices;
            certificate.skipped = std::move(skipped);
            for (uint64_t address : aliases->addresses)
              for (unsigned lane = 0; lane < bytes; ++lane)
                certificate.lanes.push_back({
                    address, lane,
                    address + lane - destination.address});
            return certificate;
          }
        }
        return std::nullopt;
      }
      if (auto *store = dyn_cast<StoreInst>(instruction)) {
        if (!store->isSimple())
          return std::nullopt;
        Type *type = store->getValueOperand()->getType();
        unsigned storeBits =
            type->isPointerTy()
                ? M.getDataLayout().getPointerSizeInBits(
                      type->getPointerAddressSpace())
                : integerBits(type);
        uint64_t storeBytes =
            storeBits == 0
                ? 0
                : fixedStoreBytes(M.getDataLayout(), type);
        auto stored = pointerAlternatives(
            store->getPointerOperand(), context, load);
        bool allocationNoAlias =
            storeBytes != 0 && stored && !stored->empty() &&
            std::all_of(
                stored->begin(), stored->end(),
                [&](const PointerAlternative &candidate) {
                  const StaticPointer &pointer = candidate.pointer;
                  if (!sameObject(pointer))
                    return true;
                  if (pointer.dynamicIndex != nullptr ||
                      pointer.objectOffset < 0)
                    return false;
                  __int128 storeBegin = pointer.address;
                  __int128 storeEnd = storeBegin + storeBytes;
                  return std::all_of(
                      aliases->addresses.begin(), aliases->addresses.end(),
                      [&](uint64_t address) {
                        __int128 loadBegin = address;
                        __int128 loadEnd = loadBegin + bytes;
                        return storeEnd <= loadBegin ||
                               loadEnd <= storeBegin;
                      });
                });
        bool aaNoAlias =
            context.aliasAnalysis->alias(
                loadLocation, MemoryLocation::get(store)) ==
            AliasResult::NoAlias;
        if (!allocationNoAlias && !aaNoAlias)
          return std::nullopt;
        skipped.push_back({
            store,
            allocationNoAlias ? "allocation-noalias" : "aa-noalias",
            storeOrdinal(*store)});
      } else
        return std::nullopt;
      access = definition->getDefiningAccess();
    }
    return std::nullopt;
  }

  json::Object dynamicByteLaneCoverRecord(
      const DynamicByteLaneCoverCertificate &certificate,
      const FunctionContext &context) const {
    json::Object transcript;
    transcript["schema"] =
        "symcc-symbolic-length-byte-lane-cover-v1";
    transcript["base"] = static_cast<int64_t>(certificate.base);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);
    json::Object writer;
    writer["block"] = context.blocks.lookup(
        certificate.writer->getParent());
    writer["site"] = std::to_string(
        stableSiteId(*certificate.writer));
    writer["kind"] = certificate.operation;
    writer["address"] =
        static_cast<int64_t>(certificate.writerAddress);
    writer["maximum_bytes"] =
        static_cast<int64_t>(certificate.maximumBytes);
    writer["length_bits"] =
        static_cast<int64_t>(certificate.lengthBits);
    writer["length"] = variableOperand(
        context.values.lookup(certificate.length));
    transcript["writer"] = std::move(writer);
    json::Object index;
    index["bits"] = static_cast<int64_t>(certificate.indexBits);
    index["minimum"] = certificate.minimumIndex;
    index["maximum"] = certificate.maximumIndex;
    json::Array indexValues;
    for (int64_t value : certificate.indexValues)
      indexValues.push_back(value);
    index["values"] = std::move(indexValues);
    transcript["index"] = std::move(index);
    json::Array addresses;
    for (uint64_t address : certificate.loadAddresses)
      addresses.push_back(static_cast<int64_t>(address));
    transcript["load_addresses"] = std::move(addresses);
    json::Array lanes;
    for (const DynamicByteLaneCoverLane &lane : certificate.lanes) {
      json::Object item;
      item["address"] = static_cast<int64_t>(lane.address);
      item["lane"] = static_cast<int64_t>(lane.lane);
      item["region_offset"] =
          static_cast<int64_t>(lane.regionOffset);
      lanes.push_back(std::move(item));
    }
    transcript["lanes"] = std::move(lanes);
    json::Array skipped;
    for (const MemorySSAHeapInitializationSkip &definition :
         certificate.skipped) {
      json::Object item;
      item["block"] = context.blocks.lookup(
          definition.instruction->getParent());
      item["ordinal"] = static_cast<int64_t>(definition.ordinal);
      item["kind"] = isa<StoreInst>(definition.instruction)
                           ? "store"
                           : "call";
      item["opcode"] = definition.instruction->getOpcodeName();
      item["proof"] = definition.proof;
      skipped.push_back(std::move(item));
    }
    transcript["skipped_defs"] = std::move(skipped);
    return transcript;
  }

  std::optional<LoopMemoryPhiByteLaneCertificate>
  loopMemoryPhiByteLaneInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumLanes = 2048;
    if (context.memorySSA == nullptr || alternatives.size() != 1)
      return std::nullopt;
    const StaticPointer &loaded = alternatives.front().pointer;
    if (loaded.dynamicIndex == nullptr || loaded.dynamicScale == 0 ||
        loaded.dynamicObjectAddress == 0 || loaded.interprocedural ||
        (loaded.stackObject == nullptr && loaded.heapObject == nullptr))
      return std::nullopt;
    auto loadAliases = memoryAliases(loaded, bytes, load, false);
    if (!loadAliases || loadAliases->addresses.empty() ||
        loadAliases->addresses.size() != loadAliases->indices.size() ||
        loadAliases->addresses.size() * bytes > maximumLanes)
      return std::nullopt;

    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    auto *memoryPhi = use == nullptr
                          ? nullptr
                          : dyn_cast<MemoryPhi>(use->getDefiningAccess());
    if (memoryPhi == nullptr)
      return std::nullopt;
    BasicBlock *header = memoryPhi->getBlock();
    LoopInfo loops(context.dominators);
    Loop *loop = loops.getLoopFor(header);
    if (loop == nullptr || loop->getHeader() != header ||
        (loop->getNumBlocks() != 3 && loop->getNumBlocks() != 5) ||
        loop->getNumBackEdges() != 1)
      return std::nullopt;
    BasicBlock *preheader = loop->getLoopPreheader();
    BasicBlock *latch = loop->getLoopLatch();
    BasicBlock *exit = loop->getExitBlock();
    if (preheader == nullptr || latch == nullptr || exit == nullptr ||
        exit != load.getParent() || exit->getUniquePredecessor() != header ||
        memoryPhi->getNumIncomingValues() != 2)
      return std::nullopt;
    auto *preheaderBranch = dyn_cast<BranchInst>(preheader->getTerminator());
    auto *headerBranch = dyn_cast<BranchInst>(header->getTerminator());
    auto *latchBranch = dyn_cast<BranchInst>(latch->getTerminator());
    if (preheaderBranch == nullptr || !preheaderBranch->isUnconditional() ||
        preheaderBranch->getSuccessor(0) != header ||
        headerBranch == nullptr || !headerBranch->isConditional() ||
        latchBranch == nullptr || !latchBranch->isUnconditional() ||
        latchBranch->getSuccessor(0) != header ||
        headerBranch->getSuccessor(1) != exit ||
        !loop->contains(headerBranch->getSuccessor(0)))
      return std::nullopt;
    BasicBlock *body = headerBranch->getSuccessor(0);
    BasicBlock *writerBlock = body;
    BasicBlock *skipBlock = nullptr;
    ICmpInst *writerGuard = nullptr;
    bool writerGuardExpected = true;
    if (loop->getNumBlocks() == 3) {
      if (body == header || body == latch ||
          body->getUniquePredecessor() != header ||
          body->getUniqueSuccessor() != latch ||
          latch->getUniquePredecessor() != body)
        return std::nullopt;
    } else {
      auto *decisionBranch = dyn_cast<BranchInst>(body->getTerminator());
      if (body == header || body == latch ||
          body->getUniquePredecessor() != header ||
          decisionBranch == nullptr || !decisionBranch->isConditional())
        return std::nullopt;
      BasicBlock *first = decisionBranch->getSuccessor(0);
      BasicBlock *second = decisionBranch->getSuccessor(1);
      if (first == second || first == header || second == header ||
          first == body || second == body || first == latch ||
          second == latch || !loop->contains(first) ||
          !loop->contains(second) ||
          first->getUniquePredecessor() != body ||
          second->getUniquePredecessor() != body ||
          first->getUniqueSuccessor() != latch ||
          second->getUniqueSuccessor() != latch || pred_size(latch) != 2)
        return std::nullopt;
      writerGuard = dyn_cast<ICmpInst>(decisionBranch->getCondition());
      auto predicate = writerGuard == nullptr
                           ? std::optional<StringRef>()
                           : comparisonOperator(writerGuard->getPredicate());
      if (writerGuard == nullptr || writerGuard->getParent() != body ||
          !predicate ||
          integerBits(writerGuard->getOperand(0)->getType()) == 0 ||
          writerGuard->getOperand(0)->getType() !=
              writerGuard->getOperand(1)->getType() ||
          context.values.lookup(writerGuard).empty())
        return std::nullopt;
      for (Value *operand : writerGuard->operands())
        if (!isa<ConstantInt>(operand) &&
            context.values.lookup(operand).empty())
          return std::nullopt;
    }

    auto *guard = dyn_cast<ICmpInst>(headerBranch->getCondition());
    if (guard == nullptr || guard->getPredicate() != ICmpInst::ICMP_ULT)
      return std::nullopt;
    auto *induction = dyn_cast<PHINode>(guard->getOperand(0));
    Value *bound = guard->getOperand(1);
    unsigned inductionBits = integerBits(
        induction == nullptr ? nullptr : induction->getType());
    if (induction == nullptr || induction->getParent() != header ||
        induction->getNumIncomingValues() != 2 || inductionBits == 0 ||
        integerBits(bound->getType()) != inductionBits ||
        !loop->isLoopInvariant(bound))
      return std::nullopt;
    auto *seed = dyn_cast<ConstantInt>(
        induction->getIncomingValueForBlock(preheader));
    auto *step = dyn_cast<BinaryOperator>(
        induction->getIncomingValueForBlock(latch));
    if (seed == nullptr || !seed->isZero() || step == nullptr ||
        step->getOpcode() != Instruction::Add ||
        step->getParent() != latch || step->getOperand(0) != induction)
      return std::nullopt;
    auto *stepConstant = dyn_cast<ConstantInt>(step->getOperand(1));
    if (stepConstant == nullptr || inductionBits > 64 ||
        stepConstant->isZero() || stepConstant->isNegative() ||
        stepConstant->getValue().getActiveBits() > 64)
      return std::nullopt;
    uint64_t stepValue = stepConstant->getZExtValue();
    if (stepValue > 64)
      return std::nullopt;
    for (BasicBlock *block : loop->blocks())
      for (Instruction &instruction : *block)
        if ((isa<PHINode>(instruction) && &instruction != induction) ||
            isa<CallBase>(instruction) || isa<AllocaInst>(instruction))
          return std::nullopt;

    Value *boundSource = bound;
    if (auto *extension = dyn_cast<ZExtInst>(boundSource))
      boundSource = extension->getOperand(0);
    auto *boundArgument = dyn_cast<Argument>(boundSource);
    unsigned boundSourceBits = integerBits(
        boundArgument == nullptr ? nullptr : boundArgument->getType());
    if (boundArgument == nullptr || boundSourceBits == 0 ||
        context.values.lookup(bound).empty())
      return std::nullopt;

    MemoryAccess *preheaderAccess =
        memoryPhi->getIncomingValueForBlock(preheader);
    MemoryAccess *backedgeAccess =
        memoryPhi->getIncomingValueForBlock(latch);
    MemoryDef *writerDefinition = nullptr;
    MemoryPhi *latchMemoryPhi = nullptr;
    StoreInst *writer = nullptr;
    if (writerGuard == nullptr) {
      writerDefinition = dyn_cast_or_null<MemoryDef>(backedgeAccess);
      writer = writerDefinition == nullptr
                   ? nullptr
                   : dyn_cast_or_null<StoreInst>(
                         writerDefinition->getMemoryInst());
    } else {
      latchMemoryPhi = dyn_cast_or_null<MemoryPhi>(backedgeAccess);
      if (latchMemoryPhi == nullptr ||
          latchMemoryPhi->getBlock() != latch ||
          latchMemoryPhi->getNumIncomingValues() != 2)
        return std::nullopt;
      for (unsigned index = 0;
           index < latchMemoryPhi->getNumIncomingValues(); ++index) {
        MemoryAccess *incoming = latchMemoryPhi->getIncomingValue(index);
        BasicBlock *incomingBlock = latchMemoryPhi->getIncomingBlock(index);
        if (incoming == memoryPhi) {
          if (skipBlock != nullptr)
            return std::nullopt;
          skipBlock = incomingBlock;
          continue;
        }
        auto *definition = dyn_cast<MemoryDef>(incoming);
        auto *candidate = definition == nullptr
                              ? nullptr
                              : dyn_cast_or_null<StoreInst>(
                                    definition->getMemoryInst());
        if (candidate == nullptr || writerDefinition != nullptr)
          return std::nullopt;
        writerDefinition = definition;
        writer = candidate;
        writerBlock = incomingBlock;
      }
      auto *decisionBranch = dyn_cast<BranchInst>(body->getTerminator());
      if (writer == nullptr || skipBlock == nullptr ||
          decisionBranch == nullptr ||
          (decisionBranch->getSuccessor(0) != writerBlock &&
           decisionBranch->getSuccessor(1) != writerBlock) ||
          (decisionBranch->getSuccessor(0) != skipBlock &&
           decisionBranch->getSuccessor(1) != skipBlock))
        return std::nullopt;
      writerGuardExpected =
          decisionBranch->getSuccessor(0) == writerBlock;
    }
    if (preheaderAccess != context.memorySSA->getLiveOnEntryDef() ||
        writerDefinition == nullptr || writer == nullptr ||
        !writer->isSimple() || writer->getParent() != writerBlock ||
        writerDefinition->getDefiningAccess() != memoryPhi)
      return std::nullopt;
    for (BasicBlock *block : loop->blocks()) {
      MemoryPhi *blockPhi = context.memorySSA->getMemoryAccess(block);
      if ((block == header && blockPhi != memoryPhi) ||
          (block == latch && blockPhi != latchMemoryPhi) ||
          (block != header && block != latch && blockPhi != nullptr))
        return std::nullopt;
      for (Instruction &instruction : *block) {
        MemoryAccess *access = context.memorySSA->getMemoryAccess(&instruction);
        if (access != nullptr && access != writerDefinition)
          return std::nullopt;
      }
    }

    Type *writerType = writer->getValueOperand()->getType();
    unsigned writerBits =
        writerType->isPointerTy()
            ? M.getDataLayout().getPointerSizeInBits(
                  writerType->getPointerAddressSpace())
            : integerBits(writerType);
    uint64_t writerBytes =
        writerBits == 0
            ? 0
            : fixedStoreBytes(M.getDataLayout(), writerType);
    if (writerBytes == 0 || writerBytes > 8)
      return std::nullopt;
    auto writerPointer = staticPointer(writer->getPointerOperand(), load);
    if (!writerPointer || writerPointer->dynamicIndex != induction ||
        writerPointer->dynamicScale <= 0 ||
        writerPointer->stackObject != loaded.stackObject ||
        writerPointer->heapObject != loaded.heapObject ||
        writerPointer->dynamicObjectAddress != loaded.dynamicObjectAddress)
      return std::nullopt;
    __int128 addressStride =
        static_cast<__int128>(stepValue) *
        static_cast<__int128>(writerPointer->dynamicScale);
    if (addressStride <= 0 || addressStride > INT64_MAX ||
        writerBytes > static_cast<uint64_t>(addressStride))
      return std::nullopt;
    auto writerAliases = memoryAliases(*writerPointer, writerBytes, load, true);
    if (!writerAliases || writerAliases->addresses.empty() ||
        writerAliases->addresses.size() != writerAliases->indices.size())
      return std::nullopt;

    std::vector<uint64_t> reachableWriterAddresses;
    std::vector<int64_t> reachableWriterIndices;
    for (size_t index = 0; index < writerAliases->addresses.size(); ++index) {
      int64_t value = writerAliases->indices[index];
      if (value < seed->getSExtValue() ||
          static_cast<uint64_t>(value - seed->getSExtValue()) % stepValue != 0)
        continue;
      reachableWriterAddresses.push_back(writerAliases->addresses[index]);
      reachableWriterIndices.push_back(value);
    }
    if (reachableWriterAddresses.empty() ||
        reachableWriterIndices.front() != seed->getSExtValue())
      return std::nullopt;
    uint64_t requiredExclusive =
        static_cast<uint64_t>(reachableWriterIndices.back()) + 1;
    APInt maximumBound = APInt::getMaxValue(boundSourceBits);
    if (!isUIntN(boundSourceBits, requiredExclusive) ||
        maximumBound.ult(APInt(boundSourceBits, requiredExclusive)))
      return std::nullopt;

    unsigned __int128 sourceMaximum =
        boundSourceBits == 64
            ? static_cast<unsigned __int128>(UINT64_MAX)
            : (static_cast<unsigned __int128>(1) << boundSourceBits) - 1;
    unsigned __int128 inductionMaximum =
        inductionBits == 64
            ? static_cast<unsigned __int128>(UINT64_MAX)
            : (static_cast<unsigned __int128>(1) << inductionBits) - 1;
    if (sourceMaximum > inductionMaximum - stepValue + 1)
      return std::nullopt;

    struct WriterLane {
      uint64_t start = 0;
      unsigned lane = 0;
      int64_t induction = 0;
    };
    std::map<uint64_t, WriterLane> writerLanes;
    for (size_t index = 0; index < reachableWriterAddresses.size(); ++index)
      for (unsigned lane = 0; lane < writerBytes; ++lane)
        if (!writerLanes.emplace(
                reachableWriterAddresses[index] + lane,
                WriterLane{reachableWriterAddresses[index], lane,
                           reachableWriterIndices[index]}).second)
          return std::nullopt;

    LoopMemoryPhiByteLaneCertificate certificate;
    certificate.header = header;
    certificate.preheader = preheader;
    certificate.body = body;
    certificate.writerBlock = writerBlock;
    certificate.skipBlock = skipBlock;
    certificate.latch = latch;
    certificate.exit = exit;
    certificate.induction = induction;
    certificate.step = step;
    certificate.guard = guard;
    certificate.writerGuard = writerGuard;
    certificate.writerGuardExpected = writerGuardExpected;
    certificate.bound = bound;
    certificate.writer = writer;
    certificate.base = loaded.dynamicObjectAddress;
    certificate.loadBytes = bytes;
    certificate.inductionBits = inductionBits;
    certificate.stepValue = static_cast<int64_t>(stepValue);
    certificate.writerBytes = static_cast<unsigned>(writerBytes);
    certificate.writerScale = writerPointer->dynamicScale;
    certificate.writerAddressStride = static_cast<uint64_t>(addressStride);
    certificate.writerMinimumIndex = writerAliases->minimumIndex;
    certificate.writerMaximumIndex = writerAliases->maximumIndex;
    certificate.writerAddresses = writerAliases->addresses;
    certificate.writerIndexValues = writerAliases->indices;
    certificate.reachableWriterAddresses =
        std::move(reachableWriterAddresses);
    certificate.reachableWriterIndexValues =
        std::move(reachableWriterIndices);
    certificate.loadIndexBits = loaded.dynamicIndexBits;
    certificate.loadMinimumIndex = loadAliases->minimumIndex;
    certificate.loadMaximumIndex = loadAliases->maximumIndex;
    certificate.loadAddresses = loadAliases->addresses;
    certificate.loadIndexValues = loadAliases->indices;
    for (const Instruction &instruction : *writerBlock) {
      auto *candidate = dyn_cast<StoreInst>(&instruction);
      if (candidate == writer)
        break;
      if (candidate != nullptr)
        ++certificate.writerOrdinal;
    }
    for (uint64_t address : loadAliases->addresses)
      for (unsigned lane = 0; lane < bytes; ++lane) {
        uint64_t writerAddress = address + lane;
        auto found = writerLanes.find(writerAddress);
        if (found == writerLanes.end())
          return std::nullopt;
        certificate.witnesses.push_back(
            {address, lane, found->second.start, found->second.lane,
             found->second.induction});
      }
    return certificate;
  }

  json::Object loopMemoryPhiByteLaneRecord(
      const LoopMemoryPhiByteLaneCertificate &certificate,
      const FunctionContext &context) const {
    json::Object transcript;
    if (certificate.isConditional())
      transcript["schema"] =
          "symcc-loop-memoryphi-byte-lane-induction-v3";
    else if (certificate.isStrided())
      transcript["schema"] =
          "symcc-loop-memoryphi-byte-lane-induction-v2";
    else
      transcript["schema"] =
          "symcc-loop-memoryphi-byte-lane-induction-v1";
    transcript["base"] = static_cast<int64_t>(certificate.base);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);
    json::Object loop;
    loop["header"] = context.blocks.lookup(certificate.header);
    loop["preheader"] = context.blocks.lookup(certificate.preheader);
    loop["preheader_edge"] = context.edgeBlocks.at(
        {certificate.preheader, certificate.header});
    if (certificate.isConditional()) {
      loop["decision"] = context.blocks.lookup(certificate.body);
      loop["writer_block"] =
          context.blocks.lookup(certificate.writerBlock);
      loop["skip"] = context.blocks.lookup(certificate.skipBlock);
    } else {
      loop["body"] = context.blocks.lookup(certificate.body);
    }
    loop["latch"] = context.blocks.lookup(certificate.latch);
    loop["latch_edge"] = context.edgeBlocks.at(
        {certificate.latch, certificate.header});
    loop["exit"] = context.blocks.lookup(certificate.exit);
    transcript["loop"] = std::move(loop);
    json::Object memoryPhi;
    memoryPhi["block"] = context.blocks.lookup(certificate.header);
    memoryPhi["preheader"] = "live-on-entry";
    if (certificate.isConditional()) {
      memoryPhi["backedge"] = "latch-memory-phi";
      memoryPhi["latch_block"] =
          context.blocks.lookup(certificate.latch);
      memoryPhi["writer"] = "writer-memory-def";
      memoryPhi["skip"] = "header-memory-phi";
    } else {
      memoryPhi["backedge"] = "writer-memory-def";
    }
    transcript["memory_phi"] = std::move(memoryPhi);
    json::Object induction;
    induction["variable"] = variableOperand(
        context.values.lookup(certificate.induction));
    induction["bits"] =
        static_cast<int64_t>(certificate.inductionBits);
    induction["seed"] = certificate.seed;
    induction["step"] = certificate.stepValue;
    induction["next"] = variableOperand(
        context.values.lookup(certificate.step));
    transcript["induction"] = std::move(induction);
    json::Object guard;
    guard["variable"] = variableOperand(
        context.values.lookup(certificate.guard));
    guard["predicate"] = "ult";
    guard["bound"] = variableOperand(
        context.values.lookup(certificate.bound));
    guard["continue"] = context.blocks.lookup(certificate.body);
    guard["exit"] = context.blocks.lookup(certificate.exit);
    transcript["guard"] = std::move(guard);
    if (certificate.isConditional()) {
      auto encodeGuardOperand = [&](const Value *value) {
        if (auto *constant = dyn_cast<ConstantInt>(value))
          return constantOperand(*constant);
        return variableOperand(context.values.lookup(value));
      };
      json::Object writerGuard;
      writerGuard["block"] = context.blocks.lookup(certificate.body);
      writerGuard["variable"] = variableOperand(
          context.values.lookup(certificate.writerGuard));
      writerGuard["predicate"] = *comparisonOperator(
          certificate.writerGuard->getPredicate());
      writerGuard["left"] = encodeGuardOperand(
          certificate.writerGuard->getOperand(0));
      writerGuard["right"] = encodeGuardOperand(
          certificate.writerGuard->getOperand(1));
      writerGuard["writer_when"] = certificate.writerGuardExpected;
      writerGuard["writer"] =
          context.blocks.lookup(certificate.writerBlock);
      writerGuard["skip"] = context.blocks.lookup(certificate.skipBlock);
      transcript["writer_guard"] = std::move(writerGuard);
    }
    json::Object writer;
    writer["block"] = context.blocks.lookup(certificate.writerBlock);
    writer["ordinal"] =
        static_cast<int64_t>(certificate.writerOrdinal);
    writer["bytes"] =
        static_cast<int64_t>(certificate.writerBytes);
    writer["minimum"] = certificate.writerMinimumIndex;
    writer["maximum"] = certificate.writerMaximumIndex;
    json::Array writerAddresses;
    for (uint64_t address : certificate.writerAddresses)
      writerAddresses.push_back(static_cast<int64_t>(address));
    writer["addresses"] = std::move(writerAddresses);
    json::Array writerValues;
    for (int64_t value : certificate.writerIndexValues)
      writerValues.push_back(value);
    writer["index_values"] = std::move(writerValues);
    if (certificate.isStrided() || certificate.isConditional()) {
      writer["scale"] = certificate.writerScale;
      writer["address_stride"] =
          static_cast<int64_t>(certificate.writerAddressStride);
      json::Array reachableAddresses;
      for (uint64_t address : certificate.reachableWriterAddresses)
        reachableAddresses.push_back(static_cast<int64_t>(address));
      writer["reachable_addresses"] = std::move(reachableAddresses);
      json::Array reachableValues;
      for (int64_t value : certificate.reachableWriterIndexValues)
        reachableValues.push_back(value);
      writer["reachable_index_values"] = std::move(reachableValues);
      writer["residue_origin"] = static_cast<int64_t>(
          certificate.reachableWriterAddresses.front());
      json::Array coveredResidues;
      for (unsigned lane = 0; lane < certificate.writerBytes; ++lane)
        coveredResidues.push_back(static_cast<int64_t>(lane));
      writer["covered_residues"] = std::move(coveredResidues);
    }
    transcript["writer"] = std::move(writer);
    json::Object loadIndex;
    loadIndex["bits"] =
        static_cast<int64_t>(certificate.loadIndexBits);
    loadIndex["minimum"] = certificate.loadMinimumIndex;
    loadIndex["maximum"] = certificate.loadMaximumIndex;
    json::Array loadValues;
    for (int64_t value : certificate.loadIndexValues)
      loadValues.push_back(value);
    loadIndex["values"] = std::move(loadValues);
    transcript["load_index"] = std::move(loadIndex);
    json::Array loadAddresses;
    for (uint64_t address : certificate.loadAddresses)
      loadAddresses.push_back(static_cast<int64_t>(address));
    transcript["load_addresses"] = std::move(loadAddresses);
    json::Array witnesses;
    for (const LoopMemoryPhiByteLaneWitness &witness :
         certificate.witnesses) {
      json::Object item;
      item["load_address"] =
          static_cast<int64_t>(witness.loadAddress);
      item["lane"] = static_cast<int64_t>(witness.lane);
      item["writer_address"] =
          static_cast<int64_t>(witness.writerAddress);
      if (certificate.isStrided() || certificate.isConditional())
        item["writer_lane"] =
            static_cast<int64_t>(witness.writerLane);
      if (certificate.isConditional()) {
        json::Object writerGuard;
        writerGuard["variable"] = variableOperand(
            context.values.lookup(certificate.writerGuard));
        writerGuard["equals"] = certificate.writerGuardExpected;
        item["writer_guard"] = std::move(writerGuard);
      }
      item["induction_value"] = witness.inductionValue;
      witnesses.push_back(std::move(item));
    }
    transcript["witnesses"] = std::move(witnesses);
    return transcript;
  }

  std::optional<MultiLatchLoopMemoryPhiCertificate>
  nestedLoopMemoryPhiSummaryInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumLanes = 2048;
    constexpr size_t maximumWitnessAlternatives = 8192;
    if (context.memorySSA == nullptr || alternatives.size() != 1)
      return std::nullopt;
    const StaticPointer &loaded = alternatives.front().pointer;
    if (loaded.dynamicIndex == nullptr || loaded.dynamicScale <= 0 ||
        loaded.dynamicObjectAddress == 0 || loaded.interprocedural ||
        (loaded.stackObject == nullptr && loaded.heapObject == nullptr))
      return std::nullopt;
    auto loadAliases = memoryAliases(loaded, bytes, load, false);
    if (!loadAliases || loadAliases->addresses.empty() ||
        loadAliases->addresses.size() != loadAliases->indices.size() ||
        loadAliases->addresses.size() * bytes > maximumLanes)
      return std::nullopt;

    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    auto *outerMemoryPhi =
        use == nullptr ? nullptr
                       : dyn_cast<MemoryPhi>(use->getDefiningAccess());
    if (outerMemoryPhi == nullptr)
      return std::nullopt;
    BasicBlock *outerHeader = outerMemoryPhi->getBlock();
    LoopInfo loops(context.dominators);
    Loop *outerLoop = loops.getLoopFor(outerHeader);
    if (outerLoop == nullptr || outerLoop->getHeader() != outerHeader ||
        outerLoop->getParentLoop() != nullptr ||
        outerLoop->getNumBackEdges() != 1 ||
        outerLoop->getSubLoops().size() != 1 ||
        outerLoop->getNumBlocks() != 5)
      return std::nullopt;
    Loop *innerLoop = outerLoop->getSubLoops().front();
    if (innerLoop == nullptr || innerLoop->getParentLoop() != outerLoop ||
        !innerLoop->getSubLoops().empty() ||
        innerLoop->getNumBackEdges() != 1 ||
        innerLoop->getNumBlocks() != 2)
      return std::nullopt;

    BasicBlock *outerPreheader = outerLoop->getLoopPreheader();
    BasicBlock *outerLatch = outerLoop->getLoopLatch();
    BasicBlock *outerExit = outerLoop->getExitBlock();
    BasicBlock *innerPreheader = innerLoop->getLoopPreheader();
    BasicBlock *innerHeader = innerLoop->getHeader();
    BasicBlock *innerBody = innerLoop->getLoopLatch();
    BasicBlock *innerExit = innerLoop->getExitBlock();
    if (outerPreheader == nullptr || outerLatch == nullptr ||
        outerExit == nullptr || innerPreheader == nullptr ||
        innerHeader == nullptr || innerBody == nullptr ||
        innerExit != outerLatch || outerExit != load.getParent() ||
        outerExit->getUniquePredecessor() != outerHeader ||
        innerPreheader->getUniquePredecessor() != outerHeader ||
        outerLatch->getUniquePredecessor() != innerHeader)
      return std::nullopt;

    auto *outerPreheaderBranch =
        dyn_cast<BranchInst>(outerPreheader->getTerminator());
    auto *outerHeaderBranch =
        dyn_cast<BranchInst>(outerHeader->getTerminator());
    auto *innerPreheaderBranch =
        dyn_cast<BranchInst>(innerPreheader->getTerminator());
    auto *innerHeaderBranch =
        dyn_cast<BranchInst>(innerHeader->getTerminator());
    auto *innerBodyBranch =
        dyn_cast<BranchInst>(innerBody->getTerminator());
    auto *outerLatchBranch =
        dyn_cast<BranchInst>(outerLatch->getTerminator());
    if (outerPreheaderBranch == nullptr ||
        !outerPreheaderBranch->isUnconditional() ||
        outerPreheaderBranch->getSuccessor(0) != outerHeader ||
        outerHeaderBranch == nullptr || !outerHeaderBranch->isConditional() ||
        outerHeaderBranch->getSuccessor(0) != innerPreheader ||
        outerHeaderBranch->getSuccessor(1) != outerExit ||
        innerPreheaderBranch == nullptr ||
        !innerPreheaderBranch->isUnconditional() ||
        innerPreheaderBranch->getSuccessor(0) != innerHeader ||
        innerHeaderBranch == nullptr || !innerHeaderBranch->isConditional() ||
        innerHeaderBranch->getSuccessor(0) != innerBody ||
        innerHeaderBranch->getSuccessor(1) != outerLatch ||
        innerBodyBranch == nullptr || !innerBodyBranch->isUnconditional() ||
        innerBodyBranch->getSuccessor(0) != innerHeader ||
        outerLatchBranch == nullptr ||
        !outerLatchBranch->isUnconditional() ||
        outerLatchBranch->getSuccessor(0) != outerHeader)
      return std::nullopt;

    auto *outerGuard = dyn_cast<ICmpInst>(outerHeaderBranch->getCondition());
    auto *innerGuard = dyn_cast<ICmpInst>(innerHeaderBranch->getCondition());
    auto *outerInduction = dyn_cast_or_null<PHINode>(
        outerGuard == nullptr ? nullptr : outerGuard->getOperand(0));
    auto *innerInduction = dyn_cast_or_null<PHINode>(
        innerGuard == nullptr ? nullptr : innerGuard->getOperand(0));
    Value *outerBound =
        outerGuard == nullptr ? nullptr : outerGuard->getOperand(1);
    Value *innerBound =
        innerGuard == nullptr ? nullptr : innerGuard->getOperand(1);
    unsigned outerBits = integerBits(
        outerInduction == nullptr ? nullptr : outerInduction->getType());
    unsigned innerBits = integerBits(
        innerInduction == nullptr ? nullptr : innerInduction->getType());
    if (outerGuard == nullptr || innerGuard == nullptr ||
        outerGuard->getPredicate() != ICmpInst::ICMP_ULT ||
        innerGuard->getPredicate() != ICmpInst::ICMP_ULT ||
        outerInduction == nullptr || innerInduction == nullptr ||
        outerInduction->getParent() != outerHeader ||
        innerInduction->getParent() != innerHeader || outerBits == 0 ||
        innerBits == 0 || integerBits(outerBound->getType()) != outerBits ||
        integerBits(innerBound->getType()) != innerBits ||
        !outerLoop->isLoopInvariant(outerBound) ||
        !outerLoop->isLoopInvariant(innerBound) ||
        context.values.lookup(outerGuard).empty() ||
        context.values.lookup(innerGuard).empty() ||
        context.values.lookup(outerBound).empty() ||
        context.values.lookup(innerBound).empty())
      return std::nullopt;

    auto *outerSeed = dyn_cast<ConstantInt>(
        outerInduction->getIncomingValueForBlock(outerPreheader));
    auto *innerSeed = dyn_cast<ConstantInt>(
        innerInduction->getIncomingValueForBlock(innerPreheader));
    auto *outerStep = dyn_cast_or_null<BinaryOperator>(
        outerInduction->getIncomingValueForBlock(outerLatch));
    auto *innerStep = dyn_cast_or_null<BinaryOperator>(
        innerInduction->getIncomingValueForBlock(innerBody));
    auto stepConstant = [](BinaryOperator *step, PHINode *induction,
                           BasicBlock *block) -> ConstantInt * {
      if (step == nullptr || step->getOpcode() != Instruction::Add ||
          step->getParent() != block || step->getOperand(0) != induction)
        return nullptr;
      return dyn_cast<ConstantInt>(step->getOperand(1));
    };
    ConstantInt *outerStepConstant =
        stepConstant(outerStep, outerInduction, outerLatch);
    ConstantInt *innerStepConstant =
        stepConstant(innerStep, innerInduction, innerBody);
    auto validStep = [](const ConstantInt *constant) {
      return constant != nullptr && !constant->isZero() &&
             !constant->isNegative() &&
             constant->getValue().getActiveBits() <= 64 &&
             constant->getZExtValue() <= 64;
    };
    if (outerSeed == nullptr || !outerSeed->isZero() ||
        innerSeed == nullptr || !innerSeed->isZero() || outerBits > 64 ||
        innerBits > 64 || !validStep(outerStepConstant) ||
        !validStep(innerStepConstant))
      return std::nullopt;
    uint64_t outerStepValue = outerStepConstant->getZExtValue();
    uint64_t innerStepValue = innerStepConstant->getZExtValue();

    for (BasicBlock *block : outerLoop->blocks())
      for (Instruction &instruction : *block)
        if ((isa<PHINode>(instruction) && &instruction != outerInduction &&
             &instruction != innerInduction) ||
            isa<CallBase>(instruction) || isa<AllocaInst>(instruction))
          return std::nullopt;

    auto *innerMemoryPhi = dyn_cast_or_null<MemoryPhi>(
        outerMemoryPhi->getIncomingValueForBlock(outerLatch));
    if (outerMemoryPhi->getNumIncomingValues() != 2 ||
        outerMemoryPhi->getIncomingValueForBlock(outerPreheader) !=
            context.memorySSA->getLiveOnEntryDef() ||
        innerMemoryPhi == nullptr || innerMemoryPhi->getBlock() != innerHeader ||
        innerMemoryPhi->getNumIncomingValues() != 2 ||
        innerMemoryPhi->getIncomingValueForBlock(innerPreheader) !=
            outerMemoryPhi)
      return std::nullopt;

    MultiLatchLoopMemoryPhiTransfer transfer;
    transfer.latch = innerBody;
    transfer.step = innerStep;
    std::set<const MemoryAccess *> writerDefinitions;
    MemoryAccess *incoming =
        innerMemoryPhi->getIncomingValueForBlock(innerBody);
    while (incoming != innerMemoryPhi) {
      auto *definition = dyn_cast_or_null<MemoryDef>(incoming);
      auto *instruction =
          definition == nullptr
              ? nullptr
              : dyn_cast_or_null<StoreInst>(definition->getMemoryInst());
      if (instruction == nullptr || !instruction->isSimple() ||
          instruction->getParent() != innerBody ||
          transfer.writers.size() == 4)
        return std::nullopt;
      MultiLatchLoopMemoryPhiWriter writer;
      writer.instruction = instruction;
      transfer.writers.push_back(std::move(writer));
      writerDefinitions.insert(definition);
      incoming = definition->getDefiningAccess();
    }
    if (transfer.writers.empty())
      return std::nullopt;
    std::reverse(transfer.writers.begin(), transfer.writers.end());
    for (size_t index = 1; index < transfer.writers.size(); ++index)
      if (!transfer.writers[index - 1].instruction->comesBefore(
              transfer.writers[index].instruction))
        return std::nullopt;

    for (BasicBlock *block : outerLoop->blocks()) {
      MemoryPhi *blockPhi = context.memorySSA->getMemoryAccess(block);
      if ((block == outerHeader && blockPhi != outerMemoryPhi) ||
          (block == innerHeader && blockPhi != innerMemoryPhi) ||
          (block != outerHeader && block != innerHeader &&
           blockPhi != nullptr))
        return std::nullopt;
      for (Instruction &instruction : *block) {
        MemoryAccess *access = context.memorySSA->getMemoryAccess(&instruction);
        if (access != nullptr && !writerDefinitions.count(access))
          return std::nullopt;
      }
    }

    auto boundedInputMaximum = [&](Value *bound, unsigned inductionBits,
                                   uint64_t step)
        -> std::optional<uint64_t> {
      Value *source = bound;
      if (auto *extension = dyn_cast<ZExtInst>(source))
        source = extension->getOperand(0);
      auto *argument = dyn_cast<Argument>(source);
      unsigned sourceBits = integerBits(
          argument == nullptr ? nullptr : argument->getType());
      if (argument == nullptr || sourceBits == 0 || sourceBits >= 64)
        return std::nullopt;
      uint64_t sourceMaximum = (UINT64_C(1) << sourceBits) - 1;
      unsigned __int128 inductionMaximum =
          inductionBits == 64
              ? static_cast<unsigned __int128>(UINT64_MAX)
              : (static_cast<unsigned __int128>(1) << inductionBits) - 1;
      if (step > inductionMaximum + 1 ||
          sourceMaximum > inductionMaximum - step + 1)
        return std::nullopt;
      return sourceMaximum;
    };
    std::optional<uint64_t> outerBoundMaximum = boundedInputMaximum(
        outerBound, outerBits, outerStepValue);
    std::optional<uint64_t> innerBoundMaximum = boundedInputMaximum(
        innerBound, innerBits, innerStepValue);

    struct TwoDimensionalAffineTerms {
      int64_t constant = 0;
      int64_t outerScale = 0;
      int64_t innerScale = 0;
      unsigned bits = 0;
    };
    std::function<std::optional<TwoDimensionalAffineTerms>(
        Value *, const StoreInst *, unsigned, std::set<const Value *> &)>
        affineTerms = [&](Value *value, const StoreInst *store,
                          unsigned depth, std::set<const Value *> &active)
        -> std::optional<TwoDimensionalAffineTerms> {
      unsigned bits = integerBits(value == nullptr ? nullptr : value->getType());
      if (value == nullptr || bits == 0 || bits > 64 || depth > 4)
        return std::nullopt;
      if (value == outerInduction)
        return TwoDimensionalAffineTerms{0, 1, 0, bits};
      if (value == innerInduction)
        return TwoDimensionalAffineTerms{0, 0, 1, bits};
      if (auto *constant = dyn_cast<ConstantInt>(value)) {
        if (constant->isNegative() ||
            constant->getValue().getActiveBits() > 63)
          return std::nullopt;
        return TwoDimensionalAffineTerms{
            static_cast<int64_t>(constant->getZExtValue()), 0, 0, bits};
      }
      auto *binary = dyn_cast<BinaryOperator>(value);
      if (binary == nullptr || binary->getParent() != innerBody ||
          !binary->comesBefore(store) || !active.insert(value).second)
        return std::nullopt;

      auto finish = [&](std::optional<TwoDimensionalAffineTerms> result) {
        active.erase(value);
        return result;
      };
      if (binary->getOpcode() == Instruction::Add) {
        auto left = affineTerms(
            binary->getOperand(0), store, depth + 1, active);
        auto right = affineTerms(
            binary->getOperand(1), store, depth + 1, active);
        if (!left || !right || left->bits != bits || right->bits != bits)
          return finish(std::nullopt);
        __int128 constant = static_cast<__int128>(left->constant) +
                            static_cast<__int128>(right->constant);
        __int128 outer = static_cast<__int128>(left->outerScale) +
                         static_cast<__int128>(right->outerScale);
        __int128 inner = static_cast<__int128>(left->innerScale) +
                         static_cast<__int128>(right->innerScale);
        if (constant > INT64_MAX || outer > INT64_MAX || inner > INT64_MAX)
          return finish(std::nullopt);
        return finish(TwoDimensionalAffineTerms{
            static_cast<int64_t>(constant), static_cast<int64_t>(outer),
            static_cast<int64_t>(inner), bits});
      }
      if (binary->getOpcode() == Instruction::Mul) {
        ConstantInt *factor = dyn_cast<ConstantInt>(binary->getOperand(0));
        Value *expression = binary->getOperand(1);
        if (factor == nullptr) {
          factor = dyn_cast<ConstantInt>(binary->getOperand(1));
          expression = binary->getOperand(0);
        }
        if (factor == nullptr || factor->isNegative() || factor->isZero() ||
            factor->getValue().getActiveBits() > 63)
          return finish(std::nullopt);
        auto child = affineTerms(expression, store, depth + 1, active);
        if (!child || child->bits != bits)
          return finish(std::nullopt);
        __int128 multiplier = factor->getZExtValue();
        __int128 constant =
            static_cast<__int128>(child->constant) * multiplier;
        __int128 outer =
            static_cast<__int128>(child->outerScale) * multiplier;
        __int128 inner =
            static_cast<__int128>(child->innerScale) * multiplier;
        if (constant > INT64_MAX || outer > INT64_MAX || inner > INT64_MAX)
          return finish(std::nullopt);
        return finish(TwoDimensionalAffineTerms{
            static_cast<int64_t>(constant), static_cast<int64_t>(outer),
            static_cast<int64_t>(inner), bits});
      }
      return finish(std::nullopt);
    };

    struct AffineValueTerms {
      int64_t constant = 0;
      int64_t outerScale = 0;
      int64_t innerScale = 0;
      int64_t inputScale = 0;
      unsigned bits = 0;
      const Argument *input = nullptr;
    };
    std::function<std::optional<AffineValueTerms>(
        Value *, const StoreInst *, unsigned, unsigned,
        std::set<const Value *> &)>
        affineValueTerms = [&](Value *value, const StoreInst *store,
                               unsigned expectedBits, unsigned depth,
                               std::set<const Value *> &active)
        -> std::optional<AffineValueTerms> {
      unsigned bits = integerBits(value == nullptr ? nullptr : value->getType());
      if (value == nullptr || bits != expectedBits || depth > 4)
        return std::nullopt;
      if (value == outerInduction)
        return AffineValueTerms{0, 1, 0, 0, bits, nullptr};
      if (value == innerInduction)
        return AffineValueTerms{0, 0, 1, 0, bits, nullptr};
      if (auto *argument = dyn_cast<Argument>(value)) {
        if (argument->getParent() != innerBody->getParent() ||
            argument->getParent()->getName() != entryName)
          return std::nullopt;
        return AffineValueTerms{0, 0, 0, 1, bits, argument};
      }
      if (auto *constant = dyn_cast<ConstantInt>(value)) {
        if (constant->isNegative() ||
            constant->getValue().getActiveBits() > 63)
          return std::nullopt;
        return AffineValueTerms{
            static_cast<int64_t>(constant->getZExtValue()),
            0, 0, 0, bits, nullptr};
      }
      auto *binary = dyn_cast<BinaryOperator>(value);
      if (binary == nullptr || binary->getParent() != innerBody ||
          !binary->comesBefore(store) || !active.insert(value).second ||
          binary->hasNoUnsignedWrap() || binary->hasNoSignedWrap())
        return std::nullopt;

      auto finish = [&](std::optional<AffineValueTerms> result) {
        active.erase(value);
        return result;
      };
      auto checked = [&](const AffineValueTerms &left,
                         const AffineValueTerms &right,
                         uint64_t multiplier)
          -> std::optional<AffineValueTerms> {
        if (left.bits != bits || right.bits != bits ||
            (left.input != nullptr && right.input != nullptr &&
             left.input != right.input))
          return std::nullopt;
        __int128 constant =
            (static_cast<__int128>(left.constant) + right.constant) *
            multiplier;
        __int128 outer =
            (static_cast<__int128>(left.outerScale) + right.outerScale) *
            multiplier;
        __int128 inner =
            (static_cast<__int128>(left.innerScale) + right.innerScale) *
            multiplier;
        __int128 input =
            (static_cast<__int128>(left.inputScale) + right.inputScale) *
            multiplier;
        if (constant > INT64_MAX || outer > INT64_MAX ||
            inner > INT64_MAX || input > INT64_MAX)
          return std::nullopt;
        return AffineValueTerms{
            static_cast<int64_t>(constant), static_cast<int64_t>(outer),
            static_cast<int64_t>(inner), static_cast<int64_t>(input), bits,
            left.input != nullptr ? left.input : right.input};
      };
      if (binary->getOpcode() == Instruction::Add) {
        auto left = affineValueTerms(
            binary->getOperand(0), store, expectedBits, depth + 1, active);
        auto right = affineValueTerms(
            binary->getOperand(1), store, expectedBits, depth + 1, active);
        if (!left || !right)
          return finish(std::nullopt);
        return finish(checked(*left, *right, 1));
      }
      if (binary->getOpcode() == Instruction::Mul) {
        ConstantInt *factor = dyn_cast<ConstantInt>(binary->getOperand(0));
        Value *expression = binary->getOperand(1);
        if (factor == nullptr) {
          factor = dyn_cast<ConstantInt>(binary->getOperand(1));
          expression = binary->getOperand(0);
        }
        if (factor == nullptr || factor->isNegative() || factor->isZero() ||
            factor->getValue().getActiveBits() > 63)
          return finish(std::nullopt);
        auto child = affineValueTerms(
            expression, store, expectedBits, depth + 1, active);
        if (!child)
          return finish(std::nullopt);
        AffineValueTerms zero{0, 0, 0, 0, bits, nullptr};
        return finish(checked(
            *child, zero, static_cast<uint64_t>(factor->getZExtValue())));
      }
      return finish(std::nullopt);
    };

    auto materializeAffineValue = [&](Value *operand,
                                      const AffineValueTerms &terms)
        -> std::optional<NestedLoopMemoryPhiAffineWriterValue> {
      NestedLoopMemoryPhiAffineWriterValue value;
      value.operand = operand;
      value.input = terms.input;
      value.bits = terms.bits;
      value.constant = terms.constant;
      value.outerScale = terms.outerScale;
      value.innerScale = terms.innerScale;
      value.inputScale = terms.inputScale;
      if (terms.input != nullptr) {
        uint64_t offset = 0;
        for (const Argument &argument : terms.input->getParent()->args()) {
          if (&argument == terms.input)
            break;
          unsigned argumentBits = integerBits(argument.getType());
          if (argumentBits == 0)
            return std::nullopt;
          offset += (argumentBits + 7) / 8;
        }
        value.inputOffset = offset;
        value.inputBytes = (terms.bits + 7) / 8;
      }
      return value;
    };

    auto piecewiseValue = [&](Value *value, const StoreInst *store)
        -> std::optional<NestedLoopMemoryPhiPiecewiseWriterValue> {
      auto *selection = dyn_cast<SelectInst>(value);
      if (selection == nullptr || selection->getParent() != innerBody ||
          !selection->comesBefore(store))
        return std::nullopt;
      auto *comparison = dyn_cast<ICmpInst>(selection->getCondition());
      if (comparison == nullptr || comparison->getParent() != innerBody ||
          !comparison->comesBefore(selection))
        return std::nullopt;
      switch (comparison->getPredicate()) {
      case ICmpInst::ICMP_EQ:
      case ICmpInst::ICMP_NE:
      case ICmpInst::ICMP_UGT:
      case ICmpInst::ICMP_UGE:
      case ICmpInst::ICMP_ULT:
      case ICmpInst::ICMP_ULE:
        break;
      default:
        return std::nullopt;
      }
      ConstantInt *guardConstant =
          dyn_cast<ConstantInt>(comparison->getOperand(1));
      Value *guardInduction = comparison->getOperand(0);
      bool constantOnLeft = false;
      if (guardConstant == nullptr) {
        guardConstant = dyn_cast<ConstantInt>(comparison->getOperand(0));
        guardInduction = comparison->getOperand(1);
        constantOnLeft = true;
      }
      bool usesOuter = guardInduction == outerInduction;
      if (guardConstant == nullptr ||
          (!usesOuter && guardInduction != innerInduction) ||
          guardConstant->isNegative() ||
          guardConstant->getValue().getActiveBits() > 63 ||
          guardConstant->getBitWidth() !=
              integerBits(guardInduction->getType()))
        return std::nullopt;

      std::set<const Value *> trueActive;
      std::set<const Value *> falseActive;
      unsigned bits = integerBits(selection->getType());
      auto trueTerms = affineValueTerms(
          selection->getTrueValue(), store, bits, 0, trueActive);
      auto falseTerms = affineValueTerms(
          selection->getFalseValue(), store, bits, 0, falseActive);
      if (!trueTerms || !falseTerms ||
          (trueTerms->input != nullptr && falseTerms->input != nullptr &&
           trueTerms->input != falseTerms->input))
        return std::nullopt;
      auto whenTrue = materializeAffineValue(
          selection->getTrueValue(), *trueTerms);
      auto whenFalse = materializeAffineValue(
          selection->getFalseValue(), *falseTerms);
      if (!whenTrue || !whenFalse)
        return std::nullopt;
      auto equalModulo = [&](int64_t left, int64_t right) {
        return APInt(bits, static_cast<uint64_t>(left)) ==
               APInt(bits, static_cast<uint64_t>(right));
      };
      bool trueUsesInput = !APInt(
          bits, static_cast<uint64_t>(whenTrue->inputScale)).isZero();
      bool falseUsesInput = !APInt(
          bits, static_cast<uint64_t>(whenFalse->inputScale)).isZero();
      bool identical =
          equalModulo(whenTrue->constant, whenFalse->constant) &&
          equalModulo(whenTrue->outerScale, whenFalse->outerScale) &&
          equalModulo(whenTrue->innerScale, whenFalse->innerScale) &&
          equalModulo(whenTrue->inputScale, whenFalse->inputScale) &&
          ((!trueUsesInput && !falseUsesInput) ||
           (trueUsesInput && falseUsesInput &&
            whenTrue->input == whenFalse->input));
      if (identical)
        return std::nullopt;
      NestedLoopMemoryPhiPiecewiseWriterValue result;
      result.selection = selection;
      result.guard = comparison;
      result.guardUsesOuterInduction = usesOuter;
      result.guardConstantOnLeft = constantOnLeft;
      result.guardConstant = static_cast<int64_t>(
          guardConstant->getZExtValue());
      result.whenTrue = *whenTrue;
      result.whenFalse = *whenFalse;
      return result;
    };

    auto decisionDagValue = [&](Value *value, const StoreInst *store)
        -> std::optional<NestedLoopMemoryPhiDecisionDagWriterValue> {
      constexpr unsigned maximumDepth = 4;
      constexpr unsigned maximumNodes = 31;
      NestedLoopMemoryPhiDecisionDagWriterValue result;
      result.rootOperand = value;
      DenseMap<const Value *, std::pair<unsigned, unsigned>> nodeByValue;
      std::set<const Value *> active;
      unsigned guardNodes = 0;

      std::function<std::optional<std::pair<unsigned, unsigned>>(
          Value *, unsigned)>
          lowerNode = [&](Value *operand, unsigned depth)
          -> std::optional<std::pair<unsigned, unsigned>> {
        if (depth > maximumDepth)
          return std::nullopt;
        auto found = nodeByValue.find(operand);
        if (found != nodeByValue.end())
          return found->second;
        if (!active.insert(operand).second ||
            result.nodes.size() >= maximumNodes)
          return std::nullopt;
        auto finish = [&](std::optional<std::pair<unsigned, unsigned>> value) {
          active.erase(operand);
          return value;
        };

        if (auto *selection = dyn_cast<SelectInst>(operand)) {
          if (selection->getParent() != innerBody ||
              !selection->comesBefore(store))
            return finish(std::nullopt);
          auto *comparison = dyn_cast<ICmpInst>(selection->getCondition());
          if (comparison == nullptr || comparison->getParent() != innerBody ||
              !comparison->comesBefore(selection))
            return finish(std::nullopt);
          switch (comparison->getPredicate()) {
          case ICmpInst::ICMP_EQ:
          case ICmpInst::ICMP_NE:
          case ICmpInst::ICMP_UGT:
          case ICmpInst::ICMP_UGE:
          case ICmpInst::ICMP_ULT:
          case ICmpInst::ICMP_ULE:
            break;
          default:
            return finish(std::nullopt);
          }
          ConstantInt *constant =
              dyn_cast<ConstantInt>(comparison->getOperand(1));
          Value *induction = comparison->getOperand(0);
          bool constantOnLeft = false;
          if (constant == nullptr) {
            constant = dyn_cast<ConstantInt>(comparison->getOperand(0));
            induction = comparison->getOperand(1);
            constantOnLeft = true;
          }
          bool usesOuter = induction == outerInduction;
          if (constant == nullptr ||
              (!usesOuter && induction != innerInduction) ||
              constant->isNegative() ||
              constant->getValue().getActiveBits() > 63 ||
              constant->getBitWidth() != integerBits(induction->getType()))
            return finish(std::nullopt);

          auto whenTrue = lowerNode(selection->getTrueValue(), depth + 1);
          auto whenFalse = lowerNode(selection->getFalseValue(), depth + 1);
          if (!whenTrue || !whenFalse)
            return finish(std::nullopt);
          NestedLoopMemoryPhiDecisionDagNode node;
          node.kind = NestedLoopMemoryPhiDecisionDagNode::Kind::Guard;
          node.operand = selection;
          node.guard = comparison;
          node.guardUsesOuterInduction = usesOuter;
          node.guardConstantOnLeft = constantOnLeft;
          node.guardConstant = static_cast<int64_t>(constant->getZExtValue());
          node.whenTrue = whenTrue->first;
          node.whenFalse = whenFalse->first;
          unsigned identifier = static_cast<unsigned>(result.nodes.size());
          result.nodes.push_back(std::move(node));
          unsigned nodeDepth = std::max(whenTrue->second, whenFalse->second) + 1;
          nodeByValue[operand] = std::make_pair(identifier, nodeDepth);
          ++guardNodes;
          return finish(std::make_pair(identifier, nodeDepth));
        }

        std::set<const Value *> affineActive;
        unsigned bits = integerBits(value->getType());
        auto terms = affineValueTerms(
            operand, store, bits, 0, affineActive);
        if (!terms)
          return finish(std::nullopt);
        auto affine = materializeAffineValue(operand, *terms);
        if (!affine)
          return finish(std::nullopt);
        NestedLoopMemoryPhiDecisionDagNode node;
        node.kind = NestedLoopMemoryPhiDecisionDagNode::Kind::Affine;
        node.operand = operand;
        node.affine = *affine;
        unsigned identifier = static_cast<unsigned>(result.nodes.size());
        result.nodes.push_back(std::move(node));
        nodeByValue[operand] = std::make_pair(identifier, 0U);
        return finish(std::make_pair(identifier, 0U));
      };

      auto root = lowerNode(value, 0);
      if (!root || guardNodes < 2 || root->second > maximumDepth ||
          result.nodes.size() > maximumNodes)
        return std::nullopt;
      result.root = root->first;
      result.depth = root->second;
      return result;
    };

    uint64_t requiredInnerExclusive = 0;
    bool sawDirectInnerWriter = false;
    bool sawTwoDimensionalWriter = false;
    for (size_t writerIndex = 0; writerIndex < transfer.writers.size();
         ++writerIndex) {
      MultiLatchLoopMemoryPhiWriter &writer = transfer.writers[writerIndex];
      Type *writerType = writer.instruction->getValueOperand()->getType();
      unsigned writerBits =
          writerType->isPointerTy()
              ? M.getDataLayout().getPointerSizeInBits(
                    writerType->getPointerAddressSpace())
              : integerBits(writerType);
      uint64_t writerBytes =
          writerBits == 0 ? 0 : fixedStoreBytes(M.getDataLayout(), writerType);
      // The value summary binds every stored bit to an exact memory byte.
      // Reject non-byte-sized integers because their padding-bit memory
      // representation is outside this certificate's proof boundary.
      if (writerBytes == 0 || writerBytes > 8 ||
          writerBits != writerBytes * 8)
        return std::nullopt;
      if (auto *constant = dyn_cast<ConstantInt>(
              writer.instruction->getValueOperand())) {
        writer.constantValueBits = constant->getBitWidth();
        writer.constantValue = constant->getValue().getSExtValue();
        APInt stored = constant->getValue().zextOrTrunc(writerBytes * 8);
        for (uint64_t memoryByte = 0; memoryByte < writerBytes;
             ++memoryByte) {
          uint64_t valueByte = M.getDataLayout().isLittleEndian()
                                   ? memoryByte
                                   : writerBytes - memoryByte - 1;
          writer.constantValueBytes.push_back(static_cast<uint8_t>(
              stored.extractBits(8, valueByte * 8).getZExtValue()));
        }
      } else {
        std::set<const Value *> active;
        auto affineValue = affineValueTerms(
            writer.instruction->getValueOperand(), writer.instruction,
            writerBits, 0, active);
        if (affineValue &&
            (affineValue->outerScale != 0 ||
             affineValue->innerScale != 0 ||
             affineValue->inputScale != 0)) {
          auto materialized = materializeAffineValue(
              writer.instruction->getValueOperand(), *affineValue);
          if (!materialized)
            return std::nullopt;
          writer.affineValue = materialized->operand;
          writer.affineValueInput = materialized->input;
          writer.affineValueBits = materialized->bits;
          writer.affineValueInputOffset = materialized->inputOffset;
          writer.affineValueInputBytes = materialized->inputBytes;
          writer.affineValueConstant = materialized->constant;
          writer.affineValueOuterScale = materialized->outerScale;
          writer.affineValueInnerScale = materialized->innerScale;
          writer.affineValueInputScale = materialized->inputScale;
        } else {
          writer.piecewiseValue = piecewiseValue(
              writer.instruction->getValueOperand(), writer.instruction);
          if (!writer.piecewiseValue)
            writer.decisionDagValue = decisionDagValue(
                writer.instruction->getValueOperand(), writer.instruction);
        }
      }
      auto writerPointer =
          staticPointer(writer.instruction->getPointerOperand(), load);
      if (!writerPointer || writerPointer->dynamicIndex == nullptr ||
          writerPointer->dynamicScale <= 0 ||
          writerPointer->stackObject != loaded.stackObject ||
          writerPointer->heapObject != loaded.heapObject ||
          writerPointer->dynamicObjectAddress != loaded.dynamicObjectAddress)
        return std::nullopt;
      auto aliases =
          memoryAliases(*writerPointer, writerBytes, load, true);
      if (!aliases || aliases->addresses.empty() ||
          aliases->addresses.size() != aliases->indices.size())
        return std::nullopt;
      writer.bytes = static_cast<unsigned>(writerBytes);
      writer.minimumIndex = aliases->minimumIndex;
      writer.maximumIndex = aliases->maximumIndex;
      writer.addresses = aliases->addresses;
      writer.indexValues = aliases->indices;
      if (writerPointer->dynamicIndex == innerInduction) {
        sawDirectInnerWriter = true;
        __int128 addressStride =
            static_cast<__int128>(innerStepValue) *
            static_cast<__int128>(writerPointer->dynamicScale);
        if (addressStride <= 0 || addressStride > INT64_MAX ||
            writerBytes > static_cast<uint64_t>(addressStride))
          return std::nullopt;
        writer.scale = writerPointer->dynamicScale;
        writer.addressStride = static_cast<uint64_t>(addressStride);
        for (size_t index = 0; index < aliases->addresses.size(); ++index) {
          int64_t value = aliases->indices[index];
          if (value < 0 ||
              static_cast<uint64_t>(value) % innerStepValue != 0)
            continue;
          writer.reachableAddresses.push_back(aliases->addresses[index]);
          writer.reachableIndexValues.push_back(value);
        }
        if (writer.reachableAddresses.empty() ||
            writer.reachableIndexValues.front() != 0 ||
            !std::is_sorted(writer.reachableAddresses.begin(),
                            writer.reachableAddresses.end()) ||
            !std::is_sorted(writer.reachableIndexValues.begin(),
                            writer.reachableIndexValues.end()))
          return std::nullopt;
        requiredInnerExclusive = std::max(
            requiredInnerExclusive,
            static_cast<uint64_t>(writer.reachableIndexValues.back()) + 1);
      } else {
        sawTwoDimensionalWriter = true;
        if (!outerBoundMaximum || !innerBoundMaximum)
          return std::nullopt;
        std::set<const Value *> active;
        auto affine = affineTerms(
            writerPointer->dynamicIndex, writer.instruction, 0, active);
        if (!affine || affine->bits != writerPointer->dynamicIndexBits ||
            affine->bits != outerBits || affine->bits != innerBits ||
            affine->outerScale <= 0 || affine->innerScale <= 0)
          return std::nullopt;
        __int128 innerAddressStride =
            static_cast<__int128>(affine->innerScale) *
            static_cast<__int128>(writerPointer->dynamicScale) *
            static_cast<__int128>(innerStepValue);
        if (innerAddressStride <= 0 || innerAddressStride > INT64_MAX ||
            writerBytes > static_cast<uint64_t>(innerAddressStride))
          return std::nullopt;
        uint64_t outerInstances =
            (*outerBoundMaximum + outerStepValue - 1) / outerStepValue;
        uint64_t innerInstances =
            (*innerBoundMaximum + innerStepValue - 1) / innerStepValue;
        if (outerInstances == 0 || innerInstances == 0 ||
            outerInstances > 64 || innerInstances > 64 ||
            outerInstances * innerInstances > 256)
          return std::nullopt;
        std::map<int64_t, uint64_t> addressByIndex;
        for (size_t index = 0; index < aliases->addresses.size(); ++index)
          if (!addressByIndex.emplace(
                  aliases->indices[index], aliases->addresses[index]).second)
            return std::nullopt;
        unsigned __int128 indexMaximum =
            affine->bits == 64
                ? static_cast<unsigned __int128>(UINT64_MAX)
                : (static_cast<unsigned __int128>(1) << affine->bits) - 1;
        for (uint64_t outerValue = 0;
             outerValue < *outerBoundMaximum;
             outerValue += outerStepValue) {
          for (uint64_t innerValue = 0;
               innerValue < *innerBoundMaximum;
               innerValue += innerStepValue) {
            unsigned __int128 affineIndex =
                static_cast<unsigned __int128>(affine->constant) +
                static_cast<unsigned __int128>(affine->outerScale) *
                    outerValue +
                static_cast<unsigned __int128>(affine->innerScale) *
                    innerValue;
            if (affineIndex > indexMaximum || affineIndex > INT64_MAX)
              return std::nullopt;
            int64_t indexValue = static_cast<int64_t>(affineIndex);
            auto address = addressByIndex.find(indexValue);
            if (address == addressByIndex.end())
              return std::nullopt;
            writer.affineInstances.push_back({
                address->second, indexValue,
                static_cast<int64_t>(outerValue),
                static_cast<int64_t>(innerValue)});
          }
        }
        if (writer.affineInstances.empty())
          return std::nullopt;
        writer.affineIndex = writerPointer->dynamicIndex;
        writer.affineIndexBits = affine->bits;
        writer.affineConstant = affine->constant;
        writer.affineOuterScale = affine->outerScale;
        writer.affineInnerScale = affine->innerScale;
        writer.pointerScale = writerPointer->dynamicScale;
        writer.pointerBase = writerPointer->address;
        requiredInnerExclusive = std::max<uint64_t>(
            requiredInnerExclusive, 1);
      }
      unsigned ordinal = 0;
      for (const Instruction &instruction : *innerBody) {
        auto *candidate = dyn_cast<StoreInst>(&instruction);
        if (candidate == writer.instruction)
          break;
        if (candidate != nullptr)
          ++ordinal;
      }
      writer.ordinal = ordinal;
      if (writer.ordinal != writerIndex)
        return std::nullopt;
    }
    if (sawDirectInnerWriter && sawTwoDimensionalWriter)
      return std::nullopt;

    auto boundedInputDomain =
        [&](Value *bound, unsigned inductionBits, uint64_t step,
            uint64_t requiredExclusive) {
          Value *source = bound;
          if (auto *extension = dyn_cast<ZExtInst>(source))
            source = extension->getOperand(0);
          auto *argument = dyn_cast<Argument>(source);
          unsigned sourceBits = integerBits(
              argument == nullptr ? nullptr : argument->getType());
          if (argument == nullptr || sourceBits == 0 || sourceBits > 64 ||
              requiredExclusive == 0)
            return false;
          unsigned __int128 sourceMaximum =
              sourceBits == 64
                  ? static_cast<unsigned __int128>(UINT64_MAX)
                  : (static_cast<unsigned __int128>(1) << sourceBits) - 1;
          unsigned __int128 inductionMaximum =
              inductionBits == 64
                  ? static_cast<unsigned __int128>(UINT64_MAX)
                  : (static_cast<unsigned __int128>(1) << inductionBits) - 1;
          return sourceMaximum >= requiredExclusive &&
                 step <= inductionMaximum + 1 &&
                 sourceMaximum <= inductionMaximum - step + 1;
        };
    if (!boundedInputDomain(outerBound, outerBits, outerStepValue, 1) ||
        !boundedInputDomain(innerBound, innerBits, innerStepValue,
                            requiredInnerExclusive))
      return std::nullopt;

    std::map<uint64_t, std::vector<MultiLatchLoopMemoryPhiAlternative>>
        laneAlternatives;
    std::set<int64_t> domain;
    std::set<std::pair<int64_t, int64_t>> pairDomain;
    for (unsigned writerIndex = 0;
         writerIndex < transfer.writers.size(); ++writerIndex) {
      const MultiLatchLoopMemoryPhiWriter &writer =
          transfer.writers[writerIndex];
      if (sawTwoDimensionalWriter) {
        for (const NestedLoopMemoryPhiWriterInstance &instance :
             writer.affineInstances) {
          pairDomain.insert({instance.outerInductionValue,
                             instance.innerInductionValue});
          for (unsigned lane = 0; lane < writer.bytes; ++lane)
            laneAlternatives[instance.address + lane].push_back(
                {0, writerIndex, instance.address, lane,
                 instance.innerInductionValue,
                 instance.outerInductionValue});
        }
      } else {
        for (size_t index = 0; index < writer.reachableAddresses.size();
             ++index) {
          uint64_t address = writer.reachableAddresses[index];
          int64_t inductionValue = writer.reachableIndexValues[index];
          domain.insert(inductionValue);
          for (unsigned lane = 0; lane < writer.bytes; ++lane)
            laneAlternatives[address + lane].push_back(
                {0, writerIndex, address, lane, inductionValue});
        }
      }
    }

    MultiLatchLoopMemoryPhiCertificate certificate;
    certificate.nestedSummary = true;
    certificate.nestedValueSummary = std::all_of(
        transfer.writers.begin(), transfer.writers.end(),
        [&](const MultiLatchLoopMemoryPhiWriter &writer) {
          bool constant =
              writer.constantValueBits != 0 &&
              writer.constantValueBytes.size() == writer.bytes;
          bool affine =
              sawTwoDimensionalWriter && writer.affineValue != nullptr &&
              writer.affineValueBits != 0 &&
              (writer.affineValueOuterScale != 0 ||
               writer.affineValueInnerScale != 0 ||
               writer.affineValueInputScale != 0);
          bool piecewise =
              sawTwoDimensionalWriter && writer.piecewiseValue.has_value();
          bool decisionDag =
              sawTwoDimensionalWriter && writer.decisionDagValue.has_value();
          return constant || affine || piecewise || decisionDag;
        });
    certificate.nestedDecisionDagValueSummary =
        sawTwoDimensionalWriter && certificate.nestedValueSummary &&
        std::any_of(
            transfer.writers.begin(), transfer.writers.end(),
            [](const MultiLatchLoopMemoryPhiWriter &writer) {
              return writer.decisionDagValue.has_value();
            });
    certificate.nestedPiecewiseValueSummary =
        !certificate.nestedDecisionDagValueSummary && sawTwoDimensionalWriter &&
        certificate.nestedValueSummary &&
        std::any_of(
            transfer.writers.begin(), transfer.writers.end(),
            [](const MultiLatchLoopMemoryPhiWriter &writer) {
              return writer.piecewiseValue.has_value();
            });
    certificate.nestedSymbolicValueSummary =
        sawTwoDimensionalWriter && certificate.nestedValueSummary &&
        std::any_of(
            transfer.writers.begin(), transfer.writers.end(),
            [](const MultiLatchLoopMemoryPhiWriter &writer) {
              return writer.affineValue != nullptr ||
                     writer.piecewiseValue.has_value() ||
                     writer.decisionDagValue.has_value();
            });
    certificate.nestedTwoDimensionalSummary =
        sawTwoDimensionalWriter && certificate.nestedValueSummary;
    if (sawTwoDimensionalWriter &&
        !certificate.nestedTwoDimensionalSummary)
      return std::nullopt;
    certificate.header = outerHeader;
    certificate.preheader = outerPreheader;
    certificate.root = innerPreheader;
    certificate.exit = outerExit;
    certificate.induction = outerInduction;
    certificate.guard = outerGuard;
    certificate.bound = outerBound;
    certificate.innerPreheader = innerPreheader;
    certificate.innerHeader = innerHeader;
    certificate.innerBody = innerBody;
    certificate.outerLatch = outerLatch;
    certificate.innerInduction = innerInduction;
    certificate.outerStep = outerStep;
    certificate.innerStep = innerStep;
    certificate.innerGuard = innerGuard;
    certificate.innerBound = innerBound;
    certificate.base = loaded.dynamicObjectAddress;
    certificate.loadBytes = bytes;
    certificate.inductionBits = outerBits;
    certificate.innerInductionBits = innerBits;
    certificate.stepValue = static_cast<int64_t>(outerStepValue);
    certificate.innerStepValue = static_cast<int64_t>(innerStepValue);
    certificate.loadIndexBits = loaded.dynamicIndexBits;
    certificate.loadMinimumIndex = loadAliases->minimumIndex;
    certificate.loadMaximumIndex = loadAliases->maximumIndex;
    certificate.transfers.push_back(std::move(transfer));
    certificate.loadAddresses = loadAliases->addresses;
    certificate.loadIndexValues = loadAliases->indices;
    certificate.closureDomain.assign(domain.begin(), domain.end());
    certificate.closurePairs.assign(pairDomain.begin(), pairDomain.end());
    std::set<uint64_t> closedLanes;
    if (certificate.nestedTwoDimensionalSummary) {
      for (const auto &[outerValue, innerValue] : certificate.closurePairs) {
        size_t before = closedLanes.size();
        for (const MultiLatchLoopMemoryPhiWriter &writer :
             certificate.transfers.front().writers)
          for (const NestedLoopMemoryPhiWriterInstance &instance :
               writer.affineInstances)
            if (instance.outerInductionValue == outerValue &&
                instance.innerInductionValue == innerValue)
              for (unsigned lane = 0; lane < writer.bytes; ++lane)
                closedLanes.insert(instance.address + lane);
        certificate.roundNewLanes.push_back(
            static_cast<unsigned>(closedLanes.size() - before));
        certificate.roundTotalLanes.push_back(
            static_cast<unsigned>(closedLanes.size()));
      }
    } else {
      for (int64_t inductionValue : certificate.closureDomain) {
        size_t before = closedLanes.size();
        for (const MultiLatchLoopMemoryPhiWriter &writer :
             certificate.transfers.front().writers) {
          auto found = std::lower_bound(
              writer.reachableIndexValues.begin(),
              writer.reachableIndexValues.end(), inductionValue);
          if (found == writer.reachableIndexValues.end() ||
              *found != inductionValue)
            continue;
          size_t index = static_cast<size_t>(std::distance(
              writer.reachableIndexValues.begin(), found));
          uint64_t address = writer.reachableAddresses[index];
          for (unsigned lane = 0; lane < writer.bytes; ++lane)
            closedLanes.insert(address + lane);
        }
        certificate.roundNewLanes.push_back(
            static_cast<unsigned>(closedLanes.size() - before));
        certificate.roundTotalLanes.push_back(
            static_cast<unsigned>(closedLanes.size()));
      }
    }
    certificate.finalLanes.assign(closedLanes.begin(), closedLanes.end());
    size_t alternativesCount = 0;
    for (uint64_t address : loadAliases->addresses)
      for (unsigned lane = 0; lane < bytes; ++lane) {
        auto found = laneAlternatives.find(address + lane);
        if (found == laneAlternatives.end() || found->second.empty())
          return std::nullopt;
        alternativesCount += found->second.size();
        if (alternativesCount > maximumWitnessAlternatives)
          return std::nullopt;
        certificate.witnesses.push_back({address, lane, found->second});
      }
    if (certificate.nestedDecisionDagValueSummary &&
        loaded.stackObject != nullptr) {
      std::set<const StoreInst *> summarizedStores;
      for (const MultiLatchLoopMemoryPhiTransfer &summaryTransfer :
           certificate.transfers)
        for (const MultiLatchLoopMemoryPhiWriter &writer :
             summaryTransfer.writers)
          summarizedStores.insert(writer.instruction);
      bool effectClosed = true;
      std::set<const StoreInst *> actualStores;
      for (BasicBlock *block : outerLoop->blocks())
        for (Instruction &loopInstruction : *block) {
          if (auto *store = dyn_cast<StoreInst>(&loopInstruction))
            actualStores.insert(store);
          bool supported =
              isa<PHINode>(loopInstruction) ||
              isa<BranchInst>(loopInstruction) ||
              isa<StoreInst>(loopInstruction) ||
              isa<BinaryOperator>(loopInstruction) ||
              isa<ICmpInst>(loopInstruction) ||
              isa<SelectInst>(loopInstruction) ||
              isa<GetElementPtrInst>(loopInstruction) ||
              isa<CastInst>(loopInstruction) ||
              isa<FreezeInst>(loopInstruction);
          if (!supported ||
              (loopInstruction.mayHaveSideEffects() &&
               !isa<BranchInst>(loopInstruction) &&
               !isa<StoreInst>(loopInstruction))) {
            effectClosed = false;
            break;
          }
          if (!loopInstruction.getType()->isVoidTy())
            for (const User *user : loopInstruction.users()) {
              auto *use = dyn_cast<Instruction>(user);
              if (use == nullptr || !outerLoop->contains(use)) {
                effectClosed = false;
                break;
              }
            }
          if (!effectClosed)
            break;
        }
      if (effectClosed && actualStores == summarizedStores) {
        certificate.executableMemoryTransfer = true;
        certificate.executableStackObject = loaded.stackObject;
      }
    }
    return certificate;
  }

  std::optional<MultiLatchLoopMemoryPhiCertificate>
  multiLatchLoopMemoryPhiInitialization(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumLanes = 2048;
    constexpr size_t maximumWitnessAlternatives = 8192;
    if (context.memorySSA == nullptr || alternatives.size() != 1)
      return std::nullopt;
    const StaticPointer &loaded = alternatives.front().pointer;
    if (loaded.dynamicIndex == nullptr || loaded.dynamicScale == 0 ||
        loaded.dynamicObjectAddress == 0 || loaded.interprocedural ||
        (loaded.stackObject == nullptr && loaded.heapObject == nullptr))
      return std::nullopt;
    auto loadAliases = memoryAliases(loaded, bytes, load, false);
    if (!loadAliases || loadAliases->addresses.empty() ||
        loadAliases->addresses.size() != loadAliases->indices.size() ||
        loadAliases->addresses.size() * bytes > maximumLanes)
      return std::nullopt;

    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    auto *memoryPhi = use == nullptr
                          ? nullptr
                          : dyn_cast<MemoryPhi>(use->getDefiningAccess());
    if (memoryPhi == nullptr)
      return std::nullopt;
    BasicBlock *header = memoryPhi->getBlock();
    LoopInfo loops(context.dominators);
    Loop *loop = loops.getLoopFor(header);
    if (loop == nullptr || loop->getHeader() != header ||
        loop->getNumBackEdges() < 2 || loop->getNumBackEdges() > 4)
      return std::nullopt;
    BasicBlock *preheader = loop->getLoopPreheader();
    BasicBlock *exit = loop->getExitBlock();
    if (preheader == nullptr || exit == nullptr || exit != load.getParent() ||
        exit->getUniquePredecessor() != header ||
        memoryPhi->getNumIncomingValues() != loop->getNumBackEdges() + 1)
      return std::nullopt;
    auto *preheaderBranch = dyn_cast<BranchInst>(preheader->getTerminator());
    auto *headerBranch = dyn_cast<BranchInst>(header->getTerminator());
    if (preheaderBranch == nullptr || !preheaderBranch->isUnconditional() ||
        preheaderBranch->getSuccessor(0) != header ||
        headerBranch == nullptr || !headerBranch->isConditional() ||
        headerBranch->getSuccessor(1) != exit ||
        !loop->contains(headerBranch->getSuccessor(0)))
      return std::nullopt;
    BasicBlock *root = headerBranch->getSuccessor(0);

    std::vector<const ICmpInst *> decisions;
    std::vector<MultiLatchLoopMemoryPhiTransfer> transfers;
    std::set<const BasicBlock *> visited;
    bool invalidTree = false;
    std::function<void(BasicBlock *, BasicBlock *, unsigned,
                       std::vector<MultiLatchLoopMemoryPhiGuard>)>
        visit = [&](BasicBlock *block, BasicBlock *predecessor,
                    unsigned depth,
                    std::vector<MultiLatchLoopMemoryPhiGuard> guards) {
          if (invalidTree)
            return;
          if (depth > 4 || block == header || block == exit ||
              !loop->contains(block) ||
              block->getUniquePredecessor() != predecessor ||
              !visited.insert(block).second) {
            invalidTree = true;
            return;
          }
          auto *branch = dyn_cast<BranchInst>(block->getTerminator());
          if (branch == nullptr) {
            invalidTree = true;
            return;
          }
          if (branch->isUnconditional()) {
            if (branch->getSuccessor(0) != header || transfers.size() == 4) {
              invalidTree = true;
              return;
            }
            MultiLatchLoopMemoryPhiTransfer transfer;
            transfer.latch = block;
            transfer.guards = std::move(guards);
            transfers.push_back(std::move(transfer));
            return;
          }
          auto *comparison = dyn_cast<ICmpInst>(branch->getCondition());
          auto predicate = comparison == nullptr
                               ? std::optional<StringRef>()
                               : comparisonOperator(comparison->getPredicate());
          if (comparison == nullptr || comparison->getParent() != block ||
              !predicate ||
              integerBits(comparison->getOperand(0)->getType()) == 0 ||
              comparison->getOperand(0)->getType() !=
                  comparison->getOperand(1)->getType() ||
              context.values.lookup(comparison).empty() ||
              depth > 3 || decisions.size() == 3) {
            invalidTree = true;
            return;
          }
          for (Value *operand : comparison->operands())
            if ((isa<ConstantInt>(operand) &&
                 cast<ConstantInt>(operand)->isNegative()) ||
                (!isa<ConstantInt>(operand) &&
                 context.values.lookup(operand).empty())) {
              invalidTree = true;
              return;
            }
          decisions.push_back(comparison);
          auto trueGuards = guards;
          trueGuards.push_back({comparison, true});
          guards.push_back({comparison, false});
          visit(branch->getSuccessor(0), block, depth + 1,
                std::move(trueGuards));
          visit(branch->getSuccessor(1), block, depth + 1,
                std::move(guards));
        };
    visit(root, header, 1, {});
    if (invalidTree || transfers.size() < 2 || transfers.size() > 4 ||
        decisions.empty() || visited.size() + 1 != loop->getNumBlocks())
      return std::nullopt;
    SmallVector<BasicBlock *, 4> loopLatches;
    loop->getLoopLatches(loopLatches);
    std::set<const BasicBlock *> expectedLatches(
        loopLatches.begin(), loopLatches.end());
    std::set<const BasicBlock *> actualLatches;
    for (const MultiLatchLoopMemoryPhiTransfer &transfer : transfers)
      actualLatches.insert(transfer.latch);
    if (expectedLatches != actualLatches ||
        pred_size(header) != transfers.size() + 1)
      return std::nullopt;

    auto *guard = dyn_cast<ICmpInst>(headerBranch->getCondition());
    if (guard == nullptr || guard->getPredicate() != ICmpInst::ICMP_ULT)
      return std::nullopt;
    auto *induction = dyn_cast<PHINode>(guard->getOperand(0));
    Value *bound = guard->getOperand(1);
    unsigned inductionBits = integerBits(
        induction == nullptr ? nullptr : induction->getType());
    if (induction == nullptr || induction->getParent() != header ||
        induction->getNumIncomingValues() != transfers.size() + 1 ||
        inductionBits == 0 || integerBits(bound->getType()) != inductionBits ||
        !loop->isLoopInvariant(bound))
      return std::nullopt;
    auto *seed = dyn_cast<ConstantInt>(
        induction->getIncomingValueForBlock(preheader));
    uint64_t stepValue = 0;
    for (MultiLatchLoopMemoryPhiTransfer &transfer : transfers) {
      auto *step = dyn_cast_or_null<BinaryOperator>(
          induction->getIncomingValueForBlock(transfer.latch));
      auto *constant =
          step == nullptr ? nullptr : dyn_cast<ConstantInt>(step->getOperand(1));
      if (seed == nullptr || !seed->isZero() || step == nullptr ||
          step->getOpcode() != Instruction::Add ||
          step->getParent() != transfer.latch ||
          step->getOperand(0) != induction || constant == nullptr ||
          inductionBits > 64 || constant->isZero() ||
          constant->isNegative() ||
          constant->getValue().getActiveBits() > 64 ||
          constant->getZExtValue() > 64 ||
          (stepValue != 0 && stepValue != constant->getZExtValue()))
        return std::nullopt;
      stepValue = constant->getZExtValue();
      transfer.step = step;
    }

    std::function<bool(const Value *, std::set<const Value *> &)>
        supportedDecisionOperand =
            [&](const Value *value,
                std::set<const Value *> &active) -> bool {
      if (value == induction)
        return true;
      if (auto *constant = dyn_cast<ConstantInt>(value))
        return integerBits(constant->getType()) != 0 &&
               !constant->isNegative();
      if (isa<Argument>(value))
        return integerBits(value->getType()) != 0 &&
               !context.values.lookup(value).empty();
      if (integerBits(value->getType()) == 0 ||
          context.values.lookup(value).empty() ||
          !active.insert(value).second)
        return false;

      bool supported = false;
      if (auto *cast = dyn_cast<CastInst>(value)) {
        supported =
            (cast->getOpcode() == Instruction::Trunc ||
             cast->getOpcode() == Instruction::ZExt ||
             cast->getOpcode() == Instruction::SExt ||
             cast->getOpcode() == Instruction::BitCast) &&
            supportedDecisionOperand(cast->getOperand(0), active);
      } else if (auto *binary = dyn_cast<BinaryOperator>(value)) {
        supported =
            supportedDecisionOperand(binary->getOperand(0), active) &&
            supportedDecisionOperand(binary->getOperand(1), active);
      } else if (auto *comparison = dyn_cast<ICmpInst>(value)) {
        supported =
            comparisonOperator(comparison->getPredicate()).has_value() &&
            supportedDecisionOperand(comparison->getOperand(0), active) &&
            supportedDecisionOperand(comparison->getOperand(1), active);
      } else if (auto *select = dyn_cast<SelectInst>(value)) {
        supported =
            integerBits(select->getCondition()->getType()) == 1 &&
            supportedDecisionOperand(select->getCondition(), active) &&
            supportedDecisionOperand(select->getTrueValue(), active) &&
            supportedDecisionOperand(select->getFalseValue(), active);
      } else if (auto *freeze = dyn_cast<FreezeInst>(value)) {
        supported =
            !isa<UndefValue>(freeze->getOperand(0)) &&
            !isa<PoisonValue>(freeze->getOperand(0)) &&
            !context.poisonConditions.count(freeze->getOperand(0)) &&
            supportedDecisionOperand(freeze->getOperand(0), active);
      }
      active.erase(value);
      return supported;
    };
    for (const ICmpInst *decision : decisions)
      for (const Value *operand : decision->operands()) {
        std::set<const Value *> active;
        if (!supportedDecisionOperand(operand, active))
          return std::nullopt;
      }

    for (BasicBlock *block : loop->blocks())
      for (Instruction &instruction : *block)
        if ((isa<PHINode>(instruction) && &instruction != induction) ||
            isa<CallBase>(instruction) || isa<AllocaInst>(instruction))
          return std::nullopt;

    Value *boundSource = bound;
    if (auto *extension = dyn_cast<ZExtInst>(boundSource))
      boundSource = extension->getOperand(0);
    auto *boundArgument = dyn_cast<Argument>(boundSource);
    unsigned boundSourceBits = integerBits(
        boundArgument == nullptr ? nullptr : boundArgument->getType());
    if (boundArgument == nullptr || boundSourceBits == 0 ||
        context.values.lookup(bound).empty())
      return std::nullopt;

    if (memoryPhi->getIncomingValueForBlock(preheader) !=
        context.memorySSA->getLiveOnEntryDef())
      return std::nullopt;
    std::set<const MemoryAccess *> writerDefinitions;
    unsigned writerCount = 0;
    for (MultiLatchLoopMemoryPhiTransfer &transfer : transfers) {
      MemoryAccess *incoming = memoryPhi->getIncomingValueForBlock(
          transfer.latch);
      if (incoming == memoryPhi)
        continue;
      while (incoming != memoryPhi) {
        auto *definition = dyn_cast_or_null<MemoryDef>(incoming);
        auto *instruction =
            definition == nullptr
                ? nullptr
                : dyn_cast_or_null<StoreInst>(definition->getMemoryInst());
        if (instruction == nullptr || !instruction->isSimple() ||
            instruction->getParent() != transfer.latch ||
            transfer.writers.size() == 4 || writerCount == 16)
          return std::nullopt;
        MultiLatchLoopMemoryPhiWriter writer;
        writer.instruction = instruction;
        transfer.writers.push_back(std::move(writer));
        writerDefinitions.insert(definition);
        ++writerCount;
        incoming = definition->getDefiningAccess();
      }
      std::reverse(transfer.writers.begin(), transfer.writers.end());
      for (size_t index = 1; index < transfer.writers.size(); ++index)
        if (!transfer.writers[index - 1].instruction->comesBefore(
                transfer.writers[index].instruction))
          return std::nullopt;
    }
    if (writerCount == 0)
      return std::nullopt;
    for (BasicBlock *block : loop->blocks()) {
      MemoryPhi *blockPhi = context.memorySSA->getMemoryAccess(block);
      if ((block == header && blockPhi != memoryPhi) ||
          (block != header && blockPhi != nullptr))
        return std::nullopt;
      for (Instruction &instruction : *block) {
        MemoryAccess *access = context.memorySSA->getMemoryAccess(&instruction);
        if (access != nullptr && !writerDefinitions.count(access))
          return std::nullopt;
      }
    }

    uint64_t requiredExclusive = 0;
    for (MultiLatchLoopMemoryPhiTransfer &transfer : transfers) {
      if (!transfer.isWriter())
        continue;
      for (size_t writerIndex = 0; writerIndex < transfer.writers.size();
           ++writerIndex) {
        MultiLatchLoopMemoryPhiWriter &writer =
            transfer.writers[writerIndex];
        Type *writerType = writer.instruction->getValueOperand()->getType();
        unsigned writerBits =
            writerType->isPointerTy()
                ? M.getDataLayout().getPointerSizeInBits(
                      writerType->getPointerAddressSpace())
                : integerBits(writerType);
        uint64_t writerBytes =
            writerBits == 0
                ? 0
                : fixedStoreBytes(M.getDataLayout(), writerType);
        if (writerBytes == 0 || writerBytes > 8)
          return std::nullopt;
        auto writerPointer = staticPointer(
            writer.instruction->getPointerOperand(), load);
        if (!writerPointer || writerPointer->dynamicIndex != induction ||
            writerPointer->dynamicScale <= 0 ||
            writerPointer->stackObject != loaded.stackObject ||
            writerPointer->heapObject != loaded.heapObject ||
            writerPointer->dynamicObjectAddress !=
                loaded.dynamicObjectAddress)
          return std::nullopt;
        __int128 addressStride =
            static_cast<__int128>(stepValue) *
            static_cast<__int128>(writerPointer->dynamicScale);
        if (addressStride <= 0 || addressStride > INT64_MAX ||
            writerBytes > static_cast<uint64_t>(addressStride))
          return std::nullopt;
        auto aliases = memoryAliases(
            *writerPointer, writerBytes, load, true);
        if (!aliases || aliases->addresses.empty() ||
            aliases->addresses.size() != aliases->indices.size())
          return std::nullopt;
        writer.bytes = static_cast<unsigned>(writerBytes);
        writer.scale = writerPointer->dynamicScale;
        writer.addressStride = static_cast<uint64_t>(addressStride);
        writer.minimumIndex = aliases->minimumIndex;
        writer.maximumIndex = aliases->maximumIndex;
        writer.addresses = aliases->addresses;
        writer.indexValues = aliases->indices;
        for (size_t index = 0; index < aliases->addresses.size(); ++index) {
          int64_t value = aliases->indices[index];
          if (value < seed->getSExtValue() ||
              static_cast<uint64_t>(value - seed->getSExtValue()) %
                      stepValue !=
                  0)
            continue;
          writer.reachableAddresses.push_back(aliases->addresses[index]);
          writer.reachableIndexValues.push_back(value);
        }
        if (writer.reachableAddresses.empty() ||
            writer.reachableIndexValues.front() != seed->getSExtValue() ||
            !std::is_sorted(
                writer.reachableAddresses.begin(),
                writer.reachableAddresses.end()) ||
            !std::is_sorted(
                writer.reachableIndexValues.begin(),
                writer.reachableIndexValues.end()))
          return std::nullopt;
        requiredExclusive = std::max(
            requiredExclusive,
            static_cast<uint64_t>(writer.reachableIndexValues.back()) + 1);
        unsigned ordinal = 0;
        for (const Instruction &instruction : *transfer.latch) {
          auto *candidate = dyn_cast<StoreInst>(&instruction);
          if (candidate == writer.instruction)
            break;
          if (candidate != nullptr)
            ++ordinal;
        }
        writer.ordinal = ordinal;
        if (writer.ordinal != writerIndex)
          return std::nullopt;
      }
    }
    APInt maximumBound = APInt::getMaxValue(boundSourceBits);
    if (requiredExclusive == 0 ||
        !isUIntN(boundSourceBits, requiredExclusive) ||
        maximumBound.ult(APInt(boundSourceBits, requiredExclusive)))
      return std::nullopt;
    unsigned __int128 sourceMaximum =
        boundSourceBits == 64
            ? static_cast<unsigned __int128>(UINT64_MAX)
            : (static_cast<unsigned __int128>(1) << boundSourceBits) - 1;
    unsigned __int128 inductionMaximum =
        inductionBits == 64
            ? static_cast<unsigned __int128>(UINT64_MAX)
            : (static_cast<unsigned __int128>(1) << inductionBits) - 1;
    if (stepValue > inductionMaximum + 1 ||
        sourceMaximum > inductionMaximum - stepValue + 1)
      return std::nullopt;

    std::map<uint64_t, std::vector<MultiLatchLoopMemoryPhiAlternative>>
        laneAlternatives;
    std::set<int64_t> domain;
    for (unsigned transferIndex = 0; transferIndex < transfers.size();
         ++transferIndex) {
      const MultiLatchLoopMemoryPhiTransfer &transfer =
          transfers[transferIndex];
      for (unsigned writerIndex = 0;
           writerIndex < transfer.writers.size(); ++writerIndex) {
        const MultiLatchLoopMemoryPhiWriter &writer =
            transfer.writers[writerIndex];
        for (size_t index = 0;
             index < writer.reachableAddresses.size(); ++index) {
          uint64_t address = writer.reachableAddresses[index];
          int64_t inductionValue = writer.reachableIndexValues[index];
          domain.insert(inductionValue);
          for (unsigned lane = 0; lane < writer.bytes; ++lane)
            laneAlternatives[address + lane].push_back(
                {transferIndex, writerIndex, address, lane, inductionValue});
        }
      }
    }

    MultiLatchLoopMemoryPhiCertificate certificate;
    certificate.header = header;
    certificate.preheader = preheader;
    certificate.root = root;
    certificate.exit = exit;
    certificate.induction = induction;
    certificate.guard = guard;
    certificate.bound = bound;
    certificate.base = loaded.dynamicObjectAddress;
    certificate.loadBytes = bytes;
    certificate.inductionBits = inductionBits;
    certificate.stepValue = static_cast<int64_t>(stepValue);
    certificate.loadIndexBits = loaded.dynamicIndexBits;
    certificate.loadMinimumIndex = loadAliases->minimumIndex;
    certificate.loadMaximumIndex = loadAliases->maximumIndex;
    certificate.decisions = std::move(decisions);
    certificate.transfers = std::move(transfers);
    certificate.loadAddresses = loadAliases->addresses;
    certificate.loadIndexValues = loadAliases->indices;
    certificate.closureDomain.assign(domain.begin(), domain.end());
    std::set<uint64_t> closedLanes;
    for (int64_t inductionValue : certificate.closureDomain) {
      size_t before = closedLanes.size();
      for (const MultiLatchLoopMemoryPhiTransfer &transfer :
           certificate.transfers) {
        for (const MultiLatchLoopMemoryPhiWriter &writer :
             transfer.writers) {
          auto found = std::lower_bound(
              writer.reachableIndexValues.begin(),
              writer.reachableIndexValues.end(), inductionValue);
          if (found == writer.reachableIndexValues.end() ||
              *found != inductionValue)
            continue;
          size_t index = static_cast<size_t>(std::distance(
              writer.reachableIndexValues.begin(), found));
          uint64_t address = writer.reachableAddresses[index];
          for (unsigned lane = 0; lane < writer.bytes; ++lane)
            closedLanes.insert(address + lane);
        }
      }
      certificate.roundNewLanes.push_back(
          static_cast<unsigned>(closedLanes.size() - before));
      certificate.roundTotalLanes.push_back(
          static_cast<unsigned>(closedLanes.size()));
    }
    certificate.finalLanes.assign(closedLanes.begin(), closedLanes.end());
    size_t alternativesCount = 0;
    for (uint64_t address : loadAliases->addresses)
      for (unsigned lane = 0; lane < bytes; ++lane) {
        auto found = laneAlternatives.find(address + lane);
        if (found == laneAlternatives.end() || found->second.empty())
          return std::nullopt;
        alternativesCount += found->second.size();
        if (alternativesCount > maximumWitnessAlternatives)
          return std::nullopt;
        certificate.witnesses.push_back(
            {address, lane, found->second});
      }
    return certificate;
  }

  json::Object nestedLoopMemoryPhiSummaryRecord(
      const MultiLatchLoopMemoryPhiCertificate &certificate,
      const FunctionContext &context) const {
    json::Object transcript;
    transcript["schema"] =
        certificate.nestedDecisionDagValueSummary
            ? "symcc-loop-memoryphi-byte-lane-induction-v11"
        : certificate.nestedPiecewiseValueSummary
            ? "symcc-loop-memoryphi-byte-lane-induction-v10"
        : certificate.nestedSymbolicValueSummary
            ? "symcc-loop-memoryphi-byte-lane-induction-v9"
            : certificate.nestedTwoDimensionalSummary
            ? "symcc-loop-memoryphi-byte-lane-induction-v8"
            : certificate.nestedValueSummary
                  ? "symcc-loop-memoryphi-byte-lane-induction-v7"
                  : "symcc-loop-memoryphi-byte-lane-induction-v6";
    transcript["base"] = static_cast<int64_t>(certificate.base);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);

    json::Object loops;
    json::Object outer;
    outer["header"] = context.blocks.lookup(certificate.header);
    outer["preheader"] = context.blocks.lookup(certificate.preheader);
    outer["preheader_edge"] = context.edgeBlocks.at(
        {certificate.preheader, certificate.header});
    outer["inner_entry"] =
        context.blocks.lookup(certificate.innerPreheader);
    outer["latch"] = context.blocks.lookup(certificate.outerLatch);
    outer["latch_edge"] = context.edgeBlocks.at(
        {certificate.outerLatch, certificate.header});
    outer["exit"] = context.blocks.lookup(certificate.exit);
    loops["outer"] = std::move(outer);
    json::Object inner;
    inner["header"] = context.blocks.lookup(certificate.innerHeader);
    inner["preheader"] =
        context.blocks.lookup(certificate.innerPreheader);
    inner["preheader_edge"] = context.edgeBlocks.at(
        {certificate.innerPreheader, certificate.innerHeader});
    inner["body"] = context.blocks.lookup(certificate.innerBody);
    inner["body_edge"] = context.edgeBlocks.at(
        {certificate.innerBody, certificate.innerHeader});
    inner["exit"] = context.blocks.lookup(certificate.outerLatch);
    loops["inner"] = std::move(inner);
    transcript["loops"] = std::move(loops);

    json::Object memoryPhis;
    memoryPhis["equation"] =
        "H_outer=phi(entry,S_inner(H_outer))";
    json::Object outerPhi;
    outerPhi["block"] = context.blocks.lookup(certificate.header);
    outerPhi["preheader"] = "live-on-entry";
    outerPhi["backedge"] = "inner-memory-phi-summary";
    memoryPhis["outer"] = std::move(outerPhi);
    json::Object innerPhi;
    innerPhi["block"] = context.blocks.lookup(certificate.innerHeader);
    innerPhi["preheader"] = "outer-memory-phi";
    innerPhi["backedge"] = "ordered-writer-memory-def-chain";
    memoryPhis["inner"] = std::move(innerPhi);
    transcript["memory_phis"] = std::move(memoryPhis);

    auto encodeInduction = [&](const PHINode *induction,
                               unsigned bits, int64_t step,
                               const BinaryOperator *next) {
      json::Object item;
      item["variable"] =
          variableOperand(context.values.lookup(induction));
      item["bits"] = static_cast<int64_t>(bits);
      item["seed"] = 0;
      item["step"] = step;
      item["next"] = variableOperand(context.values.lookup(next));
      return item;
    };
    transcript["outer_induction"] = encodeInduction(
        certificate.induction, certificate.inductionBits,
        certificate.stepValue, certificate.outerStep);
    transcript["inner_induction"] = encodeInduction(
        certificate.innerInduction, certificate.innerInductionBits,
        certificate.innerStepValue, certificate.innerStep);

    auto encodeGuard = [&](const ICmpInst *guard, const Value *bound,
                           const BasicBlock *continueBlock,
                           const BasicBlock *exitBlock) {
      json::Object item;
      item["variable"] = variableOperand(context.values.lookup(guard));
      item["predicate"] = "ult";
      item["bound"] = variableOperand(context.values.lookup(bound));
      item["continue"] = context.blocks.lookup(continueBlock);
      item["exit"] = context.blocks.lookup(exitBlock);
      return item;
    };
    transcript["outer_guard"] = encodeGuard(
        certificate.guard, certificate.bound, certificate.innerPreheader,
        certificate.exit);
    transcript["inner_guard"] = encodeGuard(
        certificate.innerGuard, certificate.innerBound,
        certificate.innerBody, certificate.outerLatch);

    const MultiLatchLoopMemoryPhiTransfer &transfer =
        certificate.transfers.front();
    auto encodePiecewiseArm = [&](
                                  const NestedLoopMemoryPhiAffineWriterValue
                                      &source) {
      json::Object arm;
      arm["kind"] = "affine-bitvector-arm";
      arm["bits"] = static_cast<int64_t>(source.bits);
      if (auto *constant = dyn_cast<ConstantInt>(source.operand))
        arm["operand"] = integerConstant(
            static_cast<int64_t>(constant->getZExtValue()),
            constant->getBitWidth());
      else
        arm["operand"] = variableOperand(
            context.values.lookup(source.operand));
      arm["constant"] = source.constant;
      arm["outer_scale"] = source.outerScale;
      arm["inner_scale"] = source.innerScale;
      arm["input_scale"] = source.inputScale;
      arm["semantics"] = "modulo-2^bits";
      if (source.input != nullptr) {
        json::Object input;
        input["variable"] = variableOperand(
            context.values.lookup(source.input));
        input["offset"] = static_cast<int64_t>(source.inputOffset);
        input["bytes"] = static_cast<int64_t>(source.inputBytes);
        arm["input"] = std::move(input);
      } else {
        arm["input"] = nullptr;
      }
      return arm;
    };
    auto encodeStoredValue = [&](const MultiLatchLoopMemoryPhiWriter &source) {
      json::Object storedValue;
      if (source.decisionDagValue) {
        const NestedLoopMemoryPhiDecisionDagWriterValue &dag =
            *source.decisionDagValue;
        storedValue["kind"] = "affine-decision-dag-bitvector";
        storedValue["bits"] = static_cast<int64_t>(
            integerBits(source.instruction->getValueOperand()->getType()));
        storedValue["variable"] = variableOperand(
            context.values.lookup(dag.rootOperand));
        storedValue["root"] = static_cast<int64_t>(dag.root);
        storedValue["depth"] = static_cast<int64_t>(dag.depth);
        json::Array nodes;
        for (size_t index = 0; index < dag.nodes.size(); ++index) {
          const NestedLoopMemoryPhiDecisionDagNode &sourceNode =
              dag.nodes[index];
          json::Object node;
          node["id"] = static_cast<int64_t>(index);
          if (sourceNode.kind ==
              NestedLoopMemoryPhiDecisionDagNode::Kind::Affine) {
            node["kind"] = "affine-leaf";
            node["value"] = encodePiecewiseArm(sourceNode.affine);
          } else {
            node["kind"] = "guard";
            node["operand"] = variableOperand(
                context.values.lookup(sourceNode.operand));
            json::Object guard;
            guard["variable"] = variableOperand(
                context.values.lookup(sourceNode.guard));
            guard["predicate"] = *comparisonOperator(
                sourceNode.guard->getPredicate());
            guard["induction"] =
                sourceNode.guardUsesOuterInduction ? "outer" : "inner";
            guard["constant_on_left"] =
                sourceNode.guardConstantOnLeft;
            guard["constant"] = sourceNode.guardConstant;
            guard["bits"] = static_cast<int64_t>(integerBits(
                sourceNode.guard->getOperand(0)->getType()));
            node["guard"] = std::move(guard);
            node["when_true"] = static_cast<int64_t>(sourceNode.whenTrue);
            node["when_false"] = static_cast<int64_t>(sourceNode.whenFalse);
          }
          nodes.push_back(std::move(node));
        }
        storedValue["nodes"] = std::move(nodes);
        storedValue["semantics"] =
            "postorder-shared-guard-specialized-modulo-2^bits";
        return storedValue;
      }
      if (source.piecewiseValue) {
        const NestedLoopMemoryPhiPiecewiseWriterValue &piecewise =
            *source.piecewiseValue;
        storedValue["kind"] = "piecewise-affine-bitvector";
        storedValue["bits"] = static_cast<int64_t>(
            piecewise.whenTrue.bits);
        storedValue["variable"] = variableOperand(
            context.values.lookup(piecewise.selection));
        json::Object guard;
        guard["variable"] = variableOperand(
            context.values.lookup(piecewise.guard));
        guard["predicate"] = *comparisonOperator(
            piecewise.guard->getPredicate());
        guard["induction"] =
            piecewise.guardUsesOuterInduction ? "outer" : "inner";
        guard["constant_on_left"] = piecewise.guardConstantOnLeft;
        guard["constant"] = piecewise.guardConstant;
        guard["bits"] = static_cast<int64_t>(integerBits(
            piecewise.guard->getOperand(0)->getType()));
        storedValue["guard"] = std::move(guard);
        storedValue["when_true"] = encodePiecewiseArm(piecewise.whenTrue);
        storedValue["when_false"] = encodePiecewiseArm(piecewise.whenFalse);
        storedValue["semantics"] = "guard-specialized-modulo-2^bits";
        return storedValue;
      }
      if (source.affineValue != nullptr) {
        storedValue["kind"] = "affine-bitvector";
        storedValue["bits"] =
            static_cast<int64_t>(source.affineValueBits);
        storedValue["variable"] = variableOperand(
            context.values.lookup(source.affineValue));
        storedValue["constant"] = source.affineValueConstant;
        storedValue["outer_scale"] = source.affineValueOuterScale;
        storedValue["inner_scale"] = source.affineValueInnerScale;
        storedValue["input_scale"] = source.affineValueInputScale;
        storedValue["semantics"] = "modulo-2^bits";
        if (source.affineValueInput != nullptr) {
          json::Object input;
          input["variable"] = variableOperand(
              context.values.lookup(source.affineValueInput));
          input["offset"] = static_cast<int64_t>(
              source.affineValueInputOffset);
          input["bytes"] = static_cast<int64_t>(
              source.affineValueInputBytes);
          storedValue["input"] = std::move(input);
        } else {
          storedValue["input"] = nullptr;
        }
        return storedValue;
      }
      storedValue["kind"] = "constant-integer";
      storedValue["bits"] =
          static_cast<int64_t>(source.constantValueBits);
      storedValue["operand"] = source.constantValue;
      json::Array valueBytes;
      for (uint8_t valueByte : source.constantValueBytes)
        valueBytes.push_back(static_cast<int64_t>(valueByte));
      storedValue["bytes"] = std::move(valueBytes);
      return storedValue;
    };
    auto encodeWriter = [&](const MultiLatchLoopMemoryPhiWriter &source) {
      json::Object writer;
      writer["block"] = context.blocks.lookup(certificate.innerBody);
      writer["ordinal"] = static_cast<int64_t>(source.ordinal);
      writer["bytes"] = static_cast<int64_t>(source.bytes);
      writer["minimum"] = source.minimumIndex;
      writer["maximum"] = source.maximumIndex;
      if (certificate.nestedTwoDimensionalSummary) {
        writer["pointer_base"] = static_cast<int64_t>(source.pointerBase);
        writer["pointer_scale"] = source.pointerScale;
        json::Array addresses;
        for (uint64_t address : source.addresses)
          addresses.push_back(static_cast<int64_t>(address));
        writer["addresses"] = std::move(addresses);
        json::Array values;
        for (int64_t value : source.indexValues)
          values.push_back(value);
        writer["index_values"] = std::move(values);
        json::Object affineIndex;
        affineIndex["variable"] = variableOperand(
            context.values.lookup(source.affineIndex));
        affineIndex["bits"] =
            static_cast<int64_t>(source.affineIndexBits);
        affineIndex["constant"] = source.affineConstant;
        affineIndex["outer_scale"] = source.affineOuterScale;
        affineIndex["inner_scale"] = source.affineInnerScale;
        writer["affine_index"] = std::move(affineIndex);
        json::Array instances;
        for (const NestedLoopMemoryPhiWriterInstance &sourceInstance :
             source.affineInstances) {
          json::Object instance;
          instance["outer_induction_value"] =
              sourceInstance.outerInductionValue;
          instance["inner_induction_value"] =
              sourceInstance.innerInductionValue;
          instance["index_value"] = sourceInstance.indexValue;
          instance["address"] =
              static_cast<int64_t>(sourceInstance.address);
          instances.push_back(std::move(instance));
        }
        writer["instances"] = std::move(instances);
        writer["stored_value"] = encodeStoredValue(source);
        return writer;
      }
      writer["scale"] = source.scale;
      writer["address_stride"] =
          static_cast<int64_t>(source.addressStride);
      json::Array addresses;
      for (uint64_t address : source.addresses)
        addresses.push_back(static_cast<int64_t>(address));
      writer["addresses"] = std::move(addresses);
      json::Array values;
      for (int64_t value : source.indexValues)
        values.push_back(value);
      writer["index_values"] = std::move(values);
      json::Array reachableAddresses;
      for (uint64_t address : source.reachableAddresses)
        reachableAddresses.push_back(static_cast<int64_t>(address));
      writer["reachable_addresses"] = std::move(reachableAddresses);
      json::Array reachableValues;
      for (int64_t value : source.reachableIndexValues)
        reachableValues.push_back(value);
      writer["reachable_index_values"] = std::move(reachableValues);
      writer["residue_origin"] =
          static_cast<int64_t>(source.reachableAddresses.front());
      json::Array residues;
      for (unsigned lane = 0; lane < source.bytes; ++lane)
        residues.push_back(static_cast<int64_t>(lane));
      writer["covered_residues"] = std::move(residues);
      if (certificate.nestedValueSummary) {
        writer["stored_value"] = encodeStoredValue(source);
      }
      return writer;
    };
    json::Object summary;
    summary["order"] = "inner-to-outer";
    summary["input"] = "outer-memory-phi";
    summary["output"] = "inner-memory-phi";
    json::Array writers;
    for (const MultiLatchLoopMemoryPhiWriter &writer : transfer.writers)
      writers.push_back(encodeWriter(writer));
    summary["writers"] = std::move(writers);
    json::Object fixedPoint;
    fixedPoint["algorithm"] = "finite-monotone-byte-lane-union";
    fixedPoint["semantics"] =
        "inner-to-outer-ordered-writer-summary";
    json::Array domain;
    json::Array rounds;
    if (certificate.nestedTwoDimensionalSummary) {
      fixedPoint["algorithm"] =
          "finite-lexicographic-two-dimensional-byte-lane-union";
      fixedPoint["semantics"] =
          "outer-inner-affine-ordered-writer-summary";
      for (size_t index = 0; index < certificate.closurePairs.size();
           ++index) {
        const auto &[outerValue, innerValue] =
            certificate.closurePairs[index];
        json::Object domainItem;
        domainItem["outer_induction_value"] = outerValue;
        domainItem["inner_induction_value"] = innerValue;
        domain.push_back(std::move(domainItem));
        json::Object round;
        round["outer_induction_value"] = outerValue;
        round["inner_induction_value"] = innerValue;
        round["new_lanes"] =
            static_cast<int64_t>(certificate.roundNewLanes[index]);
        round["total_lanes"] =
            static_cast<int64_t>(certificate.roundTotalLanes[index]);
        rounds.push_back(std::move(round));
      }
    } else {
      for (size_t index = 0; index < certificate.closureDomain.size();
           ++index) {
        domain.push_back(certificate.closureDomain[index]);
        json::Object round;
        round["inner_induction_value"] = certificate.closureDomain[index];
        round["new_lanes"] =
            static_cast<int64_t>(certificate.roundNewLanes[index]);
        round["total_lanes"] =
            static_cast<int64_t>(certificate.roundTotalLanes[index]);
        rounds.push_back(std::move(round));
      }
    }
    json::Object stableRound;
    stableRound["kind"] = "stability-check";
    stableRound["new_lanes"] = 0;
    stableRound["total_lanes"] =
        static_cast<int64_t>(certificate.finalLanes.size());
    rounds.push_back(std::move(stableRound));
    fixedPoint["domain"] = std::move(domain);
    fixedPoint["rounds"] = std::move(rounds);
    json::Array finalLanes;
    for (uint64_t lane : certificate.finalLanes)
      finalLanes.push_back(static_cast<int64_t>(lane));
    fixedPoint["final_lanes"] = std::move(finalLanes);
    fixedPoint["stable"] = true;
    summary["fixed_point"] = std::move(fixedPoint);
    if (certificate.nestedValueSummary) {
      json::Object valueSemantics;
      valueSemantics["kind"] =
          certificate.nestedDecisionDagValueSummary
              ? "guard-specialized-affine-decision-dag-bitvector-byte-"
                "two-dimensional-last-write"
          : certificate.nestedPiecewiseValueSummary
              ? "guard-specialized-piecewise-affine-bitvector-byte-"
                "two-dimensional-last-write"
          : certificate.nestedSymbolicValueSummary
              ? "affine-bitvector-byte-two-dimensional-last-write"
              : certificate.nestedTwoDimensionalSummary
              ? "constant-byte-two-dimensional-last-write"
              : "constant-byte-last-write";
      valueSemantics["endianness"] =
          M.getDataLayout().isLittleEndian() ? "little" : "big";
      valueSemantics["outer_activation"] = "outer-bound-positive";
      valueSemantics["case_order"] =
          certificate.nestedTwoDimensionalSummary
              ? "descending-outer-then-inner-induction-then-writer-ordinal"
              : "descending-inner-induction-then-writer-ordinal";
      valueSemantics["case_predicate"] =
          certificate.nestedTwoDimensionalSummary
              ? "outer-and-inner-bounds-at-least-minima"
              : "inner-bound-at-least-minimum";
      valueSemantics["selection"] = "first-match";
      valueSemantics["fallback"] = "uninitialized";
      summary["value_semantics"] = std::move(valueSemantics);
    }
    transcript["summary"] = std::move(summary);

    json::Object loadIndex;
    loadIndex["bits"] = static_cast<int64_t>(certificate.loadIndexBits);
    loadIndex["minimum"] = certificate.loadMinimumIndex;
    loadIndex["maximum"] = certificate.loadMaximumIndex;
    json::Array loadValues;
    for (int64_t value : certificate.loadIndexValues)
      loadValues.push_back(value);
    loadIndex["values"] = std::move(loadValues);
    transcript["load_index"] = std::move(loadIndex);
    json::Array loadAddresses;
    for (uint64_t address : certificate.loadAddresses)
      loadAddresses.push_back(static_cast<int64_t>(address));
    transcript["load_addresses"] = std::move(loadAddresses);
    json::Array witnesses;
    for (const MultiLatchLoopMemoryPhiWitness &witness :
         certificate.witnesses) {
      json::Object item;
      item["load_address"] = static_cast<int64_t>(witness.loadAddress);
      item["lane"] = static_cast<int64_t>(witness.lane);
      std::vector<MultiLatchLoopMemoryPhiAlternative> sources =
          witness.alternatives;
      if (certificate.nestedValueSummary)
        std::stable_sort(
            sources.begin(), sources.end(),
            [&](const MultiLatchLoopMemoryPhiAlternative &left,
                const MultiLatchLoopMemoryPhiAlternative &right) {
              if (certificate.nestedTwoDimensionalSummary &&
                  left.outerInductionValue != right.outerInductionValue)
                return left.outerInductionValue >
                       right.outerInductionValue;
              if (left.inductionValue != right.inductionValue)
                return left.inductionValue > right.inductionValue;
              return left.writer > right.writer;
            });
      json::Array alternatives;
      for (const MultiLatchLoopMemoryPhiAlternative &source : sources) {
        json::Object alternative;
        alternative["writer"] = static_cast<int64_t>(source.writer);
        alternative["writer_address"] =
            static_cast<int64_t>(source.writerAddress);
        alternative["writer_lane"] =
            static_cast<int64_t>(source.writerLane);
        alternative["inner_induction_value"] = source.inductionValue;
        if (certificate.nestedValueSummary) {
          if (certificate.nestedTwoDimensionalSummary) {
            alternative["outer_induction_value"] =
                source.outerInductionValue;
            alternative["minimum_outer_bound"] =
                source.outerInductionValue + 1;
          }
          alternative["minimum_inner_bound"] =
              source.inductionValue + 1;
          const MultiLatchLoopMemoryPhiWriter &writer =
              transfer.writers[source.writer];
          if (certificate.nestedSymbolicValueSummary) {
            json::Object expression;
            if (writer.decisionDagValue) {
              const NestedLoopMemoryPhiDecisionDagWriterValue &dag =
                  *writer.decisionDagValue;
              unsigned current = dag.root;
              json::Array path;
              while (dag.nodes[current].kind ==
                     NestedLoopMemoryPhiDecisionDagNode::Kind::Guard) {
                const NestedLoopMemoryPhiDecisionDagNode &node =
                    dag.nodes[current];
                uint64_t induction = static_cast<uint64_t>(
                    node.guardUsesOuterInduction
                        ? source.outerInductionValue
                        : source.inductionValue);
                uint64_t constant = static_cast<uint64_t>(node.guardConstant);
                uint64_t left = node.guardConstantOnLeft ? constant : induction;
                uint64_t right = node.guardConstantOnLeft ? induction : constant;
                bool result = false;
                switch (node.guard->getPredicate()) {
                case ICmpInst::ICMP_EQ:
                  result = left == right;
                  break;
                case ICmpInst::ICMP_NE:
                  result = left != right;
                  break;
                case ICmpInst::ICMP_UGT:
                  result = left > right;
                  break;
                case ICmpInst::ICMP_UGE:
                  result = left >= right;
                  break;
                case ICmpInst::ICMP_ULT:
                  result = left < right;
                  break;
                case ICmpInst::ICMP_ULE:
                  result = left <= right;
                  break;
                default:
                  llvm_unreachable("unsupported decision DAG guard predicate");
                }
                json::Object step;
                step["node"] = static_cast<int64_t>(current);
                step["guard"] = variableOperand(
                    context.values.lookup(node.guard));
                step["guard_result"] = result;
                step["selected_arm"] = result ? "true" : "false";
                path.push_back(std::move(step));
                current = result ? node.whenTrue : node.whenFalse;
              }
              const NestedLoopMemoryPhiAffineWriterValue &selected =
                  dag.nodes[current].affine;
              APInt specialized(selected.bits, selected.constant);
              specialized += APInt(selected.bits, selected.outerScale) *
                             APInt(selected.bits,
                                   source.outerInductionValue);
              specialized += APInt(selected.bits, selected.innerScale) *
                             APInt(selected.bits, source.inductionValue);
              json::Object selectedExpression;
              selectedExpression["kind"] =
                  "extract-affine-bitvector-byte";
              selectedExpression["bits"] =
                  static_cast<int64_t>(selected.bits);
              selectedExpression["input"] =
                  selected.input == nullptr
                      ? json::Value(nullptr)
                      : json::Value(variableOperand(
                            context.values.lookup(selected.input)));
              selectedExpression["input_scale"] = selected.inputScale;
              selectedExpression["constant"] =
                  specialized.sextOrTrunc(64).getSExtValue();
              unsigned valueLane =
                  M.getDataLayout().isLittleEndian()
                      ? source.writerLane
                      : writer.bytes - source.writerLane - 1;
              selectedExpression["low_bit"] =
                  static_cast<int64_t>(valueLane * 8);
              expression["kind"] =
                  "guard-specialized-decision-dag-byte";
              expression["root"] = static_cast<int64_t>(dag.root);
              expression["path"] = std::move(path);
              expression["leaf"] = static_cast<int64_t>(current);
              expression["value"] = std::move(selectedExpression);
            } else if (writer.piecewiseValue) {
              const NestedLoopMemoryPhiPiecewiseWriterValue &piecewise =
                  *writer.piecewiseValue;
              uint64_t induction = static_cast<uint64_t>(
                  piecewise.guardUsesOuterInduction
                      ? source.outerInductionValue
                      : source.inductionValue);
              uint64_t constant = static_cast<uint64_t>(
                  piecewise.guardConstant);
              uint64_t left = piecewise.guardConstantOnLeft
                                  ? constant
                                  : induction;
              uint64_t right = piecewise.guardConstantOnLeft
                                   ? induction
                                   : constant;
              bool result = false;
              switch (piecewise.guard->getPredicate()) {
              case ICmpInst::ICMP_EQ:
                result = left == right;
                break;
              case ICmpInst::ICMP_NE:
                result = left != right;
                break;
              case ICmpInst::ICMP_UGT:
                result = left > right;
                break;
              case ICmpInst::ICMP_UGE:
                result = left >= right;
                break;
              case ICmpInst::ICMP_ULT:
                result = left < right;
                break;
              case ICmpInst::ICMP_ULE:
                result = left <= right;
                break;
              default:
                llvm_unreachable("unsupported piecewise guard predicate");
              }
              const NestedLoopMemoryPhiAffineWriterValue &selected =
                  result ? piecewise.whenTrue : piecewise.whenFalse;
              APInt specialized(selected.bits, selected.constant);
              specialized += APInt(selected.bits, selected.outerScale) *
                             APInt(selected.bits,
                                   source.outerInductionValue);
              specialized += APInt(selected.bits, selected.innerScale) *
                             APInt(selected.bits, source.inductionValue);
              json::Object selectedExpression;
              selectedExpression["kind"] =
                  "extract-affine-bitvector-byte";
              selectedExpression["bits"] =
                  static_cast<int64_t>(selected.bits);
              selectedExpression["input"] =
                  selected.input == nullptr
                      ? json::Value(nullptr)
                      : json::Value(variableOperand(
                            context.values.lookup(selected.input)));
              selectedExpression["input_scale"] = selected.inputScale;
              selectedExpression["constant"] =
                  specialized.sextOrTrunc(64).getSExtValue();
              unsigned valueLane =
                  M.getDataLayout().isLittleEndian()
                      ? source.writerLane
                      : writer.bytes - source.writerLane - 1;
              selectedExpression["low_bit"] =
                  static_cast<int64_t>(valueLane * 8);
              expression["kind"] = "guard-specialized-byte";
              expression["guard"] = variableOperand(
                  context.values.lookup(piecewise.guard));
              expression["guard_result"] = result;
              expression["selected_arm"] = result ? "true" : "false";
              expression["value"] = std::move(selectedExpression);
            } else if (writer.affineValue == nullptr) {
              expression["kind"] = "constant-byte";
              expression["value"] = static_cast<int64_t>(
                  writer.constantValueBytes[source.writerLane]);
            } else {
              APInt specialized(writer.affineValueBits,
                                writer.affineValueConstant);
              specialized +=
                  APInt(writer.affineValueBits,
                        writer.affineValueOuterScale) *
                  APInt(writer.affineValueBits,
                        source.outerInductionValue);
              specialized +=
                  APInt(writer.affineValueBits,
                        writer.affineValueInnerScale) *
                  APInt(writer.affineValueBits,
                        source.inductionValue);
              expression["kind"] =
                  "extract-affine-bitvector-byte";
              expression["bits"] =
                  static_cast<int64_t>(writer.affineValueBits);
              expression["input"] =
                  writer.affineValueInput == nullptr
                      ? json::Value(nullptr)
                      : json::Value(variableOperand(context.values.lookup(
                            writer.affineValueInput)));
              expression["input_scale"] = writer.affineValueInputScale;
              expression["constant"] =
                  specialized.sextOrTrunc(64).getSExtValue();
              unsigned valueLane =
                  M.getDataLayout().isLittleEndian()
                      ? source.writerLane
                      : writer.bytes - source.writerLane - 1;
              expression["low_bit"] =
                  static_cast<int64_t>(valueLane * 8);
            }
            alternative["value_byte_expression"] =
                std::move(expression);
          } else {
            alternative["value_byte"] = static_cast<int64_t>(
                writer.constantValueBytes[source.writerLane]);
          }
        }
        alternatives.push_back(std::move(alternative));
      }
      item[certificate.nestedValueSummary ? "last_write_cases"
                                          : "alternatives"] =
          std::move(alternatives);
      witnesses.push_back(std::move(item));
    }
    transcript["witnesses"] = std::move(witnesses);
    return transcript;
  }

  json::Object multiLatchLoopMemoryPhiRecord(
      const MultiLatchLoopMemoryPhiCertificate &certificate,
      const FunctionContext &context) const {
    if (certificate.nestedSummary)
      return nestedLoopMemoryPhiSummaryRecord(certificate, context);
    auto encodeGuardOperand = [&](const Value *value) {
      if (auto *constant = dyn_cast<ConstantInt>(value))
        return constantOperand(*constant);
      return variableOperand(context.values.lookup(value));
    };
    bool orderedWriterTransfer = certificate.hasOrderedWriterTransfer();
    json::Object transcript;
    transcript["schema"] =
        orderedWriterTransfer
            ? "symcc-loop-memoryphi-byte-lane-induction-v5"
            : "symcc-loop-memoryphi-byte-lane-induction-v4";
    transcript["base"] = static_cast<int64_t>(certificate.base);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);
    json::Object loop;
    loop["header"] = context.blocks.lookup(certificate.header);
    loop["preheader"] = context.blocks.lookup(certificate.preheader);
    loop["preheader_edge"] = context.edgeBlocks.at(
        {certificate.preheader, certificate.header});
    loop["root"] = context.blocks.lookup(certificate.root);
    loop["exit"] = context.blocks.lookup(certificate.exit);
    json::Array decisionBlocks;
    for (const ICmpInst *decision : certificate.decisions)
      decisionBlocks.push_back(
          context.blocks.lookup(decision->getParent()));
    loop["decision_blocks"] = std::move(decisionBlocks);
    json::Array latches;
    json::Array latchEdges;
    for (const MultiLatchLoopMemoryPhiTransfer &transfer :
         certificate.transfers) {
      latches.push_back(context.blocks.lookup(transfer.latch));
      latchEdges.push_back(context.edgeBlocks.at(
          {transfer.latch, certificate.header}));
    }
    loop["latches"] = std::move(latches);
    loop["latch_edges"] = std::move(latchEdges);
    transcript["loop"] = std::move(loop);

    json::Object memoryPhi;
    memoryPhi["block"] = context.blocks.lookup(certificate.header);
    memoryPhi["preheader"] = "live-on-entry";
    memoryPhi["equation"] = "header=phi(entry,T0(header),...,Tn(header))";
    json::Array incoming;
    for (unsigned index = 0; index < certificate.transfers.size(); ++index) {
      const MultiLatchLoopMemoryPhiTransfer &transfer =
          certificate.transfers[index];
      json::Object item;
      item["transfer"] = static_cast<int64_t>(index);
      item["latch"] = context.blocks.lookup(transfer.latch);
      item["kind"] =
          transfer.isWriter()
              ? (orderedWriterTransfer
                     ? "ordered-writer-memory-def-chain"
                     : "writer-memory-def")
              : "header-memory-phi-carry";
      incoming.push_back(std::move(item));
    }
    memoryPhi["incoming"] = std::move(incoming);
    transcript["memory_phi"] = std::move(memoryPhi);

    json::Object induction;
    induction["variable"] = variableOperand(
        context.values.lookup(certificate.induction));
    induction["bits"] = static_cast<int64_t>(certificate.inductionBits);
    induction["seed"] = certificate.seed;
    induction["step"] = certificate.stepValue;
    json::Array next;
    for (unsigned index = 0; index < certificate.transfers.size(); ++index) {
      json::Object item;
      item["transfer"] = static_cast<int64_t>(index);
      item["latch"] = context.blocks.lookup(
          certificate.transfers[index].latch);
      item["variable"] = variableOperand(context.values.lookup(
          certificate.transfers[index].step));
      next.push_back(std::move(item));
    }
    induction["next"] = std::move(next);
    transcript["induction"] = std::move(induction);
    json::Object guard;
    guard["variable"] = variableOperand(
        context.values.lookup(certificate.guard));
    guard["predicate"] = "ult";
    guard["bound"] = variableOperand(
        context.values.lookup(certificate.bound));
    guard["continue"] = context.blocks.lookup(certificate.root);
    guard["exit"] = context.blocks.lookup(certificate.exit);
    transcript["guard"] = std::move(guard);

    json::Array decisions;
    for (const ICmpInst *comparison : certificate.decisions) {
      auto *branch = cast<BranchInst>(comparison->getParent()->getTerminator());
      json::Object item;
      item["block"] = context.blocks.lookup(comparison->getParent());
      item["variable"] = variableOperand(
          context.values.lookup(comparison));
      item["predicate"] = *comparisonOperator(comparison->getPredicate());
      item["left"] = encodeGuardOperand(comparison->getOperand(0));
      item["right"] = encodeGuardOperand(comparison->getOperand(1));
      item["true"] = context.blocks.lookup(branch->getSuccessor(0));
      item["false"] = context.blocks.lookup(branch->getSuccessor(1));
      decisions.push_back(std::move(item));
    }
    transcript["decisions"] = std::move(decisions);

    json::Array transfers;
    for (unsigned index = 0; index < certificate.transfers.size(); ++index) {
      const MultiLatchLoopMemoryPhiTransfer &transfer =
          certificate.transfers[index];
      json::Object item;
      item["ordinal"] = static_cast<int64_t>(index);
      item["latch"] = context.blocks.lookup(transfer.latch);
      item["latch_edge"] = context.edgeBlocks.at(
          {transfer.latch, certificate.header});
      item["kind"] = transfer.isWriter() ? "writer" : "carry";
      item["next"] = variableOperand(
          context.values.lookup(transfer.step));
      json::Array guards;
      for (const MultiLatchLoopMemoryPhiGuard &pathGuard : transfer.guards) {
        json::Object path;
        path["variable"] = variableOperand(
            context.values.lookup(pathGuard.comparison));
        path["equals"] = pathGuard.expected;
        guards.push_back(std::move(path));
      }
      item["guards"] = std::move(guards);
      if (transfer.isWriter()) {
        auto encodeWriter = [&](const MultiLatchLoopMemoryPhiWriter &source) {
          json::Object writer;
          writer["block"] = context.blocks.lookup(transfer.latch);
          writer["ordinal"] = static_cast<int64_t>(source.ordinal);
          writer["bytes"] = static_cast<int64_t>(source.bytes);
          writer["minimum"] = source.minimumIndex;
          writer["maximum"] = source.maximumIndex;
          writer["scale"] = source.scale;
          writer["address_stride"] =
              static_cast<int64_t>(source.addressStride);
          json::Array addresses;
          for (uint64_t address : source.addresses)
            addresses.push_back(static_cast<int64_t>(address));
          writer["addresses"] = std::move(addresses);
          json::Array values;
          for (int64_t value : source.indexValues)
            values.push_back(value);
          writer["index_values"] = std::move(values);
          json::Array reachableAddresses;
          for (uint64_t address : source.reachableAddresses)
            reachableAddresses.push_back(static_cast<int64_t>(address));
          writer["reachable_addresses"] = std::move(reachableAddresses);
          json::Array reachableValues;
          for (int64_t value : source.reachableIndexValues)
            reachableValues.push_back(value);
          writer["reachable_index_values"] = std::move(reachableValues);
          writer["residue_origin"] =
              static_cast<int64_t>(source.reachableAddresses.front());
          json::Array residues;
          for (unsigned lane = 0; lane < source.bytes; ++lane)
            residues.push_back(static_cast<int64_t>(lane));
          writer["covered_residues"] = std::move(residues);
          return writer;
        };
        if (orderedWriterTransfer) {
          json::Array writers;
          for (const MultiLatchLoopMemoryPhiWriter &writer :
               transfer.writers)
            writers.push_back(encodeWriter(writer));
          item["writers"] = std::move(writers);
        } else {
          item["writer"] = encodeWriter(transfer.writers.front());
        }
      }
      transfers.push_back(std::move(item));
    }
    transcript["transfers"] = std::move(transfers);

    json::Object fixedPoint;
    fixedPoint["algorithm"] = "finite-monotone-byte-lane-union";
    fixedPoint["semantics"] =
        orderedWriterTransfer
            ? "mutually-exclusive-ordered-writer-transfer"
            : "mutually-exclusive-backedge-transfer";
    json::Array domain;
    json::Array rounds;
    for (size_t index = 0; index < certificate.closureDomain.size(); ++index) {
      domain.push_back(certificate.closureDomain[index]);
      json::Object round;
      round["induction_value"] = certificate.closureDomain[index];
      round["new_lanes"] =
          static_cast<int64_t>(certificate.roundNewLanes[index]);
      round["total_lanes"] =
          static_cast<int64_t>(certificate.roundTotalLanes[index]);
      rounds.push_back(std::move(round));
    }
    json::Object stableRound;
    stableRound["kind"] = "stability-check";
    stableRound["new_lanes"] = 0;
    stableRound["total_lanes"] =
        static_cast<int64_t>(certificate.finalLanes.size());
    rounds.push_back(std::move(stableRound));
    fixedPoint["domain"] = std::move(domain);
    fixedPoint["rounds"] = std::move(rounds);
    json::Array finalLanes;
    for (uint64_t lane : certificate.finalLanes)
      finalLanes.push_back(static_cast<int64_t>(lane));
    fixedPoint["final_lanes"] = std::move(finalLanes);
    fixedPoint["stable"] = true;
    transcript["fixed_point"] = std::move(fixedPoint);

    json::Object loadIndex;
    loadIndex["bits"] = static_cast<int64_t>(certificate.loadIndexBits);
    loadIndex["minimum"] = certificate.loadMinimumIndex;
    loadIndex["maximum"] = certificate.loadMaximumIndex;
    json::Array loadValues;
    for (int64_t value : certificate.loadIndexValues)
      loadValues.push_back(value);
    loadIndex["values"] = std::move(loadValues);
    transcript["load_index"] = std::move(loadIndex);
    json::Array loadAddresses;
    for (uint64_t address : certificate.loadAddresses)
      loadAddresses.push_back(static_cast<int64_t>(address));
    transcript["load_addresses"] = std::move(loadAddresses);
    json::Array witnesses;
    for (const MultiLatchLoopMemoryPhiWitness &witness :
         certificate.witnesses) {
      json::Object item;
      item["load_address"] = static_cast<int64_t>(witness.loadAddress);
      item["lane"] = static_cast<int64_t>(witness.lane);
      json::Array alternatives;
      for (const MultiLatchLoopMemoryPhiAlternative &alternative :
           witness.alternatives) {
        json::Object path;
        path["transfer"] = static_cast<int64_t>(alternative.transfer);
        if (orderedWriterTransfer)
          path["writer"] = static_cast<int64_t>(alternative.writer);
        path["writer_address"] =
            static_cast<int64_t>(alternative.writerAddress);
        path["writer_lane"] =
            static_cast<int64_t>(alternative.writerLane);
        path["induction_value"] = alternative.inductionValue;
        alternatives.push_back(std::move(path));
      }
      item["alternatives"] = std::move(alternatives);
      witnesses.push_back(std::move(item));
    }
    transcript["witnesses"] = std::move(witnesses);
    return transcript;
  }

  std::optional<std::vector<InterproceduralHeapEffectCertificate>>
  interproceduralHeapEffectInitializations(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context) {
    constexpr size_t maximumSkippedDefinitions = 64;
    if (context.aliasAnalysis == nullptr || context.memorySSA == nullptr ||
        alternatives.empty() || alternatives.size() > 256) {
      return std::nullopt;
    }
    const StaticPointer &loaded = alternatives.front().pointer;
    if (std::any_of(
            alternatives.begin(), alternatives.end(),
            [](const PointerAlternative &alternative) {
              return alternative.pointer.heapObject == nullptr ||
                     alternative.pointer.dynamicIndex != nullptr ||
                     alternative.pointer.objectOffset < 0;
            })) {
      return std::nullopt;
    }
    auto *use = dyn_cast_or_null<MemoryUse>(
        context.memorySSA->getMemoryAccess(&load));
    if (use == nullptr)
      return std::nullopt;

    auto objectBase = [](const StaticPointer &pointer) {
      return pointer.address - static_cast<uint64_t>(pointer.objectOffset);
    };
    auto sameObject = [&](const StaticPointer &pointer,
                          const StaticPointer &target) {
      return pointer.heapObject == target.heapObject &&
             pointer.stackObject == target.stackObject &&
             pointer.dynamicIndex == nullptr && pointer.objectOffset >= 0 &&
             objectBase(pointer) == objectBase(target);
    };
    std::function<std::optional<int64_t>(Value *, const Value *)>
        relativeOffset =
            [&](Value *value, const Value *root)
        -> std::optional<int64_t> {
      if (value == root)
        return 0;
      if (auto *gep = dyn_cast<GEPOperator>(value)) {
        auto base = relativeOffset(gep->getPointerOperand(), root);
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            gep->getPointerAddressSpace());
        APInt offset(pointerBits, 0, true);
        if (!base || pointerBits == 0 || pointerBits > 64 ||
            !gep->accumulateConstantOffset(M.getDataLayout(), offset) ||
            !offset.isSignedIntN(64))
          return std::nullopt;
        int64_t delta = offset.getSExtValue();
        if ((delta > 0 && *base > INT64_MAX - delta) ||
            (delta < 0 && *base < INT64_MIN - delta))
          return std::nullopt;
        return *base + delta;
      }
      Value *stripped = value->stripPointerCasts();
      return stripped == value ? std::nullopt
                               : relativeOffset(stripped, root);
    };
    std::function<const CallInst *(Value *)> originCall =
        [&](Value *value) -> const CallInst * {
          if (auto *call = dyn_cast<CallInst>(value))
            return call;
          if (auto *gep = dyn_cast<GEPOperator>(value))
            return originCall(gep->getPointerOperand());
          Value *stripped = value->stripPointerCasts();
          return stripped == value ? nullptr : originCall(stripped);
        };
    const CallInst *returnedOrigin = originCall(load.getPointerOperand());

    auto scalarStoreBytes = [&](const StoreInst &store) -> uint64_t {
      if (!store.isSimple())
        return 0;
      Type *type = store.getValueOperand()->getType();
      unsigned bits = type->isPointerTy()
                          ? M.getDataLayout().getPointerSizeInBits(
                                type->getPointerAddressSpace())
                          : integerBits(type);
      return bits == 0 ? 0 : fixedStoreBytes(M.getDataLayout(), type);
    };

    auto summarize = [&](CallInst &call)
        -> std::optional<std::vector<InterproceduralHeapEffectCertificate>> {
      Function *callee = call.getCalledFunction();
      if (callee == nullptr || callee->isDeclaration() ||
          callee->isIntrinsic() || callee->size() != 1 ||
          !context.dominators.dominates(&call, &load)) {
        return std::nullopt;
      }
      BasicBlock &block = callee->getEntryBlock();
      auto *returned = dyn_cast<ReturnInst>(block.getTerminator());
      if (returned == nullptr)
        return std::nullopt;
      std::vector<StoreInst *> stores;
      std::vector<CallBase *> calls;
      for (Instruction &instruction : block) {
        if (auto *store = dyn_cast<StoreInst>(&instruction))
          stores.push_back(store);
        if (auto *nestedCall = dyn_cast<CallBase>(&instruction))
          calls.push_back(nestedCall);
      }

      auto baseCertificate = [&](const StaticPointer &pointer) {
        InterproceduralHeapEffectCertificate certificate;
        certificate.call = &call;
        certificate.callee = callee;
        certificate.returnInstruction = returned;
        certificate.base = objectBase(pointer);
        certificate.loadAddress = pointer.address;
        certificate.loadBytes = bytes;
        return certificate;
      };

      if (returnedOrigin == &call && call.getType()->isPointerTy()) {
        CallBase *allocation = const_cast<CallBase *>(loaded.heapObject);
        Function *allocator = allocation->getCalledFunction();
        Value *returnedValue = returned->getReturnValue();
        bool isMalloc = allocator != nullptr && allocator->isDeclaration() &&
                        allocator->getName() == "malloc";
        bool isCalloc = allocator != nullptr && allocator->isDeclaration() &&
                        allocator->getName() == "calloc";
        if (allocation->getFunction() != callee ||
            std::any_of(
                alternatives.begin(), alternatives.end(),
                [&](const PointerAlternative &alternative) {
                  return alternative.pointer.heapObject != allocation;
                }) ||
            (!isMalloc && !isCalloc) || returnedValue == nullptr ||
            returnedValue->stripPointerCasts() != allocation ||
            calls.size() != 1 ||
            calls.front() != allocation) {
          return std::nullopt;
        }
        std::vector<InterproceduralHeapEffectCertificate> certificates;
        certificates.reserve(alternatives.size());
        if (isCalloc) {
          if (!stores.empty())
            return std::nullopt;
          for (const PointerAlternative &alternative : alternatives) {
            auto certificate = baseCertificate(alternative.pointer);
            certificate.kind =
                InterproceduralHeapEffectCertificate::Kind::ReturnedAllocation;
            certificate.allocation = allocation;
            certificate.zeroInitialize = true;
            certificates.push_back(std::move(certificate));
          }
          return certificates;
        }
        if (stores.size() != 1)
          return std::nullopt;
        StoreInst *store = stores.front();
        uint64_t storeBytes = scalarStoreBytes(*store);
        FunctionContext calleeContext(*callee);
        auto stored = pointerAlternatives(
            store->getPointerOperand(), calleeContext, load);
        if (storeBytes == 0 || !stored || stored->empty()) {
          return std::nullopt;
        }
        for (const PointerAlternative &alternative : alternatives) {
          const StaticPointer *matchingStore = nullptr;
          for (const PointerAlternative &candidate : *stored) {
            if (!sameObject(candidate.pointer, alternative.pointer) ||
                !pointerIntervalCovers(
                    candidate.pointer, storeBytes,
                    alternative.pointer, bytes))
              continue;
            if (matchingStore != nullptr)
              return std::nullopt;
            matchingStore = &candidate.pointer;
          }
          if (matchingStore == nullptr)
            return std::nullopt;
          auto certificate = baseCertificate(alternative.pointer);
          certificate.kind =
              InterproceduralHeapEffectCertificate::Kind::ReturnedAllocation;
          certificate.allocation = allocation;
          certificate.zeroInitialize = false;
          certificate.store = store;
          certificate.storeAddress = matchingStore->address;
          certificate.storeBytes = storeBytes;
          certificates.push_back(std::move(certificate));
        }
        return certificates;
      }

      if (alternatives.size() != 1 ||
          !callee->getReturnType()->isVoidTy() || !calls.empty() ||
          stores.size() != 1) {
        return std::nullopt;
      }
      StoreInst *store = stores.front();
      uint64_t storeBytes = scalarStoreBytes(*store);
      if (storeBytes == 0)
        return std::nullopt;
      for (Argument &parameter : callee->args()) {
        unsigned index = parameter.getArgNo();
        auto offset = relativeOffset(
            store->getPointerOperand(), &parameter);
        if (!parameter.getType()->isPointerTy() || index >= call.arg_size() ||
            !offset) {
          continue;
        }
        FunctionContext callerContext(*call.getFunction());
        auto actual = pointerAlternatives(
            call.getArgOperand(index), callerContext, load);
        if (!actual || actual->size() != 1 ||
            !sameObject(actual->front().pointer, loaded) ||
            actual->front().pointer.objectOffset != 0 ||
            actual->front().pointer.interprocedural) {
          continue;
        }
        StaticPointer stored = actual->front().pointer;
        __int128 storedAddress =
            static_cast<__int128>(stored.address) + *offset;
        __int128 storedOffset =
            static_cast<__int128>(stored.objectOffset) + *offset;
        if (storedAddress <= 0 || storedAddress > UINT64_MAX ||
            storedOffset < 0 ||
            storedOffset > static_cast<__int128>(stored.objectSize)) {
          continue;
        }
        stored.address = static_cast<uint64_t>(storedAddress);
        stored.objectOffset = static_cast<int64_t>(storedOffset);
        if (!pointerIntervalCovers(stored, storeBytes, loaded, bytes))
          continue;
        auto certificate = baseCertificate(loaded);
        certificate.kind =
            InterproceduralHeapEffectCertificate::Kind::ArgumentInitializer;
        certificate.parameterIndex = index;
        certificate.store = store;
        certificate.storeAddress = stored.address;
        certificate.storeBytes = storeBytes;
        return std::vector<InterproceduralHeapEffectCertificate>{
            std::move(certificate)};
      }
      return std::nullopt;
    };

    auto storeOrdinal = [](const StoreInst &store) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *store.getParent()) {
        auto *candidate = dyn_cast<StoreInst>(&instruction);
        if (candidate == nullptr)
          continue;
        if (candidate == &store)
          break;
        ++ordinal;
      }
      return ordinal;
    };
    auto memoryDefinitionOrdinal = [](const Instruction &target) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *target.getParent()) {
        if (!instruction.mayWriteToMemory())
          continue;
        if (&instruction == &target)
          break;
        ++ordinal;
      }
      return ordinal;
    };

    MemoryLocation loadLocation = MemoryLocation::get(&load);
    std::vector<MemorySSAHeapInitializationSkip> skipped;
    MemoryAccess *access = use->getDefiningAccess();
    while (access != nullptr &&
           access != context.memorySSA->getLiveOnEntryDef()) {
      if (skipped.size() > maximumSkippedDefinitions)
        return std::nullopt;
      auto *definition = dyn_cast<MemoryDef>(access);
      if (definition == nullptr)
        return std::nullopt;
      Instruction *instruction = definition->getMemoryInst();
      if (instruction == nullptr)
        return std::nullopt;
      if (auto *call = dyn_cast<CallInst>(instruction)) {
        auto certificates = summarize(*call);
        if (certificates) {
          for (InterproceduralHeapEffectCertificate &certificate :
               *certificates)
            certificate.skipped = skipped;
          return certificates;
        }
      }
      if (auto *store = dyn_cast<StoreInst>(instruction)) {
        if (!store->isSimple())
          return std::nullopt;
        uint64_t storeBytes = scalarStoreBytes(*store);
        auto stored = pointerAlternatives(
            store->getPointerOperand(), context, load);
        bool allocationNoAlias =
            storeBytes != 0 && stored && !stored->empty() &&
            std::all_of(
                stored->begin(), stored->end(),
                [&](const PointerAlternative &candidate) {
                  if (candidate.pointer.dynamicIndex != nullptr ||
                      candidate.pointer.objectOffset < 0)
                    return false;
                  uint64_t storeOffset = static_cast<uint64_t>(
                      candidate.pointer.objectOffset);
                  __int128 storeEnd =
                      static_cast<__int128>(storeOffset) + storeBytes;
                  return std::all_of(
                      alternatives.begin(), alternatives.end(),
                      [&](const PointerAlternative &loadedAlternative) {
                        if (!sameObject(
                                candidate.pointer,
                                loadedAlternative.pointer))
                          return true;
                        uint64_t loadOffset = static_cast<uint64_t>(
                            loadedAlternative.pointer.objectOffset);
                        __int128 loadEnd =
                            static_cast<__int128>(loadOffset) + bytes;
                        return storeEnd <= loadOffset ||
                               loadEnd <= storeOffset;
                      });
                });
        bool aaNoAlias =
            context.aliasAnalysis->alias(
                loadLocation, MemoryLocation::get(store)) ==
            AliasResult::NoAlias;
        if (!allocationNoAlias && !aaNoAlias)
          return std::nullopt;
        skipped.push_back({
            store,
            allocationNoAlias ? "allocation-noalias" : "aa-noalias",
            storeOrdinal(*store)});
      } else {
        auto *call = dyn_cast<CallBase>(instruction);
        Function *callee =
            call == nullptr ? nullptr : call->getCalledFunction();
        if (call == nullptr || !isa<CallInst>(call) || callee == nullptr ||
            callee->isDeclaration() || callee->isIntrinsic() ||
            isModSet(context.aliasAnalysis->getModRefInfo(
                instruction, loadLocation))) {
          return std::nullopt;
        }
        skipped.push_back({
            instruction, "aa-no-modref",
            memoryDefinitionOrdinal(*instruction)});
      }
      access = definition->getDefiningAccess();
    }
    return std::nullopt;
  }

  json::Object interproceduralHeapEffectRecord(
      const InterproceduralHeapEffectCertificate &certificate,
      const FunctionContext &callerContext) const {
    auto callOrdinal = [](const CallInst &target) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *target.getParent()) {
        auto *call = dyn_cast<CallInst>(&instruction);
        if (call == nullptr || call->getCalledFunction() == nullptr ||
            call->getCalledFunction()->isDeclaration() ||
            call->getCalledFunction()->isIntrinsic())
          continue;
        if (call == &target)
          break;
        ++ordinal;
      }
      return ordinal;
    };
    auto storeOrdinal = [](const StoreInst &target) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *target.getParent()) {
        if (auto *store = dyn_cast<StoreInst>(&instruction)) {
          if (store == &target)
            break;
          ++ordinal;
        }
      }
      return ordinal;
    };
    auto allocationOrdinal = [](const CallBase &target) {
      unsigned ordinal = 0;
      for (const Instruction &instruction : *target.getParent()) {
        auto *call = dyn_cast<CallBase>(&instruction);
        Function *callee = call == nullptr ? nullptr : call->getCalledFunction();
        if (callee == nullptr || !callee->isDeclaration() ||
            (callee->getName() != "malloc" &&
             callee->getName() != "calloc"))
          continue;
        if (call == &target)
          break;
        ++ordinal;
      }
      return ordinal;
    };

    FunctionContext calleeContext(
        *const_cast<Function *>(certificate.callee));
    json::Object transcript;
    transcript["schema"] =
        "symcc-interprocedural-heap-effect-summary-v1";
    transcript["kind"] =
        certificate.kind ==
                InterproceduralHeapEffectCertificate::Kind::ReturnedAllocation
            ? "returned-allocation"
            : "argument-initializer";
    transcript["callee"] = certificate.callee->getName();
    transcript["base"] = static_cast<int64_t>(certificate.base);
    transcript["load_address"] =
        static_cast<int64_t>(certificate.loadAddress);
    transcript["load_bytes"] =
        static_cast<int64_t>(certificate.loadBytes);
    json::Object call;
    call["block"] = callerContext.blocks.lookup(
        certificate.call->getParent());
    call["ordinal"] = static_cast<int64_t>(
        callOrdinal(*certificate.call));
    transcript["call"] = std::move(call);
    if (certificate.kind ==
        InterproceduralHeapEffectCertificate::Kind::ArgumentInitializer) {
      transcript["parameter"] = static_cast<int64_t>(
          certificate.parameterIndex);
    } else {
      json::Object allocation;
      allocation["block"] = calleeContext.blocks.lookup(
          certificate.allocation->getParent());
      allocation["ordinal"] = static_cast<int64_t>(
          allocationOrdinal(*certificate.allocation));
      allocation["address"] = static_cast<int64_t>(certificate.base);
      allocation["allocator"] =
          certificate.allocation->getCalledFunction()->getName();
      allocation["zero_initialize"] = certificate.zeroInitialize;
      transcript["allocation"] = std::move(allocation);
    }
    if (certificate.store != nullptr) {
      json::Object store;
      store["block"] = calleeContext.blocks.lookup(
          certificate.store->getParent());
      store["ordinal"] = static_cast<int64_t>(
          storeOrdinal(*certificate.store));
      store["address"] = static_cast<int64_t>(
          certificate.storeAddress);
      store["bytes"] = static_cast<int64_t>(certificate.storeBytes);
      transcript["store"] = std::move(store);
    } else
      transcript["store"] = nullptr;
    json::Object returned;
    returned["block"] = calleeContext.blocks.lookup(
        certificate.returnInstruction->getParent());
    returned["ordinal"] = 0;
    transcript["return"] = std::move(returned);
    json::Array skipped;
    for (const MemorySSAHeapInitializationSkip &definition :
         certificate.skipped) {
      json::Object item;
      item["block"] = callerContext.blocks.lookup(
          definition.instruction->getParent());
      item["ordinal"] = static_cast<int64_t>(definition.ordinal);
      item["kind"] = isa<StoreInst>(definition.instruction)
                           ? "store"
                           : "call";
      item["opcode"] = definition.instruction->getOpcodeName();
      item["proof"] = definition.proof;
      skipped.push_back(std::move(item));
    }
    transcript["skipped_defs"] = std::move(skipped);
    return transcript;
  }

  bool hasDominatingUnionStore(
      LoadInst &load, ArrayRef<PointerAlternative> alternatives,
      uint64_t bytes, FunctionContext &context,
      std::vector<uint64_t> *collectiveInitializationBases = nullptr,
      std::optional<GuardedHeapInitializationCertificate>
          *guardedInitializationCertificate = nullptr,
      std::optional<MemorySSAHeapInitializationCertificate>
          *memorySSAInitializationCertificate = nullptr,
      std::optional<DynamicByteLaneCoverCertificate>
          *dynamicByteLaneCoverCertificate = nullptr,
      std::optional<LoopMemoryPhiByteLaneCertificate>
          *loopMemoryPhiByteLaneCertificate = nullptr,
      std::optional<MultiLatchLoopMemoryPhiCertificate>
          *multiLatchLoopMemoryPhiCertificate = nullptr,
      std::optional<std::vector<InterproceduralHeapEffectCertificate>>
          *interproceduralInitializationCertificate = nullptr) {
    if (collectiveInitializationBases != nullptr)
      collectiveInitializationBases->clear();
    if (guardedInitializationCertificate != nullptr)
      guardedInitializationCertificate->reset();
    if (memorySSAInitializationCertificate != nullptr)
      memorySSAInitializationCertificate->reset();
    if (dynamicByteLaneCoverCertificate != nullptr)
      dynamicByteLaneCoverCertificate->reset();
    if (loopMemoryPhiByteLaneCertificate != nullptr)
      loopMemoryPhiByteLaneCertificate->reset();
    if (multiLatchLoopMemoryPhiCertificate != nullptr)
      multiLatchLoopMemoryPhiCertificate->reset();
    if (interproceduralInitializationCertificate != nullptr)
      interproceduralInitializationCertificate->reset();
    auto zeroInitialized = [](const StaticPointer &pointer) {
      if (pointer.heapObject == nullptr)
        return false;
      Function *callee = pointer.heapObject->getCalledFunction();
      return callee != nullptr && callee->isDeclaration() &&
             callee->getName() == "calloc";
    };
    bool needsLocalInitialization = false;
    bool needsInterproceduralInitialization = false;
    for (const PointerAlternative &alternative : alternatives) {
      const StaticPointer &pointer = alternative.pointer;
      bool tracked =
          pointer.stackObject != nullptr || pointer.heapObject != nullptr;
      needsLocalInitialization |=
          tracked && !pointer.interprocedural &&
          !zeroInitialized(pointer);
      needsInterproceduralInitialization |=
          tracked && pointer.interprocedural;
      if (pointer.heapObject != nullptr && !pointer.interprocedural &&
          !context.dominators.dominates(pointer.heapObject, &load)) {
        reject(load, "heap pointer union load is not dominated by allocation");
        return false;
      }
    }
    if (!needsLocalInitialization && !needsInterproceduralInitialization)
      return true;
    bool collectiveEligible =
        alternatives.size() >= 2 &&
        std::all_of(
            alternatives.begin(), alternatives.end(),
            [&](const PointerAlternative &alternative) {
              const StaticPointer &pointer = alternative.pointer;
              return pointer.heapObject != nullptr &&
                     !pointer.interprocedural &&
                     !zeroInitialized(pointer) &&
                     pointer.dynamicIndex == nullptr &&
                     pointer.objectOffset >= 0;
            });
    std::vector<bool> collectivelyCovered(
        alternatives.size(), false);
    std::set<const StoreInst *> contributingStores;
    for (Instruction &candidate : instructions(load.getFunction())) {
      auto *store = dyn_cast<StoreInst>(&candidate);
      if (store == nullptr || store->isVolatile() || store->isAtomic() ||
          !context.dominators.dominates(store, &load))
        continue;
      unsigned bits =
          store->getValueOperand()->getType()->isPointerTy()
              ? M.getDataLayout().getPointerSizeInBits(
                    store->getValueOperand()->getType()
                        ->getPointerAddressSpace())
              : integerBits(store->getValueOperand()->getType());
      uint64_t storeBytes =
          bits == 0 ? 0
                      : fixedStoreBytes(
                          M.getDataLayout(),
                          store->getValueOperand()->getType());
      if (storeBytes < bytes)
        continue;
      auto storeAlternatives = pointerAlternatives(
          store->getPointerOperand(), context, *store);
      if (!storeAlternatives || storeAlternatives->empty())
        continue;
      bool coversAll = std::all_of(
          alternatives.begin(), alternatives.end(),
          [&](const PointerAlternative &loaded) {
            if (loaded.pointer.interprocedural ||
                zeroInitialized(loaded.pointer) ||
                (loaded.pointer.stackObject == nullptr &&
                 loaded.pointer.heapObject == nullptr))
              return true;
            return std::any_of(
                storeAlternatives->begin(), storeAlternatives->end(),
                [&](const PointerAlternative &stored) {
                  return pointerIntervalCovers(
                      stored.pointer, storeBytes, loaded.pointer, bytes);
                });
          });
      if (coversAll)
        return true;
      if (
          !collectiveEligible || storeAlternatives->size() != 1)
        continue;
      const StaticPointer &stored =
          storeAlternatives->front().pointer;
      if (
          stored.heapObject == nullptr || stored.interprocedural ||
          stored.dynamicIndex != nullptr || stored.objectOffset < 0)
        continue;
      bool contributed = false;
      for (size_t index = 0; index < alternatives.size(); ++index) {
        if (
            !collectivelyCovered[index] &&
            pointerIntervalCovers(
                stored, storeBytes, alternatives[index].pointer, bytes)) {
          collectivelyCovered[index] = true;
          contributed = true;
        }
      }
      if (contributed)
        contributingStores.insert(store);
    }
    if (
        collectiveEligible && contributingStores.size() >= 2 &&
        std::all_of(
            collectivelyCovered.begin(), collectivelyCovered.end(),
            [](bool covered) { return covered; })) {
      std::set<uint64_t> bases;
      for (const PointerAlternative &alternative : alternatives) {
        const StaticPointer &pointer = alternative.pointer;
        bases.insert(
            pointer.address -
            static_cast<uint64_t>(pointer.objectOffset));
      }
      if (bases.size() >= 2) {
        if (collectiveInitializationBases != nullptr)
          collectiveInitializationBases->assign(
              bases.begin(), bases.end());
        return true;
      }
    }
    auto guarded = guardedHeapUnionInitialization(
        load, alternatives, bytes, context);
    if (guarded) {
      std::set<uint64_t> bases;
      for (const GuardedHeapInitializationPath &path : guarded->paths)
        bases.insert(path.base);
      if (collectiveInitializationBases != nullptr && bases.size() >= 2)
        collectiveInitializationBases->assign(
            bases.begin(), bases.end());
      if (guardedInitializationCertificate != nullptr)
        *guardedInitializationCertificate = std::move(guarded);
      return true;
    }
    auto memorySSA = memorySSAHeapInitialization(
        load, alternatives, bytes, context);
    if (memorySSA) {
      std::set<uint64_t> bases;
      for (const PointerAlternative &alternative : alternatives)
        bases.insert(
            alternative.pointer.address -
            static_cast<uint64_t>(alternative.pointer.objectOffset));
      if (collectiveInitializationBases != nullptr && bases.size() >= 2)
        collectiveInitializationBases->assign(
            bases.begin(), bases.end());
      if (memorySSAInitializationCertificate != nullptr)
        *memorySSAInitializationCertificate = std::move(memorySSA);
      return true;
    }
    auto dynamicCover = dynamicByteLaneCoverInitialization(
        load, alternatives, bytes, context);
    if (dynamicCover) {
      if (dynamicByteLaneCoverCertificate != nullptr)
        *dynamicByteLaneCoverCertificate = std::move(dynamicCover);
      return true;
    }
    auto loopMemoryPhi = loopMemoryPhiByteLaneInitialization(
        load, alternatives, bytes, context);
    if (loopMemoryPhi) {
      if (loopMemoryPhiByteLaneCertificate != nullptr)
        *loopMemoryPhiByteLaneCertificate = std::move(loopMemoryPhi);
      return true;
    }
    auto multiLatchLoopMemoryPhi = multiLatchLoopMemoryPhiInitialization(
        load, alternatives, bytes, context);
    if (!multiLatchLoopMemoryPhi)
      multiLatchLoopMemoryPhi = nestedLoopMemoryPhiSummaryInitialization(
          load, alternatives, bytes, context);
    if (multiLatchLoopMemoryPhi) {
      if (multiLatchLoopMemoryPhiCertificate != nullptr)
        *multiLatchLoopMemoryPhiCertificate =
            std::move(multiLatchLoopMemoryPhi);
      return true;
    }
    auto interprocedural = interproceduralHeapEffectInitializations(
        load, alternatives, bytes, context);
    if (interprocedural) {
      if (interproceduralInitializationCertificate != nullptr)
        *interproceduralInitializationCertificate =
            std::move(interprocedural);
      return true;
    }
    bool heapOnly = std::all_of(
        alternatives.begin(), alternatives.end(),
        [](const PointerAlternative &alternative) {
          return alternative.pointer.interprocedural ||
                 alternative.pointer.heapObject != nullptr;
        });
    reject(
        load,
        heapOnly
            ? "heap load lacks a dominating full-width initializing store "
              "for its pointer pool"
            : "stack/heap pointer union load lacks a dominating full-width "
              "initializing store");
    return false;
  }

  bool hasDominatingObjectStore(LoadInst &load,
                                const StaticPointer &pointer,
                                uint64_t bytes,
                                FunctionContext &context,
                                std::optional<MemorySSAHeapInitializationCertificate>
                                    *memorySSAInitializationCertificate = nullptr,
                                std::optional<DynamicByteLaneCoverCertificate>
                                    *dynamicByteLaneCoverCertificate = nullptr,
                                std::optional<LoopMemoryPhiByteLaneCertificate>
                                    *loopMemoryPhiByteLaneCertificate = nullptr,
                                std::optional<MultiLatchLoopMemoryPhiCertificate>
                                    *multiLatchLoopMemoryPhiCertificate = nullptr,
                                std::optional<std::vector<InterproceduralHeapEffectCertificate>>
                                    *interproceduralInitializationCertificate = nullptr) {
    if (memorySSAInitializationCertificate != nullptr)
      memorySSAInitializationCertificate->reset();
    if (dynamicByteLaneCoverCertificate != nullptr)
      dynamicByteLaneCoverCertificate->reset();
    if (loopMemoryPhiByteLaneCertificate != nullptr)
      loopMemoryPhiByteLaneCertificate->reset();
    if (multiLatchLoopMemoryPhiCertificate != nullptr)
      multiLatchLoopMemoryPhiCertificate->reset();
    if (interproceduralInitializationCertificate != nullptr)
      interproceduralInitializationCertificate->reset();
    if (pointer.stackObject == nullptr && pointer.heapObject == nullptr)
      return true;
    if (pointer.heapObject != nullptr && !pointer.interprocedural &&
        !context.dominators.dominates(pointer.heapObject, &load)) {
      reject(load, "heap load is not dominated by its allocation");
      return false;
    }
    if (pointer.heapObject != nullptr && !pointer.interprocedural) {
      Function *callee = pointer.heapObject->getCalledFunction();
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "calloc")
        return true;
    }
    bool dynamic = pointer.dynamicIndex != nullptr;
    uint64_t loadOffset =
        dynamic ? 0 : static_cast<uint64_t>(pointer.objectOffset);
    for (Instruction &candidate : instructions(load.getFunction())) {
      auto *store = dyn_cast<StoreInst>(&candidate);
      if (store == nullptr || store->isVolatile() || store->isAtomic())
        continue;
      unsigned storeBits =
          store->getValueOperand()->getType()->isPointerTy()
              ? M.getDataLayout().getPointerSizeInBits(
                    store->getValueOperand()->getType()
                        ->getPointerAddressSpace())
              : integerBits(store->getValueOperand()->getType());
      if (storeBits == 0)
        continue;
      uint64_t storeBytes = fixedStoreBytes(
          M.getDataLayout(), store->getValueOperand()->getType());
      auto storePointer =
          staticPointer(store->getPointerOperand(), *store);
      if (!storePointer ||
          storePointer->stackObject != pointer.stackObject ||
          storePointer->heapObject != pointer.heapObject)
        continue;
      if (dynamic) {
        if (
            storePointer->dynamicIndex == pointer.dynamicIndex &&
            storePointer->dynamicScale == pointer.dynamicScale &&
            storePointer->dynamicIndexBits == pointer.dynamicIndexBits &&
            storePointer->objectOffset == pointer.objectOffset &&
            storeBytes >= bytes &&
            context.dominators.dominates(store, &load))
          return true;
        continue;
      }
      if (storePointer->dynamicIndex != nullptr ||
          storePointer->objectOffset < 0)
        continue;
      uint64_t storeOffset =
          static_cast<uint64_t>(storePointer->objectOffset);
      if (storeOffset <= loadOffset &&
          bytes <= storeBytes &&
          loadOffset - storeOffset <= storeBytes - bytes &&
          context.dominators.dominates(store, &load))
        return true;
    }
    PointerAlternative alternative;
    alternative.pointer = pointer;
    auto memorySSA = memorySSAHeapInitialization(
        load, ArrayRef<PointerAlternative>(&alternative, 1), bytes,
        context);
    if (memorySSA) {
      if (memorySSAInitializationCertificate != nullptr)
        *memorySSAInitializationCertificate = std::move(memorySSA);
      return true;
    }
    auto dynamicCover = dynamicByteLaneCoverInitialization(
        load, ArrayRef<PointerAlternative>(&alternative, 1), bytes,
        context);
    if (dynamicCover) {
      if (dynamicByteLaneCoverCertificate != nullptr)
        *dynamicByteLaneCoverCertificate = std::move(dynamicCover);
      return true;
    }
    auto loopMemoryPhi = loopMemoryPhiByteLaneInitialization(
        load, ArrayRef<PointerAlternative>(&alternative, 1), bytes,
        context);
    if (loopMemoryPhi) {
      if (loopMemoryPhiByteLaneCertificate != nullptr)
        *loopMemoryPhiByteLaneCertificate = std::move(loopMemoryPhi);
      return true;
    }
    auto multiLatchLoopMemoryPhi = multiLatchLoopMemoryPhiInitialization(
        load, ArrayRef<PointerAlternative>(&alternative, 1), bytes,
        context);
    if (!multiLatchLoopMemoryPhi)
      multiLatchLoopMemoryPhi = nestedLoopMemoryPhiSummaryInitialization(
          load, ArrayRef<PointerAlternative>(&alternative, 1), bytes,
          context);
    if (multiLatchLoopMemoryPhi) {
      if (multiLatchLoopMemoryPhiCertificate != nullptr)
        *multiLatchLoopMemoryPhiCertificate =
            std::move(multiLatchLoopMemoryPhi);
      return true;
    }
    PointerAlternative interproceduralAlternative;
    interproceduralAlternative.pointer = pointer;
    auto interprocedural = interproceduralHeapEffectInitializations(
        load,
        ArrayRef<PointerAlternative>(&interproceduralAlternative, 1),
        bytes, context);
    if (interprocedural) {
      if (interproceduralInitializationCertificate != nullptr)
        *interproceduralInitializationCertificate =
            std::move(interprocedural);
      return true;
    }
    reject(
        load,
        pointer.heapObject == nullptr
            ? "stack load lacks a dominating full-width initializing store"
            : "heap load lacks a dominating full-width initializing store");
    return false;
  }

  std::optional<json::Object> operand(Value *value, FunctionContext &context,
                                      const Instruction &user) {
    if (auto *constant = dyn_cast<ConstantInt>(value))
      return constantOperand(*constant);
    if (value->getType()->isPointerTy()) {
      auto pointer = staticPointer(value, user);
      if (pointer && pointer->dynamicIndex == nullptr)
        return integerConstant(
            static_cast<int64_t>(pointer->address),
            M.getDataLayout().getPointerSizeInBits());
      if (pointer)
        reject(user,
               "symbolic pointer value is only supported by memory access");
      return std::nullopt;
    }
    auto found = context.values.find(value);
    if (found != context.values.end())
      return variableOperand(found->second);
    reject(user, "operand is not a supported integer SSA value");
    return std::nullopt;
  }

  static std::optional<StringRef> binaryOperator(unsigned opcode) {
    switch (opcode) {
    case Instruction::Add:
      return "add";
    case Instruction::Sub:
      return "sub";
    case Instruction::Mul:
      return "mul";
    case Instruction::UDiv:
      return "udiv";
    case Instruction::SDiv:
      return "sdiv";
    case Instruction::URem:
      return "urem";
    case Instruction::SRem:
      return "srem";
    case Instruction::Shl:
      return "shl";
    case Instruction::LShr:
      return "lshr";
    case Instruction::AShr:
      return "ashr";
    case Instruction::And:
      return "and";
    case Instruction::Or:
      return "or";
    case Instruction::Xor:
      return "xor";
    default:
      return std::nullopt;
    }
  }

  static std::optional<StringRef> comparisonOperator(
      CmpInst::Predicate predicate) {
    switch (predicate) {
    case CmpInst::ICMP_EQ:
      return "eq";
    case CmpInst::ICMP_NE:
      return "ne";
    case CmpInst::ICMP_UGT:
      return "ugt";
    case CmpInst::ICMP_UGE:
      return "uge";
    case CmpInst::ICMP_ULT:
      return "ult";
    case CmpInst::ICMP_ULE:
      return "ule";
    case CmpInst::ICMP_SGT:
      return "sgt";
    case CmpInst::ICMP_SGE:
      return "sge";
    case CmpInst::ICMP_SLT:
      return "slt";
    case CmpInst::ICMP_SLE:
      return "sle";
    default:
      return std::nullopt;
    }
  }

  void appendEntryInputs(Function &function, FunctionContext &context,
                         json::Array &instructions) {
    if (inputBufferAbi) {
      json::Object pointer;
      pointer["op"] = "const";
      pointer["dst"] = context.values.lookup(inputPointerArgument);
      pointer["value"] =
          static_cast<int64_t>(inputMemoryObject.address);
      pointer["bits"] = static_cast<int64_t>(
          M.getDataLayout().getPointerSizeInBits());
      instructions.push_back(std::move(pointer));

      json::Object size;
      size["op"] = "input_size";
      size["dst"] = context.values.lookup(inputSizeArgument);
      size["bits"] =
          static_cast<int64_t>(integerBits(inputSizeArgument->getType()));
      instructions.push_back(std::move(size));
      return;
    }
    for (Argument &argument : function.args()) {
      unsigned bits = integerBits(argument.getType());
      if (bits == 0) {
        errors.push_back(function.getName().str() +
                         ": entry arguments must be integers up to 64 bits");
        continue;
      }
      unsigned bytes = (bits + 7) / 8;
      std::string accumulated;
      for (unsigned byte = 0; byte < bytes; ++byte) {
        std::string raw = context.temporaryName("input_raw_");
        json::Object input;
        input["op"] = "input";
        input["dst"] = raw;
        input["offset"] = static_cast<int64_t>(inputSize++);
        instructions.push_back(std::move(input));

        std::string extended = context.temporaryName("input_ext_");
        json::Object extension;
        extension["op"] = "unary";
        extension["operator"] = bits == 8 ? "identity" : "zext";
        extension["dst"] = extended;
        extension["value"] = variableOperand(raw);
        extension["bits"] = static_cast<int64_t>(bits);
        instructions.push_back(std::move(extension));

        std::string part = extended;
        if (byte != 0) {
          part = context.temporaryName("input_shift_");
          json::Object shift;
          shift["op"] = "binary";
          shift["operator"] = "shl";
          shift["dst"] = part;
          shift["left"] = variableOperand(extended);
          shift["right"] = integerConstant(byte * 8, bits);
          shift["bits"] = static_cast<int64_t>(bits);
          instructions.push_back(std::move(shift));
        }
        if (accumulated.empty()) {
          accumulated = part;
        } else {
          std::string combined = context.temporaryName("input_or_");
          json::Object bitOr;
          bitOr["op"] = "binary";
          bitOr["operator"] = "or";
          bitOr["dst"] = combined;
          bitOr["left"] = variableOperand(accumulated);
          bitOr["right"] = variableOperand(part);
          bitOr["bits"] = static_cast<int64_t>(bits);
          instructions.push_back(std::move(bitOr));
          accumulated = combined;
        }
      }
      json::Object bind;
      bind["op"] = "unary";
      bind["operator"] = bits < 8 ? "trunc" : "identity";
      bind["dst"] = context.values.lookup(&argument);
      bind["value"] = variableOperand(accumulated);
      bind["bits"] = static_cast<int64_t>(bits);
      instructions.push_back(std::move(bind));
    }
  }

  std::string appendBinaryTemporary(
      StringRef prefix, StringRef operation, json::Object left,
      json::Object right, unsigned bits, FunctionContext &context,
      json::Array &output) {
    std::string destination = context.temporaryName(prefix);
    json::Object lowered;
    lowered["op"] = "binary";
    lowered["operator"] = operation;
    lowered["dst"] = destination;
    lowered["left"] = std::move(left);
    lowered["right"] = std::move(right);
    lowered["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(lowered));
    return destination;
  }

  std::string appendUnaryTemporary(
      StringRef prefix, StringRef operation, json::Object value,
      unsigned bits, FunctionContext &context, json::Array &output) {
    std::string destination = context.temporaryName(prefix);
    json::Object lowered;
    lowered["op"] = "unary";
    lowered["operator"] = operation;
    lowered["dst"] = destination;
    lowered["value"] = std::move(value);
    lowered["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(lowered));
    return destination;
  }

  std::string appendSelectTemporary(
      StringRef prefix, json::Object condition, json::Object whenTrue,
      json::Object whenFalse, unsigned bits, FunctionContext &context,
      json::Array &output) {
    std::string destination = context.temporaryName(prefix);
    json::Object lowered;
    lowered["op"] = "select";
    lowered["dst"] = destination;
    lowered["condition"] = std::move(condition);
    lowered["true"] = std::move(whenTrue);
    lowered["false"] = std::move(whenFalse);
    lowered["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(lowered));
    return destination;
  }

  void appendAssumption(StringRef condition, json::Array &output) {
    json::Object assume;
    assume["op"] = "assume";
    assume["condition"] = variableOperand(condition);
    output.push_back(std::move(assume));
    usesUbGuards = true;
  }

  bool lowerByteSwap(
      Value *value, StringRef destination, unsigned bits,
      FunctionContext &context, const Instruction &user,
      json::Array &output) {
    if (
        value == nullptr || destination.empty() ||
        !value->getType()->isIntegerTy(bits) ||
        (bits != 16 && bits != 32 && bits != 64)) {
      reject(user, "byte-swap summary has an unsupported signature");
      return false;
    }
    auto loweredValue = operand(value, context, user);
    if (!loweredValue)
      return false;
    std::string source = appendUnaryTemporary(
        "scalar_bswap_source_", "identity",
        std::move(*loweredValue), bits, context, output);
    std::string accumulated;
    for (unsigned byte = 0; byte < bits / 8; ++byte) {
      uint64_t mask = UINT64_C(0xff) << (byte * 8);
      std::string selected = appendBinaryTemporary(
          "scalar_bswap_mask_", "and", variableOperand(source),
          integerConstant(static_cast<int64_t>(mask), bits),
          bits, context, output);
      unsigned destinationByte = bits / 8 - byte - 1;
      unsigned distance =
          static_cast<unsigned>(
              std::abs(
                  static_cast<int>(destinationByte) -
                  static_cast<int>(byte))) *
          8;
      std::string shifted = selected;
      if (distance != 0)
        shifted = appendBinaryTemporary(
            "scalar_bswap_shift_",
            destinationByte > byte ? "shl" : "lshr",
            variableOperand(selected),
            integerConstant(static_cast<int64_t>(distance), bits),
            bits, context, output);
      if (accumulated.empty()) {
        accumulated = std::move(shifted);
      } else {
        accumulated = appendBinaryTemporary(
            "scalar_bswap_or_", "or",
            variableOperand(accumulated),
            variableOperand(shifted),
            bits, context, output);
      }
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination.str();
    bind["value"] = variableOperand(accumulated);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    return true;
  }

  bool lowerBitCountIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic())
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool population = intrinsic == Intrinsic::ctpop;
    bool leading = intrinsic == Intrinsic::ctlz;
    bool trailing = intrinsic == Intrinsic::cttz;
    if (!population && !leading && !trailing)
      return false;
    unsigned bits = integerBits(call.getType());
    bool validArity =
        population ? call.arg_size() == 1 : call.arg_size() == 2;
    bool validSignature =
        validArity && bits != 0 &&
        call.getArgOperand(0)->getType() == call.getType();
    auto *zeroPoison =
        !population && validArity
            ? dyn_cast<ConstantInt>(call.getArgOperand(1))
            : nullptr;
    if (
        !validArity ||
        (!population &&
         (zeroPoison == nullptr ||
          !zeroPoison->getType()->isIntegerTy(1))) ||
        !validSignature) {
      reject(user, "bit-count intrinsic has an unsupported signature");
      return true;
    }
    std::string destination = context.values.lookup(&call);
    auto value = operand(call.getArgOperand(0), context, user);
    if (destination.empty() || !value) {
      reject(user, "bit-count intrinsic result has no bounded binding");
      return true;
    }
    std::string source = appendUnaryTemporary(
        "bitcount_source_", "identity", std::move(*value),
        bits, context, output);
    if (zeroPoison != nullptr && !zeroPoison->isZero()) {
      std::string nonzero = appendBinaryTemporary(
          "bitcount_nonzero_", "ne", variableOperand(source),
          integerConstant(0, bits), 1, context, output);
      appendAssumption(nonzero, output);
    }

    std::string accumulated;
    std::string active;
    for (unsigned step = 0; step < bits; ++step) {
      unsigned bitIndex =
          leading ? bits - step - 1 : step;
      std::string shifted = source;
      if (bitIndex != 0)
        shifted = appendBinaryTemporary(
            "bitcount_shift_", "lshr", variableOperand(source),
            integerConstant(bitIndex, bits), bits, context, output);
      std::string selected = appendBinaryTemporary(
          "bitcount_bit_", "and", variableOperand(shifted),
          integerConstant(1, bits), bits, context, output);
      std::string contribution = selected;
      if (!population) {
        std::string zero = appendBinaryTemporary(
            "bitcount_zero_", "eq", variableOperand(selected),
            integerConstant(0, bits), 1, context, output);
        active = active.empty()
                     ? std::move(zero)
                     : appendBinaryTemporary(
                           "bitcount_prefix_", "and",
                           variableOperand(active), variableOperand(zero),
                           1, context, output);
        contribution = appendUnaryTemporary(
            "bitcount_extend_", bits == 1 ? "identity" : "zext",
            variableOperand(active), bits, context, output);
      }
      accumulated =
          accumulated.empty()
              ? std::move(contribution)
              : appendBinaryTemporary(
                    "bitcount_sum_", "add",
                    variableOperand(accumulated),
                    variableOperand(contribution),
                    bits, context, output);
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination;
    bind["value"] = variableOperand(accumulated);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    usesBitCountIntrinsics = true;
    return true;
  }

  bool lowerBitPermutationIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic())
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool reverse = intrinsic == Intrinsic::bitreverse;
    bool funnelLeft = intrinsic == Intrinsic::fshl;
    bool funnelRight = intrinsic == Intrinsic::fshr;
    if (!reverse && !funnelLeft && !funnelRight)
      return false;
    unsigned bits = integerBits(call.getType());
    unsigned expectedArguments = reverse ? 1 : 3;
    bool valid =
        bits != 0 && call.arg_size() == expectedArguments;
    for (unsigned index = 0;
         valid && index < call.arg_size(); ++index)
      valid = call.getArgOperand(index)->getType() == call.getType();
    std::string destination = context.values.lookup(&call);
    if (!valid || destination.empty()) {
      reject(user, "bit-permutation intrinsic has an unsupported signature");
      return true;
    }
    std::vector<std::string> arguments;
    arguments.reserve(call.arg_size());
    for (Value *argument : call.args()) {
      auto lowered = operand(argument, context, user);
      if (!lowered)
        return true;
      arguments.push_back(appendUnaryTemporary(
          "bit_permutation_source_", "identity",
          std::move(*lowered), bits, context, output));
    }

    std::string result;
    if (reverse) {
      for (unsigned sourceBit = 0; sourceBit < bits; ++sourceBit) {
        std::string shifted = arguments[0];
        if (sourceBit != 0)
          shifted = appendBinaryTemporary(
              "bit_reverse_extract_", "lshr",
              variableOperand(arguments[0]),
              integerConstant(sourceBit, bits),
              bits, context, output);
        std::string selected = appendBinaryTemporary(
            "bit_reverse_bit_", "and", variableOperand(shifted),
            integerConstant(1, bits), bits, context, output);
        unsigned destinationBit = bits - sourceBit - 1;
        std::string placed = selected;
        if (destinationBit != 0)
          placed = appendBinaryTemporary(
              "bit_reverse_place_", "shl",
              variableOperand(selected),
              integerConstant(destinationBit, bits),
              bits, context, output);
        result =
            result.empty()
                ? std::move(placed)
                : appendBinaryTemporary(
                      "bit_reverse_or_", "or",
                      variableOperand(result),
                      variableOperand(placed),
                      bits, context, output);
      }
    } else {
      std::string normalized = appendBinaryTemporary(
          "funnel_shift_mod_", "urem", variableOperand(arguments[2]),
          integerConstant(bits, bits), bits, context, output);
      std::string distance = appendBinaryTemporary(
          "funnel_shift_complement_", "sub",
          integerConstant(bits, bits), variableOperand(normalized),
          bits, context, output);
      std::string low;
      std::string high;
      if (funnelLeft) {
        high = appendBinaryTemporary(
            "funnel_shift_left_", "shl",
            variableOperand(arguments[0]),
            variableOperand(normalized), bits, context, output);
        low = appendBinaryTemporary(
            "funnel_shift_right_", "lshr",
            variableOperand(arguments[1]),
            variableOperand(distance), bits, context, output);
      } else {
        low = appendBinaryTemporary(
            "funnel_shift_right_", "lshr",
            variableOperand(arguments[1]),
            variableOperand(normalized), bits, context, output);
        high = appendBinaryTemporary(
            "funnel_shift_left_", "shl",
            variableOperand(arguments[0]),
            variableOperand(distance), bits, context, output);
      }
      result = appendBinaryTemporary(
          "funnel_shift_or_", "or", variableOperand(high),
          variableOperand(low), bits, context, output);
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination;
    bind["value"] = variableOperand(result);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    usesBitPermutationIntrinsics = true;
    return true;
  }

  bool lowerSaturatingArithmeticIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic())
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool signedAdd = intrinsic == Intrinsic::sadd_sat;
    bool unsignedAdd = intrinsic == Intrinsic::uadd_sat;
    bool signedSub = intrinsic == Intrinsic::ssub_sat;
    bool unsignedSub = intrinsic == Intrinsic::usub_sat;
    bool signedShift = intrinsic == Intrinsic::sshl_sat;
    bool unsignedShift = intrinsic == Intrinsic::ushl_sat;
    if (!signedAdd && !unsignedAdd &&
        !signedSub && !unsignedSub &&
        !signedShift && !unsignedShift)
      return false;
    unsigned bits = integerBits(call.getType());
    std::string destination = context.values.lookup(&call);
    if (
        bits == 0 || call.arg_size() != 2 ||
        call.getArgOperand(0)->getType() != call.getType() ||
        call.getArgOperand(1)->getType() != call.getType() ||
        destination.empty()) {
      reject(
          user,
          "saturating arithmetic intrinsic has an unsupported signature");
      return true;
    }
    auto leftValue = operand(call.getArgOperand(0), context, user);
    auto rightValue = operand(call.getArgOperand(1), context, user);
    if (!leftValue || !rightValue)
      return true;
    std::string left = appendUnaryTemporary(
        "saturating_left_", "identity", std::move(*leftValue),
        bits, context, output);
    std::string right = appendUnaryTemporary(
        "saturating_right_", "identity", std::move(*rightValue),
        bits, context, output);
    bool addition = signedAdd || unsignedAdd;
    bool shift = signedShift || unsignedShift;
    bool signedOperation =
        signedAdd || signedSub || signedShift;
    if (shift) {
      std::string inRange = appendBinaryTemporary(
          "saturating_shift_in_range_", "ult",
          variableOperand(right),
          integerConstant(bits, bits), 1, context, output);
      appendAssumption(inRange, output);
    }
    std::string raw = appendBinaryTemporary(
        "saturating_raw_",
        shift ? "shl" : (addition ? "add" : "sub"),
        variableOperand(left), variableOperand(right),
        bits, context, output);
    std::string overflow;
    json::Object clamp;
    if (!signedOperation) {
      if (shift) {
        std::string recovered = appendBinaryTemporary(
            "saturating_unsigned_recovered_", "lshr",
            variableOperand(raw), variableOperand(right),
            bits, context, output);
        overflow = appendBinaryTemporary(
            "saturating_unsigned_overflow_", "ne",
            variableOperand(recovered), variableOperand(left),
            1, context, output);
      } else {
        overflow = appendBinaryTemporary(
            "saturating_unsigned_overflow_", "ult",
            addition ? variableOperand(raw) : variableOperand(left),
            addition ? variableOperand(left) : variableOperand(right),
            1, context, output);
      }
      clamp = (addition || shift)
                  ? integerConstant(-1, bits)
                  : integerConstant(0, bits);
    } else {
      std::string leftNegative = appendBinaryTemporary(
          "saturating_left_negative_", "slt",
          variableOperand(left), integerConstant(0, bits),
          1, context, output);
      if (shift) {
        std::string recovered = appendBinaryTemporary(
            "saturating_signed_recovered_", "ashr",
            variableOperand(raw), variableOperand(right),
            bits, context, output);
        overflow = appendBinaryTemporary(
            "saturating_signed_overflow_", "ne",
            variableOperand(recovered), variableOperand(left),
            1, context, output);
      } else {
        std::string rightNegative = appendBinaryTemporary(
            "saturating_right_negative_", "slt",
            variableOperand(right), integerConstant(0, bits),
            1, context, output);
        std::string resultNegative = appendBinaryTemporary(
            "saturating_result_negative_", "slt",
            variableOperand(raw), integerConstant(0, bits),
            1, context, output);
        std::string risky = appendBinaryTemporary(
            "saturating_sign_risk_", addition ? "eq" : "ne",
            variableOperand(leftNegative),
            variableOperand(rightNegative), 1, context, output);
        std::string changed = appendBinaryTemporary(
            "saturating_sign_changed_", "ne",
            variableOperand(resultNegative),
            variableOperand(leftNegative), 1, context, output);
        overflow = appendBinaryTemporary(
            "saturating_signed_overflow_", "and",
            variableOperand(risky), variableOperand(changed),
            1, context, output);
      }
      int64_t minimum =
          bits == 64
              ? INT64_MIN
              : -(INT64_C(1) << (bits - 1));
      int64_t maximum =
          bits == 64
              ? INT64_MAX
              : (INT64_C(1) << (bits - 1)) - 1;
      std::string signedClamp = appendSelectTemporary(
          "saturating_signed_clamp_",
          variableOperand(leftNegative),
          integerConstant(minimum, bits),
          integerConstant(maximum, bits),
          bits, context, output);
      clamp = variableOperand(signedClamp);
    }
    json::Object bind;
    bind["op"] = "select";
    bind["dst"] = destination;
    bind["condition"] = variableOperand(overflow);
    bind["true"] = std::move(clamp);
    bind["false"] = variableOperand(raw);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    usesSaturatingArithmeticIntrinsics = true;
    return true;
  }

  static bool isOverflowArithmeticIntrinsic(Intrinsic::ID intrinsic) {
    return intrinsic == Intrinsic::sadd_with_overflow ||
           intrinsic == Intrinsic::uadd_with_overflow ||
           intrinsic == Intrinsic::ssub_with_overflow ||
           intrinsic == Intrinsic::usub_with_overflow ||
           intrinsic == Intrinsic::smul_with_overflow ||
           intrinsic == Intrinsic::umul_with_overflow;
  }

  bool lowerOverflowArithmeticIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic() ||
        !isOverflowArithmeticIntrinsic(callee->getIntrinsicID()))
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool signedOperation =
        intrinsic == Intrinsic::sadd_with_overflow ||
        intrinsic == Intrinsic::ssub_with_overflow ||
        intrinsic == Intrinsic::smul_with_overflow;
    bool addition =
        intrinsic == Intrinsic::sadd_with_overflow ||
        intrinsic == Intrinsic::uadd_with_overflow;
    bool subtraction =
        intrinsic == Intrinsic::ssub_with_overflow ||
        intrinsic == Intrinsic::usub_with_overflow;
    auto *resultType = dyn_cast<StructType>(call.getType());
    unsigned bits =
        call.arg_size() == 2
            ? integerBits(call.getArgOperand(0)->getType())
            : 0;
    std::string aggregate = context.values.lookup(&call);
    bool valid =
        bits != 0 && call.arg_size() == 2 &&
        call.getArgOperand(1)->getType() ==
            call.getArgOperand(0)->getType() &&
        resultType != nullptr && resultType->getNumElements() == 2 &&
        resultType->getElementType(0) ==
            call.getArgOperand(0)->getType() &&
        resultType->getElementType(1)->isIntegerTy(1) &&
        !aggregate.empty();
    if (!valid) {
      reject(
          user,
          "overflow arithmetic intrinsic has an unsupported signature");
      return true;
    }
    auto leftValue = operand(call.getArgOperand(0), context, user);
    auto rightValue = operand(call.getArgOperand(1), context, user);
    if (!leftValue || !rightValue)
      return true;
    std::string left = appendUnaryTemporary(
        "overflow_left_", "identity", std::move(*leftValue),
        bits, context, output);
    std::string right = appendUnaryTemporary(
        "overflow_right_", "identity", std::move(*rightValue),
        bits, context, output);
    std::string wrapped = aggregate + "__value";
    json::Object arithmetic;
    arithmetic["op"] = "binary";
    arithmetic["operator"] =
        addition ? "add" : (subtraction ? "sub" : "mul");
    arithmetic["dst"] = wrapped;
    arithmetic["left"] = variableOperand(left);
    arithmetic["right"] = variableOperand(right);
    arithmetic["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(arithmetic));

    std::string overflow;
    if (!signedOperation && (addition || subtraction)) {
      overflow = appendBinaryTemporary(
          "overflow_unsigned_", "ult",
          addition ? variableOperand(wrapped) : variableOperand(left),
          addition ? variableOperand(left) : variableOperand(right),
          1, context, output);
    } else if (signedOperation && (addition || subtraction)) {
      std::string leftNegative = appendBinaryTemporary(
          "overflow_left_negative_", "slt",
          variableOperand(left), integerConstant(0, bits),
          1, context, output);
      std::string rightNegative = appendBinaryTemporary(
          "overflow_right_negative_", "slt",
          variableOperand(right), integerConstant(0, bits),
          1, context, output);
      std::string resultNegative = appendBinaryTemporary(
          "overflow_result_negative_", "slt",
          variableOperand(wrapped), integerConstant(0, bits),
          1, context, output);
      std::string risky = appendBinaryTemporary(
          "overflow_sign_risk_", addition ? "eq" : "ne",
          variableOperand(leftNegative),
          variableOperand(rightNegative), 1, context, output);
      std::string changed = appendBinaryTemporary(
          "overflow_sign_changed_", "ne",
          variableOperand(resultNegative),
          variableOperand(leftNegative), 1, context, output);
      overflow = appendBinaryTemporary(
          "overflow_signed_", "and", variableOperand(risky),
          variableOperand(changed), 1, context, output);
    } else {
      std::string nonzero = appendBinaryTemporary(
          "overflow_multiplier_nonzero_", "ne",
          variableOperand(right), integerConstant(0, bits),
          1, context, output);
      std::string safeDivisor = appendSelectTemporary(
          "overflow_safe_divisor_", variableOperand(nonzero),
          variableOperand(right), integerConstant(1, bits),
          bits, context, output);
      std::string restored = appendBinaryTemporary(
          "overflow_mul_restored_",
          signedOperation ? "sdiv" : "udiv",
          variableOperand(wrapped), variableOperand(safeDivisor),
          bits, context, output);
      std::string changed = appendBinaryTemporary(
          "overflow_mul_changed_", "ne",
          variableOperand(restored), variableOperand(left),
          1, context, output);
      overflow = appendBinaryTemporary(
          "overflow_mul_nonzero_changed_", "and",
          variableOperand(nonzero), variableOperand(changed),
          1, context, output);
      if (signedOperation) {
        int64_t minimum =
            bits == 64
                ? INT64_MIN
                : -(INT64_C(1) << (bits - 1));
        std::string leftMinimum = appendBinaryTemporary(
            "overflow_mul_left_minimum_", "eq",
            variableOperand(left), integerConstant(minimum, bits),
            1, context, output);
        std::string rightMinusOne = appendBinaryTemporary(
            "overflow_mul_right_minus_one_", "eq",
            variableOperand(right), integerConstant(-1, bits),
            1, context, output);
        std::string minimumNegation = appendBinaryTemporary(
            "overflow_mul_minimum_negation_", "and",
            variableOperand(leftMinimum),
            variableOperand(rightMinusOne), 1, context, output);
        overflow = appendBinaryTemporary(
            "overflow_mul_signed_", "or",
            variableOperand(overflow),
            variableOperand(minimumNegation), 1, context, output);
      }
    }
    json::Object overflowBind;
    overflowBind["op"] = "unary";
    overflowBind["operator"] = "identity";
    overflowBind["dst"] = aggregate + "__overflow";
    overflowBind["value"] = variableOperand(overflow);
    overflowBind["bits"] = 1;
    output.push_back(std::move(overflowBind));
    usesOverflowArithmeticIntrinsics = true;
    return true;
  }

  bool lowerScalarSelectionIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic())
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool absolute = intrinsic == Intrinsic::abs;
    StringRef predicate;
    switch (intrinsic) {
    case Intrinsic::smax:
      predicate = "sgt";
      break;
    case Intrinsic::smin:
      predicate = "slt";
      break;
    case Intrinsic::umax:
      predicate = "ugt";
      break;
    case Intrinsic::umin:
      predicate = "ult";
      break;
    default:
      if (!absolute)
        return false;
      break;
    }
    unsigned bits = integerBits(call.getType());
    unsigned expectedArguments = absolute ? 2 : 2;
    std::string destination = context.values.lookup(&call);
    bool valid =
        bits != 0 && call.arg_size() == expectedArguments &&
        call.getArgOperand(0)->getType() == call.getType() &&
        destination.size() != 0;
    auto *minimumPoison =
        absolute && valid
            ? dyn_cast<ConstantInt>(call.getArgOperand(1))
            : nullptr;
    if (
        !valid ||
        (absolute &&
         (minimumPoison == nullptr ||
          !minimumPoison->getType()->isIntegerTy(1))) ||
        (!absolute &&
         call.getArgOperand(1)->getType() != call.getType())) {
      reject(
          user,
          "scalar selection intrinsic has an unsupported signature");
      return true;
    }
    auto leftValue = operand(
        call.getArgOperand(0), context, user);
    if (!leftValue)
      return true;
    std::string left = appendUnaryTemporary(
        "scalar_selection_left_", "identity",
        std::move(*leftValue), bits, context, output);
    if (absolute) {
      int64_t minimum =
          bits == 64
              ? INT64_MIN
              : -(INT64_C(1) << (bits - 1));
      if (!minimumPoison->isZero()) {
        std::string defined = appendBinaryTemporary(
            "scalar_abs_defined_", "ne",
            variableOperand(left),
            integerConstant(minimum, bits),
            1, context, output);
        appendAssumption(defined, output);
      }
      std::string negative = appendBinaryTemporary(
          "scalar_abs_negative_", "slt",
          variableOperand(left), integerConstant(0, bits),
          1, context, output);
      std::string negated = appendBinaryTemporary(
          "scalar_abs_negated_", "sub",
          integerConstant(0, bits), variableOperand(left),
          bits, context, output);
      json::Object bind;
      bind["op"] = "select";
      bind["dst"] = destination;
      bind["condition"] = variableOperand(negative);
      bind["true"] = variableOperand(negated);
      bind["false"] = variableOperand(left);
      bind["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(bind));
    } else {
      auto rightValue = operand(
          call.getArgOperand(1), context, user);
      if (!rightValue)
        return true;
      std::string right = appendUnaryTemporary(
          "scalar_selection_right_", "identity",
          std::move(*rightValue), bits, context, output);
      std::string chooseLeft = appendBinaryTemporary(
          "scalar_selection_compare_", predicate,
          variableOperand(left), variableOperand(right),
          1, context, output);
      json::Object bind;
      bind["op"] = "select";
      bind["dst"] = destination;
      bind["condition"] = variableOperand(chooseLeft);
      bind["true"] = variableOperand(left);
      bind["false"] = variableOperand(right);
      bind["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(bind));
    }
    usesScalarSelectionIntrinsics = true;
    return true;
  }

  bool lowerOptimizationHintIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isIntrinsic())
      return false;
    Intrinsic::ID intrinsic = callee->getIntrinsicID();
    bool probability =
        intrinsic == Intrinsic::expect_with_probability;
    if (intrinsic != Intrinsic::expect && !probability)
      return false;
    unsigned bits = integerBits(call.getType());
    unsigned expectedArguments = probability ? 3 : 2;
    std::string destination = context.values.lookup(&call);
    bool valid =
        bits != 0 && call.arg_size() == expectedArguments &&
        call.getArgOperand(0)->getType() == call.getType() &&
        call.getArgOperand(1)->getType() == call.getType() &&
        !destination.empty();
    if (probability && valid) {
      auto *constant =
          dyn_cast<ConstantFP>(call.getArgOperand(2));
      double value =
          constant == nullptr
              ? -1.0
              : constant->getValueAPF().convertToDouble();
      valid = constant != nullptr && value >= 0.0 && value <= 1.0;
    }
    if (!valid) {
      reject(
          user,
          "optimization hint intrinsic has an unsupported signature");
      return true;
    }
    auto value = operand(
        call.getArgOperand(0), context, user);
    if (!value)
      return true;
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination;
    bind["value"] = std::move(*value);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    usesOptimizationHintIntrinsics = true;
    return true;
  }

  bool lowerSsaCopyIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (
        callee == nullptr || !callee->isIntrinsic() ||
        callee->getIntrinsicID() != Intrinsic::ssa_copy)
      return false;
    unsigned bits = integerBits(call.getType());
    bool pointer = call.getType()->isPointerTy();
    if (pointer)
      bits = M.getDataLayout().getPointerSizeInBits(
          call.getType()->getPointerAddressSpace());
    std::string destination = context.values.lookup(&call);
    bool valid =
        call.arg_size() == 1 &&
        call.getArgOperand(0)->getType() == call.getType() &&
        bits != 0 && bits <= 64 && !destination.empty() &&
        (!pointer ||
         call.getType()->getPointerAddressSpace() == 0);
    if (!valid) {
      reject(user, "ssa.copy intrinsic has an unsupported signature");
      return true;
    }
    std::optional<json::Object> value;
    if (pointer) {
      if (isFunctionPointerValue(call.getArgOperand(0))) {
        value = functionPointerOperand(
            call.getArgOperand(0), context, user);
        auto alternatives = functionPointerAlternatives(
            &call, context, user);
        if (!alternatives)
          return true;
      } else {
        value = pointerOperand(
            call.getArgOperand(0), context, user);
        auto alternatives = pointerAlternatives(
            &call, context, user);
        if (!alternatives)
          return true;
      }
    } else {
      value = operand(call.getArgOperand(0), context, user);
    }
    if (!value)
      return true;
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination;
    bind["value"] = std::move(*value);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    if (!pointer) {
      auto poison =
          context.poisonConditions.find(call.getArgOperand(0));
      if (poison != context.poisonConditions.end()) {
        context.poisonConditions[&call] = poison->second;
        usesTransitiveDeferredPoison = true;
      }
    }
    usesSsaCopyIntrinsics = true;
    return true;
  }

  std::optional<json::Object> pointerGuardCondition(
      ArrayRef<PointerGuard> guards, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    if (guards.empty())
      return integerConstant(1, 1);
    std::string combined;
    for (const PointerGuard &guard : guards) {
      std::optional<json::Object> value;
      if (guard.condition != nullptr)
        value = operand(
            const_cast<Value *>(guard.condition), context, user);
      else
        value = variableOperand(guard.variable);
      if (!value)
        return std::nullopt;
      std::string equal = appendBinaryTemporary(
          "objectsize_guard_", "eq", std::move(*value),
          integerConstant(guard.expected, guard.bits),
          1, context, output);
      combined =
          combined.empty()
              ? std::move(equal)
              : appendBinaryTemporary(
                    "objectsize_guard_and_", "and",
                    variableOperand(combined),
                    variableOperand(equal), 1, context, output);
    }
    return variableOperand(combined);
  }

  bool lowerObjectSizeIntrinsic(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (
        callee == nullptr || !callee->isIntrinsic() ||
        callee->getIntrinsicID() != Intrinsic::objectsize)
      return false;
    unsigned bits = integerBits(call.getType());
    std::string destination = context.values.lookup(&call);
    if (
        bits == 0 || call.arg_size() != 4 ||
        !call.getArgOperand(0)->getType()->isPointerTy() ||
        destination.empty()) {
      reject(user, "objectsize intrinsic has an unsupported signature");
      return true;
    }
    std::array<ConstantInt *, 3> flags = {
        dyn_cast<ConstantInt>(call.getArgOperand(1)),
        dyn_cast<ConstantInt>(call.getArgOperand(2)),
        dyn_cast<ConstantInt>(call.getArgOperand(3)),
    };
    if (std::any_of(
            flags.begin(), flags.end(), [](const ConstantInt *flag) {
              return flag == nullptr ||
                     !flag->getType()->isIntegerTy(1);
            })) {
      reject(user, "objectsize flags must be constant i1 values");
      return true;
    }
    auto alternatives = pointerAlternatives(
        call.getArgOperand(0), context, user);
    if (!alternatives || alternatives->empty())
      return true;
    bool minimum = !flags[0]->isZero();
    bool nullUnknown = !flags[1]->isZero();
    bool dynamic = !flags[2]->isZero();
    bool hasReallocation = std::any_of(
        alternatives->begin(), alternatives->end(),
        [](const PointerAlternative &alternative) {
          return alternative.pointer.reallocationObject != nullptr;
        });
    json::Object fallback =
        minimum ? integerConstant(0, bits)
                : integerConstant(-1, bits);
    json::Object result =
        hasReallocation && !nullUnknown
            ? integerConstant(0, bits)
            : std::move(fallback);
    for (auto iterator = alternatives->rbegin();
         iterator != alternatives->rend(); ++iterator) {
      const PointerAlternative &alternative = *iterator;
      const StaticPointer &pointer = alternative.pointer;
      json::Object size;
      if (pointer.address == 0) {
        size = nullUnknown
                   ? (minimum ? integerConstant(0, bits)
                              : integerConstant(-1, bits))
                   : integerConstant(0, bits);
      } else {
        const CallBase *reallocation =
            pointer.reallocationObject;
        bool inputObject =
            inputBufferAbi &&
            pointer.address >= inputMemoryObject.address &&
            pointer.address <
                inputMemoryObject.address + inputMemoryObject.size;
        auto heap = pointer.heapObject == nullptr
                        ? heapObjects.end()
                        : heapObjects.find(pointer.heapObject);
        if (
            pointer.objectOffset < 0 ||
            static_cast<uint64_t>(pointer.objectOffset) >
                pointer.objectSize) {
          reject(
              user,
              "objectsize pointer offset is outside its finite object");
          return true;
        }
        bool runtimeSized =
            reallocation != nullptr || inputObject ||
            (heap != heapObjects.end() && heap->second.nullable);
        if ((runtimeSized || pointer.dynamicIndex != nullptr) && !dynamic) {
          size = minimum ? integerConstant(0, bits)
                         : integerConstant(-1, bits);
        } else if (runtimeSized || pointer.dynamicIndex != nullptr) {
          std::optional<json::Object> logicalSize;
          unsigned logicalBits = 0;
          if (reallocation != nullptr) {
            logicalSize = operand(
                reallocation->getArgOperand(1), context, user);
            logicalBits = integerBits(
                reallocation->getArgOperand(1)->getType());
          } else if (inputObject) {
            logicalSize = operand(
                inputSizeArgument, context, user);
            logicalBits =
                integerBits(inputSizeArgument->getType());
          } else if (
              heap != heapObjects.end() && heap->second.nullable) {
            CallBase *allocation =
                const_cast<CallBase *>(pointer.heapObject);
            Function *allocator = allocation->getCalledFunction();
            logicalBits = heap->second.sizeBits;
            if (
                allocator != nullptr &&
                allocator->getName() == "malloc") {
              logicalSize = operand(
                  allocation->getArgOperand(0), context, user);
            } else if (
                allocator != nullptr &&
                allocator->getName() == "calloc") {
              auto count = operand(
                  allocation->getArgOperand(0), context, user);
              auto elementSize = operand(
                  allocation->getArgOperand(1), context, user);
              if (count && elementSize) {
                std::string product = appendBinaryTemporary(
                    "objectsize_calloc_product_", "mul",
                    std::move(*count), std::move(*elementSize),
                    logicalBits, context, output);
                logicalSize = variableOperand(product);
              }
            }
          } else {
            logicalBits = bits;
            logicalSize = integerConstant(
                static_cast<int64_t>(pointer.objectSize), bits);
          }
          auto actualPointer = pointerOperand(
              call.getArgOperand(0), context, user);
          unsigned pointerBits =
              M.getDataLayout().getPointerSizeInBits(
                  call.getArgOperand(0)
                      ->getType()->getPointerAddressSpace());
          uint64_t base =
              pointer.dynamicObjectAddress != 0
                  ? pointer.dynamicObjectAddress
                  : pointer.address -
                        static_cast<uint64_t>(pointer.objectOffset);
          if (
              !logicalSize || logicalBits == 0 ||
              logicalBits > 64 || !actualPointer ||
              pointerBits == 0 || pointerBits > 64) {
            reject(
                user,
                "dynamic objectsize has no bounded logical size");
            return true;
          }
          std::string offsetAtPointerWidth = appendBinaryTemporary(
              "objectsize_pointer_offset_", "sub",
              std::move(*actualPointer),
              integerConstant(static_cast<int64_t>(base), pointerBits),
              pointerBits, context, output);
          std::string offset = appendUnaryTemporary(
              "objectsize_offset_cast_",
              pointerBits == bits
                  ? "identity"
                  : (pointerBits < bits ? "zext" : "trunc"),
              variableOperand(offsetAtPointerWidth),
              bits, context, output);
          std::string logical = appendUnaryTemporary(
              "objectsize_logical_cast_",
              logicalBits == bits
                  ? "identity"
                  : (logicalBits < bits ? "zext" : "trunc"),
              std::move(*logicalSize), bits, context, output);
          std::string within = appendBinaryTemporary(
              "objectsize_offset_within_", "uge",
              variableOperand(logical), variableOperand(offset),
              1, context, output);
          std::string remaining = appendBinaryTemporary(
              "objectsize_dynamic_remaining_", "sub",
              variableOperand(logical), variableOperand(offset),
              bits, context, output);
          std::string bounded = appendSelectTemporary(
              "objectsize_dynamic_bounded_",
              variableOperand(within),
              variableOperand(remaining),
              integerConstant(0, bits),
              bits, context, output);
          size = variableOperand(bounded);
          usesDynamicObjectSizeIntrinsics = true;
        } else {
          uint64_t remaining =
              pointer.objectSize -
              static_cast<uint64_t>(pointer.objectOffset);
          size = integerConstant(
              static_cast<int64_t>(remaining), bits);
        }
      }
      auto condition = pointerGuardCondition(
          alternative.guards, context, user, output);
      if (!condition)
        return true;
      std::string selected = appendSelectTemporary(
          "objectsize_case_", std::move(*condition),
          std::move(size), std::move(result),
          bits, context, output);
      result = variableOperand(selected);
    }
    json::Object bind;
    bind["op"] = "unary";
    bind["operator"] = "identity";
    bind["dst"] = destination;
    bind["value"] = std::move(result);
    bind["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(bind));
    usesObjectSizeIntrinsics = true;
    return true;
  }

  struct DeclarativePureExternalModel {
    enum class Kind { Constant, Identity, Unary, Binary, Select } kind;
    std::string specification;
    std::string operation;
    SmallVector<unsigned, 3> arguments;
    uint64_t constant = 0;
  };

  std::optional<DeclarativePureExternalModel>
  parseDeclarativePureExternalModel(
      StringRef specification, unsigned argumentCount) const {
    if (specification.empty() || specification.size() > 256)
      return std::nullopt;
    SmallVector<StringRef, 8> parts;
    specification.split(parts, ':', -1, true);
    if (parts.empty() || parts.front() != "pure-v1")
      return std::nullopt;
    auto parseUnsigned = [](StringRef text, uint64_t &value) {
      if (text.empty() ||
          (text.size() > 1 && text.front() == '0') ||
          text.getAsInteger(10, value))
        return false;
      return std::to_string(value) == text.str();
    };
    auto parseArgument = [&](StringRef text, unsigned &argument) {
      uint64_t value = 0;
      if (!parseUnsigned(text, value) || value >= argumentCount)
        return false;
      argument = static_cast<unsigned>(value);
      return true;
    };
    DeclarativePureExternalModel model;
    model.specification = specification.str();
    if (parts.size() == 3 && parts[1] == "constant") {
      if (!parseUnsigned(parts[2], model.constant))
        return std::nullopt;
      model.kind = DeclarativePureExternalModel::Kind::Constant;
      return model;
    }
    if (parts.size() == 3 && parts[1] == "identity") {
      unsigned argument = 0;
      if (!parseArgument(parts[2], argument))
        return std::nullopt;
      model.kind = DeclarativePureExternalModel::Kind::Identity;
      model.arguments.push_back(argument);
      return model;
    }
    if (parts.size() == 4 && parts[1] == "unary") {
      if (parts[2] != "neg" && parts[2] != "bitnot")
        return std::nullopt;
      unsigned argument = 0;
      if (!parseArgument(parts[3], argument))
        return std::nullopt;
      model.kind = DeclarativePureExternalModel::Kind::Unary;
      model.operation = parts[2].str();
      model.arguments.push_back(argument);
      return model;
    }
    if (parts.size() == 5 && parts[1] == "binary") {
      static const std::set<StringRef> supported = {
          "add", "sub", "mul", "and", "or", "xor",
          "eq", "ne", "ult", "ule", "ugt", "uge",
          "slt", "sle", "sgt", "sge"};
      if (!supported.count(parts[2]))
        return std::nullopt;
      unsigned left = 0;
      unsigned right = 0;
      if (!parseArgument(parts[3], left) ||
          !parseArgument(parts[4], right))
        return std::nullopt;
      model.kind = DeclarativePureExternalModel::Kind::Binary;
      model.operation = parts[2].str();
      model.arguments.push_back(left);
      model.arguments.push_back(right);
      return model;
    }
    if (parts.size() == 5 && parts[1] == "select") {
      unsigned condition = 0;
      unsigned whenTrue = 0;
      unsigned whenFalse = 0;
      if (!parseArgument(parts[2], condition) ||
          !parseArgument(parts[3], whenTrue) ||
          !parseArgument(parts[4], whenFalse))
        return std::nullopt;
      model.kind = DeclarativePureExternalModel::Kind::Select;
      model.arguments.push_back(condition);
      model.arguments.push_back(whenTrue);
      model.arguments.push_back(whenFalse);
      return model;
    }
    return std::nullopt;
  }

  bool lowerDeclarativePureExternal(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isDeclaration())
      return false;
    Attribute modelAttribute =
        callee->getFnAttribute("symcc-continuation-model");
    if (!modelAttribute.isStringAttribute())
      return false;
    if (call.arg_size() > 32) {
      reject(user, "declarative pure external argument budget is exceeded");
      return true;
    }
    StringRef specification = modelAttribute.getValueAsString();
    auto model = parseDeclarativePureExternalModel(
        specification, static_cast<unsigned>(call.arg_size()));
    if (!model) {
      reject(user, "declarative pure external model is invalid");
      return true;
    }
    if (
        callee->isVarArg() ||
        call.arg_size() != callee->arg_size() ||
        call.getType() != callee->getReturnType() ||
        call.hasOperandBundles() ||
        !callee->doesNotAccessMemory() ||
        !callee->hasFnAttribute(Attribute::NoUnwind) ||
        !callee->hasFnAttribute(Attribute::WillReturn) ||
        !callee->hasFnAttribute(Attribute::Speculatable) ||
        !callee->hasFnAttribute(Attribute::NoFree) ||
        !callee->hasFnAttribute(Attribute::NoSync) ||
        callee->hasFnAttribute(Attribute::NoReturn) ||
        callee->hasFnAttribute(Attribute::ReturnsTwice) ||
        callee->hasFnAttribute(Attribute::Convergent)) {
      reject(
          user,
          "declarative pure external lacks its total-purity contract");
      return true;
    }
    StringRef functionName = callee->getName();
    auto validNameCharacter = [](unsigned char character) {
      return (character >= 'a' && character <= 'z') ||
             (character >= 'A' && character <= 'Z') ||
             (character >= '0' && character <= '9') ||
             character == '.' || character == '_' ||
             character == '$' || character == '-';
    };
    if (
        functionName.empty() || functionName.size() > 256 ||
        !std::all_of(
            functionName.bytes_begin(), functionName.bytes_end(),
            validNameCharacter)) {
      reject(user, "declarative pure external function name is invalid");
      return true;
    }
    unsigned resultBits = integerBits(call.getType());
    if (resultBits == 0) {
      reject(user, "declarative pure external result is not a bounded integer");
      return true;
    }
    std::string destination = context.values.lookup(&call);
    if (destination.empty()) {
      reject(user, "declarative pure external result has no bounded binding");
      return true;
    }
    SmallVector<unsigned, 8> argumentBits;
    json::Array arguments;
    json::Array widths;
    for (unsigned index = 0; index < call.arg_size(); ++index) {
      Value *actual = call.getArgOperand(index);
      unsigned bits = integerBits(actual->getType());
      if (
          bits == 0 ||
          actual->getType() != callee->getArg(index)->getType() ||
          context.poisonConditions.count(actual) != 0) {
        reject(
            user,
            "declarative pure external argument is not a defined bounded integer");
        return true;
      }
      auto value = operand(actual, context, user);
      if (!value)
        return true;
      argumentBits.push_back(bits);
      arguments.push_back(std::move(*value));
      widths.push_back(static_cast<int64_t>(bits));
    }
    auto argumentWidth = [&](unsigned index) {
      return index < argumentBits.size() ? argumentBits[index] : 0U;
    };
    bool compatible = false;
    switch (model->kind) {
    case DeclarativePureExternalModel::Kind::Constant:
      compatible =
          resultBits == 64 || model->constant < (UINT64_C(1) << resultBits);
      break;
    case DeclarativePureExternalModel::Kind::Identity:
    case DeclarativePureExternalModel::Kind::Unary:
      compatible =
          argumentWidth(model->arguments[0]) == resultBits;
      break;
    case DeclarativePureExternalModel::Kind::Binary: {
      unsigned leftBits = argumentWidth(model->arguments[0]);
      unsigned rightBits = argumentWidth(model->arguments[1]);
      bool comparison =
          model->operation == "eq" || model->operation == "ne" ||
          model->operation == "ult" || model->operation == "ule" ||
          model->operation == "ugt" || model->operation == "uge" ||
          model->operation == "slt" || model->operation == "sle" ||
          model->operation == "sgt" || model->operation == "sge";
      compatible = leftBits == rightBits &&
                   resultBits == (comparison ? 1U : leftBits);
      break;
    }
    case DeclarativePureExternalModel::Kind::Select:
      compatible = argumentWidth(model->arguments[0]) == 1 &&
                   argumentWidth(model->arguments[1]) == resultBits &&
                   argumentWidth(model->arguments[2]) == resultBits;
      break;
    default:
      compatible = false;
      break;
    }
    if (!compatible) {
      reject(
          user,
          "declarative pure external model does not match its integer ABI");
      return true;
    }
    uint64_t site = stableSiteId(user);
    if (site == 0) {
      reject(user, "declarative pure external stable site is invalid");
      return true;
    }
    auto [sitePosition, inserted] =
        declarativePureExternalSites.emplace(site, &user);
    if (!inserted && sitePosition->second != &user) {
      reject(user, "declarative pure external stable site collides");
      return true;
    }
    json::Object lowered;
    lowered["op"] = "external_pure";
    lowered["function"] = functionName.str();
    lowered["model"] = model->specification;
    lowered["args"] = std::move(arguments);
    lowered["arg_bits"] = std::move(widths);
    lowered["dst"] = destination;
    lowered["bits"] = static_cast<int64_t>(resultBits);
    lowered["site"] = std::to_string(site);
    if (auto *invoke = dyn_cast<InvokeInst>(&call)) {
      lowered["normal"] = context.edgeTarget(
          invoke->getParent(), invoke->getNormalDest());
      usesNoUnwindInvokes = true;
    }
    output.push_back(std::move(lowered));
    usesDeclarativePureExternalSummaries = true;
    return true;
  }

  bool lowerBoundedScalarExternal(
      CallBase &call, FunctionContext &context,
      const Instruction &user, json::Array &output) {
    Function *callee = call.getCalledFunction();
    if (callee == nullptr || !callee->isDeclaration())
      return false;
    StringRef name = callee->getName();
    bool absolute =
        name == "abs" || name == "labs" || name == "llabs";
    bool byteOrder =
        name == "htons" || name == "ntohs" ||
        name == "htonl" || name == "ntohl";
    if (!absolute && !byteOrder)
      return false;
    unsigned bits = integerBits(call.getType());
    unsigned expectedBits =
        (name == "htons" || name == "ntohs") ? 16 : 32;
    if (name == "abs")
      expectedBits = 32;
    else if (name == "labs")
      expectedBits = M.getDataLayout().getPointerSizeInBits();
    else if (name == "llabs")
      expectedBits = 64;
    if (
        call.arg_size() != 1 || bits == 0 ||
        call.getArgOperand(0)->getType() != call.getType() ||
        bits != expectedBits) {
      reject(user, "scalar external has an unsupported signature");
      return true;
    }
    std::string destination = context.values.lookup(&call);
    if (destination.empty()) {
      reject(user, "scalar external result has no bounded binding");
      return true;
    }
    if (byteOrder) {
      if (M.getDataLayout().isLittleEndian()) {
        if (!lowerByteSwap(
                call.getArgOperand(0), destination, bits,
                context, user, output))
          return true;
      } else {
        auto value = operand(
            call.getArgOperand(0), context, user);
        if (!value)
          return true;
        json::Object bind;
        bind["op"] = "unary";
        bind["operator"] = "identity";
        bind["dst"] = destination;
        bind["value"] = std::move(*value);
        bind["bits"] = static_cast<int64_t>(bits);
        output.push_back(std::move(bind));
      }
    } else {
      auto value = operand(
          call.getArgOperand(0), context, user);
      if (!value)
        return true;
      std::string source = appendUnaryTemporary(
          "scalar_abs_source_", "identity",
          std::move(*value), bits, context, output);
      int64_t minimum =
          bits == 64
              ? INT64_MIN
              : -(INT64_C(1) << (bits - 1));
      std::string defined = appendBinaryTemporary(
          "scalar_abs_defined_", "ne", variableOperand(source),
          integerConstant(minimum, bits), 1, context, output);
      appendAssumption(defined, output);
      std::string negative = appendBinaryTemporary(
          "scalar_abs_negative_", "slt", variableOperand(source),
          integerConstant(0, bits), 1, context, output);
      std::string negated = appendBinaryTemporary(
          "scalar_abs_negated_", "sub", integerConstant(0, bits),
          variableOperand(source), bits, context, output);
      json::Object lowered;
      lowered["op"] = "select";
      lowered["dst"] = destination;
      lowered["condition"] = variableOperand(negative);
      lowered["true"] = variableOperand(negated);
      lowered["false"] = variableOperand(source);
      lowered["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(lowered));
    }
    usesExternalSummaries = true;
    usesScalarExternalSummaries = true;
    return true;
  }

  bool exactMemoryAddress(
      const Value *firstPointer, const Value *secondPointer,
      bool *usedCanonicalAddress = nullptr) const {
    const Value *first =
        firstPointer->stripPointerCasts();
    const Value *second =
        secondPointer->stripPointerCasts();
    if (first == second) {
      if (usedCanonicalAddress != nullptr)
        *usedCanonicalAddress = false;
      return true;
    }
    int64_t storeOffset = 0;
    int64_t loadOffset = 0;
    const DataLayout &layout = M.getDataLayout();
    const Value *storeBase = GetPointerBaseWithConstantOffset(
        first, storeOffset, layout);
    const Value *loadBase = GetPointerBaseWithConstantOffset(
        second, loadOffset, layout);
    bool equal =
        storeBase->stripPointerCasts() ==
            loadBase->stripPointerCasts() &&
        storeOffset == loadOffset;
    if (usedCanonicalAddress != nullptr)
      *usedCanonicalAddress = equal;
    return equal;
  }

  bool exactScalarMemoryPair(
      const StoreInst &store, const LoadInst &load,
      bool *usedCanonicalAddress = nullptr) const {
    return
        !store.isVolatile() && !store.isAtomic() &&
        !load.isVolatile() && !load.isAtomic() &&
        store.getValueOperand()->getType() == load.getType() &&
        integerBits(load.getType()) != 0 &&
        exactMemoryAddress(
            store.getPointerOperand(), load.getPointerOperand(),
            usedCanonicalAddress);
  }

  bool isLowerableStaticMemoryBase(
      const Value *base) const {
    return
        isa<GlobalVariable>(base) ||
        (isa<AllocaInst>(base) &&
         cast<AllocaInst>(base)->isStaticAlloca());
  }

  bool isFixedHeapMemoryBase(
      const Value *base) const {
    auto *allocation =
        dyn_cast<CallBase>(base);
    if (
        allocation == nullptr ||
        allocation->arg_size() != 1 ||
        !allocation->getType()->isPointerTy() ||
        allocation->getType()
                ->getPointerAddressSpace() != 0)
      return false;
    Function *callee =
        allocation->getCalledFunction();
    auto *size =
        dyn_cast<ConstantInt>(
            allocation->getArgOperand(0));
    return
        callee != nullptr &&
        callee->isDeclaration() &&
        (callee->getName() == "malloc" ||
         callee->getName() == "__cxa_allocate_exception") &&
        size != nullptr && !size->isZero() &&
        integerBits(size->getType()) != 0 &&
        size->getValue().getActiveBits() <= 64 &&
        size->getZExtValue() <= heapObjectLimit;
  }

  std::optional<std::vector<
      ConstantScalarMemoryRegion>>
  finiteConstantScalarMemoryRegions(
      const Value *root,
      bool &encounteredUnion,
      bool &encounteredSymbolicIndex) const {
    std::vector<ConstantScalarMemoryRegion> regions;
    std::set<const Value *> active;
    unsigned pointerBits =
        M.getDataLayout().getPointerSizeInBits();
    if (pointerBits == 0 || pointerBits > 64)
      return std::nullopt;
    auto appendGuard =
        [](std::vector<
               ConstantScalarMemoryRegionGuard> &guards,
           const Value *condition,
           bool expected) {
          for (const auto &guard : guards)
            if (guard.condition == condition)
              return guard.expected == expected;
          guards.push_back(
              ConstantScalarMemoryRegionGuard{
                  condition, expected,
                  nullptr, nullptr});
          return true;
        };
    auto appendPhiGuard =
        [](std::vector<
               ConstantScalarMemoryRegionGuard> &guards,
           const BasicBlock *phiBlock,
           const BasicBlock *predecessor) {
          for (const auto &guard : guards)
            if (guard.phiBlock == phiBlock)
              return
                  guard.phiPredecessor ==
                  predecessor;
          guards.push_back(
              ConstantScalarMemoryRegionGuard{
                  nullptr, false,
                  phiBlock, predecessor});
          return true;
        };
    std::function<bool(
        const Value *, int64_t, int64_t,
        const std::vector<
            ConstantScalarMemoryRegionGuard> &)>
        collect =
        [&](const Value *value,
            int64_t minimumDisplacement,
            int64_t maximumDisplacement,
            const std::vector<
                ConstantScalarMemoryRegionGuard>
                &guards) -> bool {
          value = value->stripPointerCasts();
          if (auto *getElement =
                  dyn_cast<GEPOperator>(value)) {
            APInt offset(pointerBits, 0, true);
            int64_t minimumDelta = 0;
            int64_t maximumDelta = 0;
            if (getElement->accumulateConstantOffset(
                    M.getDataLayout(), offset)) {
              if (!offset.isSignedIntN(64))
                return false;
              minimumDelta = offset.getSExtValue();
              maximumDelta = minimumDelta;
            } else {
              MapVector<Value *, APInt> variableOffsets;
              APInt constantOffset(pointerBits, 0, true);
              if (
                  !getElement->collectOffset(
                      M.getDataLayout(), pointerBits,
                      variableOffsets,
                      constantOffset) ||
                  variableOffsets.size() != 1 ||
                  !constantOffset.isSignedIntN(64))
                return false;
              Value *index =
                  variableOffsets.begin()->first;
              const APInt &scaleValue =
                  variableOffsets.begin()->second;
              unsigned indexBits =
                  integerBits(index->getType());
              if (
                  indexBits == 0 ||
                  !scaleValue.isSignedIntN(64) ||
                  scaleValue.isZero())
                return false;
              ConstantRange range =
                  computeConstantRange(
                      index, true, true);
              if (range.isEmptySet())
                return false;
              int64_t minimumIndex =
                  range.getSignedMin()
                      .sextOrTrunc(64)
                      .getSExtValue();
              int64_t maximumIndex =
                  range.getSignedMax()
                      .sextOrTrunc(64)
                      .getSExtValue();
              __int128 constant =
                  constantOffset.getSExtValue();
              __int128 scale =
                  scaleValue.getSExtValue();
              __int128 first =
                  constant +
                  scale * minimumIndex;
              __int128 second =
                  constant +
                  scale * maximumIndex;
              __int128 boundedMinimum =
                  std::min(first, second);
              __int128 boundedMaximum =
                  std::max(first, second);
              if (
                  boundedMinimum < INT64_MIN ||
                  boundedMaximum > INT64_MAX)
                return false;
              minimumDelta =
                  static_cast<int64_t>(
                      boundedMinimum);
              maximumDelta =
                  static_cast<int64_t>(
                      boundedMaximum);
              encounteredSymbolicIndex = true;
            }
            __int128 nextMinimum =
                static_cast<__int128>(
                    minimumDisplacement) +
                minimumDelta;
            __int128 nextMaximum =
                static_cast<__int128>(
                    maximumDisplacement) +
                maximumDelta;
            if (
                nextMinimum < INT64_MIN ||
                nextMaximum > INT64_MAX)
              return false;
            return collect(
                getElement->getPointerOperand(),
                static_cast<int64_t>(nextMinimum),
                static_cast<int64_t>(nextMaximum),
                guards);
          }
          if (auto *select =
                  dyn_cast<SelectInst>(value)) {
            encounteredUnion = true;
            if (!active.insert(value).second)
              return false;
            auto trueGuards = guards;
            auto falseGuards = guards;
            bool trueFeasible =
                appendGuard(
                    trueGuards,
                    select->getCondition(), true);
            bool falseFeasible =
                appendGuard(
                    falseGuards,
                    select->getCondition(), false);
            bool valid =
                (!trueFeasible ||
                 collect(
                     select->getTrueValue(),
                     minimumDisplacement,
                     maximumDisplacement,
                     trueGuards)) &&
                (!falseFeasible ||
                 collect(
                     select->getFalseValue(),
                     minimumDisplacement,
                     maximumDisplacement,
                     falseGuards));
            active.erase(value);
            return valid;
          }
          if (auto *phi = dyn_cast<PHINode>(value)) {
            encounteredUnion = true;
            if (
                phi->getNumIncomingValues() == 0 ||
                !active.insert(value).second)
              return false;
            bool valid = true;
            for (
                unsigned index = 0;
                index < phi->getNumIncomingValues();
                ++index) {
              auto incomingGuards = guards;
              bool feasible =
                  appendPhiGuard(
                      incomingGuards,
                      phi->getParent(),
                      phi->getIncomingBlock(index));
              valid &=
                  !feasible ||
                  collect(
                      phi->getIncomingValue(index),
                      minimumDisplacement,
                      maximumDisplacement,
                      incomingGuards);
            }
            active.erase(value);
            return valid;
          }
          if (
              !isLowerableStaticMemoryBase(value) &&
              !isFixedHeapMemoryBase(value))
            return false;
          ConstantScalarMemoryRegion region{
              value, minimumDisplacement,
              maximumDisplacement, guards};
          regions.push_back(std::move(region));
          return
              !regions.empty() &&
              regions.size() <= aliasLimit;
        };
    if (!collect(root, 0, 0, {}) || regions.empty())
      return std::nullopt;
    return regions;
  }

  static bool scalarRegionGuardsConflict(
      const ConstantScalarMemoryRegion &first,
      const ConstantScalarMemoryRegion &second,
      bool &selectConflict,
      bool &phiConflict) {
    for (const auto &firstGuard : first.guards)
      for (const auto &secondGuard : second.guards)
        if (
            firstGuard.condition != nullptr &&
            firstGuard.condition ==
                secondGuard.condition &&
            firstGuard.expected !=
                secondGuard.expected) {
          selectConflict = true;
          return true;
        } else if (
            firstGuard.phiBlock != nullptr &&
            firstGuard.phiBlock ==
                secondGuard.phiBlock &&
            firstGuard.phiPredecessor !=
                secondGuard.phiPredecessor) {
          phiConflict = true;
          return true;
        }
    return false;
  }

  static bool disjointConstantScalarRegions(
      const ConstantScalarMemoryRegion &first,
      uint64_t firstBytes,
      const ConstantScalarMemoryRegion &second,
      uint64_t secondBytes) {
    if (first.base != second.base)
      return true;
    __int128 firstEnd =
        static_cast<__int128>(
            first.maximumOffset) +
        firstBytes;
    __int128 secondEnd =
        static_cast<__int128>(
            second.maximumOffset) +
        secondBytes;
    return
        firstEnd <= second.offset ||
        secondEnd <= first.offset;
  }

  bool provablyDisjointScalarMemoryAccess(
      const Value *firstPointer, Type *firstType,
      const Value *secondPointer, Type *secondType,
      bool *usedIdentifiedObjects = nullptr,
      bool *usedFixedHeapObjects = nullptr,
      bool *usedFinitePointerDomains = nullptr,
      bool *usedGuardCorrelatedPointerDomains =
          nullptr,
      bool *usedPhiCorrelatedPointerDomains =
          nullptr,
      bool *usedSymbolicIndexIntervals =
          nullptr) const {
    if (usedIdentifiedObjects != nullptr)
      *usedIdentifiedObjects = false;
    if (usedFixedHeapObjects != nullptr)
      *usedFixedHeapObjects = false;
    if (usedFinitePointerDomains != nullptr)
      *usedFinitePointerDomains = false;
    if (usedGuardCorrelatedPointerDomains != nullptr)
      *usedGuardCorrelatedPointerDomains = false;
    if (usedPhiCorrelatedPointerDomains != nullptr)
      *usedPhiCorrelatedPointerDomains = false;
    if (usedSymbolicIndexIntervals != nullptr)
      *usedSymbolicIndexIntervals = false;
    uint64_t firstBytes =
        fixedStoreBytes(M.getDataLayout(), firstType);
    uint64_t secondBytes =
        fixedStoreBytes(M.getDataLayout(), secondType);
    if (
        integerBits(firstType) == 0 ||
        integerBits(secondType) == 0 ||
        firstBytes == 0 || secondBytes == 0)
      return false;
    if (
        firstBytes > static_cast<uint64_t>(INT64_MAX) ||
        secondBytes > static_cast<uint64_t>(INT64_MAX))
      return false;
    bool firstUnion = false;
    bool secondUnion = false;
    bool firstSymbolicIndex = false;
    bool secondSymbolicIndex = false;
    auto firstRegions =
        finiteConstantScalarMemoryRegions(
            firstPointer, firstUnion,
            firstSymbolicIndex);
    auto secondRegions =
        finiteConstantScalarMemoryRegions(
            secondPointer, secondUnion,
            secondSymbolicIndex);
    if (
        (firstUnion || secondUnion ||
         firstSymbolicIndex ||
         secondSymbolicIndex) &&
        firstRegions && secondRegions) {
      bool selectCorrelation = false;
      bool phiCorrelation = false;
      for (const auto &first : *firstRegions)
        for (const auto &second : *secondRegions) {
          if (
              disjointConstantScalarRegions(
                  first, firstBytes,
                  second, secondBytes))
            continue;
          if (
              scalarRegionGuardsConflict(
                  first, second,
                  selectCorrelation,
                  phiCorrelation)) {
            continue;
          }
          return false;
        }
      if (
          !selectCorrelation &&
          !phiCorrelation &&
          !firstSymbolicIndex &&
          !secondSymbolicIndex &&
          usedFinitePointerDomains != nullptr)
        *usedFinitePointerDomains = true;
      if (
          (firstSymbolicIndex ||
           secondSymbolicIndex) &&
          usedSymbolicIndexIntervals != nullptr)
        *usedSymbolicIndexIntervals = true;
      if (
          selectCorrelation &&
          usedGuardCorrelatedPointerDomains !=
              nullptr)
        *usedGuardCorrelatedPointerDomains = true;
      if (
          phiCorrelation &&
          usedPhiCorrelatedPointerDomains !=
              nullptr)
        *usedPhiCorrelatedPointerDomains = true;
      return true;
    }
    int64_t firstOffset = 0;
    int64_t secondOffset = 0;
    const DataLayout &layout = M.getDataLayout();
    const Value *firstBase =
        GetPointerBaseWithConstantOffset(
            firstPointer->stripPointerCasts(),
            firstOffset, layout)
            ->stripPointerCasts();
    const Value *secondBase =
        GetPointerBaseWithConstantOffset(
            secondPointer->stripPointerCasts(),
            secondOffset, layout)
            ->stripPointerCasts();
    if (firstBase != secondBase) {
      bool firstStatic =
          isLowerableStaticMemoryBase(firstBase);
      bool secondStatic =
          isLowerableStaticMemoryBase(secondBase);
      bool firstHeap =
          isFixedHeapMemoryBase(firstBase);
      bool secondHeap =
          isFixedHeapMemoryBase(secondBase);
      bool identifiedStatic =
          firstStatic && secondStatic;
      bool fixedHeap =
          (firstHeap || secondHeap) &&
          (firstStatic || firstHeap) &&
          (secondStatic || secondHeap);
      if (usedIdentifiedObjects != nullptr)
        *usedIdentifiedObjects =
            identifiedStatic;
      if (usedFixedHeapObjects != nullptr)
        *usedFixedHeapObjects = fixedHeap;
      return identifiedStatic || fixedHeap;
    }
    return disjointConstantScalarRegions(
        ConstantScalarMemoryRegion{
            firstBase, firstOffset,
            firstOffset, {}},
        firstBytes,
        ConstantScalarMemoryRegion{
            secondBase, secondOffset,
            secondOffset, {}},
        secondBytes);
  }

  bool hasDefinedScalarInitialDefinition(
      const LoadInst &load) const {
    const Value *pointer =
        load.getPointerOperand()->stripPointerCasts();
    int64_t offset = 0;
    const DataLayout &layout = M.getDataLayout();
    const Value *base =
        GetPointerBaseWithConstantOffset(
            pointer, offset, layout);
    auto *global =
        dyn_cast<GlobalVariable>(
            base->stripPointerCasts());
    if (
        load.getFunction()->getName() != entryName ||
        global == nullptr || !global->hasInitializer() ||
        global->isThreadLocal() ||
        global->getAddressSpace() != 0)
      return false;
    unsigned pointerBits =
        layout.getPointerSizeInBits(
            global->getAddressSpace());
    if (pointerBits == 0 || pointerBits > 64)
      return false;
    Constant *folded = ConstantFoldLoadFromConst(
        const_cast<Constant *>(global->getInitializer()),
        load.getType(),
        APInt(pointerBits, offset, true), layout);
    return isa_and_nonnull<ConstantInt>(folded);
  }

  bool initialDefinitionUsesSubobject(
      const LoadInst &load) const {
    const Value *pointer =
        load.getPointerOperand()->stripPointerCasts();
    int64_t offset = 0;
    const Value *base =
        GetPointerBaseWithConstantOffset(
            pointer, offset, M.getDataLayout());
    return
        isa<GlobalVariable>(
            base->stripPointerCasts()) &&
        (offset != 0 ||
         pointer != base->stripPointerCasts());
  }

  std::optional<std::vector<
      FunctionContext::MemoryPoisonIncoming>>
  predecessorPoisonStores(
      const LoadInst &load) const {
    const BasicBlock *merge = load.getParent();
    if (pred_size(merge) < 2 || pred_size(merge) > 64)
      return std::nullopt;
    for (const Instruction &instruction : *merge) {
      if (&instruction == &load)
        break;
      if (!instruction.mayReadOrWriteMemory())
        continue;
      if (auto *earlierLoad =
              dyn_cast<LoadInst>(&instruction)) {
        if (
            !earlierLoad->isVolatile() &&
            !earlierLoad->isAtomic() &&
            ((earlierLoad->getType() ==
                  load.getType() &&
              exactMemoryAddress(
                  earlierLoad->getPointerOperand(),
                  load.getPointerOperand())) ||
             provablyDisjointScalarMemoryAccess(
                 earlierLoad->getPointerOperand(),
                 earlierLoad->getType(),
                 load.getPointerOperand(),
                 load.getType())))
          continue;
      } else if (auto *earlierStore =
                     dyn_cast<StoreInst>(
                         &instruction)) {
        if (
            !earlierStore->isVolatile() &&
            !earlierStore->isAtomic() &&
            provablyDisjointScalarMemoryAccess(
                earlierStore->getPointerOperand(),
                earlierStore->getValueOperand()
                    ->getType(),
                load.getPointerOperand(),
                load.getType()))
          continue;
      }
      return std::nullopt;
    }
    std::vector<
        FunctionContext::MemoryPoisonIncoming>
        incoming;
    incoming.reserve(pred_size(merge));
    DominatorTree dominators(
        *const_cast<Function *>(load.getFunction()));
    unsigned visitedBlocks = 0;
    struct ReachingStore {
      bool valid = false;
      const StoreInst *store = nullptr;
      FunctionContext::MemoryPoisonIncoming::Kind kind =
          FunctionContext::MemoryPoisonIncoming::Kind::Store;
      const Value *condition = nullptr;
      bool storeWhenTrue = false;
      bool conditionalForwarded = false;
      bool multiArmConditional = false;
      bool equivalentDefinedStores = false;
      bool sharedPoisonStores = false;
      std::vector<const StoreInst *> equivalentStores;
      const StoreInst *secondaryStore = nullptr;
      const Value *innerCondition = nullptr;
      bool firstStoreWhenTrue = false;
      bool nestedConditional = false;
      std::vector<
          FunctionContext::MemoryDefinednessTreeNode>
          conditionTree;
      int conditionTreeRoot = -1;
      unsigned conditionTreeDepth = 0;
      bool groupedRecursiveConditional = false;
      bool repeatedSourceRecursiveConditional = false;
      bool multiCarryRecursiveConditional = false;
    };
    for (const BasicBlock *predecessor :
         predecessors(merge)) {
      if (succ_size(predecessor) != 1)
        return std::nullopt;
      std::set<const BasicBlock *> active;
      std::function<ReachingStore(
          const BasicBlock *)>
          findStore =
              [&](const BasicBlock *block)
                  -> ReachingStore {
                if (
                    ++visitedBlocks > 64 ||
                    !active.insert(block).second)
                  return {};
                auto iterator = block->end();
                while (iterator != block->begin()) {
                  --iterator;
                  const Instruction &instruction =
                      *iterator;
                  if (
                      block == merge &&
                      &instruction == &load) {
                    active.erase(block);
                    return ReachingStore{
                        true, nullptr,
                        FunctionContext::MemoryPoisonIncoming::
                            Kind::Carry,
                        nullptr, false, false, false, false,
                        false, {}, nullptr, nullptr, false,
                        false, {}, -1, 0, false, false,
                        false};
                  }
                  if (
                      !instruction
                           .mayReadOrWriteMemory())
                    continue;
                  if (auto *earlierLoad =
                          dyn_cast<LoadInst>(
                              &instruction)) {
                    if (
                        earlierLoad->isVolatile() ||
                        earlierLoad->isAtomic()) {
                      active.erase(block);
                      return {};
                    }
                    bool sameCell =
                        earlierLoad->getType() ==
                            load.getType() &&
                        exactMemoryAddress(
                            earlierLoad
                                ->getPointerOperand(),
                            load.getPointerOperand());
                    bool disjointCell =
                        provablyDisjointScalarMemoryAccess(
                            earlierLoad
                                ->getPointerOperand(),
                            earlierLoad->getType(),
                            load.getPointerOperand(),
                            load.getType());
                    if (!sameCell && !disjointCell) {
                      active.erase(block);
                      return {};
                    }
                    continue;
                  }
                  auto *writer =
                      dyn_cast<StoreInst>(
                          &instruction);
                  if (
                      writer != nullptr &&
                      !writer->isVolatile() &&
                      !writer->isAtomic() &&
                      provablyDisjointScalarMemoryAccess(
                          writer->getPointerOperand(),
                          writer->getValueOperand()
                              ->getType(),
                          load.getPointerOperand(),
                          load.getType()))
                    continue;
                  active.erase(block);
                  return
                      writer != nullptr &&
                              exactScalarMemoryPair(
                                  *writer, load)
                          ? ReachingStore{
                                true, writer,
                                FunctionContext::
                                        MemoryPoisonIncoming::
                                        Kind::Store,
                                nullptr, false, false, false,
                                false, false, {}, nullptr,
                                nullptr, false, false, {},
                                -1, 0, false, false, false}
                          : ReachingStore{};
                }
                if (pred_empty(block)) {
                  active.erase(block);
                  return ReachingStore{
                      hasDefinedScalarInitialDefinition(
                          load),
                      nullptr,
                      FunctionContext::MemoryPoisonIncoming::
                          Kind::Initial,
                      nullptr, false, false, false, false,
                      false, {}, nullptr, nullptr, false,
                      false, {}, -1, 0, false, false,
                      false};
                }
                std::vector<
                    std::pair<
                        const BasicBlock *, ReachingStore>>
                    candidates;
                for (const BasicBlock *parent :
                     predecessors(block)) {
                  ReachingStore candidate =
                      findStore(parent);
                  if (!candidate.valid) {
                    active.erase(block);
                    return {};
                  }
                  candidates.emplace_back(
                      parent, candidate);
                }
                const ReachingStore &common =
                    candidates.front().second;
                bool allCommon =
                    std::all_of(
                        std::next(candidates.begin()),
                        candidates.end(),
                        [&](const auto &item) {
                          const ReachingStore &candidate =
                              item.second;
                          return
                              candidate.store ==
                                  common.store &&
                              candidate.kind ==
                                  common.kind &&
                              candidate.condition ==
                                  common.condition &&
                              candidate.storeWhenTrue ==
                                  common.storeWhenTrue &&
                              candidate.conditionalForwarded ==
                                  common.conditionalForwarded &&
                              candidate.multiArmConditional ==
                                  common.multiArmConditional &&
                              candidate.equivalentDefinedStores ==
                                  common.equivalentDefinedStores &&
                              candidate.sharedPoisonStores ==
                                  common.sharedPoisonStores &&
                              candidate.equivalentStores.size() ==
                                  common.equivalentStores.size() &&
                              std::is_permutation(
                                  candidate.equivalentStores.begin(),
                                  candidate.equivalentStores.end(),
                                  common.equivalentStores.begin()) &&
                              candidate.secondaryStore ==
                                  common.secondaryStore &&
                              candidate.innerCondition ==
                                  common.innerCondition &&
                              candidate.firstStoreWhenTrue ==
                                  common.firstStoreWhenTrue &&
                              candidate.nestedConditional ==
                                  common.nestedConditional &&
                              candidate.conditionTree ==
                                  common.conditionTree &&
                              candidate.conditionTreeRoot ==
                                  common.conditionTreeRoot &&
                              candidate.conditionTreeDepth ==
                                  common.conditionTreeDepth &&
                              candidate.groupedRecursiveConditional ==
                                  common.groupedRecursiveConditional &&
                              candidate.repeatedSourceRecursiveConditional ==
                                  common.repeatedSourceRecursiveConditional &&
                              candidate.multiCarryRecursiveConditional ==
                                  common.multiCarryRecursiveConditional;
                        });
                if (allCommon) {
                  active.erase(block);
                  return common;
                }
                auto sameSource =
                    [&](const ReachingStore &left,
                        const ReachingStore &right) {
                      bool equivalentDefinedStores =
                          left.kind ==
                              FunctionContext::
                                  MemoryPoisonIncoming::
                                      Kind::Store &&
                          right.kind ==
                              FunctionContext::
                                  MemoryPoisonIncoming::
                                      Kind::Store &&
                          left.store != nullptr &&
                          right.store != nullptr &&
                          !valueMayCreateDeferredPoison(
                              left.store->getValueOperand()) &&
                          !valueMayCreateDeferredPoison(
                              right.store->getValueOperand());
                      bool equivalentSharedPoisonStores =
                          left.kind ==
                              FunctionContext::
                                  MemoryPoisonIncoming::
                                      Kind::Store &&
                          right.kind ==
                              FunctionContext::
                                  MemoryPoisonIncoming::
                                      Kind::Store &&
                          left.store != nullptr &&
                          right.store != nullptr &&
                          left.store->getValueOperand() ==
                              right.store->getValueOperand();
                      return
                          (left.store == right.store ||
                           equivalentDefinedStores ||
                           equivalentSharedPoisonStores) &&
                          left.kind == right.kind &&
                          left.condition ==
                              right.condition &&
                          left.storeWhenTrue ==
                              right.storeWhenTrue &&
                          left.conditionalForwarded ==
                              right.conditionalForwarded &&
                          left.multiArmConditional ==
                              right.multiArmConditional &&
                          left.equivalentDefinedStores ==
                              right.equivalentDefinedStores &&
                          left.sharedPoisonStores ==
                              right.sharedPoisonStores &&
                          left.equivalentStores.size() ==
                              right.equivalentStores.size() &&
                          std::is_permutation(
                              left.equivalentStores.begin(),
                              left.equivalentStores.end(),
                              right.equivalentStores.begin()) &&
                          left.secondaryStore ==
                              right.secondaryStore &&
                          left.innerCondition ==
                              right.innerCondition &&
                          left.firstStoreWhenTrue ==
                              right.firstStoreWhenTrue &&
                          left.nestedConditional ==
                              right.nestedConditional &&
                          left.conditionTree ==
                              right.conditionTree &&
                          left.conditionTreeRoot ==
                              right.conditionTreeRoot &&
                          left.conditionTreeDepth ==
                              right.conditionTreeDepth &&
                          left.groupedRecursiveConditional ==
                              right.groupedRecursiveConditional &&
                          left.repeatedSourceRecursiveConditional ==
                              right.repeatedSourceRecursiveConditional &&
                          left.multiCarryRecursiveConditional ==
                              right.multiCarryRecursiveConditional;
                    };
                using Candidate =
                    std::pair<
                        const BasicBlock *, ReachingStore>;
                std::vector<
                    std::vector<const Candidate *>>
                    sourceGroups;
                for (const auto &candidate :
                     candidates) {
                  auto group = std::find_if(
                      sourceGroups.begin(),
                      sourceGroups.end(),
                      [&](const auto &item) {
                        return sameSource(
                            item.front()->second,
                            candidate.second);
                      });
                  if (group == sourceGroups.end())
                    sourceGroups.push_back(
                        {&candidate});
                  else
                    group->push_back(&candidate);
                }
                if (sourceGroups.size() == 2) {
                  const auto *storeGroup =
                      &sourceGroups[0];
                  const auto *carryGroup =
                      &sourceGroups[1];
                  if (
                      storeGroup->front()->second.kind !=
                          FunctionContext::
                              MemoryPoisonIncoming::
                                  Kind::Store)
                    std::swap(storeGroup, carryGroup);
                  const StoreInst *conditionalStore =
                      storeGroup->front()->second.store;
                  if (
                      storeGroup->front()->second.kind ==
                          FunctionContext::
                              MemoryPoisonIncoming::
                                  Kind::Store &&
                      carryGroup->front()->second.kind ==
                          FunctionContext::
                              MemoryPoisonIncoming::
                                  Kind::Carry &&
                      conditionalStore != nullptr) {
                    struct ConditionalArm {
                      const BranchInst *branch = nullptr;
                      const BasicBlock *successor = nullptr;
                      bool forwarded = false;
                    };
                    auto findConditionalArms =
                        [&](const BasicBlock *endpoint,
                            const StoreInst *requiredStore) {
                          std::vector<ConditionalArm> arms;
                          std::set<const BasicBlock *> seen;
                          const BasicBlock *current =
                              endpoint;
                          bool forwarded = false;
                          bool sawStore =
                              requiredStore == nullptr;
                          while (
                              current != nullptr &&
                              seen.insert(current).second &&
                              ++visitedBlocks <= 64) {
                            sawStore |=
                                requiredStore != nullptr &&
                                current ==
                                    requiredStore
                                        ->getParent();
                            if (pred_size(current) != 1)
                              break;
                            const BasicBlock *parent =
                                *pred_begin(current);
                            auto *branch =
                                dyn_cast<BranchInst>(
                                    parent->getTerminator());
                            if (
                                sawStore &&
                                branch != nullptr &&
                                branch->isConditional() &&
                                (branch->getSuccessor(0) ==
                                     current ||
                                 branch->getSuccessor(1) ==
                                     current))
                              arms.push_back({
                                  branch, current,
                                  forwarded});
                            current = parent;
                            forwarded = true;
                          }
                          return arms;
                        };
                    auto storeArms =
                        findConditionalArms(
                            storeGroup->front()->first,
                            conditionalStore);
                    auto carryArms =
                        findConditionalArms(
                            carryGroup->front()->first,
                            nullptr);
                    for (const auto &storeArm :
                         storeArms) {
                      auto carryArm =
                          std::find_if(
                              carryArms.begin(),
                              carryArms.end(),
                              [&](const auto &item) {
                                return
                                    item.branch ==
                                        storeArm.branch &&
                                    item.successor !=
                                        storeArm.successor;
                              });
                      if (carryArm == carryArms.end())
                        continue;
                      bool forwarded =
                          storeArm.forwarded ||
                          carryArm->forwarded;
                      auto groupMatchesArm =
                          [&](const auto &group,
                              const BasicBlock *successor,
                              const StoreInst *requiredStore) {
                            for (const Candidate *candidate :
                                 group) {
                              auto arms =
                                  findConditionalArms(
                                      candidate->first,
                                      requiredStore == nullptr
                                          ? nullptr
                                          : candidate->second
                                                .store);
                              auto match =
                                  std::find_if(
                                      arms.begin(),
                                      arms.end(),
                                      [&](const auto &item) {
                                        return
                                            item.branch ==
                                                storeArm.branch &&
                                            item.successor ==
                                                successor;
                                      });
                              if (match == arms.end())
                                return false;
                              forwarded |=
                                  match->forwarded;
                            }
                            return true;
                          };
                      if (
                          !groupMatchesArm(
                              *storeGroup,
                              storeArm.successor,
                              conditionalStore) ||
                          !groupMatchesArm(
                              *carryGroup,
                              carryArm->successor,
                              nullptr))
                        continue;
                      active.erase(block);
                      bool equivalentDefinedStores =
                          std::any_of(
                              std::next(
                                  storeGroup->begin()),
                              storeGroup->end(),
                              [&](const Candidate *item) {
                                return
                                    item->second.store !=
                                    conditionalStore;
                              });
                      bool sharedPoisonStores =
                          equivalentDefinedStores &&
                          valueMayCreateDeferredPoison(
                              conditionalStore
                                  ->getValueOperand());
                      equivalentDefinedStores &=
                          !sharedPoisonStores;
                      std::vector<const StoreInst *>
                          equivalentStoreMembers;
                      for (const Candidate *candidate :
                           *storeGroup) {
                        const StoreInst *member =
                            candidate->second.store;
                        if (
                            member != nullptr &&
                            std::find(
                                equivalentStoreMembers.begin(),
                                equivalentStoreMembers.end(),
                                member) ==
                                equivalentStoreMembers.end())
                          equivalentStoreMembers.push_back(
                              member);
                      }
                      return ReachingStore{
                          true, conditionalStore,
                          FunctionContext::
                              MemoryPoisonIncoming::
                                  Kind::
                                      ConditionalStoreCarry,
                          storeArm.branch->getCondition(),
                          storeArm.branch->getSuccessor(0) ==
                              storeArm.successor,
                          forwarded,
                          candidates.size() > 2,
                          equivalentDefinedStores,
                          sharedPoisonStores,
                          std::move(
                              equivalentStoreMembers),
                          nullptr, nullptr, false, false,
                          {}, -1, 0, false, false, false};
                    }
                  }
                }
                if (
                    sourceGroups.size() == 3 &&
                    std::all_of(
                        sourceGroups.begin(),
                        sourceGroups.end(),
                        [](const auto &group) {
                          return group.size() == 1;
                        })) {
                  std::vector<const Candidate *>
                      storeCandidates;
                  const Candidate *carryCandidate =
                      nullptr;
                  for (const auto &group :
                       sourceGroups) {
                    const Candidate *candidate =
                        group.front();
                    if (
                        candidate->second.kind ==
                        FunctionContext::
                            MemoryPoisonIncoming::
                                Kind::Store)
                      storeCandidates.push_back(
                          candidate);
                    else if (
                        candidate->second.kind ==
                        FunctionContext::
                            MemoryPoisonIncoming::
                                Kind::Carry)
                      carryCandidate = candidate;
                  }
                  if (
                      storeCandidates.size() == 2 &&
                      carryCandidate != nullptr &&
                      storeCandidates[0]->second.store !=
                          nullptr &&
                      storeCandidates[1]->second.store !=
                          nullptr) {
                    struct ConditionalArm {
                      const BranchInst *branch = nullptr;
                      const BasicBlock *successor = nullptr;
                      bool forwarded = false;
                    };
                    auto findConditionalArms =
                        [&](const Candidate *candidate,
                            const StoreInst *requiredStore) {
                          std::vector<ConditionalArm> arms;
                          std::set<const BasicBlock *> seen;
                          const BasicBlock *current =
                              candidate->first;
                          bool forwarded = false;
                          bool sawStore =
                              requiredStore == nullptr;
                          while (
                              current != nullptr &&
                              seen.insert(current).second &&
                              ++visitedBlocks <= 64) {
                            sawStore |=
                                requiredStore != nullptr &&
                                current ==
                                    requiredStore
                                        ->getParent();
                            if (pred_size(current) != 1)
                              break;
                            const BasicBlock *parent =
                                *pred_begin(current);
                            auto *branch =
                                dyn_cast<BranchInst>(
                                    parent->getTerminator());
                            if (
                                sawStore &&
                                branch != nullptr &&
                                branch->isConditional() &&
                                (branch->getSuccessor(0) ==
                                     current ||
                                 branch->getSuccessor(1) ==
                                     current))
                              arms.push_back({
                                  branch, current,
                                  forwarded});
                            current = parent;
                            forwarded = true;
                          }
                          return arms;
                        };
                    auto firstArms =
                        findConditionalArms(
                            storeCandidates[0],
                            storeCandidates[0]
                                ->second.store);
                    auto secondArms =
                        findConditionalArms(
                            storeCandidates[1],
                            storeCandidates[1]
                                ->second.store);
                    auto carryArms =
                        findConditionalArms(
                            carryCandidate, nullptr);
                    for (const auto &innerFirst :
                         firstArms) {
                      auto innerSecond =
                          std::find_if(
                              secondArms.begin(),
                              secondArms.end(),
                              [&](const auto &item) {
                                return
                                    item.branch ==
                                        innerFirst.branch &&
                                    item.successor !=
                                        innerFirst.successor;
                              });
                      if (innerSecond ==
                          secondArms.end())
                        continue;
                      for (const auto &outerFirst :
                           firstArms) {
                        if (
                            outerFirst.branch ==
                            innerFirst.branch)
                          continue;
                        auto outerSecond =
                            std::find_if(
                                secondArms.begin(),
                                secondArms.end(),
                                [&](const auto &item) {
                                  return
                                      item.branch ==
                                          outerFirst.branch &&
                                      item.successor ==
                                          outerFirst.successor;
                                });
                        auto outerCarry =
                            std::find_if(
                                carryArms.begin(),
                                carryArms.end(),
                                [&](const auto &item) {
                                  return
                                      item.branch ==
                                          outerFirst.branch &&
                                      item.successor !=
                                          outerFirst.successor;
                                });
                        if (
                            outerSecond ==
                                secondArms.end() ||
                            outerCarry ==
                                carryArms.end() ||
                            innerFirst.branch->getParent() !=
                                outerFirst.successor)
                          continue;
                        auto availableAtIncoming =
                            [&](const Value *value) {
                              if (
                                  isa<Constant>(value) ||
                                  isa<Argument>(value))
                                return true;
                              auto *producer =
                                  dyn_cast<Instruction>(
                                      value);
                              return
                                  producer != nullptr &&
                                  dominators.dominates(
                                      producer,
                                      predecessor
                                          ->getTerminator());
                            };
                        if (
                            !availableAtIncoming(
                                innerFirst.branch
                                    ->getCondition()) ||
                            !availableAtIncoming(
                                storeCandidates[0]
                                    ->second.store
                                    ->getValueOperand()) ||
                            !availableAtIncoming(
                                storeCandidates[1]
                                    ->second.store
                                    ->getValueOperand()))
                          continue;
                        ReachingStore nested;
                        nested.valid = true;
                        nested.store =
                            storeCandidates[0]
                                ->second.store;
                        nested.kind =
                            FunctionContext::
                                MemoryPoisonIncoming::
                                    Kind::
                                        NestedConditionalStoresCarry;
                        nested.condition =
                            outerFirst.branch
                                ->getCondition();
                        nested.storeWhenTrue =
                            outerFirst.branch
                                ->getSuccessor(0) ==
                            outerFirst.successor;
                        nested.conditionalForwarded =
                            innerFirst.forwarded ||
                            innerSecond->forwarded ||
                            outerFirst.forwarded ||
                            outerSecond->forwarded ||
                            outerCarry->forwarded;
                        nested.multiArmConditional = true;
                        nested.equivalentStores = {
                            storeCandidates[1]
                                ->second.store};
                        nested.secondaryStore =
                            storeCandidates[1]
                                ->second.store;
                        nested.innerCondition =
                            innerFirst.branch
                                ->getCondition();
                        nested.firstStoreWhenTrue =
                            innerFirst.branch
                                ->getSuccessor(0) ==
                            innerFirst.successor;
                        nested.nestedConditional = true;
                        active.erase(block);
                        return nested;
                      }
                    }
                  }
                }
                if (
                    sourceGroups.size() >= 4 &&
                    sourceGroups.size() <= 8) {
                  struct ConditionalArm {
                    const BranchInst *branch = nullptr;
                    const BasicBlock *successor = nullptr;
                    bool forwarded = false;
                  };
                  struct TreeCandidate {
                    const Candidate *candidate = nullptr;
                    std::vector<
                        std::vector<ConditionalArm>>
                        endpointArms;
                  };
                  auto findConditionalArms =
                      [&](const Candidate *candidate,
                          const StoreInst *requiredStore) {
                        std::vector<ConditionalArm> arms;
                        std::set<const BasicBlock *> seen;
                        const BasicBlock *current =
                            candidate->first;
                        bool forwarded = false;
                        bool sawStore =
                            requiredStore == nullptr;
                        while (
                            current != nullptr &&
                            seen.insert(current).second &&
                            ++visitedBlocks <= 64) {
                          sawStore |=
                              requiredStore != nullptr &&
                              current ==
                                  requiredStore->getParent();
                          if (pred_size(current) != 1)
                            break;
                          const BasicBlock *parent =
                              *pred_begin(current);
                          auto *branch =
                              dyn_cast<BranchInst>(
                                  parent->getTerminator());
                          if (
                              sawStore &&
                              branch != nullptr &&
                              branch->isConditional() &&
                              (branch->getSuccessor(0) ==
                                   current ||
                               branch->getSuccessor(1) ==
                                   current))
                            arms.push_back({
                                branch, current, forwarded});
                          current = parent;
                          forwarded = true;
                        }
                        return arms;
                      };
                  std::vector<TreeCandidate> treeCandidates;
                  treeCandidates.reserve(sourceGroups.size());
                  unsigned carryLeaves = 0;
                  bool validLeaves = true;
                  bool groupedSources = false;
                  bool repeatedSources = false;
                  bool multiCarrySources = false;
                  for (const auto &group : sourceGroups) {
                    const Candidate *candidate =
                        group.front();
                    const auto kind =
                        candidate->second.kind;
                    if (
                        kind != FunctionContext::
                                    MemoryPoisonIncoming::
                                        Kind::Store &&
                        kind != FunctionContext::
                                    MemoryPoisonIncoming::
                                        Kind::Carry) {
                      validLeaves = false;
                      break;
                    }
                    carryLeaves +=
                        kind == FunctionContext::
                                    MemoryPoisonIncoming::
                                        Kind::Carry;
                    TreeCandidate treeCandidate;
                    treeCandidate.candidate = candidate;
                    for (const Candidate *endpoint : group) {
                      if (endpoint->second.kind != kind) {
                        validLeaves = false;
                        break;
                      }
                      const StoreInst *requiredStore =
                          kind == FunctionContext::
                                      MemoryPoisonIncoming::
                                          Kind::Store
                              ? endpoint->second.store
                              : nullptr;
                      if (
                          kind == FunctionContext::
                                      MemoryPoisonIncoming::
                                          Kind::Store &&
                          requiredStore == nullptr) {
                        validLeaves = false;
                        break;
                      }
                      auto arms = findConditionalArms(
                          endpoint, requiredStore);
                      if (arms.empty()) {
                        validLeaves = false;
                        break;
                      }
                      treeCandidate.endpointArms.push_back(
                          std::move(arms));
                    }
                    if (!validLeaves)
                      break;
                    bool conflictingPosition = false;
                    std::map<
                        const BranchInst *,
                        const BasicBlock *>
                        groupArms;
                    for (const auto &arms :
                         treeCandidate.endpointArms) {
                      for (const auto &arm : arms) {
                        auto existing =
                            groupArms.find(arm.branch);
                        if (
                            existing != groupArms.end() &&
                            existing->second !=
                                arm.successor) {
                          conflictingPosition = true;
                          break;
                        }
                        groupArms[arm.branch] =
                            arm.successor;
                      }
                      if (conflictingPosition)
                        break;
                    }
                    if (conflictingPosition) {
                      repeatedSources = true;
                      multiCarrySources |=
                          kind == FunctionContext::
                                      MemoryPoisonIncoming::
                                          Kind::Carry;
                      for (unsigned endpointIndex = 0;
                           endpointIndex < group.size();
                           ++endpointIndex) {
                        TreeCandidate expanded;
                        expanded.candidate =
                            group[endpointIndex];
                        expanded.endpointArms.push_back(
                            std::move(
                                treeCandidate.endpointArms[
                                    endpointIndex]));
                        treeCandidates.push_back(
                            std::move(expanded));
                      }
                    } else {
                      groupedSources |= group.size() > 1;
                      treeCandidates.push_back(
                          std::move(treeCandidate));
                    }
                  }
                  if (
                      validLeaves && carryLeaves == 1 &&
                      treeCandidates.size() >= 4 &&
                      treeCandidates.size() <= 8 &&
                      visitedBlocks <= 64) {
                    using TreeNode =
                        FunctionContext::
                            MemoryDefinednessTreeNode;
                    std::vector<TreeNode> tree;
                    bool forwarded = false;
                    std::function<std::pair<int, unsigned>(
                        const std::vector<unsigned> &)>
                        buildTree =
                            [&](const std::vector<unsigned>
                                    &members)
                                -> std::pair<int, unsigned> {
                              if (members.size() == 1) {
                                const auto &source =
                                    treeCandidates[
                                        members.front()]
                                        .candidate->second;
                                TreeNode leaf;
                                leaf.kind =
                                    source.kind ==
                                            FunctionContext::
                                                MemoryPoisonIncoming::
                                                    Kind::Store
                                        ? TreeNode::Kind::Store
                                        : TreeNode::Kind::Carry;
                                leaf.store = source.store;
                                tree.push_back(leaf);
                                return {
                                    static_cast<int>(
                                        tree.size() - 1),
                                    0};
                              }
                              const BranchInst *split =
                                  nullptr;
                              unsigned splitScore = 0;
                              bool foundSplit = false;
                              const auto &firstArms =
                                  treeCandidates[
                                      members.front()]
                                      .endpointArms.front();
                              for (unsigned armIndex = 0;
                                   armIndex <
                                   firstArms.size();
                                   ++armIndex) {
                                const auto &candidateArm =
                                    firstArms[armIndex];
                                bool sawTrue = false;
                                bool sawFalse = false;
                                bool commonBranch = true;
                                for (unsigned member :
                                     members) {
                                  std::optional<bool>
                                      sourceOnTrue;
                                  for (const auto &arms :
                                       treeCandidates[member]
                                           .endpointArms) {
                                    auto match =
                                        std::find_if(
                                            arms.begin(),
                                            arms.end(),
                                            [&](const auto &arm) {
                                              return
                                                  arm.branch ==
                                                  candidateArm
                                                      .branch;
                                            });
                                    if (match == arms.end()) {
                                      commonBranch = false;
                                      break;
                                    }
                                    bool endpointOnTrue =
                                        match->branch
                                            ->getSuccessor(0) ==
                                        match->successor;
                                    if (
                                        sourceOnTrue &&
                                        *sourceOnTrue !=
                                            endpointOnTrue) {
                                      commonBranch = false;
                                      break;
                                    }
                                    sourceOnTrue =
                                        endpointOnTrue;
                                  }
                                  if (!commonBranch)
                                    break;
                                  sawTrue |= *sourceOnTrue;
                                  sawFalse |= !*sourceOnTrue;
                                }
                                if (
                                    commonBranch && sawTrue &&
                                    sawFalse &&
                                    (!foundSplit ||
                                     armIndex >
                                         splitScore)) {
                                  split =
                                      candidateArm.branch;
                                  splitScore = armIndex;
                                  foundSplit = true;
                                }
                              }
                              if (!foundSplit)
                                return {-1, 0};
                              std::vector<unsigned>
                                  trueMembers;
                              std::vector<unsigned>
                                  falseMembers;
                              for (unsigned member : members) {
                                std::optional<bool>
                                    sourceOnTrue;
                                for (const auto &arms :
                                     treeCandidates[member]
                                         .endpointArms) {
                                  auto match =
                                      std::find_if(
                                          arms.begin(),
                                          arms.end(),
                                          [&](const auto &arm) {
                                            return
                                                arm.branch ==
                                                split;
                                          });
                                  if (match == arms.end())
                                    return {-1, 0};
                                  bool endpointOnTrue =
                                      split->getSuccessor(0) ==
                                      match->successor;
                                  if (
                                      sourceOnTrue &&
                                      *sourceOnTrue !=
                                          endpointOnTrue)
                                    return {-1, 0};
                                  sourceOnTrue =
                                      endpointOnTrue;
                                  forwarded |=
                                      match->forwarded;
                                }
                                (
                                    *sourceOnTrue
                                        ? trueMembers
                                        : falseMembers)
                                    .push_back(member);
                              }
                              auto trueTree =
                                  buildTree(trueMembers);
                              auto falseTree =
                                  buildTree(falseMembers);
                              unsigned depth =
                                  std::max(
                                      trueTree.second,
                                      falseTree.second) +
                                  1;
                              if (
                                  trueTree.first < 0 ||
                                  falseTree.first < 0 ||
                                  depth > 6)
                                return {-1, 0};
                              TreeNode select;
                              select.kind =
                                  TreeNode::Kind::Select;
                              select.condition =
                                  split->getCondition();
                              select.trueNode =
                                  trueTree.first;
                              select.falseNode =
                                  falseTree.first;
                              tree.push_back(select);
                              return {
                                  static_cast<int>(
                                      tree.size() - 1),
                                  depth};
                            };
                    std::vector<unsigned> allMembers;
                    allMembers.reserve(
                        treeCandidates.size());
                    for (unsigned index = 0;
                         index < treeCandidates.size();
                         ++index)
                      allMembers.push_back(index);
                    auto root =
                        buildTree(allMembers);
                    auto availableAtIncoming =
                        [&](const Value *value) {
                          if (
                              isa<Constant>(value) ||
                              isa<Argument>(value))
                            return true;
                          auto *producer =
                              dyn_cast<Instruction>(value);
                          return
                              producer != nullptr &&
                              dominators.dominates(
                                  producer,
                                  predecessor
                                      ->getTerminator());
                        };
                    bool available =
                        root.first >= 0 &&
                        root.second >= 3 &&
                        std::all_of(
                            tree.begin(), tree.end(),
                            [&](const TreeNode &node) {
                              if (
                                  node.kind ==
                                  TreeNode::Kind::Select)
                                return
                                    node.condition !=
                                        nullptr &&
                                    availableAtIncoming(
                                        node.condition);
                              return
                                  node.kind !=
                                      TreeNode::Kind::Store ||
                                  (node.store != nullptr &&
                                   availableAtIncoming(
                                       node.store
                                           ->getValueOperand()));
                            });
                    if (available) {
                      ReachingStore recursive;
                      recursive.valid = true;
                      recursive.kind =
                          FunctionContext::
                              MemoryPoisonIncoming::
                                  Kind::
                                      RecursiveConditionalTree;
                      recursive.store =
                          treeCandidates.front()
                              .candidate->second.store;
                      if (recursive.store == nullptr) {
                        auto firstStore = std::find_if(
                            treeCandidates.begin(),
                            treeCandidates.end(),
                            [](const auto &item) {
                              return
                                  item.candidate->second
                                      .store != nullptr;
                            });
                        recursive.store =
                            firstStore->candidate->second
                                .store;
                      }
                      for (const auto &group :
                           sourceGroups) {
                        for (const Candidate *item : group) {
                          const StoreInst *store =
                              item->second.store;
                          if (
                              store != nullptr &&
                              store != recursive.store &&
                              std::find(
                                  recursive
                                      .equivalentStores
                                      .begin(),
                                  recursive
                                      .equivalentStores
                                      .end(),
                                  store) ==
                                  recursive
                                      .equivalentStores
                                      .end())
                            recursive.equivalentStores
                                .push_back(store);
                        }
                      }
                      recursive.conditionalForwarded =
                          forwarded;
                      recursive.multiArmConditional = true;
                      recursive.nestedConditional = true;
                      recursive.conditionTree =
                          std::move(tree);
                      recursive.conditionTreeRoot =
                          root.first;
                      recursive.conditionTreeDepth =
                          root.second;
                      recursive.groupedRecursiveConditional =
                          groupedSources;
                      recursive.repeatedSourceRecursiveConditional =
                          repeatedSources;
                      recursive.multiCarryRecursiveConditional =
                          multiCarrySources;
                      active.erase(block);
                      return recursive;
                    }
                  }
                }
                active.erase(block);
                return {};
              };
      ReachingStore writer =
          findStore(predecessor);
      if (!writer.valid)
        return std::nullopt;
      incoming.push_back({
          predecessor, writer.store, writer.kind,
          writer.condition, writer.storeWhenTrue,
          writer.conditionalForwarded,
          writer.multiArmConditional,
          writer.equivalentDefinedStores,
          writer.sharedPoisonStores,
          std::move(writer.equivalentStores),
          writer.secondaryStore,
          writer.innerCondition,
          writer.firstStoreWhenTrue,
          writer.nestedConditional,
          std::move(writer.conditionTree),
          writer.conditionTreeRoot,
          writer.conditionTreeDepth,
          writer.groupedRecursiveConditional,
          writer.repeatedSourceRecursiveConditional,
          writer.multiCarryRecursiveConditional});
    }
    return incoming;
  }

  std::optional<std::vector<const LoadInst *>>
  poisonMergeLoadsForStore(
      const StoreInst &store) const {
    std::vector<const LoadInst *> loads;
    for (const Instruction &instruction :
         instructions(*store.getFunction())) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (
          load == nullptr ||
          load->getType() !=
              store.getValueOperand()->getType())
        continue;
      auto incoming =
          predecessorPoisonStores(*load);
      if (!incoming)
        continue;
      bool oneCommonStore =
          std::all_of(
              std::next(incoming->begin()),
              incoming->end(),
              [&](const auto &item) {
                return
                    item.store ==
                        incoming->front().store &&
                    item.kind ==
                        incoming->front().kind &&
                    item.condition ==
                        incoming->front().condition &&
                    item.storeWhenTrue ==
                        incoming->front().storeWhenTrue &&
                    item.conditionalForwarded ==
                        incoming->front().
                            conditionalForwarded &&
                    item.multiArmConditional ==
                        incoming->front().
                            multiArmConditional &&
                    item.equivalentDefinedStores ==
                        incoming->front().
                            equivalentDefinedStores &&
                    item.sharedPoisonStores ==
                        incoming->front().
                            sharedPoisonStores &&
                    item.equivalentStores.size() ==
                        incoming->front().
                            equivalentStores.size() &&
                    std::is_permutation(
                        item.equivalentStores.begin(),
                        item.equivalentStores.end(),
                        incoming->front().
                            equivalentStores.begin()) &&
                    item.secondaryStore ==
                        incoming->front().secondaryStore &&
                    item.innerCondition ==
                        incoming->front().innerCondition &&
                    item.firstStoreWhenTrue ==
                        incoming->front().
                            firstStoreWhenTrue &&
                    item.nestedConditional ==
                        incoming->front().nestedConditional &&
                    item.conditionTree ==
                        incoming->front().conditionTree &&
                    item.conditionTreeRoot ==
                        incoming->front().conditionTreeRoot &&
                    item.conditionTreeDepth ==
                        incoming->front().conditionTreeDepth &&
                    item.groupedRecursiveConditional ==
                        incoming->front().
                            groupedRecursiveConditional &&
                    item.repeatedSourceRecursiveConditional ==
                        incoming->front().
                            repeatedSourceRecursiveConditional &&
                    item.multiCarryRecursiveConditional ==
                        incoming->front().
                            multiCarryRecursiveConditional;
              });
      if (
          oneCommonStore ||
          std::none_of(
              incoming->begin(), incoming->end(),
              [&](const auto &item) {
                return
                    item.store == &store ||
                    std::find(
                        item.equivalentStores.begin(),
                        item.equivalentStores.end(),
                        &store) !=
                        item.equivalentStores.end();
              }))
        continue;
      if (loads.size() == 64)
        return std::nullopt;
      loads.push_back(load);
    }
    return loads.empty()
               ? std::nullopt
               : std::optional<
                     std::vector<const LoadInst *>>(
                     std::move(loads));
  }

  std::optional<std::vector<const LoadInst *>>
  boundedPoisonLoadsAfterStore(
      const StoreInst &store) const {
    std::vector<const LoadInst *> loads;
    std::set<const BasicBlock *> active;
    std::set<const BasicBlock *> completed;
    unsigned visitedBlocks = 0;
    std::function<bool(
        const BasicBlock *, BasicBlock::const_iterator)>
        scan =
            [&](const BasicBlock *block,
                BasicBlock::const_iterator iterator) {
              if (completed.count(block) != 0)
                return true;
              if (
                  ++visitedBlocks > 64 ||
                  !active.insert(block).second)
                return false;
              bool terminated = false;
              for (; iterator != block->end(); ++iterator) {
                const Instruction &instruction = *iterator;
                if (auto *load =
                        dyn_cast<LoadInst>(&instruction)) {
                  auto partial =
                      byteLanePoisonLoads.find(&store);
                  bool byteLaneSink =
                      partial !=
                          byteLanePoisonLoads.end() &&
                      std::find(
                          partial->second.begin(),
                          partial->second.end(), load) !=
                          partial->second.end();
                  if (
                      (!exactScalarMemoryPair(store, *load) &&
                       !byteLaneSink) ||
                      loads.size() == 64) {
                    active.erase(block);
                    return false;
                  }
                  if (
                      std::find(
                          loads.begin(), loads.end(), load) ==
                      loads.end())
                    loads.push_back(load);
                  continue;
                }
                if (auto *clobber =
                        dyn_cast<StoreInst>(&instruction)) {
                  if (
                      !clobber->isVolatile() &&
                      !clobber->isAtomic() &&
                      provablyDisjointScalarMemoryAccess(
                          store.getPointerOperand(),
                          store.getValueOperand()
                              ->getType(),
                          clobber->getPointerOperand(),
                          clobber->getValueOperand()
                              ->getType()))
                    continue;
                  if (
                      !clobber->isVolatile() &&
                      !clobber->isAtomic() &&
                      integerBits(
                          clobber->getValueOperand()
                              ->getType()) != 0 &&
                      byteLanePoisonLoads.find(&store) !=
                          byteLanePoisonLoads.end())
                    continue;
                  terminated =
                      !clobber->isVolatile() &&
                      !clobber->isAtomic() &&
                      clobber->getValueOperand()->getType() ==
                          store.getValueOperand()->getType() &&
                      exactMemoryAddress(
                          store.getPointerOperand(),
                          clobber->getPointerOperand());
                  active.erase(block);
                  if (terminated)
                    completed.insert(block);
                  return terminated;
                }
                if (instruction.mayReadOrWriteMemory()) {
                  active.erase(block);
                  return false;
                }
              }
              for (const BasicBlock *successor :
                   successors(block))
                if (!scan(successor, successor->begin())) {
                  active.erase(block);
                  return false;
                }
              active.erase(block);
              completed.insert(block);
              return true;
            };
    if (
        !scan(
            store.getParent(),
            std::next(store.getIterator())) ||
        loads.empty())
      return std::nullopt;
    for (const LoadInst *load : loads) {
      if (boundedPoisonStoreBeforeLoad(*load) == &store)
        continue;
      auto partial =
          byteLanePoisonLoads.find(&store);
      if (
          partial != byteLanePoisonLoads.end() &&
          std::find(
              partial->second.begin(),
              partial->second.end(), load) !=
              partial->second.end())
        continue;
      auto mergeStores =
          predecessorPoisonStores(*load);
      if (
          !mergeStores ||
          std::none_of(
              mergeStores->begin(), mergeStores->end(),
              [&](const auto &incoming) {
                return
                    incoming.store == &store ||
                    std::find(
                        incoming.equivalentStores.begin(),
                        incoming.equivalentStores.end(),
                        &store) !=
                        incoming.equivalentStores.end();
              }))
        return std::nullopt;
    }
    return loads;
  }

  const StoreInst *boundedPoisonStoreBeforeLoad(
      const LoadInst &load,
      bool *usedBranchMerge = nullptr) const {
    std::set<const BasicBlock *> active;
    unsigned visitedBlocks = 0;
    bool observedBranchMerge = false;
    std::function<const StoreInst *(
        const BasicBlock *, BasicBlock::const_iterator)>
        findStore =
            [&](const BasicBlock *block,
                BasicBlock::const_iterator boundary)
                -> const StoreInst * {
              if (
                  ++visitedBlocks > 64 ||
                  !active.insert(block).second)
                return nullptr;
              while (boundary != block->begin()) {
                --boundary;
                const Instruction &instruction = *boundary;
                if (auto *earlierLoad =
                        dyn_cast<LoadInst>(&instruction)) {
                  if (
                      earlierLoad->isVolatile() ||
                      earlierLoad->isAtomic() ||
                      earlierLoad->getType() != load.getType() ||
                      !exactMemoryAddress(
                          earlierLoad->getPointerOperand(),
                          load.getPointerOperand())) {
                    active.erase(block);
                    return nullptr;
                  }
                  continue;
                }
                if (auto *store =
                        dyn_cast<StoreInst>(&instruction)) {
                  active.erase(block);
                  return exactScalarMemoryPair(*store, load)
                             ? store
                             : nullptr;
                }
                if (instruction.mayReadOrWriteMemory()) {
                  active.erase(block);
                  return nullptr;
                }
              }
              if (pred_empty(block)) {
                active.erase(block);
                return nullptr;
              }
              observedBranchMerge |= pred_size(block) > 1;
              const StoreInst *common = nullptr;
              for (const BasicBlock *predecessor :
                   predecessors(block)) {
                observedBranchMerge |=
                    succ_size(predecessor) > 1;
                const StoreInst *candidate =
                    findStore(
                        predecessor, predecessor->end());
                if (
                    candidate == nullptr ||
                    (common != nullptr &&
                     candidate != common)) {
                  active.erase(block);
                  return nullptr;
                }
                common = candidate;
              }
              active.erase(block);
              return common;
            };
    const StoreInst *result =
        findStore(load.getParent(), load.getIterator());
    if (result != nullptr && usedBranchMerge != nullptr)
      *usedBranchMerge = observedBranchMerge;
    return result;
  }

  static bool mayCreateDeferredPoison(
      const BinaryOperator &binary) {
    bool wrapFlag =
        (binary.getOpcode() == Instruction::Add ||
         binary.getOpcode() == Instruction::Sub ||
         binary.getOpcode() == Instruction::Mul ||
         binary.getOpcode() == Instruction::Shl) &&
        (binary.hasNoUnsignedWrap() ||
         binary.hasNoSignedWrap());
    bool division =
        binary.getOpcode() == Instruction::UDiv ||
        binary.getOpcode() == Instruction::SDiv ||
        binary.getOpcode() == Instruction::URem ||
        binary.getOpcode() == Instruction::SRem;
    bool exact =
        (binary.getOpcode() == Instruction::UDiv ||
         binary.getOpcode() == Instruction::SDiv ||
         binary.getOpcode() == Instruction::LShr ||
         binary.getOpcode() == Instruction::AShr) &&
        binary.isExact();
    return division || binary.isShift() || wrapFlag || exact;
  }

  std::optional<std::vector<const CallBase *>>
  directCallsites(const Function &function) const {
    std::vector<const CallBase *> calls;
    for (const User *user : function.users()) {
      const auto *call = dyn_cast<CallBase>(user);
      if (
          call == nullptr ||
          call->getCalledFunction() != &function)
        return std::nullopt;
      calls.push_back(call);
      if (calls.size() > 64)
        return std::nullopt;
    }
    if (calls.empty())
      return std::nullopt;
    return calls;
  }

  bool hasOnlyDeferredPoisonFreezeSinks(
      const Value *value, bool *hasFanout = nullptr) const {
    std::set<const Value *> active;
    unsigned visited = 0;
    bool observedFanout = false;
    std::function<bool(const Value *)> reachesFreeze =
        [&](const Value *current) {
          if (
              current == nullptr || current->use_empty() ||
              ++visited > 256 ||
              !active.insert(current).second)
            return false;
          observedFanout |= !current->hasOneUse();
          bool result = true;
          for (const User *user : current->users()) {
            bool useReachesFreeze = false;
            if (auto *freeze = dyn_cast<FreezeInst>(user)) {
              useReachesFreeze =
                  freeze->getOperand(0) == current;
            } else if (auto *cast = dyn_cast<CastInst>(user)) {
              useReachesFreeze =
                  integerBits(cast->getType()) != 0 &&
                  integerBits(cast->getOperand(0)->getType()) != 0 &&
                  (cast->getOpcode() == Instruction::Trunc ||
                   cast->getOpcode() == Instruction::ZExt ||
                   cast->getOpcode() == Instruction::SExt ||
                   cast->getOpcode() == Instruction::BitCast) &&
                  reachesFreeze(cast);
            } else if (auto *binary =
                           dyn_cast<BinaryOperator>(user)) {
              useReachesFreeze =
                  integerBits(binary->getType()) != 0 &&
                  binaryOperator(binary->getOpcode()).has_value() &&
                  reachesFreeze(binary);
            } else if (auto *comparison =
                           dyn_cast<ICmpInst>(user)) {
              useReachesFreeze =
                  integerBits(
                      comparison->getOperand(0)->getType()) != 0 &&
                  reachesFreeze(comparison);
            } else if (auto *select =
                           dyn_cast<SelectInst>(user)) {
              useReachesFreeze =
                  integerBits(select->getType()) != 0 &&
                  select->getCondition()->getType()->isIntegerTy(1) &&
                  reachesFreeze(select);
            } else if (auto *phi = dyn_cast<PHINode>(user)) {
              useReachesFreeze =
                  integerBits(phi->getType()) != 0 &&
                  reachesFreeze(phi);
            } else if (auto *store = dyn_cast<StoreInst>(user)) {
              auto loads =
                  boundedPoisonLoadsAfterStore(*store);
              if (!loads)
                loads =
                    poisonMergeLoadsForStore(*store);
              if (
                  !loads &&
                  cyclicByteLanePoisonStores.count(
                      store) != 0) {
                auto cyclicLoads =
                    byteLanePoisonLoads.find(store);
                if (
                    cyclicLoads !=
                        byteLanePoisonLoads.end() &&
                    !cyclicLoads->second.empty())
                  loads = cyclicLoads->second;
              }
              useReachesFreeze =
                  store->getValueOperand() == current &&
                  loads &&
                  std::all_of(
                      loads->begin(), loads->end(),
                      [&](const LoadInst *load) {
                        return reachesFreeze(load);
                      });
            } else if (auto *returnInstruction =
                           dyn_cast<ReturnInst>(user)) {
              const Function *function =
                  returnInstruction->getFunction();
              if (
                  returnInstruction->getReturnValue() == current &&
                  function != nullptr) {
                auto calls = directCallsites(*function);
                useReachesFreeze =
                    calls &&
                    std::all_of(
                        calls->begin(), calls->end(),
                        [&](const CallBase *call) {
                          return
                              integerBits(call->getType()) != 0 &&
                              reachesFreeze(call);
                        });
              }
            } else if (auto *call = dyn_cast<CallBase>(user)) {
              Function *callee = call->getCalledFunction();
              useReachesFreeze =
                  callee != nullptr && callee->isIntrinsic() &&
                  callee->getIntrinsicID() ==
                      Intrinsic::ssa_copy &&
                  call->arg_size() == 1 &&
                  call->getArgOperand(0) == current &&
                  integerBits(call->getType()) != 0 &&
                  reachesFreeze(call);
              if (
                  !useReachesFreeze && callee != nullptr &&
                  !callee->isDeclaration() &&
                  directCallsites(*callee).has_value()) {
                bool matched = false;
                useReachesFreeze = true;
                for (unsigned index = 0;
                     index < call->arg_size(); ++index) {
                  if (call->getArgOperand(index) != current)
                    continue;
                  matched = true;
                  Argument *parameter = callee->getArg(index);
                  if (
                      parameter == nullptr ||
                      integerBits(parameter->getType()) == 0 ||
                      !reachesFreeze(parameter)) {
                    useReachesFreeze = false;
                    break;
                  }
                }
                useReachesFreeze &= matched;
              }
            }
            if (!useReachesFreeze) {
              result = false;
              break;
            }
          }
          active.erase(current);
          return result;
        };
    bool result = reachesFreeze(value);
    if (result && hasFanout != nullptr)
      *hasFanout = observedFanout;
    return result;
  }

  bool valueHasDeferredPoisonSource(
      const Value *value) const {
    std::set<const Value *> active;
    std::function<bool(const Value *, unsigned)> findSource =
        [&](const Value *current, unsigned depth) {
          if (
              current == nullptr || depth > 64 ||
              !active.insert(current).second)
            return false;
          bool result = false;
          if (auto *binary =
                  dyn_cast<BinaryOperator>(current)) {
            result =
                mayCreateDeferredPoison(*binary) &&
                hasOnlyDeferredPoisonFreezeSinks(binary);
          }
          auto *instruction =
              dyn_cast<Instruction>(current);
          bool transparent =
              instruction != nullptr &&
              (isa<BinaryOperator>(instruction) ||
               isa<ICmpInst>(instruction) ||
               isa<SelectInst>(instruction) ||
               isa<PHINode>(instruction) ||
               isa<CastInst>(instruction));
          if (auto *call =
                  dyn_cast_or_null<CallBase>(instruction)) {
            Function *callee = call->getCalledFunction();
            transparent =
                callee != nullptr && callee->isIntrinsic() &&
                callee->getIntrinsicID() ==
                    Intrinsic::ssa_copy;
          }
          if (!result && transparent)
            result = std::any_of(
                instruction->op_begin(),
                instruction->op_end(),
                [&](const Use &operandUse) {
                  return findSource(
                      operandUse.get(), depth + 1);
                });
          active.erase(current);
          return result;
        };
    return findSource(value, 0);
  }

  bool valueMayCreateDeferredPoison(
      const Value *value) const {
    std::set<const Value *> active;
    std::function<bool(const Value *, unsigned)> findSource =
        [&](const Value *current, unsigned depth) {
          if (
              current == nullptr || depth > 64 ||
              !active.insert(current).second)
            return false;
          bool result = false;
          if (auto *binary =
                  dyn_cast<BinaryOperator>(current))
            result = mayCreateDeferredPoison(*binary);
          if (auto *argument =
                  dyn_cast<Argument>(current))
            result =
                argumentHasDeferredPoison(*argument);
          auto *instruction =
              dyn_cast<Instruction>(current);
          bool transparent =
              instruction != nullptr &&
              (isa<BinaryOperator>(instruction) ||
               isa<ICmpInst>(instruction) ||
               isa<SelectInst>(instruction) ||
               isa<PHINode>(instruction) ||
               isa<CastInst>(instruction));
          if (auto *call =
                  dyn_cast_or_null<CallBase>(instruction)) {
            Function *callee = call->getCalledFunction();
            if (
                callee != nullptr &&
                !callee->isDeclaration() &&
                !callee->isIntrinsic())
              result =
                  functionHasDeferredPoisonReturn(
                      *callee);
            transparent =
                callee != nullptr && callee->isIntrinsic() &&
                callee->getIntrinsicID() ==
                    Intrinsic::ssa_copy;
          }
          if (!result && transparent)
            result = std::any_of(
                instruction->op_begin(),
                instruction->op_end(),
                [&](const Use &operandUse) {
                  return findSource(
                      operandUse.get(), depth + 1);
                });
          active.erase(current);
          return result;
        };
    return findSource(value, 0);
  }

  bool valueMayReceiveInterproceduralPoison(
      const Value *value) const {
    std::set<const Value *> active;
    std::function<bool(const Value *, unsigned)> findSource =
        [&](const Value *current, unsigned depth) {
          if (
              current == nullptr || depth > 64 ||
              !active.insert(current).second)
            return false;
          bool result = false;
          if (auto *argument =
                  dyn_cast<Argument>(current))
            result =
                argumentHasDeferredPoison(*argument);
          auto *instruction =
              dyn_cast<Instruction>(current);
          bool transparent =
              instruction != nullptr &&
              (isa<BinaryOperator>(instruction) ||
               isa<ICmpInst>(instruction) ||
               isa<SelectInst>(instruction) ||
               isa<PHINode>(instruction) ||
               isa<CastInst>(instruction));
          if (auto *call =
                  dyn_cast_or_null<CallBase>(instruction)) {
            Function *callee = call->getCalledFunction();
            if (
                callee != nullptr &&
                !callee->isDeclaration() &&
                !callee->isIntrinsic())
              result =
                  functionHasDeferredPoisonReturn(
                      *callee);
            transparent =
                callee != nullptr && callee->isIntrinsic() &&
                callee->getIntrinsicID() ==
                    Intrinsic::ssa_copy;
          }
          if (!result && transparent)
            result = std::any_of(
                instruction->op_begin(),
                instruction->op_end(),
                [&](const Use &operandUse) {
                  return findSource(
                      operandUse.get(), depth + 1);
                });
          active.erase(current);
          return result;
        };
    return findSource(value, 0);
  }

  std::optional<FunctionContext::ByteLaneMemoryComposition>
  boundedByteLaneMemoryComposition(
      const LoadInst &load) const {
    unsigned loadBits = integerBits(load.getType());
    uint64_t loadBytes =
        loadBits == 0
            ? 0
            : fixedStoreBytes(
                  M.getDataLayout(), load.getType());
    if (
        load.isVolatile() || load.isAtomic() ||
        loadBits < 16 || loadBits > 64 ||
        loadBits % 8 != 0 ||
        loadBytes != loadBits / 8 ||
        loadBytes < 2 || loadBytes > 8)
      return std::nullopt;

    int64_t loadOffset = 0;
    const Value *loadBase =
        GetPointerBaseWithConstantOffset(
            load.getPointerOperand()->stripPointerCasts(),
            loadOffset, M.getDataLayout())
            ->stripPointerCasts();
    if (loadBase == nullptr)
      return std::nullopt;

    FunctionContext::ByteLaneMemoryComposition composition;
    composition.load = &load;
    composition.bytes = loadBytes;
    composition.lanes.resize(loadBytes);
    std::vector<bool> covered(loadBytes, false);
    unsigned coveredCount = 0;
    unsigned visitedBlocks = 0;
    std::set<const BasicBlock *> active;
    auto complete = [&]() {
      return coveredCount == loadBytes;
    };
    std::function<bool(
        const BasicBlock *, BasicBlock::const_iterator)>
        scan =
            [&](const BasicBlock *block,
                BasicBlock::const_iterator boundary) {
              if (
                  ++visitedBlocks > 64 ||
                  !active.insert(block).second)
                return false;
              while (boundary != block->begin()) {
                --boundary;
                const Instruction &instruction = *boundary;
                if (!instruction.mayReadOrWriteMemory())
                  continue;
                if (auto *earlierLoad =
                        dyn_cast<LoadInst>(&instruction)) {
                  if (
                      earlierLoad->isVolatile() ||
                      earlierLoad->isAtomic()) {
                    active.erase(block);
                    return false;
                  }
                  continue;
                }
                auto *store =
                    dyn_cast<StoreInst>(&instruction);
                if (store == nullptr ||
                    store->isVolatile() ||
                    store->isAtomic()) {
                  active.erase(block);
                  return false;
                }
                unsigned storeBits =
                    integerBits(
                        store->getValueOperand()->getType());
                uint64_t storeBytes =
                    storeBits == 0
                        ? 0
                        : fixedStoreBytes(
                              M.getDataLayout(),
                              store->getValueOperand()
                                  ->getType());
                if (
                    storeBits == 0 || storeBits > 64 ||
                    storeBits % 8 != 0 ||
                    storeBytes != storeBits / 8 ||
                    storeBytes == 0 || storeBytes > 8) {
                  active.erase(block);
                  return false;
                }
                if (provablyDisjointScalarMemoryAccess(
                        store->getPointerOperand(),
                        store->getValueOperand()->getType(),
                        load.getPointerOperand(),
                        load.getType()))
                  continue;
                int64_t storeOffset = 0;
                const Value *storeBase =
                    GetPointerBaseWithConstantOffset(
                        store->getPointerOperand()
                            ->stripPointerCasts(),
                        storeOffset, M.getDataLayout())
                        ->stripPointerCasts();
                if (storeBase != loadBase) {
                  active.erase(block);
                  return false;
                }
                __int128 loadBegin = loadOffset;
                __int128 loadEnd =
                    loadBegin + loadBytes;
                __int128 storeBegin = storeOffset;
                __int128 storeEnd =
                    storeBegin + storeBytes;
                if (
                    storeEnd <= loadBegin ||
                    loadEnd <= storeBegin) {
                  active.erase(block);
                  return false;
                }
                for (unsigned lane = 0;
                     lane < loadBytes; ++lane) {
                  if (covered[lane])
                    continue;
                  __int128 address =
                      loadBegin + lane;
                  if (
                      address < storeBegin ||
                      address >= storeEnd)
                    continue;
                  composition.lanes[lane] = {
                      store,
                      static_cast<unsigned>(
                          address - storeBegin)};
                  covered[lane] = true;
                  ++coveredCount;
                }
                if (complete()) {
                  active.erase(block);
                  return true;
                }
              }
              if (pred_empty(block)) {
                if (!hasDefinedScalarInitialDefinition(load)) {
                  active.erase(block);
                  return false;
                }
                for (unsigned lane = 0;
                     lane < loadBytes; ++lane) {
                  if (covered[lane])
                    continue;
                  composition.lanes[lane] = {
                      nullptr, lane};
                  covered[lane] = true;
                  ++coveredCount;
                }
                composition.initial = true;
                active.erase(block);
                return true;
              }
              if (pred_size(block) != 1) {
                active.erase(block);
                return false;
              }
              const BasicBlock *predecessor =
                  *pred_begin(block);
              bool result =
                  scan(predecessor, predecessor->end());
              active.erase(block);
              return result;
            };
    if (!scan(load.getParent(), load.getIterator()) ||
        !complete())
      return std::nullopt;

    std::set<const StoreInst *> sources;
    bool partial = false;
    for (const auto &lane : composition.lanes) {
      sources.insert(lane.store);
      if (lane.store == nullptr)
        continue;
      uint64_t storeBytes =
          fixedStoreBytes(
              M.getDataLayout(),
              lane.store->getValueOperand()->getType());
      int64_t storeOffset = 0;
      GetPointerBaseWithConstantOffset(
          lane.store->getPointerOperand()
              ->stripPointerCasts(),
          storeOffset, M.getDataLayout());
      partial |=
          storeOffset != loadOffset ||
          storeBytes != loadBytes;
      composition.crossBlock |=
          lane.store->getParent() !=
          load.getParent();
    }
    if (!partial || sources.size() < 2)
      return std::nullopt;
    return composition;
  }

  void prepareByteLaneMemoryCompositions(
      Function &function, FunctionContext &context) {
    for (Instruction &instruction :
         instructions(function))
      if (auto *store =
              dyn_cast<StoreInst>(&instruction))
        byteLanePoisonLoads.erase(store);

    std::vector<
        FunctionContext::ByteLaneMemoryComposition>
        candidates;
    for (Instruction &instruction :
         instructions(function)) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (load == nullptr)
        continue;
      auto composition =
          boundedByteLaneMemoryComposition(*load);
      if (!composition)
        continue;
      for (const auto &lane : composition->lanes) {
        if (lane.store == nullptr)
          continue;
        auto &loads =
            byteLanePoisonLoads[lane.store];
        if (
            std::find(
                loads.begin(), loads.end(), load) ==
            loads.end())
          loads.push_back(load);
      }
      candidates.push_back(std::move(*composition));
    }

    std::vector<
        FunctionContext::ByteLaneMemoryComposition>
        accepted;
    for (auto &composition : candidates) {
      if (
          !hasOnlyDeferredPoisonFreezeSinks(
              composition.load))
        continue;
      bool hasPotentialPoison =
          std::any_of(
              composition.lanes.begin(),
              composition.lanes.end(),
              [&](const auto &lane) {
                return
                    lane.store != nullptr &&
                    valueMayCreateDeferredPoison(
                        lane.store
                            ->getValueOperand());
              });
      if (!hasPotentialPoison)
        continue;
      accepted.push_back(
          std::move(composition));
    }

    for (Instruction &instruction :
         instructions(function))
      if (auto *store =
              dyn_cast<StoreInst>(&instruction))
        byteLanePoisonLoads.erase(store);
    for (auto &composition : accepted) {
      composition.defined =
          context.values.lookup(composition.load) +
          "__byte_defined";
      unsigned index =
          static_cast<unsigned>(
              context.byteLaneCompositions.size());
      context.byteLaneCompositionIndices[
          composition.load] = index;
      context.poisonConditions[
          composition.load] =
          composition.defined;
      for (const auto &lane : composition.lanes) {
        const StoreInst *store = lane.store;
        if (store == nullptr)
          continue;
        if (
            context.byteLaneStoreIds
                .find(store) ==
            context.byteLaneStoreIds.end())
          context.byteLaneStoreIds[store] =
              context.temporaryName(
                  "byte_lane_store_");
        if (
            valueMayCreateDeferredPoison(
                store->getValueOperand()) &&
            context.byteLaneStoreDefinedNames
                .find(store) ==
                context.byteLaneStoreDefinedNames.end())
          context.byteLaneStoreDefinedNames[store] =
              context.temporaryName(
                  "byte_lane_store_defined_");
        auto &loads =
            byteLanePoisonLoads[store];
        if (
            std::find(
                loads.begin(), loads.end(),
                composition.load) ==
            loads.end())
          loads.push_back(composition.load);
      }
      context.byteLaneCompositions.push_back(
          std::move(composition));
    }
  }

  std::optional<FunctionContext::ByteLaneMemoryPhi>
  boundedByteLaneMemoryPhi(
      const LoadInst &load) const {
    const BasicBlock *merge = load.getParent();
    unsigned loadBits = integerBits(load.getType());
    uint64_t loadBytes =
        loadBits == 0
            ? 0
            : fixedStoreBytes(
                  M.getDataLayout(), load.getType());
    if (
        load.isVolatile() || load.isAtomic() ||
        loadBits < 16 || loadBits > 64 ||
        loadBits % 8 != 0 ||
        loadBytes != loadBits / 8 ||
        loadBytes < 2 || loadBytes > 8 ||
        pred_size(merge) < 2 ||
        pred_size(merge) > 64)
      return std::nullopt;
    for (const BasicBlock *predecessor :
         predecessors(merge))
      if (succ_size(predecessor) != 1)
        return std::nullopt;

    for (const Instruction &instruction : *merge) {
      if (&instruction == &load)
        break;
      if (!instruction.mayReadOrWriteMemory())
        continue;
      if (auto *earlierLoad =
              dyn_cast<LoadInst>(&instruction)) {
        if (
            !earlierLoad->isVolatile() &&
            !earlierLoad->isAtomic())
          continue;
      } else if (auto *store =
                     dyn_cast<StoreInst>(
                         &instruction)) {
        if (
            !store->isVolatile() &&
            !store->isAtomic() &&
            provablyDisjointScalarMemoryAccess(
                store->getPointerOperand(),
                store->getValueOperand()->getType(),
                load.getPointerOperand(),
                load.getType()))
          continue;
      }
      return std::nullopt;
    }

    int64_t loadOffset = 0;
    const Value *loadBase =
        GetPointerBaseWithConstantOffset(
            load.getPointerOperand()->stripPointerCasts(),
            loadOffset, M.getDataLayout())
            ->stripPointerCasts();
    FunctionContext::ByteLaneMemoryPhi phi;
    phi.load = &load;
    phi.bytes = loadBytes;
    unsigned visitedBlocks = 0;
    bool partial = false;
    for (const BasicBlock *endpoint :
         predecessors(merge)) {
      FunctionContext::ByteLaneMemoryIncoming incoming;
      incoming.block = endpoint;
      incoming.lanes.resize(loadBytes);
      std::vector<bool> covered(loadBytes, false);
      unsigned coveredCount = 0;
      std::set<const BasicBlock *> active;
      auto complete = [&]() {
        return coveredCount == loadBytes;
      };
      std::function<bool(
          const BasicBlock *,
          BasicBlock::const_iterator)>
          scan =
              [&](const BasicBlock *block,
                  BasicBlock::const_iterator boundary) {
                if (
                    ++visitedBlocks > 64 ||
                    !active.insert(block).second)
                  return false;
                while (boundary != block->begin()) {
                  --boundary;
                  const Instruction &instruction =
                      *boundary;
                  if (!instruction.mayReadOrWriteMemory())
                    continue;
                  if (auto *earlierLoad =
                          dyn_cast<LoadInst>(
                              &instruction)) {
                    if (
                        earlierLoad->isVolatile() ||
                        earlierLoad->isAtomic()) {
                      active.erase(block);
                      return false;
                    }
                    continue;
                  }
                  auto *store =
                      dyn_cast<StoreInst>(
                          &instruction);
                  if (
                      store == nullptr ||
                      store->isVolatile() ||
                      store->isAtomic()) {
                    active.erase(block);
                    return false;
                  }
                  unsigned storeBits =
                      integerBits(
                          store->getValueOperand()
                              ->getType());
                  uint64_t storeBytes =
                      storeBits == 0
                          ? 0
                          : fixedStoreBytes(
                                M.getDataLayout(),
                                store->getValueOperand()
                                    ->getType());
                  if (
                      storeBits == 0 ||
                      storeBits > 64 ||
                      storeBits % 8 != 0 ||
                      storeBytes != storeBits / 8 ||
                      storeBytes == 0 ||
                      storeBytes > 8) {
                    active.erase(block);
                    return false;
                  }
                  if (provablyDisjointScalarMemoryAccess(
                          store->getPointerOperand(),
                          store->getValueOperand()
                              ->getType(),
                          load.getPointerOperand(),
                          load.getType()))
                    continue;
                  int64_t storeOffset = 0;
                  const Value *storeBase =
                      GetPointerBaseWithConstantOffset(
                          store->getPointerOperand()
                              ->stripPointerCasts(),
                          storeOffset,
                          M.getDataLayout())
                          ->stripPointerCasts();
                  if (storeBase != loadBase) {
                    active.erase(block);
                    return false;
                  }
                  __int128 loadBegin = loadOffset;
                  __int128 loadEnd =
                      loadBegin + loadBytes;
                  __int128 storeBegin = storeOffset;
                  __int128 storeEnd =
                      storeBegin + storeBytes;
                  if (
                      storeEnd <= loadBegin ||
                      loadEnd <= storeBegin) {
                    active.erase(block);
                    return false;
                  }
                  partial |=
                      storeOffset != loadOffset ||
                      storeBytes != loadBytes;
                  for (unsigned lane = 0;
                       lane < loadBytes; ++lane) {
                    if (covered[lane])
                      continue;
                    __int128 address =
                        loadBegin + lane;
                    if (
                        address < storeBegin ||
                        address >= storeEnd)
                      continue;
                    incoming.lanes[lane] = {
                        store,
                        static_cast<unsigned>(
                            address - storeBegin)};
                    covered[lane] = true;
                    ++coveredCount;
                  }
                  if (complete()) {
                    active.erase(block);
                    return true;
                  }
                }
                if (pred_empty(block)) {
                  if (
                      !hasDefinedScalarInitialDefinition(
                          load)) {
                    active.erase(block);
                    return false;
                  }
                  for (unsigned lane = 0;
                       lane < loadBytes; ++lane) {
                    if (covered[lane])
                      continue;
                    incoming.lanes[lane] = {
                        nullptr, lane};
                    covered[lane] = true;
                    ++coveredCount;
                  }
                  incoming.initial = true;
                  active.erase(block);
                  return true;
                }
                if (pred_size(block) != 1) {
                  active.erase(block);
                  return false;
                }
                const BasicBlock *predecessor =
                    *pred_begin(block);
                bool result =
                    scan(
                        predecessor,
                        predecessor->end());
                active.erase(block);
                return result;
              };
      if (
          !scan(endpoint, endpoint->end()) ||
          !complete())
        return std::nullopt;
      phi.incoming.push_back(
          std::move(incoming));
    }
    if (!partial)
      return std::nullopt;
    return phi;
  }

  void prepareByteLaneMemoryPhis(
      Function &function, FunctionContext &context) {
    std::vector<FunctionContext::ByteLaneMemoryPhi>
        candidates;
    for (Instruction &instruction :
         instructions(function)) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (
          load == nullptr ||
          context.byteLaneCompositionIndices
                  .find(load) !=
              context.byteLaneCompositionIndices.end())
        continue;
      auto phi =
          boundedByteLaneMemoryPhi(*load);
      if (!phi)
        continue;
      for (const auto &incoming :
           phi->incoming)
        for (const auto &lane :
             incoming.lanes) {
          if (lane.store == nullptr)
            continue;
          auto &loads =
              byteLanePoisonLoads[lane.store];
          if (
              std::find(
                  loads.begin(), loads.end(),
                  load) == loads.end())
            loads.push_back(load);
        }
      candidates.push_back(std::move(*phi));
    }

    std::vector<FunctionContext::ByteLaneMemoryPhi>
        accepted;
    for (auto &phi : candidates) {
      if (
          !hasOnlyDeferredPoisonFreezeSinks(
              phi.load))
        continue;
      bool hasPotentialPoison = false;
      for (const auto &incoming : phi.incoming)
        hasPotentialPoison |=
            std::any_of(
                incoming.lanes.begin(),
                incoming.lanes.end(),
                [&](const auto &lane) {
                  return
                      lane.store != nullptr &&
                      valueMayCreateDeferredPoison(
                          lane.store
                              ->getValueOperand());
                });
      if (hasPotentialPoison)
        accepted.push_back(std::move(phi));
    }

    for (Instruction &instruction :
         instructions(function))
      if (auto *store =
              dyn_cast<StoreInst>(&instruction))
        byteLanePoisonLoads.erase(store);
    auto registerStore =
        [&](const StoreInst *store,
            const LoadInst *load) {
          if (store == nullptr)
            return;
          if (
              context.byteLaneStoreIds.find(store) ==
              context.byteLaneStoreIds.end())
            context.byteLaneStoreIds[store] =
                context.temporaryName(
                    "byte_lane_store_");
          if (
              valueMayCreateDeferredPoison(
                  store->getValueOperand()) &&
              context.byteLaneStoreDefinedNames
                      .find(store) ==
                  context.byteLaneStoreDefinedNames.end())
            context.byteLaneStoreDefinedNames[store] =
                context.temporaryName(
                    "byte_lane_store_defined_");
          auto &loads =
              byteLanePoisonLoads[store];
          if (
              std::find(
                  loads.begin(), loads.end(),
                  load) == loads.end())
            loads.push_back(load);
        };
    for (const auto &composition :
         context.byteLaneCompositions)
      for (const auto &lane :
           composition.lanes)
        registerStore(
            lane.store, composition.load);
    for (auto &phi : accepted) {
      phi.defined =
          context.values.lookup(phi.load) +
          "__byte_phi_defined";
      unsigned index =
          static_cast<unsigned>(
              context.byteLanePhis.size());
      context.byteLanePhiIndices[phi.load] =
          index;
      context.poisonConditions[phi.load] =
          phi.defined;
      for (const auto &incoming :
           phi.incoming) {
        context.ensureEdgeBlock(
            incoming.block,
            phi.load->getParent());
        for (const auto &lane :
             incoming.lanes)
          registerStore(
              lane.store, phi.load);
      }
      context.byteLanePhis.push_back(
          std::move(phi));
    }
  }

  std::optional<
      FunctionContext::ConditionalCyclicByteLaneTransfer>
  boundedConditionalCyclicByteLaneTransfer(
      const BasicBlock &join, const LoadInst &load,
      const Value *loadBase, int64_t loadOffset,
      uint64_t loadBytes) const {
    if (pred_size(&join) != 2)
      return std::nullopt;
    const BasicBlock *firstArm =
        *pred_begin(&join);
    const BasicBlock *secondArm =
        *std::next(pred_begin(&join));
    if (
        succ_size(firstArm) != 1 ||
        succ_size(secondArm) != 1 ||
        *succ_begin(firstArm) != &join ||
        *succ_begin(secondArm) != &join ||
        firstArm == secondArm)
      return std::nullopt;

    struct ArmPath {
      const BranchInst *branch = nullptr;
      const BasicBlock *successor = nullptr;
      std::vector<const BasicBlock *> blocks;
    };
    auto pathForEndpoint =
        [](const BasicBlock *endpoint)
            -> std::optional<ArmPath> {
          std::vector<const BasicBlock *> reversed{
              endpoint};
          std::set<const BasicBlock *> seen{
              endpoint};
          const BasicBlock *current = endpoint;
          for (unsigned depth = 0;
               depth < 16; ++depth) {
            if (pred_size(current) != 1)
              return std::nullopt;
            const BasicBlock *parent =
                *pred_begin(current);
            auto *branch =
                dyn_cast<BranchInst>(
                    parent->getTerminator());
            if (branch == nullptr)
              return std::nullopt;
            if (branch->isConditional()) {
              if (
                  branch->getSuccessor(0) !=
                      current &&
                  branch->getSuccessor(1) !=
                      current)
                return std::nullopt;
              std::reverse(
                  reversed.begin(),
                  reversed.end());
              return ArmPath{
                  branch, current,
                  std::move(reversed)};
            }
            if (
                branch->getNumSuccessors() != 1 ||
                branch->getSuccessor(0) !=
                    current ||
                !seen.insert(parent).second)
              return std::nullopt;
            current = parent;
            reversed.push_back(current);
          }
          return std::nullopt;
        };
    auto firstPath =
        pathForEndpoint(firstArm);
    auto secondPath =
        pathForEndpoint(secondArm);
    if (
        !firstPath || !secondPath ||
        firstPath->branch !=
            secondPath->branch ||
        firstPath->successor ==
            secondPath->successor)
      return std::nullopt;
    const BranchInst *branch =
        firstPath->branch;
    if (
        !((branch->getSuccessor(0) ==
               firstPath->successor &&
           branch->getSuccessor(1) ==
               secondPath->successor) ||
          (branch->getSuccessor(0) ==
               secondPath->successor &&
           branch->getSuccessor(1) ==
               firstPath->successor)))
      return std::nullopt;

    struct ArmWriter {
      bool valid = true;
      const StoreInst *store = nullptr;
      int64_t offset = 0;
      uint64_t bytes = 0;
    };
    auto writerForArm =
        [&](const std::vector<
                const BasicBlock *> &blocks) {
          ArmWriter result;
          for (const BasicBlock *block : blocks)
            for (const Instruction &instruction :
                 *block) {
            if (!instruction.mayReadOrWriteMemory())
              continue;
            if (auto *armLoad =
                    dyn_cast<LoadInst>(
                        &instruction)) {
              if (
                  armLoad->isVolatile() ||
                  armLoad->isAtomic() ||
                  !provablyDisjointScalarMemoryAccess(
                      armLoad->getPointerOperand(),
                      armLoad->getType(),
                      load.getPointerOperand(),
                      load.getType())) {
                result.valid = false;
                return result;
              }
              continue;
            }
            auto *store =
                dyn_cast<StoreInst>(&instruction);
            if (
                store == nullptr ||
                store->isVolatile() ||
                store->isAtomic()) {
              result.valid = false;
              return result;
            }
            if (provablyDisjointScalarMemoryAccess(
                    store->getPointerOperand(),
                    store->getValueOperand()
                        ->getType(),
                    load.getPointerOperand(),
                    load.getType()))
              continue;
            unsigned storeBits = integerBits(
                store->getValueOperand()->getType());
            uint64_t storeBytes =
                storeBits == 0
                    ? 0
                    : fixedStoreBytes(
                          M.getDataLayout(),
                          store->getValueOperand()
                              ->getType());
            int64_t storeOffset = 0;
            const Value *storeBase =
                GetPointerBaseWithConstantOffset(
                    store->getPointerOperand()
                        ->stripPointerCasts(),
                    storeOffset, M.getDataLayout())
                    ->stripPointerCasts();
            __int128 loadBegin = loadOffset;
            __int128 loadEnd =
                loadBegin + loadBytes;
            __int128 storeBegin = storeOffset;
            __int128 storeEnd =
                storeBegin + storeBytes;
            if (
                storeBits == 0 ||
                storeBits > 64 ||
                storeBits % 8 != 0 ||
                storeBytes != storeBits / 8 ||
                storeBytes == 0 ||
                storeBytes > 8 ||
                storeBase != loadBase ||
                storeEnd <= loadBegin ||
                loadEnd <= storeBegin ||
                result.store != nullptr) {
              result.valid = false;
              return result;
            }
            result.store = store;
            result.offset = storeOffset;
            result.bytes = storeBytes;
            }
          return result;
        };
    ArmWriter first =
        writerForArm(firstPath->blocks);
    ArmWriter second =
        writerForArm(secondPath->blocks);
    if (!first.valid || !second.valid)
      return std::nullopt;
    const BasicBlock *storeArm = nullptr;
    const BasicBlock *carryArm = nullptr;
    const BasicBlock *storeSuccessor = nullptr;
    const BasicBlock *carrySuccessor = nullptr;
    ArmWriter writer;
    if (
        first.store != nullptr &&
        second.store == nullptr) {
      storeArm = firstArm;
      carryArm = secondArm;
      storeSuccessor =
          firstPath->successor;
      carrySuccessor =
          secondPath->successor;
      writer = first;
    } else if (
        second.store != nullptr &&
        first.store == nullptr) {
      storeArm = secondArm;
      carryArm = firstArm;
      storeSuccessor =
          secondPath->successor;
      carrySuccessor =
          firstPath->successor;
      writer = second;
    } else {
      return std::nullopt;
    }
    if (
        writer.offset == loadOffset &&
        writer.bytes == loadBytes)
      return std::nullopt;

    FunctionContext::
        ConditionalCyclicByteLaneTransfer
            transfer;
    transfer.branch = branch;
    transfer.storeArm = storeArm;
    transfer.carryArm = carryArm;
    transfer.storeSuccessor =
        storeSuccessor;
    transfer.carrySuccessor =
        carrySuccessor;
    transfer.join = &join;
    transfer.store = writer.store;
    transfer.storeWhenTrue =
        branch->getSuccessor(0) ==
        storeSuccessor;
    transfer.forwarded =
        storeArm != storeSuccessor ||
        carryArm != carrySuccessor;
    transfer.lanes.resize(loadBytes);
    __int128 loadBegin = loadOffset;
    __int128 storeBegin = writer.offset;
    __int128 storeEnd =
        storeBegin + writer.bytes;
    for (unsigned lane = 0;
         lane < loadBytes; ++lane) {
      __int128 address = loadBegin + lane;
      if (
          address >= storeBegin &&
          address < storeEnd) {
        transfer.lanes[lane] = {
            FunctionContext::
                CyclicByteLaneMemorySource::
                    Kind::Store,
            writer.store,
            static_cast<unsigned>(
                address - storeBegin)};
      } else {
        transfer.lanes[lane] = {
            FunctionContext::
                CyclicByteLaneMemorySource::
                    Kind::Carry,
            nullptr, lane};
      }
    }
    return transfer;
  }

  std::optional<
      FunctionContext::MultiArmCyclicByteLaneTransfer>
  boundedMultiArmCyclicByteLaneTransfer(
      const BasicBlock &join, const LoadInst &load,
      const Value *loadBase, int64_t loadOffset,
      uint64_t loadBytes) const {
    if (pred_size(&join) != 3)
      return std::nullopt;

    struct ArmPath {
      const BranchInst *branch = nullptr;
      const BasicBlock *successor = nullptr;
      const BasicBlock *endpoint = nullptr;
      std::vector<const BasicBlock *> blocks;
    };
    auto pathForEndpoint =
        [](const BasicBlock *endpoint)
            -> std::optional<ArmPath> {
          std::vector<const BasicBlock *> reversed{
              endpoint};
          std::set<const BasicBlock *> seen{
              endpoint};
          const BasicBlock *current = endpoint;
          for (unsigned depth = 0;
               depth < 16; ++depth) {
            if (pred_size(current) != 1)
              return std::nullopt;
            const BasicBlock *parent =
                *pred_begin(current);
            auto *branch =
                dyn_cast<BranchInst>(
                    parent->getTerminator());
            if (branch == nullptr)
              return std::nullopt;
            if (branch->isConditional()) {
              if (
                  branch->getSuccessor(0) !=
                      current &&
                  branch->getSuccessor(1) !=
                      current)
                return std::nullopt;
              std::reverse(
                  reversed.begin(),
                  reversed.end());
              return ArmPath{
                  branch, current, endpoint,
                  std::move(reversed)};
            }
            if (
                branch->getNumSuccessors() != 1 ||
                branch->getSuccessor(0) !=
                    current ||
                !seen.insert(parent).second)
              return std::nullopt;
            current = parent;
            reversed.push_back(current);
          }
          return std::nullopt;
        };

    std::vector<ArmPath> paths;
    for (const BasicBlock *endpoint :
         predecessors(&join)) {
      if (
          endpoint == &join ||
          succ_size(endpoint) != 1 ||
          *succ_begin(endpoint) != &join)
        return std::nullopt;
      auto path = pathForEndpoint(endpoint);
      if (!path)
        return std::nullopt;
      paths.push_back(std::move(*path));
    }
    if (paths.size() != 3)
      return std::nullopt;
    for (size_t first = 0;
         first < paths.size(); ++first)
      for (size_t second = first + 1;
           second < paths.size(); ++second)
        for (const BasicBlock *block :
             paths[first].blocks)
          if (
              std::find(
                  paths[second].blocks.begin(),
                  paths[second].blocks.end(),
                  block) !=
              paths[second].blocks.end())
            return std::nullopt;
    std::map<
        const BranchInst *,
        std::vector<const ArmPath *>>
        branchPaths;
    for (const ArmPath &path : paths)
      branchPaths[path.branch].push_back(&path);
    if (branchPaths.size() != 2)
      return std::nullopt;
    const BranchInst *rootBranch = nullptr;
    const BranchInst *innerBranch = nullptr;
    const ArmPath *rootPath = nullptr;
    for (const auto &[branch, armPaths] :
         branchPaths) {
      if (armPaths.size() == 1) {
        if (rootBranch != nullptr)
          return std::nullopt;
        rootBranch = branch;
        rootPath = armPaths.front();
      } else if (armPaths.size() == 2) {
        if (innerBranch != nullptr)
          return std::nullopt;
        innerBranch = branch;
      } else {
        return std::nullopt;
      }
    }
    if (
        rootBranch == nullptr ||
        innerBranch == nullptr ||
        rootPath == nullptr)
      return std::nullopt;
    const BasicBlock *innerBlock =
        innerBranch->getParent();
    if (
        pred_size(innerBlock) != 1 ||
        *pred_begin(innerBlock) !=
            rootBranch->getParent() ||
        !((rootBranch->getSuccessor(0) ==
               rootPath->successor &&
           rootBranch->getSuccessor(1) ==
               innerBlock) ||
          (rootBranch->getSuccessor(1) ==
               rootPath->successor &&
           rootBranch->getSuccessor(0) ==
               innerBlock)))
      return std::nullopt;
    const ArmPath *innerTruePath = nullptr;
    const ArmPath *innerFalsePath = nullptr;
    for (const ArmPath *path :
         branchPaths[innerBranch]) {
      if (
          path->successor ==
          innerBranch->getSuccessor(0))
        innerTruePath = path;
      if (
          path->successor ==
          innerBranch->getSuccessor(1))
        innerFalsePath = path;
    }
    if (
        innerTruePath == nullptr ||
        innerFalsePath == nullptr ||
        innerTruePath == innerFalsePath)
      return std::nullopt;

    struct ArmWriter {
      bool valid = true;
      const StoreInst *store = nullptr;
      int64_t offset = 0;
      uint64_t bytes = 0;
    };
    auto writerForArm =
        [&](const std::vector<
                const BasicBlock *> &blocks) {
          ArmWriter result;
          for (const BasicBlock *block : blocks)
            for (const Instruction &instruction :
                 *block) {
            if (!instruction.mayReadOrWriteMemory())
              continue;
            if (auto *armLoad =
                    dyn_cast<LoadInst>(
                        &instruction)) {
              if (
                  armLoad->isVolatile() ||
                  armLoad->isAtomic() ||
                  !provablyDisjointScalarMemoryAccess(
                      armLoad->getPointerOperand(),
                      armLoad->getType(),
                      load.getPointerOperand(),
                      load.getType())) {
                result.valid = false;
                return result;
              }
              continue;
            }
            auto *store =
                dyn_cast<StoreInst>(&instruction);
            if (
                store == nullptr ||
                store->isVolatile() ||
                store->isAtomic()) {
              result.valid = false;
              return result;
            }
            if (provablyDisjointScalarMemoryAccess(
                    store->getPointerOperand(),
                    store->getValueOperand()
                        ->getType(),
                    load.getPointerOperand(),
                    load.getType()))
              continue;
            unsigned storeBits = integerBits(
                store->getValueOperand()->getType());
            uint64_t storeBytes =
                storeBits == 0
                    ? 0
                    : fixedStoreBytes(
                          M.getDataLayout(),
                          store->getValueOperand()
                              ->getType());
            int64_t storeOffset = 0;
            const Value *storeBase =
                GetPointerBaseWithConstantOffset(
                    store->getPointerOperand()
                        ->stripPointerCasts(),
                    storeOffset, M.getDataLayout())
                    ->stripPointerCasts();
            __int128 loadBegin = loadOffset;
            __int128 loadEnd =
                loadBegin + loadBytes;
            __int128 storeBegin = storeOffset;
            __int128 storeEnd =
                storeBegin + storeBytes;
            if (
                storeBits == 0 ||
                storeBits > 64 ||
                storeBits % 8 != 0 ||
                storeBytes != storeBits / 8 ||
                storeBytes == 0 ||
                storeBytes > 8 ||
                storeBase != loadBase ||
                storeEnd <= loadBegin ||
                loadEnd <= storeBegin ||
                result.store != nullptr ||
                (storeOffset == loadOffset &&
                 storeBytes == loadBytes)) {
              result.valid = false;
              return result;
            }
            result.store = store;
            result.offset = storeOffset;
            result.bytes = storeBytes;
            }
          return result;
        };

    std::array<std::pair<
        FunctionContext::MultiArmCyclicByteLaneArm::
            Route,
        const ArmPath *>, 3>
        routedArms{{
            {FunctionContext::
                 MultiArmCyclicByteLaneArm::
                     Route::Root,
             rootPath},
            {FunctionContext::
                 MultiArmCyclicByteLaneArm::
                     Route::InnerTrue,
             innerTruePath},
            {FunctionContext::
                 MultiArmCyclicByteLaneArm::
                     Route::InnerFalse,
             innerFalsePath},
        }};
    FunctionContext::
        MultiArmCyclicByteLaneTransfer transfer;
    transfer.rootBranch = rootBranch;
    transfer.innerBranch = innerBranch;
    transfer.join = &join;
    unsigned writerCount = 0;
    for (const auto &[route, path] :
         routedArms) {
      ArmWriter writer =
          writerForArm(path->blocks);
      if (!writer.valid)
        return std::nullopt;
      FunctionContext::MultiArmCyclicByteLaneArm
          arm;
      arm.route = route;
      arm.block = path->endpoint;
      arm.successor = path->successor;
      arm.store = writer.store;
      arm.lanes.resize(loadBytes);
      if (writer.store != nullptr)
        ++writerCount;
      __int128 loadBegin = loadOffset;
      __int128 storeBegin = writer.offset;
      __int128 storeEnd =
          storeBegin + writer.bytes;
      for (unsigned lane = 0;
           lane < loadBytes; ++lane) {
        __int128 address = loadBegin + lane;
        if (
            writer.store != nullptr &&
            address >= storeBegin &&
            address < storeEnd) {
          arm.lanes[lane] = {
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Store,
              writer.store,
              static_cast<unsigned>(
                  address - storeBegin)};
        } else {
          arm.lanes[lane] = {
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Carry,
              nullptr, lane};
        }
      }
      transfer.arms.push_back(
          std::move(arm));
      transfer.forwarded |=
          path->successor != path->endpoint;
    }
    if (writerCount != 2)
      return std::nullopt;
    return transfer;
  }

  std::optional<
      FunctionContext::RecursiveCyclicByteLaneTransfer>
  boundedRecursiveCyclicByteLaneTransfer(
      const BasicBlock &join, const LoadInst &load,
      const Value *loadBase, int64_t loadOffset,
      uint64_t loadBytes) const {
    if (
        pred_size(&join) < 4 ||
        pred_size(&join) > 8)
      return std::nullopt;
    std::set<const BasicBlock *> leafBlocks;
    for (const BasicBlock *leaf :
         predecessors(&join)) {
      if (
          leaf == &join ||
          succ_size(leaf) != 1 ||
          *succ_begin(leaf) != &join)
        return std::nullopt;
      leafBlocks.insert(leaf);
    }

    struct TreeCandidate {
      const BranchInst *root = nullptr;
      unsigned depth = 0;
      bool forwarded = false;
      std::vector<const BranchInst *> branches;
      std::vector<const BasicBlock *> leaves;
      std::map<
          const BasicBlock *,
          std::vector<const BasicBlock *>>
          leafCorridors;
      std::set<const BasicBlock *> corridorBlocks;
    };
    std::vector<TreeCandidate> candidates;
    for (const BasicBlock &rootBlock :
         *join.getParent()) {
      auto *root =
          dyn_cast<BranchInst>(
              rootBlock.getTerminator());
      if (
          root == nullptr ||
          !root->isConditional())
        continue;
      TreeCandidate candidate;
      candidate.root = root;
      std::set<const BasicBlock *> active;
      std::set<const BasicBlock *> visited;
      std::set<const BasicBlock *> reachedLeaves;
      std::function<bool(
          const BasicBlock *, unsigned)>
          walk;
      std::function<bool(
          const BasicBlock *, const BasicBlock *,
          unsigned)>
          descend;
      descend =
          [&](const BasicBlock *start,
              const BasicBlock *parent,
              unsigned depth) {
            const BasicBlock *current = start;
            const BasicBlock *predecessor = parent;
            std::vector<const BasicBlock *> corridor;
            for (unsigned hop = 0;
                 hop < 16; ++hop) {
              if (
                  current == &join ||
                  pred_size(current) != 1 ||
                  *pred_begin(current) !=
                      predecessor)
                return false;
              corridor.push_back(current);
              if (
                  leafBlocks.count(current) != 0) {
                candidate.forwarded |=
                    corridor.size() > 1;
                candidate.leafCorridors[current] =
                    corridor;
                return walk(current, depth);
              }
              auto *branch =
                  dyn_cast<BranchInst>(
                      current->getTerminator());
              if (
                  branch != nullptr &&
                  branch->isConditional()) {
                candidate.forwarded |=
                    corridor.size() > 1;
                return walk(current, depth);
              }
              if (
                  branch == nullptr ||
                  branch->getNumSuccessors() != 1 ||
                  !candidate.corridorBlocks
                       .insert(current)
                       .second)
                return false;
              predecessor = current;
              current =
                  branch->getSuccessor(0);
            }
            return false;
          };
      walk =
              [&](const BasicBlock *block,
                  unsigned depth) {
                if (
                    depth > 6 ||
                    !visited.insert(block).second)
                  return false;
                if (leafBlocks.count(block) != 0) {
                  reachedLeaves.insert(block);
                  candidate.leaves.push_back(block);
                  candidate.depth =
                      std::max(
                          candidate.depth, depth);
                  return true;
                }
                if (!active.insert(block).second)
                  return false;
                auto *branch =
                    dyn_cast<BranchInst>(
                        block->getTerminator());
                if (
                    branch == nullptr ||
                    !branch->isConditional() ||
                    branch->getSuccessor(0) ==
                        branch->getSuccessor(1)) {
                  active.erase(block);
                  return false;
                }
                candidate.branches.push_back(
                    branch);
                for (unsigned index = 0;
                     index < 2; ++index) {
                  const BasicBlock *successor =
                      branch->getSuccessor(index);
                  if (
                      !descend(
                          successor, block,
                          depth + 1)) {
                    active.erase(block);
                    return false;
                  }
                }
                active.erase(block);
                return true;
              };
      if (
          walk(&rootBlock, 0) &&
          reachedLeaves == leafBlocks &&
          candidate.leaves.size() ==
              leafBlocks.size() &&
          candidate.branches.size() + 1 ==
              candidate.leaves.size() &&
          candidate.branches.size() +
                  candidate.leaves.size() +
                  candidate
                      .corridorBlocks.size() <=
              64)
        candidates.push_back(
            std::move(candidate));
    }
    if (candidates.size() != 1)
      return std::nullopt;
    TreeCandidate tree =
        std::move(candidates.front());
    auto treeAncestor =
        [&](const BasicBlock *ancestor,
            const BasicBlock *leaf) {
          const BasicBlock *current = leaf;
          std::set<const BasicBlock *> seen;
          for (unsigned depth = 0;
               depth < 64; ++depth) {
            if (current == ancestor)
              return true;
            if (
                !seen.insert(current).second ||
                pred_size(current) != 1)
              return false;
            current = *pred_begin(current);
          }
          return false;
        };

    struct LeafWriter {
      bool valid = true;
      const StoreInst *store = nullptr;
      int64_t offset = 0;
      uint64_t bytes = 0;
    };
    auto writerForLeaf =
        [&](const std::vector<
                const BasicBlock *> &blocks) {
          LeafWriter result;
          for (const BasicBlock *block : blocks)
            for (const Instruction &instruction :
                 *block) {
            if (!instruction.mayReadOrWriteMemory())
              continue;
            if (auto *leafLoad =
                    dyn_cast<LoadInst>(
                        &instruction)) {
              if (
                  leafLoad->isVolatile() ||
                  leafLoad->isAtomic() ||
                  !provablyDisjointScalarMemoryAccess(
                      leafLoad->getPointerOperand(),
                      leafLoad->getType(),
                      load.getPointerOperand(),
                      load.getType())) {
                result.valid = false;
                return result;
              }
              continue;
            }
            auto *store =
                dyn_cast<StoreInst>(&instruction);
            if (
                store == nullptr ||
                store->isVolatile() ||
                store->isAtomic()) {
              result.valid = false;
              return result;
            }
            if (provablyDisjointScalarMemoryAccess(
                    store->getPointerOperand(),
                    store->getValueOperand()
                        ->getType(),
                    load.getPointerOperand(),
                    load.getType()))
              continue;
            unsigned storeBits = integerBits(
                store->getValueOperand()->getType());
            uint64_t storeBytes =
                storeBits == 0
                    ? 0
                    : fixedStoreBytes(
                          M.getDataLayout(),
                          store->getValueOperand()
                              ->getType());
            int64_t storeOffset = 0;
            const Value *storeBase =
                GetPointerBaseWithConstantOffset(
                    store->getPointerOperand()
                        ->stripPointerCasts(),
                    storeOffset, M.getDataLayout())
                    ->stripPointerCasts();
            __int128 loadBegin = loadOffset;
            __int128 loadEnd =
                loadBegin + loadBytes;
            __int128 storeBegin = storeOffset;
            __int128 storeEnd =
                storeBegin + storeBytes;
            if (
                storeBits == 0 ||
                storeBits > 64 ||
                storeBits % 8 != 0 ||
                storeBytes != storeBits / 8 ||
                storeBytes == 0 ||
                storeBytes > 8 ||
                storeBase != loadBase ||
                storeEnd <= loadBegin ||
                loadEnd <= storeBegin ||
                result.store != nullptr ||
                (storeOffset == loadOffset &&
                 storeBytes == loadBytes)) {
              result.valid = false;
              return result;
            }
            result.store = store;
            result.offset = storeOffset;
            result.bytes = storeBytes;
            }
          return result;
        };

    struct GroupWriter {
      LeafWriter writer;
      const BasicBlock *block = nullptr;
    };
    std::vector<GroupWriter> groupedWriters;
    for (const BranchInst *branch :
         tree.branches) {
      const BasicBlock *block =
          branch->getParent();
      LeafWriter writer =
          writerForLeaf({block});
      if (!writer.valid)
        return std::nullopt;
      if (writer.store == nullptr)
        continue;
      if (groupedWriters.size() >= 3)
        return std::nullopt;
      groupedWriters.push_back(
          {writer, block});
    }

    FunctionContext::
        RecursiveCyclicByteLaneTransfer transfer;
    transfer.rootBranch = tree.root;
    transfer.join = &join;
    transfer.depth = tree.depth;
    transfer.forwarded = tree.forwarded;
    for (const BranchInst *branch :
         tree.branches)
      transfer.branches.push_back({branch});
    unsigned writerLeafCount = 0;
    std::vector<unsigned> groupedLeafCounts(
        groupedWriters.size(), 0);
    std::vector<unsigned>
        groupedComposedOverrideCounts(
            groupedWriters.size(), 0);
    unsigned exactOverrideCount = 0;
    unsigned composedOverrideCount = 0;
    unsigned carryLeafCount = 0;
    std::set<const StoreInst *> writers;
    for (const BasicBlock *block :
         tree.leaves) {
      LeafWriter localWriter =
          writerForLeaf(
              tree.leafCorridors.at(block));
      if (!localWriter.valid)
        return std::nullopt;
      int groupedWriterIndex = -1;
      for (size_t index = 0;
           index < groupedWriters.size();
           ++index)
        if (treeAncestor(
                groupedWriters[index].block,
                block)) {
          if (groupedWriterIndex >= 0)
            return std::nullopt;
          groupedWriterIndex =
              static_cast<int>(index);
        }
      bool groupedDescendant =
          groupedWriterIndex >= 0;
      const LeafWriter *groupedWriter =
          groupedDescendant
              ? &groupedWriters[
                    static_cast<size_t>(
                        groupedWriterIndex)]
                     .writer
              : nullptr;
      bool exactOverride =
          groupedDescendant &&
          localWriter.store != nullptr &&
          localWriter.offset ==
              groupedWriter->offset &&
          localWriter.bytes ==
              groupedWriter->bytes;
      bool candidateComposition =
          groupedDescendant &&
          localWriter.store != nullptr &&
          !exactOverride;
      if (candidateComposition) {
        __int128 groupedBegin =
            groupedWriter->offset;
        __int128 groupedEnd =
            groupedBegin + groupedWriter->bytes;
        __int128 localBegin = localWriter.offset;
        __int128 localEnd =
            localBegin + localWriter.bytes;
        if (
            groupedEnd <= localBegin ||
            localEnd <= groupedBegin)
          return std::nullopt;
      }
      FunctionContext::MultiArmCyclicByteLaneArm
          leaf;
      leaf.block = block;
      leaf.successor =
          tree.leafCorridors.at(block).front();
      leaf.store =
          localWriter.store != nullptr
              ? localWriter.store
              : groupedDescendant
              ? groupedWriter->store
              : nullptr;
      leaf.lanes.resize(loadBytes);
      __int128 loadBegin = loadOffset;
      __int128 localBegin = localWriter.offset;
      __int128 localEnd =
          localBegin + localWriter.bytes;
      __int128 groupedBegin =
          groupedDescendant
              ? groupedWriter->offset
              : 0;
      __int128 groupedEnd =
          groupedBegin +
          (groupedDescendant
               ? groupedWriter->bytes
               : 0);
      bool usedLocal = false;
      bool usedGrouped = false;
      for (unsigned lane = 0;
           lane < loadBytes; ++lane) {
        __int128 address = loadBegin + lane;
        if (
            localWriter.store != nullptr &&
            address >= localBegin &&
            address < localEnd) {
          leaf.lanes[lane] = {
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Store,
              localWriter.store,
              static_cast<unsigned>(
                  address - localBegin)};
          usedLocal = true;
          writers.insert(localWriter.store);
        } else if (
            groupedDescendant &&
            address >= groupedBegin &&
            address < groupedEnd) {
          leaf.lanes[lane] = {
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Store,
              groupedWriter->store,
              static_cast<unsigned>(
                  address - groupedBegin)};
          usedGrouped = true;
          writers.insert(groupedWriter->store);
        } else {
          leaf.lanes[lane] = {
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Carry,
              nullptr, lane};
        }
      }
      if (leaf.store != nullptr)
        ++writerLeafCount;
      else
        ++carryLeafCount;
      if (
          groupedDescendant &&
          localWriter.store == nullptr) {
        if (!usedGrouped)
          return std::nullopt;
        ++groupedLeafCounts[
            static_cast<size_t>(
                groupedWriterIndex)];
      } else if (exactOverride) {
        if (!usedLocal || usedGrouped)
          return std::nullopt;
        ++exactOverrideCount;
      } else if (candidateComposition) {
        if (!usedLocal || !usedGrouped)
          return std::nullopt;
        ++composedOverrideCount;
        ++groupedComposedOverrideCounts[
            static_cast<size_t>(
                groupedWriterIndex)];
      }
      transfer.leaves.push_back(
          std::move(leaf));
    }
    if (
        writerLeafCount < 2 ||
        writerLeafCount >= tree.leaves.size() ||
        writers.size() < 2 ||
        (groupedWriters.empty() &&
         writers.size() != writerLeafCount) ||
        (groupedWriters.size() == 1 &&
         (groupedLeafCounts.front() < 2 ||
          writers.size() == writerLeafCount ||
          (exactOverrideCount > 0 &&
           composedOverrideCount > 0))) ||
        (groupedWriters.size() == 2 &&
         (exactOverrideCount != 0 ||
          composedOverrideCount > 2 ||
          (composedOverrideCount == 2 &&
           std::any_of(
                groupedComposedOverrideCounts.begin(),
                groupedComposedOverrideCounts.end(),
                [](unsigned count) {
                  return count != 1;
                })) ||
          writers.size() !=
              2 + composedOverrideCount ||
          std::any_of(
              groupedLeafCounts.begin(),
              groupedLeafCounts.end(),
              [](unsigned count) {
                return count < 2;
              }))) ||
        (groupedWriters.size() == 3 &&
         (exactOverrideCount != 0 ||
          composedOverrideCount > 1 ||
          writers.size() !=
              3 + composedOverrideCount ||
          std::any_of(
              groupedLeafCounts.begin(),
              groupedLeafCounts.end(),
              [](unsigned count) {
                return count < 2;
              }))))
      return std::nullopt;
    transfer.grouped =
        groupedWriters.size() == 1 &&
        exactOverrideCount == 0 &&
        composedOverrideCount == 0;
    transfer.repeatedSource =
        groupedWriters.size() == 1 &&
        exactOverrideCount > 0 &&
        composedOverrideCount == 0;
    transfer.composedRepeatedSource =
        groupedWriters.size() == 1 &&
        composedOverrideCount > 0 &&
        exactOverrideCount == 0;
    transfer.carryLeaves = carryLeafCount;
    transfer.multiCarry =
        transfer.composedRepeatedSource &&
        carryLeafCount >= 2;
    transfer.multipleGroups =
        groupedWriters.size() == 2;
    transfer.tripleGroups =
        groupedWriters.size() == 3;
    transfer.groupCount =
        static_cast<unsigned>(
            groupedWriters.size());
    transfer.mixedGroups =
        transfer.multipleGroups &&
        composedOverrideCount == 1;
    transfer.doubleComposedGroups =
        transfer.multipleGroups &&
        composedOverrideCount == 2;
    transfer.composedTripleGroups =
        transfer.tripleGroups &&
        composedOverrideCount == 1;
    transfer.mixedGroups =
        transfer.mixedGroups ||
        transfer.composedTripleGroups;
    transfer.composedGroupCount =
        transfer.mixedGroups
            ? 1
            : transfer.doubleComposedGroups
            ? 2
            : 0;
    return transfer;
  }

  std::optional<FunctionContext::CyclicByteLaneMemoryPhi>
  boundedCyclicByteLaneMemoryPhi(
      const LoadInst &load) const {
    const BasicBlock *merge = load.getParent();
    unsigned loadBits = integerBits(load.getType());
    uint64_t loadBytes =
        loadBits == 0
            ? 0
            : fixedStoreBytes(
                  M.getDataLayout(), load.getType());
    if (
        load.isVolatile() || load.isAtomic() ||
        loadBits < 16 || loadBits > 64 ||
        loadBits % 8 != 0 ||
        loadBytes != loadBits / 8 ||
        loadBytes < 2 || loadBytes > 8 ||
        pred_size(merge) < 2 ||
        pred_size(merge) > 64)
      return std::nullopt;
    for (const BasicBlock *predecessor :
         predecessors(merge))
      if (succ_size(predecessor) != 1)
        return std::nullopt;
    for (const Instruction &instruction : *merge) {
      if (&instruction == &load)
        break;
      if (!instruction.mayReadOrWriteMemory())
        continue;
      if (auto *earlierLoad =
              dyn_cast<LoadInst>(&instruction)) {
        if (
            !earlierLoad->isVolatile() &&
            !earlierLoad->isAtomic())
          continue;
      } else if (auto *store =
                     dyn_cast<StoreInst>(
                         &instruction)) {
        if (
            !store->isVolatile() &&
            !store->isAtomic() &&
            provablyDisjointScalarMemoryAccess(
                store->getPointerOperand(),
                store->getValueOperand()->getType(),
                load.getPointerOperand(),
                load.getType()))
          continue;
      }
      return std::nullopt;
    }

    int64_t loadOffset = 0;
    const Value *loadBase =
        GetPointerBaseWithConstantOffset(
            load.getPointerOperand()->stripPointerCasts(),
            loadOffset, M.getDataLayout())
            ->stripPointerCasts();
    FunctionContext::CyclicByteLaneMemoryPhi phi;
    phi.load = &load;
    phi.bytes = loadBytes;
    unsigned visitedBlocks = 0;
    bool sawCarry = false;
    bool partial = false;
    for (const BasicBlock *endpoint :
         predecessors(merge)) {
      FunctionContext::CyclicByteLaneMemoryIncoming
          incoming;
      incoming.block = endpoint;
      incoming.lanes.resize(loadBytes);
      std::vector<bool> covered(loadBytes, false);
      unsigned coveredCount = 0;
      std::set<const BasicBlock *> active;
      auto complete = [&]() {
        return coveredCount == loadBytes;
      };
      std::function<bool(
          const BasicBlock *,
          BasicBlock::const_iterator)>
          scan =
              [&](const BasicBlock *block,
                  BasicBlock::const_iterator boundary) {
                if (
                    ++visitedBlocks > 64 ||
                    !active.insert(block).second)
                  return false;
                while (boundary != block->begin()) {
                  --boundary;
                  const Instruction &instruction =
                      *boundary;
                  if (&instruction == &load) {
                    for (unsigned lane = 0;
                         lane < loadBytes; ++lane) {
                      if (covered[lane])
                        continue;
                      incoming.lanes[lane] = {
                          FunctionContext::
                              CyclicByteLaneMemorySource::
                                  Kind::Carry,
                          nullptr, lane};
                      covered[lane] = true;
                      ++coveredCount;
                    }
                    sawCarry = true;
                    active.erase(block);
                    return true;
                  }
                  if (!instruction.mayReadOrWriteMemory())
                    continue;
                  if (auto *earlierLoad =
                          dyn_cast<LoadInst>(
                              &instruction)) {
                    if (
                        earlierLoad->isVolatile() ||
                        earlierLoad->isAtomic()) {
                      active.erase(block);
                      return false;
                    }
                    continue;
                  }
                  auto *store =
                      dyn_cast<StoreInst>(
                          &instruction);
                  if (
                      store == nullptr ||
                      store->isVolatile() ||
                      store->isAtomic()) {
                    active.erase(block);
                    return false;
                  }
                  unsigned storeBits =
                      integerBits(
                          store->getValueOperand()
                              ->getType());
                  uint64_t storeBytes =
                      storeBits == 0
                          ? 0
                          : fixedStoreBytes(
                                M.getDataLayout(),
                                store->getValueOperand()
                                    ->getType());
                  if (
                      storeBits == 0 ||
                      storeBits > 64 ||
                      storeBits % 8 != 0 ||
                      storeBytes != storeBits / 8 ||
                      storeBytes == 0 ||
                      storeBytes > 8) {
                    active.erase(block);
                    return false;
                  }
                  if (provablyDisjointScalarMemoryAccess(
                          store->getPointerOperand(),
                          store->getValueOperand()
                              ->getType(),
                          load.getPointerOperand(),
                          load.getType()))
                    continue;
                  int64_t storeOffset = 0;
                  const Value *storeBase =
                      GetPointerBaseWithConstantOffset(
                          store->getPointerOperand()
                              ->stripPointerCasts(),
                          storeOffset,
                          M.getDataLayout())
                          ->stripPointerCasts();
                  if (storeBase != loadBase) {
                    active.erase(block);
                    return false;
                  }
                  __int128 loadBegin = loadOffset;
                  __int128 loadEnd =
                      loadBegin + loadBytes;
                  __int128 storeBegin = storeOffset;
                  __int128 storeEnd =
                      storeBegin + storeBytes;
                  if (
                      storeEnd <= loadBegin ||
                      loadEnd <= storeBegin) {
                    active.erase(block);
                    return false;
                  }
                  partial |=
                      storeOffset != loadOffset ||
                      storeBytes != loadBytes;
                  for (unsigned lane = 0;
                       lane < loadBytes; ++lane) {
                    if (covered[lane])
                      continue;
                    __int128 address =
                        loadBegin + lane;
                    if (
                        address < storeBegin ||
                        address >= storeEnd)
                      continue;
                    incoming.lanes[lane] = {
                        FunctionContext::
                            CyclicByteLaneMemorySource::
                                Kind::Store,
                        store,
                        static_cast<unsigned>(
                            address - storeBegin)};
                    covered[lane] = true;
                    ++coveredCount;
                  }
                  if (complete()) {
                    active.erase(block);
                    return true;
                  }
                }
                if (pred_empty(block)) {
                  if (
                      !hasDefinedScalarInitialDefinition(
                          load)) {
                    active.erase(block);
                    return false;
                  }
                  for (unsigned lane = 0;
                       lane < loadBytes; ++lane) {
                    if (covered[lane])
                      continue;
                    incoming.lanes[lane] = {
                        FunctionContext::
                            CyclicByteLaneMemorySource::
                                Kind::Initial,
                        nullptr, lane};
                    covered[lane] = true;
                    ++coveredCount;
                  }
                  active.erase(block);
                  return true;
                }
                if (
                    pred_size(block) == 2 &&
                    coveredCount == 0 &&
                    phi.conditionalTransfers.empty() &&
                    phi.multiArmTransfers.empty()) {
                  auto transfer =
                      boundedConditionalCyclicByteLaneTransfer(
                          *block, load, loadBase,
                          loadOffset, loadBytes);
                  if (transfer) {
                    for (unsigned lane = 0;
                         lane < loadBytes; ++lane) {
                      incoming.lanes[lane] = {
                          FunctionContext::
                              CyclicByteLaneMemorySource::
                                  Kind::Carry,
                          nullptr, lane};
                      covered[lane] = true;
                      ++coveredCount;
                    }
                    phi.conditional = true;
                    phi.forwardedConditional |=
                        transfer->forwarded;
                    phi.conditionalTransfers.push_back(
                        std::move(*transfer));
                    sawCarry = true;
                    partial = true;
                    active.erase(block);
                    return true;
                  }
                }
                if (
                    pred_size(block) == 3 &&
                    coveredCount == 0 &&
                    phi.conditionalTransfers.empty() &&
                    phi.multiArmTransfers.empty()) {
                  auto transfer =
                      boundedMultiArmCyclicByteLaneTransfer(
                          *block, load, loadBase,
                          loadOffset, loadBytes);
                  if (transfer) {
                    for (unsigned lane = 0;
                         lane < loadBytes; ++lane) {
                      incoming.lanes[lane] = {
                          FunctionContext::
                              CyclicByteLaneMemorySource::
                                  Kind::Carry,
                          nullptr, lane};
                      covered[lane] = true;
                      ++coveredCount;
                    }
                    phi.multiArmConditional = true;
                    phi.forwardedMultiArmConditional =
                        transfer->forwarded;
                    phi.multiArmTransfers.push_back(
                        std::move(*transfer));
                    sawCarry = true;
                    partial = true;
                    active.erase(block);
                    return true;
                  }
                }
                if (
                    pred_size(block) >= 4 &&
                    pred_size(block) <= 8 &&
                    coveredCount == 0 &&
                    phi.conditionalTransfers.empty() &&
                    phi.multiArmTransfers.empty() &&
                    phi.recursiveTransfers.empty()) {
                  auto transfer =
                      boundedRecursiveCyclicByteLaneTransfer(
                          *block, load, loadBase,
                          loadOffset, loadBytes);
                  if (transfer) {
                    for (unsigned lane = 0;
                         lane < loadBytes; ++lane) {
                      incoming.lanes[lane] = {
                          FunctionContext::
                              CyclicByteLaneMemorySource::
                                  Kind::Carry,
                          nullptr, lane};
                      covered[lane] = true;
                      ++coveredCount;
                    }
                    phi.recursiveConditional = true;
                    phi.forwardedRecursiveConditional =
                        transfer->forwarded;
                    phi.groupedRecursiveConditional =
                        transfer->grouped;
                    phi.forwardedGroupedRecursiveConditional =
                        transfer->forwarded &&
                        transfer->grouped;
                    phi.repeatedSourceRecursiveConditional =
                        transfer->repeatedSource;
                    phi.forwardedRepeatedSourceRecursiveConditional =
                        transfer->forwarded &&
                        transfer->repeatedSource;
                    phi.composedRepeatedSourceRecursiveConditional =
                        transfer->composedRepeatedSource;
                    phi.forwardedComposedRepeatedSourceRecursiveConditional =
                        transfer->forwarded &&
                        transfer->composedRepeatedSource;
                    phi.multiCarryRecursiveConditional =
                        transfer->multiCarry;
                    phi.forwardedMultiCarryRecursiveConditional =
                        transfer->forwarded &&
                        transfer->multiCarry;
                    phi.multipleGroupsRecursiveConditional =
                        transfer->multipleGroups;
                    phi.forwardedMultipleGroupsRecursiveConditional =
                        transfer->forwarded &&
                        transfer->multipleGroups;
                    phi.tripleGroupsRecursiveConditional =
                        transfer->tripleGroups;
                    phi.forwardedTripleGroupsRecursiveConditional =
                        transfer->forwarded &&
                        transfer->tripleGroups;
                    phi.mixedGroupsRecursiveConditional =
                        transfer->mixedGroups;
                    phi.forwardedMixedGroupsRecursiveConditional =
                        transfer->forwarded &&
                        transfer->mixedGroups;
                    phi.doubleComposedGroupsRecursiveConditional =
                        transfer->doubleComposedGroups;
                    phi.forwardedDoubleComposedGroupsRecursiveConditional =
                        transfer->forwarded &&
                        transfer->doubleComposedGroups;
                    phi.composedTripleGroupsRecursiveConditional =
                        transfer->composedTripleGroups;
                    phi.forwardedComposedTripleGroupsRecursiveConditional =
                        transfer->forwarded &&
                        transfer->composedTripleGroups;
                    phi.recursiveTransfers.push_back(
                        std::move(*transfer));
                    sawCarry = true;
                    partial = true;
                    active.erase(block);
                    return true;
                  }
                }
                if (pred_size(block) != 1) {
                  active.erase(block);
                  return false;
                }
                const BasicBlock *predecessor =
                    *pred_begin(block);
                bool result =
                    scan(
                        predecessor,
                        predecessor->end());
                active.erase(block);
                return result;
              };
      if (
          !scan(endpoint, endpoint->end()) ||
          !complete())
        return std::nullopt;
      std::set<std::pair<
          FunctionContext::CyclicByteLaneMemorySource::
              Kind,
          const StoreInst *>> sources;
      for (const auto &lane : incoming.lanes)
        sources.insert({lane.kind, lane.store});
      partial |= sources.size() > 1;
      phi.incoming.push_back(
          std::move(incoming));
    }
    if (!sawCarry || !partial)
      return std::nullopt;
    return phi;
  }

  bool cyclicByteLaneMemoryIsAliasClosed(
      const FunctionContext::CyclicByteLaneMemoryPhi
          &phi) const {
    std::set<const StoreInst *> stores;
    for (const auto &incoming : phi.incoming)
      for (const auto &lane : incoming.lanes)
        if (
            lane.kind ==
                FunctionContext::
                    CyclicByteLaneMemorySource::
                        Kind::Store &&
            lane.store != nullptr)
          stores.insert(lane.store);
    for (const auto &transfer :
         phi.conditionalTransfers)
      if (transfer.store != nullptr)
        stores.insert(transfer.store);
    for (const auto &transfer :
         phi.multiArmTransfers)
      for (const auto &arm : transfer.arms)
        if (arm.store != nullptr)
          stores.insert(arm.store);
    for (const auto &transfer :
         phi.recursiveTransfers)
      for (const auto &leaf : transfer.leaves)
        for (const auto &lane : leaf.lanes)
          if (
              lane.kind ==
                  FunctionContext::
                      CyclicByteLaneMemorySource::
                          Kind::Store &&
              lane.store != nullptr)
            stores.insert(lane.store);
    if (stores.empty())
      return false;
    for (const Instruction &instruction :
         instructions(*phi.load->getFunction())) {
      if (auto *load =
              dyn_cast<LoadInst>(&instruction)) {
        if (load == phi.load)
          continue;
        if (
            load->isVolatile() ||
            load->isAtomic() ||
            !provablyDisjointScalarMemoryAccess(
                load->getPointerOperand(),
                load->getType(),
                phi.load->getPointerOperand(),
                phi.load->getType()))
          return false;
        continue;
      }
      if (auto *store =
              dyn_cast<StoreInst>(&instruction)) {
        if (stores.count(store) != 0)
          continue;
        if (
            store->isVolatile() ||
            store->isAtomic() ||
            !provablyDisjointScalarMemoryAccess(
                store->getPointerOperand(),
                store->getValueOperand()->getType(),
                phi.load->getPointerOperand(),
                phi.load->getType()))
          return false;
        continue;
      }
      if (instruction.mayReadOrWriteMemory())
        return false;
    }
    return true;
  }

  void prepareCyclicByteLaneMemoryPhis(
      Function &function, FunctionContext &context) {
    std::vector<
        FunctionContext::CyclicByteLaneMemoryPhi>
        candidates;
    for (Instruction &instruction :
         instructions(function)) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (
          load == nullptr ||
          context.byteLaneCompositionIndices
                  .find(load) !=
              context.byteLaneCompositionIndices.end() ||
          context.byteLanePhiIndices.find(load) !=
              context.byteLanePhiIndices.end())
        continue;
      auto phi =
          boundedCyclicByteLaneMemoryPhi(*load);
      if (
          !phi ||
          !cyclicByteLaneMemoryIsAliasClosed(*phi))
        continue;
      for (const auto &incoming :
           phi->incoming)
        for (const auto &lane :
             incoming.lanes) {
          if (
              lane.kind !=
                  FunctionContext::
                      CyclicByteLaneMemorySource::
                          Kind::Store ||
              lane.store == nullptr)
            continue;
          auto &loads =
              byteLanePoisonLoads[lane.store];
          if (
              std::find(
                  loads.begin(), loads.end(),
                  load) == loads.end())
            loads.push_back(load);
          cyclicByteLanePoisonStores.insert(
              lane.store);
        }
      for (const auto &transfer :
           phi->conditionalTransfers) {
        if (transfer.store == nullptr)
          continue;
        auto &loads =
            byteLanePoisonLoads[transfer.store];
        if (
            std::find(
                loads.begin(), loads.end(),
                load) == loads.end())
          loads.push_back(load);
        cyclicByteLanePoisonStores.insert(
            transfer.store);
      }
      for (const auto &transfer :
           phi->multiArmTransfers)
        for (const auto &arm : transfer.arms) {
          if (arm.store == nullptr)
            continue;
          auto &loads =
              byteLanePoisonLoads[arm.store];
          if (
              std::find(
                  loads.begin(), loads.end(),
                  load) == loads.end())
            loads.push_back(load);
          cyclicByteLanePoisonStores.insert(
              arm.store);
        }
      for (const auto &transfer :
           phi->recursiveTransfers)
        for (const auto &leaf :
             transfer.leaves)
          for (const auto &lane :
               leaf.lanes) {
            if (
                lane.kind !=
                    FunctionContext::
                        CyclicByteLaneMemorySource::
                            Kind::Store ||
                lane.store == nullptr)
              continue;
            auto &loads =
                byteLanePoisonLoads[lane.store];
            if (
                std::find(
                    loads.begin(), loads.end(),
                    load) == loads.end())
              loads.push_back(load);
            cyclicByteLanePoisonStores.insert(
                lane.store);
          }
      candidates.push_back(std::move(*phi));
    }

    std::vector<
        FunctionContext::CyclicByteLaneMemoryPhi>
        accepted;
    for (auto &phi : candidates) {
      if (
          !hasOnlyDeferredPoisonFreezeSinks(
              phi.load))
        continue;
      bool hasPotentialPoison = false;
      for (const auto &incoming : phi.incoming)
        hasPotentialPoison |=
            std::any_of(
                incoming.lanes.begin(),
                incoming.lanes.end(),
                [&](const auto &lane) {
                  return
                      lane.kind ==
                          FunctionContext::
                              CyclicByteLaneMemorySource::
                                  Kind::Store &&
                      lane.store != nullptr &&
                      valueMayCreateDeferredPoison(
                          lane.store
                              ->getValueOperand());
                });
      for (const auto &transfer :
           phi.conditionalTransfers)
        hasPotentialPoison |=
            transfer.store != nullptr &&
            valueMayCreateDeferredPoison(
                transfer.store
                    ->getValueOperand());
      for (const auto &transfer :
           phi.multiArmTransfers)
        for (const auto &arm : transfer.arms)
          hasPotentialPoison |=
              arm.store != nullptr &&
              valueMayCreateDeferredPoison(
                  arm.store
                      ->getValueOperand());
      for (const auto &transfer :
           phi.recursiveTransfers)
        for (const auto &leaf :
             transfer.leaves)
          for (const auto &lane :
               leaf.lanes)
            hasPotentialPoison |=
                lane.kind ==
                    FunctionContext::
                        CyclicByteLaneMemorySource::
                            Kind::Store &&
                lane.store != nullptr &&
                valueMayCreateDeferredPoison(
                    lane.store
                        ->getValueOperand());
      if (hasPotentialPoison)
        accepted.push_back(std::move(phi));
    }

    for (Instruction &instruction :
         instructions(function))
      if (auto *store =
              dyn_cast<StoreInst>(&instruction)) {
        byteLanePoisonLoads.erase(store);
        cyclicByteLanePoisonStores.erase(store);
      }
    auto registerStore =
        [&](const StoreInst *store,
            const LoadInst *load,
            bool cyclic) {
          if (store == nullptr)
            return;
          if (
              context.byteLaneStoreIds.find(store) ==
              context.byteLaneStoreIds.end())
            context.byteLaneStoreIds[store] =
                context.temporaryName(
                    "byte_lane_store_");
          if (
              valueMayCreateDeferredPoison(
                  store->getValueOperand()) &&
              context.byteLaneStoreDefinedNames
                      .find(store) ==
                  context.byteLaneStoreDefinedNames.end())
            context.byteLaneStoreDefinedNames[store] =
                context.temporaryName(
                    "byte_lane_store_defined_");
          auto &loads =
              byteLanePoisonLoads[store];
          if (
              std::find(
                  loads.begin(), loads.end(),
                  load) == loads.end())
            loads.push_back(load);
          if (cyclic)
            cyclicByteLanePoisonStores.insert(
                store);
        };
    for (const auto &composition :
         context.byteLaneCompositions)
      for (const auto &lane :
           composition.lanes)
        registerStore(
            lane.store, composition.load, false);
    for (const auto &phi : context.byteLanePhis)
      for (const auto &incoming : phi.incoming)
        for (const auto &lane : incoming.lanes)
          registerStore(
              lane.store, phi.load, false);
    for (auto &phi : accepted) {
      std::string loadName =
          context.values.lookup(phi.load);
      phi.defined =
          loadName +
          "__cyclic_byte_defined";
      for (unsigned lane = 0;
           lane < phi.bytes; ++lane)
        phi.laneDefined.push_back(
            loadName + "__lane_" +
            std::to_string(lane) +
            "_defined");
      unsigned index =
          static_cast<unsigned>(
              context.cyclicByteLanePhis.size());
      context.cyclicByteLanePhiIndices[
          phi.load] = index;
      context.poisonConditions[phi.load] =
          phi.defined;
      for (const auto &incoming :
           phi.incoming) {
        context.ensureEdgeBlock(
            incoming.block,
            phi.load->getParent());
        for (const auto &lane :
             incoming.lanes)
          if (
              lane.kind ==
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Store)
            registerStore(
                lane.store, phi.load, true);
      }
      for (const auto &transfer :
           phi.conditionalTransfers) {
        context.ensureEdgeBlock(
            transfer.storeArm,
            transfer.join);
        context.ensureEdgeBlock(
            transfer.carryArm,
            transfer.join);
        registerStore(
            transfer.store, phi.load, true);
      }
      for (const auto &transfer :
           phi.multiArmTransfers)
        for (const auto &arm : transfer.arms) {
          context.ensureEdgeBlock(
              arm.block, transfer.join);
          registerStore(
              arm.store, phi.load, true);
        }
      for (const auto &transfer :
           phi.recursiveTransfers)
        for (const auto &leaf :
             transfer.leaves) {
          context.ensureEdgeBlock(
              leaf.block, transfer.join);
          for (const auto &lane :
               leaf.lanes)
            if (
                lane.kind ==
                FunctionContext::
                    CyclicByteLaneMemorySource::
                        Kind::Store)
              registerStore(
                  lane.store, phi.load, true);
        }
      context.cyclicByteLanePhis.push_back(
          std::move(phi));
    }
  }

  void prepareMemoryPoisonMerges(
      Function &function, FunctionContext &context) {
    for (Instruction &instruction :
         instructions(function)) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (
          load == nullptr ||
          integerBits(load->getType()) == 0 ||
          load->isAtomic() || load->isVolatile() ||
          context.cyclicByteLanePhiIndices.find(
              load) !=
              context.cyclicByteLanePhiIndices.end() ||
          !hasOnlyDeferredPoisonFreezeSinks(load))
        continue;
      auto incoming =
          predecessorPoisonStores(*load);
      if (!incoming)
        continue;
      bool oneCommonStore =
          std::all_of(
              std::next(incoming->begin()),
              incoming->end(),
              [&](const auto &item) {
                return
                    item.store ==
                        incoming->front().store &&
                    item.kind ==
                        incoming->front().kind &&
                    item.condition ==
                        incoming->front().condition &&
                    item.storeWhenTrue ==
                        incoming->front().storeWhenTrue &&
                    item.conditionalForwarded ==
                        incoming->front().
                            conditionalForwarded &&
                    item.multiArmConditional ==
                        incoming->front().
                            multiArmConditional &&
                    item.equivalentDefinedStores ==
                        incoming->front().
                            equivalentDefinedStores &&
                    item.sharedPoisonStores ==
                        incoming->front().
                            sharedPoisonStores &&
                    item.equivalentStores.size() ==
                        incoming->front().
                            equivalentStores.size() &&
                    std::is_permutation(
                        item.equivalentStores.begin(),
                        item.equivalentStores.end(),
                        incoming->front().
                            equivalentStores.begin()) &&
                    item.secondaryStore ==
                        incoming->front().secondaryStore &&
                    item.innerCondition ==
                        incoming->front().innerCondition &&
                    item.firstStoreWhenTrue ==
                        incoming->front().
                            firstStoreWhenTrue &&
                    item.nestedConditional ==
                        incoming->front().nestedConditional &&
                    item.conditionTree ==
                        incoming->front().conditionTree &&
                    item.conditionTreeRoot ==
                        incoming->front().conditionTreeRoot &&
                    item.conditionTreeDepth ==
                        incoming->front().conditionTreeDepth &&
                    item.groupedRecursiveConditional ==
                        incoming->front().
                            groupedRecursiveConditional &&
                    item.repeatedSourceRecursiveConditional ==
                        incoming->front().
                            repeatedSourceRecursiveConditional &&
                    item.multiCarryRecursiveConditional ==
                        incoming->front().
                            multiCarryRecursiveConditional;
              });
      if (oneCommonStore)
        continue;
      bool hasPotentialPoison =
          std::any_of(
              incoming->begin(), incoming->end(),
              [&](const auto &item) {
                if (
                    item.store != nullptr &&
                    valueMayCreateDeferredPoison(
                        item.store->getValueOperand()))
                  return true;
                return std::any_of(
                    item.equivalentStores.begin(),
                    item.equivalentStores.end(),
                    [&](const StoreInst *store) {
                      return
                          store != nullptr &&
                          valueMayCreateDeferredPoison(
                              store->getValueOperand());
                    });
              });
      if (!hasPotentialPoison)
        continue;
      FunctionContext::MemoryPoisonMerge merge;
      merge.load = load;
      merge.defined =
          context.values.lookup(load) + "__defined";
      merge.incoming = std::move(*incoming);
      merge.interprocedural =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [&](const auto &item) {
                if (
                    item.store != nullptr &&
                    valueMayReceiveInterproceduralPoison(
                        item.store->getValueOperand()))
                  return true;
                return std::any_of(
                    item.equivalentStores.begin(),
                    item.equivalentStores.end(),
                    [&](const StoreInst *store) {
                      return
                          store != nullptr &&
                          valueMayReceiveInterproceduralPoison(
                              store->getValueOperand());
                    });
              });
      merge.multilevel =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.store != nullptr &&
                       item.block !=
                       item.store->getParent();
              });
      merge.initial =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::Initial;
              });
      merge.initialSubobject =
          merge.initial &&
          initialDefinitionUsesSubobject(*load);
      merge.carry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::Carry ||
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::ConditionalStoreCarry ||
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::NestedConditionalStoresCarry ||
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::RecursiveConditionalTree;
              });
      merge.conditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::ConditionalStoreCarry ||
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::NestedConditionalStoresCarry ||
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::RecursiveConditionalTree;
              });
      merge.forwardedConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.conditionalForwarded;
              });
      merge.multiArmConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.multiArmConditional;
              });
      merge.equivalentDefinedStoreCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.equivalentDefinedStores;
              });
      merge.sharedPoisonStoreCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.sharedPoisonStores;
              });
      merge.nestedConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return item.nestedConditional;
              });
      merge.recursiveConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.kind ==
                    FunctionContext::MemoryPoisonIncoming::
                        Kind::RecursiveConditionalTree;
              });
      merge.groupedRecursiveConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.groupedRecursiveConditional;
              });
      merge.repeatedSourceRecursiveConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.repeatedSourceRecursiveConditional;
              });
      merge.multiCarryRecursiveConditionalCarry =
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [](const auto &item) {
                return
                    item.multiCarryRecursiveConditional;
              });
      for (const auto &item : merge.incoming) {
        merge.conditionTreeDepth =
            std::max(
                merge.conditionTreeDepth,
                item.conditionTreeDepth);
        merge.conditionTreeLeaves +=
            static_cast<unsigned>(
                std::count_if(
                    item.conditionTree.begin(),
                    item.conditionTree.end(),
                    [](const auto &node) {
                      return
                          node.kind !=
                          FunctionContext::
                              MemoryDefinednessTreeNode::
                                      Kind::Select;
                    }));
        merge.conditionTreeCarryLeaves +=
            static_cast<unsigned>(
                std::count_if(
                    item.conditionTree.begin(),
                    item.conditionTree.end(),
                    [](const auto &node) {
                      return
                          node.kind ==
                          FunctionContext::
                              MemoryDefinednessTreeNode::
                                  Kind::Carry;
                    }));
      }
      merge.cyclic =
          merge.carry ||
          std::any_of(
              merge.incoming.begin(),
              merge.incoming.end(),
              [&](const auto &item) {
                return context.dominators.dominates(
                    load->getParent(), item.block);
              });
      unsigned index =
          static_cast<unsigned>(
              context.memoryPoisonMerges.size());
      context.memoryPoisonMergeIndices[load] = index;
      context.poisonConditions[load] = merge.defined;
      for (const auto &item : merge.incoming)
        context.ensureEdgeBlock(
            item.block, load->getParent());
      context.memoryPoisonMerges.push_back(
          std::move(merge));
    }
    for (size_t first = 0;
         first < context.memoryPoisonMerges.size();
         ++first) {
      auto &firstMerge =
          context.memoryPoisonMerges[first];
      for (size_t second = first + 1;
           second < context.memoryPoisonMerges.size();
           ++second) {
        auto &secondMerge =
            context.memoryPoisonMerges[second];
        if (
            firstMerge.load->getParent() !=
                secondMerge.load->getParent())
          continue;
        bool identifiedObjects = false;
        bool fixedHeapObjects = false;
        bool finitePointerDomains = false;
        bool guardCorrelatedPointerDomains = false;
        bool phiCorrelatedPointerDomains = false;
        bool symbolicIndexIntervals = false;
        if (!provablyDisjointScalarMemoryAccess(
                firstMerge.load->getPointerOperand(),
                firstMerge.load->getType(),
                secondMerge.load->getPointerOperand(),
                secondMerge.load->getType(),
                &identifiedObjects,
                &fixedHeapObjects,
                &finitePointerDomains,
                &guardCorrelatedPointerDomains,
                &phiCorrelatedPointerDomains,
                &symbolicIndexIntervals))
          continue;
        firstMerge.multiCell = true;
        secondMerge.multiCell = true;
        firstMerge.identifiedObjectMultiCell |=
            identifiedObjects;
        secondMerge.identifiedObjectMultiCell |=
            identifiedObjects;
        firstMerge.fixedHeapObjectMultiCell |=
            fixedHeapObjects;
        secondMerge.fixedHeapObjectMultiCell |=
            fixedHeapObjects;
        firstMerge.finitePointerDomainMultiCell |=
            finitePointerDomains;
        secondMerge.finitePointerDomainMultiCell |=
            finitePointerDomains;
        firstMerge.guardCorrelatedPointerDomainMultiCell |=
            guardCorrelatedPointerDomains;
        secondMerge.guardCorrelatedPointerDomainMultiCell |=
            guardCorrelatedPointerDomains;
        firstMerge.phiCorrelatedPointerDomainMultiCell |=
            phiCorrelatedPointerDomains;
        secondMerge.phiCorrelatedPointerDomainMultiCell |=
            phiCorrelatedPointerDomains;
        firstMerge.symbolicIndexIntervalMultiCell |=
            symbolicIndexIntervals;
        secondMerge.symbolicIndexIntervalMultiCell |=
            symbolicIndexIntervals;
        firstMerge.aliasGraphNeighbors.push_back(
            secondMerge.load);
        secondMerge.aliasGraphNeighbors.push_back(
            firstMerge.load);
        usesMultiCellAliasGraph = true;
      }
    }
  }

  bool valueDependsOnDeferredPoisonArgument(
      const Value *root) const {
    std::set<const Value *> active;
    std::function<bool(const Value *)> dependsOnArgument =
        [&](const Value *value) {
          if (
              value == nullptr ||
              !active.insert(value).second)
            return false;
          bool result = false;
          if (auto *argument = dyn_cast<Argument>(value))
            result = argumentHasDeferredPoison(*argument);
          auto *instruction = dyn_cast<Instruction>(value);
          bool transparent =
              instruction != nullptr &&
              (isa<BinaryOperator>(instruction) ||
               isa<ICmpInst>(instruction) ||
               isa<SelectInst>(instruction) ||
               isa<PHINode>(instruction) ||
               isa<CastInst>(instruction));
          if (auto *call =
                  dyn_cast_or_null<CallBase>(instruction)) {
            Function *callee = call->getCalledFunction();
            transparent =
                callee != nullptr && callee->isIntrinsic() &&
                callee->getIntrinsicID() ==
                    Intrinsic::ssa_copy;
          }
          if (!result && transparent)
            result = std::any_of(
                instruction->op_begin(),
                instruction->op_end(),
                [&](const Use &operandUse) {
                  return dependsOnArgument(
                      operandUse.get());
                });
          active.erase(value);
          return result;
        };
    return dependsOnArgument(root);
  }

  bool functionHasTransitiveArgumentPoisonReturn(
      const Function &function) const {
    if (integerBits(function.getReturnType()) == 0)
      return false;
    for (const BasicBlock &block : function)
      if (auto *returnInstruction =
              dyn_cast<ReturnInst>(block.getTerminator()))
        if (valueDependsOnDeferredPoisonArgument(
                returnInstruction->getReturnValue()))
          return true;
    return false;
  }

  bool functionHasDeferredPoisonReturn(
      const Function &function) const {
    if (integerBits(function.getReturnType()) == 0)
      return false;
    for (const BasicBlock &block : function)
      if (auto *returnInstruction =
              dyn_cast<ReturnInst>(block.getTerminator()))
        if (
            valueHasDeferredPoisonSource(
                returnInstruction->getReturnValue()) ||
            valueDependsOnDeferredPoisonArgument(
                returnInstruction->getReturnValue()))
          return true;
    return false;
  }

  bool argumentHasDeferredPoison(
      const Argument &argument) const {
    const Function *function = argument.getParent();
    if (
        function == nullptr ||
        integerBits(argument.getType()) == 0)
      return false;
    auto calls = directCallsites(*function);
    if (
        !calls ||
        !hasOnlyDeferredPoisonFreezeSinks(&argument))
      return false;
    return std::any_of(
        calls->begin(), calls->end(),
        [&](const CallBase *call) {
          return argument.getArgNo() < call->arg_size() &&
                 valueHasDeferredPoisonSource(
                     call->getArgOperand(
                         argument.getArgNo()));
        });
  }

  void lowerDefinedBinary(
      BinaryOperator &binary, FunctionContext &context,
      json::Array &output) {
    unsigned bits = integerBits(binary.getType());
    auto operation = binaryOperator(binary.getOpcode());
    if (bits == 0 || !operation) {
      reject(binary, "unsupported binary value type or opcode");
      return;
    }
    auto leftValue = [&]() {
      return operand(binary.getOperand(0), context, binary);
    };
    auto rightValue = [&]() {
      return operand(binary.getOperand(1), context, binary);
    };
    auto comparison = [&](StringRef prefix, StringRef predicate,
                          json::Object left, json::Object right) {
      return appendBinaryTemporary(
          prefix, predicate, std::move(left), std::move(right), 1,
          context, output);
    };
    auto boolean = [&](StringRef prefix, StringRef booleanOperation,
                       StringRef left, StringRef right) {
      return appendBinaryTemporary(
          prefix, booleanOperation, variableOperand(left),
          variableOperand(right), 1, context, output);
    };
    auto invert = [&](StringRef prefix, StringRef value) {
      return appendUnaryTemporary(
          prefix, "not", variableOperand(value), 1, context, output);
    };
    std::vector<std::string> deferredConditions;
    for (Value *operandValue : binary.operands()) {
      auto poison = context.poisonConditions.find(operandValue);
      if (
          poison != context.poisonConditions.end() &&
          std::find(
              deferredConditions.begin(), deferredConditions.end(),
              poison->second) == deferredConditions.end())
        deferredConditions.push_back(poison->second);
    }
    bool hasIncomingPoison = !deferredConditions.empty();
    bool deferPoison = false;
    auto require = [&](StringRef condition) {
      if (deferPoison)
        deferredConditions.push_back(condition.str());
      else
        appendAssumption(condition, output);
    };
    bool supportsWrapFlags =
        binary.getOpcode() == Instruction::Add ||
        binary.getOpcode() == Instruction::Sub ||
        binary.getOpcode() == Instruction::Mul ||
        binary.getOpcode() == Instruction::Shl;
    bool noUnsignedWrap =
        supportsWrapFlags && binary.hasNoUnsignedWrap();
    bool noSignedWrap =
        supportsWrapFlags && binary.hasNoSignedWrap();
    bool supportsExact =
        binary.getOpcode() == Instruction::UDiv ||
        binary.getOpcode() == Instruction::SDiv ||
        binary.getOpcode() == Instruction::LShr ||
        binary.getOpcode() == Instruction::AShr;
    bool exact = supportsExact && binary.isExact();

    bool division =
        binary.getOpcode() == Instruction::UDiv ||
        binary.getOpcode() == Instruction::SDiv ||
        binary.getOpcode() == Instruction::URem ||
        binary.getOpcode() == Instruction::SRem;
    bool mayCreatePoison =
        division || binary.isShift() || noUnsignedWrap ||
        noSignedWrap || exact;
    bool deferredPoisonFanout = false;
    deferPoison =
        (hasIncomingPoison || mayCreatePoison) &&
        hasOnlyDeferredPoisonFreezeSinks(
            &binary, &deferredPoisonFanout);
    if (hasIncomingPoison && !deferPoison) {
      for (const std::string &condition : deferredConditions)
        appendAssumption(condition, output);
      deferredConditions.clear();
    }
    std::string deferredSafeDivisor;
    if (division) {
      auto divisor = rightValue();
      if (!divisor)
        return;
      std::string divisorNonzero = comparison(
          "llvm_divisor_nonzero_", "ne", std::move(*divisor),
          integerConstant(0, bits));
      require(divisorNonzero);
      if (deferPoison) {
        auto originalDivisor = rightValue();
        if (!originalDivisor)
          return;
        deferredSafeDivisor = appendSelectTemporary(
            "llvm_deferred_safe_divisor_",
            variableOperand(divisorNonzero),
            std::move(*originalDivisor),
            integerConstant(1, bits), bits, context, output);
      }
      if (binary.getOpcode() == Instruction::SDiv) {
        int64_t minimum =
            bits == 64
                ? INT64_MIN
                : -(INT64_C(1) << (bits - 1));
        auto dividend = leftValue();
        auto signedDivisor = rightValue();
        if (!dividend || !signedDivisor)
          return;
        std::string isMinimum = comparison(
            "llvm_sdiv_min_", "eq", std::move(*dividend),
            integerConstant(minimum, bits));
        std::string isMinusOne = comparison(
            "llvm_sdiv_minus_one_", "eq", std::move(*signedDivisor),
            integerConstant(-1, bits));
        std::string overflow = boolean(
            "llvm_sdiv_overflow_", "and", isMinimum, isMinusOne);
        require(invert("llvm_sdiv_defined_", overflow));
      }
    }
    if (binary.isShift()) {
      auto amount = rightValue();
      if (!amount)
        return;
      require(comparison(
          "llvm_shift_in_range_", "ult", std::move(*amount),
          integerConstant(static_cast<int64_t>(bits), bits)));
    }

    auto left = leftValue();
    auto right =
        deferredSafeDivisor.empty()
            ? rightValue()
            : std::optional<json::Object>(
                  variableOperand(deferredSafeDivisor));
    if (!left || !right)
      return;
    json::Object lowered;
    lowered["op"] = "binary";
    lowered["operator"] = *operation;
    lowered["dst"] = context.values.lookup(&binary);
    lowered["left"] = std::move(*left);
    lowered["right"] = std::move(*right);
    lowered["bits"] = static_cast<int64_t>(bits);
    output.push_back(std::move(lowered));

    std::string result = context.values.lookup(&binary);
    auto resultValue = [&]() {
      return variableOperand(result);
    };
    auto sign = [&](StringRef prefix, json::Object value) {
      return appendBinaryTemporary(
          prefix, "lshr", std::move(value),
          integerConstant(static_cast<int64_t>(bits - 1), bits),
          bits, context, output);
    };

    if (noUnsignedWrap) {
      switch (binary.getOpcode()) {
      case Instruction::Add: {
        auto lhs = leftValue();
        if (!lhs)
          return;
        require(comparison(
            "llvm_add_nuw_", "uge", resultValue(), std::move(*lhs)));
        break;
      }
      case Instruction::Sub: {
        auto lhs = leftValue();
        auto rhs =
            deferredSafeDivisor.empty()
                ? rightValue()
                : std::optional<json::Object>(
                      variableOperand(deferredSafeDivisor));
        if (!lhs || !rhs)
          return;
        require(comparison(
            "llvm_sub_nuw_", "uge", std::move(*lhs), std::move(*rhs)));
        break;
      }
      case Instruction::Mul: {
        auto rhsForZero = rightValue();
        if (!rhsForZero)
          return;
        std::string zero = comparison(
            "llvm_mul_nuw_zero_", "eq", std::move(*rhsForZero),
            integerConstant(0, bits));
        auto rhsForSelect = rightValue();
        if (!rhsForSelect)
          return;
        std::string safeDivisor = appendSelectTemporary(
            "llvm_mul_nuw_divisor_", variableOperand(zero),
            integerConstant(1, bits), std::move(*rhsForSelect),
            bits, context, output);
        std::string limit = appendBinaryTemporary(
            "llvm_mul_nuw_limit_", "udiv", integerConstant(-1, bits),
            variableOperand(safeDivisor), bits, context, output);
        auto lhs = leftValue();
        if (!lhs)
          return;
        std::string within = comparison(
            "llvm_mul_nuw_within_", "ule", std::move(*lhs),
            variableOperand(limit));
        require(boolean("llvm_mul_nuw_", "or", zero, within));
        break;
      }
      case Instruction::Shl: {
        auto amount = rightValue();
        if (!amount)
          return;
        std::string recovered = appendBinaryTemporary(
            "llvm_shl_nuw_recovered_", "lshr", resultValue(),
            std::move(*amount), bits, context, output);
        auto lhs = leftValue();
        if (!lhs)
          return;
        require(comparison(
            "llvm_shl_nuw_", "eq", variableOperand(recovered),
            std::move(*lhs)));
        break;
      }
      default:
        reject(binary, "nuw flag is unsupported for this opcode");
        return;
      }
    }

    if (noSignedWrap) {
      switch (binary.getOpcode()) {
      case Instruction::Add:
      case Instruction::Sub: {
        auto lhs = leftValue();
        auto rhs = rightValue();
        if (!lhs || !rhs)
          return;
        std::string leftSign =
            sign("llvm_signed_left_", std::move(*lhs));
        std::string rightSign =
            sign("llvm_signed_right_", std::move(*rhs));
        std::string resultSign =
            sign("llvm_signed_result_", resultValue());
        StringRef predicate =
            binary.getOpcode() == Instruction::Add ? "eq" : "ne";
        std::string risky = comparison(
            "llvm_signed_risky_", predicate,
            variableOperand(leftSign), variableOperand(rightSign));
        std::string sameResult = comparison(
            "llvm_signed_result_same_", "eq",
            variableOperand(resultSign), variableOperand(leftSign));
        std::string notRisky =
            invert("llvm_signed_not_risky_", risky);
        require(boolean(
            "llvm_signed_defined_", "or", notRisky, sameResult));
        break;
      }
      case Instruction::Mul: {
        auto rhsForZero = rightValue();
        if (!rhsForZero)
          return;
        std::string zero = comparison(
            "llvm_mul_nsw_zero_", "eq", std::move(*rhsForZero),
            integerConstant(0, bits));
        auto rhsForSelect = rightValue();
        if (!rhsForSelect)
          return;
        std::string safeDivisor = appendSelectTemporary(
            "llvm_mul_nsw_divisor_", variableOperand(zero),
            integerConstant(1, bits), std::move(*rhsForSelect),
            bits, context, output);
        int64_t minimum =
            bits == 64
                ? INT64_MIN
                : -(INT64_C(1) << (bits - 1));
        std::string resultMinimum = comparison(
            "llvm_mul_nsw_result_min_", "eq", resultValue(),
            integerConstant(minimum, bits));
        std::string divisorMinusOne = comparison(
            "llvm_mul_nsw_minus_one_", "eq",
            variableOperand(safeDivisor), integerConstant(-1, bits));
        std::string divisionOverflow = boolean(
            "llvm_mul_nsw_div_overflow_", "and",
            resultMinimum, divisorMinusOne);
        std::string safeDivision =
            invert("llvm_mul_nsw_safe_div_", divisionOverflow);
        std::string quotient = appendBinaryTemporary(
            "llvm_mul_nsw_quotient_", "sdiv", resultValue(),
            variableOperand(safeDivisor), bits, context, output);
        auto lhs = leftValue();
        if (!lhs)
          return;
        std::string reversible = comparison(
            "llvm_mul_nsw_reversible_", "eq",
            variableOperand(quotient), std::move(*lhs));
        std::string nonzeroValid = boolean(
            "llvm_mul_nsw_nonzero_", "and",
            safeDivision, reversible);
        require(boolean("llvm_mul_nsw_", "or", zero, nonzeroValid));
        break;
      }
      case Instruction::Shl: {
        auto amount = rightValue();
        if (!amount)
          return;
        std::string recovered = appendBinaryTemporary(
            "llvm_shl_nsw_recovered_", "ashr", resultValue(),
            std::move(*amount), bits, context, output);
        auto lhs = leftValue();
        if (!lhs)
          return;
        require(comparison(
            "llvm_shl_nsw_", "eq", variableOperand(recovered),
            std::move(*lhs)));
        break;
      }
      default:
        reject(binary, "nsw flag is unsupported for this opcode");
        return;
      }
    }

    if (exact) {
      if (
          binary.getOpcode() == Instruction::UDiv ||
          binary.getOpcode() == Instruction::SDiv) {
        StringRef remainderOperation =
            binary.getOpcode() == Instruction::UDiv ? "urem" : "srem";
        auto lhs = leftValue();
        auto rhs = rightValue();
        if (!lhs || !rhs)
          return;
        std::string remainder = appendBinaryTemporary(
            "llvm_exact_remainder_", remainderOperation,
            std::move(*lhs), std::move(*rhs), bits, context, output);
        require(comparison(
            "llvm_exact_division_", "eq", variableOperand(remainder),
            integerConstant(0, bits)));
      } else if (
          binary.getOpcode() == Instruction::LShr ||
          binary.getOpcode() == Instruction::AShr) {
        auto amount = rightValue();
        if (!amount)
          return;
        std::string restored = appendBinaryTemporary(
            "llvm_exact_shift_restore_", "shl", resultValue(),
            std::move(*amount), bits, context, output);
        auto lhs = leftValue();
        if (!lhs)
          return;
        require(comparison(
            "llvm_exact_shift_", "eq", variableOperand(restored),
            std::move(*lhs)));
      } else {
        reject(binary, "exact flag is unsupported for this opcode");
      }
    }
    if (deferPoison) {
      if (deferredConditions.empty()) {
        reject(binary, "deferred poison has no definedness condition");
        return;
      }
      std::string defined = deferredConditions.front();
      for (size_t index = 1; index < deferredConditions.size(); ++index)
        defined = appendBinaryTemporary(
            "llvm_deferred_defined_", "and",
            variableOperand(defined),
            variableOperand(deferredConditions[index]),
            1, context, output);
      context.poisonConditions[&binary] = std::move(defined);
      usesDeferredPoisonFreeze = true;
      usesMultiConsumerDeferredPoison |=
          deferredPoisonFanout;
      if (
          hasIncomingPoison ||
          !isa<FreezeInst>(*binary.user_begin()) ||
          (binary.getOpcode() != Instruction::Add &&
           binary.getOpcode() != Instruction::Sub &&
           binary.getOpcode() != Instruction::Mul))
        usesTransitiveDeferredPoison = true;
    }
  }

  void lowerInstruction(Instruction &instruction, FunctionContext &context,
                        json::Array &output,
                        std::map<std::string, json::Array> &extraBlocks) {
    if (isa<PHINode>(instruction) || isa<DbgInfoIntrinsic>(instruction))
      return;
    if (auto *extract = dyn_cast<ExtractValueInst>(&instruction)) {
      if (isa<LandingPadInst>(extract->getAggregateOperand())) {
        ArrayRef<unsigned> indices = extract->getIndices();
        if (indices.size() == 1 && indices.front() == 0 &&
            isExceptionTokenExtract(*extract)) {
          unsigned bits = M.getDataLayout().getPointerSizeInBits(
              extract->getType()->getPointerAddressSpace());
          if (extract->getType()->getPointerAddressSpace() != 0 ||
              bits == 0 || bits > 64) {
            reject(
                instruction,
                "landingpad exception token has an unsupported pointer type");
            return;
          }
          json::Object lowered;
          lowered["op"] = "exception_token";
          lowered["dst"] = context.values.lookup(extract);
          lowered["bits"] = static_cast<int64_t>(bits);
          output.push_back(std::move(lowered));
          usesCleanupExceptions = true;
          usesExceptionCatchLifecycle = true;
          return;
        }
        unsigned bits = integerBits(extract->getType());
        if (indices.size() != 1 || indices.front() != 1 || bits != 32) {
          reject(
              instruction,
              "landingpad extractvalue requires the i32 type selector");
          return;
        }
        json::Object lowered;
        lowered["op"] = "exception_type";
        lowered["dst"] = context.values.lookup(extract);
        lowered["bits"] = 32;
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesTypedExceptions = true;
        return;
      }
      auto *aggregate =
          dyn_cast<CallBase>(extract->getAggregateOperand());
      Function *callee =
          aggregate == nullptr ? nullptr : aggregate->getCalledFunction();
      ArrayRef<unsigned> indices = extract->getIndices();
      if (
          callee == nullptr || !callee->isIntrinsic() ||
          !isOverflowArithmeticIntrinsic(callee->getIntrinsicID()) ||
          indices.size() != 1 || indices.front() > 1) {
        reject(
            instruction,
            "extractvalue requires a supported overflow aggregate");
        return;
      }
      unsigned bits = integerBits(extract->getType());
      std::string destination = context.values.lookup(extract);
      std::string source = context.values.lookup(aggregate) +
                           (indices.front() == 0
                                ? "__value"
                                : "__overflow");
      if (bits == 0 || destination.empty() || source.empty()) {
        reject(
            instruction,
            "overflow extractvalue has no bounded scalar binding");
        return;
      }
      json::Object lowered;
      lowered["op"] = "unary";
      lowered["operator"] = "identity";
      lowered["dst"] = destination;
      lowered["value"] = variableOperand(source);
      lowered["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *allocation = dyn_cast<AllocaInst>(&instruction)) {
      auto pointer = staticPointer(allocation, instruction);
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          allocation->getAddressSpace());
      if (!pointer || pointerBits == 0 || pointerBits > 64)
        return;
      json::Object lowered;
      lowered["op"] = "const";
      lowered["dst"] = context.values.lookup(allocation);
      lowered["value"] = static_cast<int64_t>(pointer->address);
      lowered["bits"] = static_cast<int64_t>(pointerBits);
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *getElement = dyn_cast<GetElementPtrInst>(&instruction)) {
      if (programUsesExceptionObjectArena &&
          exceptionCatchObjectOffset(getElement)) {
        // Catch-object projections are materialized by the corresponding
        // exception_object_load; the host pointer is never checkpointed.
        return;
      }
      auto alternatives = pointerAlternatives(
          getElement, context, instruction);
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          instruction.getType()->getPointerAddressSpace());
      auto base = pointerOperand(
          getElement->getPointerOperand(), context, instruction);
      if (!alternatives || alternatives->empty() || !base ||
          pointerBits == 0 || pointerBits > 64)
        return;
      APInt offset(pointerBits, 0, true);
      std::string destination = context.values.lookup(&instruction);
      if (getElement->accumulateConstantOffset(
              M.getDataLayout(), offset)) {
        json::Object lowered;
        lowered["dst"] = destination;
        lowered["bits"] = static_cast<int64_t>(pointerBits);
        if (offset.isZero()) {
          lowered["op"] = "unary";
          lowered["operator"] = "identity";
          lowered["value"] = std::move(*base);
        } else {
          lowered["op"] = "binary";
          lowered["operator"] = "add";
          lowered["left"] = std::move(*base);
          lowered["right"] = integerConstant(
              offset.getSExtValue(), pointerBits);
        }
        output.push_back(std::move(lowered));
        return;
      }
      MapVector<Value *, APInt> variableOffsets;
      APInt constantOffset(pointerBits, 0, true);
      if (!getElement->collectOffset(
              M.getDataLayout(), pointerBits, variableOffsets,
              constantOffset) ||
          variableOffsets.size() != 1 ||
          !constantOffset.isSignedIntN(64)) {
        reject(instruction, "GEP requires one bounded symbolic offset");
        return;
      }
      json::Object adjustedBase = std::move(*base);
      if (!constantOffset.isZero()) {
        std::string temporary = context.temporaryName("gep_base_");
        json::Object adjustment;
        adjustment["op"] = "binary";
        adjustment["operator"] = "add";
        adjustment["dst"] = temporary;
        adjustment["left"] = std::move(adjustedBase);
        adjustment["right"] = integerConstant(
            constantOffset.getSExtValue(), pointerBits);
        adjustment["bits"] = static_cast<int64_t>(pointerBits);
        output.push_back(std::move(adjustment));
        adjustedBase = variableOperand(temporary);
      }
      Value *indexValue = variableOffsets.begin()->first;
      const APInt &scale = variableOffsets.begin()->second;
      auto index = operand(indexValue, context, instruction);
      unsigned indexBits = integerBits(indexValue->getType());
      if (!index || indexBits == 0 || !scale.isSignedIntN(64)) {
        reject(instruction, "GEP symbolic offset is not bounded");
        return;
      }
      json::Object lowered;
      lowered["op"] = "pointer_offset";
      lowered["dst"] = destination;
      lowered["bits"] = static_cast<int64_t>(pointerBits);
      lowered["base"] = std::move(adjustedBase);
      lowered["index"] = std::move(*index);
      lowered["index_bits"] = static_cast<int64_t>(indexBits);
      lowered["scale"] = integerConstant(
          scale.getSExtValue(), pointerBits);
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *load = dyn_cast<LoadInst>(&instruction)) {
      if (isTrivialScalarCatchObjectLoad(*load)) {
        auto exceptionOffset = exceptionCatchObjectOffset(
            load->getPointerOperand());
        if (!exceptionOffset) {
          reject(instruction, "exception object load has no constant offset");
          return;
        }
        json::Object lowered;
        lowered["op"] = programUsesExceptionObjectArena
                            ? "exception_object_load"
                            : "exception_value";
        lowered["dst"] = context.values.lookup(load);
        lowered["bits"] = static_cast<int64_t>(integerBits(load->getType()));
        if (programUsesExceptionObjectArena) {
          lowered["bytes"] = static_cast<int64_t>(
              fixedStoreBytes(M.getDataLayout(), load->getType()));
          lowered["offset"] = static_cast<int64_t>(*exceptionOffset);
          usesExceptionObjectFields |= *exceptionOffset != 0;
        }
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesExceptionCatchLifecycle = true;
        usesTrivialScalarCatchObjects |= !programUsesExceptionObjectArena;
        usesExceptionObjectArena |= programUsesExceptionObjectArena;
        return;
      }
      bool pointerValue = load->getType()->isPointerTy();
      unsigned bits =
          pointerValue
              ? M.getDataLayout().getPointerSizeInBits(
                    load->getType()->getPointerAddressSpace())
              : integerBits(load->getType());
      uint64_t bytes = bits == 0
                           ? 0
                           : fixedStoreBytes(
                                 M.getDataLayout(), load->getType());
      if (bits == 0 || bytes == 0 || bytes > 8 || load->isVolatile() ||
          load->isAtomic()) {
        reject(instruction,
               "load must be a non-atomic bounded scalar access");
        return;
      }
      auto alternatives = pointerAlternatives(
          load->getPointerOperand(), context, instruction);
      if (!alternatives || alternatives->empty())
        return;
      bool isUnion = alternatives->size() != 1 ||
                     !alternatives->front().guards.empty();
      bool trackedPointerCell =
          pointerValue &&
          std::any_of(
              alternatives->begin(), alternatives->end(),
              [](const PointerAlternative &alternative) {
                return alternative.pointer.stackObject != nullptr ||
                       alternative.pointer.heapObject != nullptr;
              });
      if (
          trackedPointerCell &&
          !reachingPointerStores(
              *load, context, instruction)) {
        return;
      }
      std::optional<json::Array>
          collectiveInitializationCertificate;
      std::optional<json::Object> guardedInitializationTranscript;
      std::optional<MemorySSAHeapInitializationCertificate>
          memorySSAInitialization;
      std::optional<json::Object> memorySSAInitializationTranscript;
      std::optional<DynamicByteLaneCoverCertificate>
          dynamicByteLaneCover;
      std::optional<json::Object> dynamicByteLaneCoverTranscript;
      std::optional<LoopMemoryPhiByteLaneCertificate>
          loopMemoryPhiByteLane;
      std::optional<json::Object> loopMemoryPhiByteLaneTranscript;
      std::optional<MultiLatchLoopMemoryPhiCertificate>
          multiLatchLoopMemoryPhi;
      std::optional<json::Object> multiLatchLoopMemoryPhiTranscript;
      std::optional<std::vector<InterproceduralHeapEffectCertificate>>
          interproceduralInitialization;
      std::optional<json::Value>
          interproceduralInitializationTranscript;
      if (!trackedPointerCell && isUnion) {
        std::vector<uint64_t> collectiveInitializationBases;
        std::optional<GuardedHeapInitializationCertificate>
            guardedInitialization;
        if (!hasDominatingUnionStore(
                *load, *alternatives, bytes, context,
                &collectiveInitializationBases,
                &guardedInitialization,
                &memorySSAInitialization,
                &dynamicByteLaneCover,
                &loopMemoryPhiByteLane,
                &multiLatchLoopMemoryPhi,
                &interproceduralInitialization))
          return;
        if (!collectiveInitializationBases.empty()) {
          json::Array initializationBases;
          for (uint64_t base : collectiveInitializationBases)
            initializationBases.push_back(
                static_cast<int64_t>(base));
          collectiveInitializationCertificate =
              std::move(initializationBases);
          usesCollectiveHeapUnionInitialization = true;
        }
        if (guardedInitialization) {
          json::Object transcript;
          transcript["schema"] =
              "symcc-guarded-heap-union-initialization-v1";
          transcript["root"] = context.blocks.lookup(
              guardedInitialization->root);
          transcript["merge"] = context.blocks.lookup(
              guardedInitialization->merge);
          transcript["depth"] = static_cast<int64_t>(
              guardedInitialization->depth);
          json::Array paths;
          for (const GuardedHeapInitializationPath &path :
               guardedInitialization->paths) {
            json::Object item;
            json::Array pathBlocks;
            for (const BasicBlock *block : path.blocks)
              pathBlocks.push_back(context.blocks.lookup(block));
            item["blocks"] = std::move(pathBlocks);
            json::Array decisions;
            for (const GuardedHeapInitializationDecision &decision :
                 path.decisions) {
              json::Object branch;
              branch["block"] = context.blocks.lookup(
                  decision.branch->getParent());
              branch["equals"] = decision.expected;
              decisions.push_back(std::move(branch));
            }
            item["decisions"] = std::move(decisions);
            item["predecessor"] = context.blocks.lookup(
                path.predecessor);
            item["base"] = static_cast<int64_t>(path.base);
            item["load_address"] =
                static_cast<int64_t>(path.loadAddress);
            json::Object store;
            store["block"] = context.blocks.lookup(
                path.store->getParent());
            store["ordinal"] = static_cast<int64_t>(
                path.storeOrdinal);
            store["address"] = static_cast<int64_t>(
                path.storeAddress);
            store["bytes"] = static_cast<int64_t>(
                path.storeBytes);
            item["store"] = std::move(store);
            paths.push_back(std::move(item));
          }
          transcript["paths"] = std::move(paths);
          guardedInitializationTranscript = std::move(transcript);
          usesGuardCorrelatedHeapUnionInitialization = true;
        }
      } else if (!trackedPointerCell) {
        if (!hasDominatingObjectStore(
                *load, alternatives->front().pointer, bytes, context,
                &memorySSAInitialization,
                &dynamicByteLaneCover,
                &loopMemoryPhiByteLane,
                &multiLatchLoopMemoryPhi,
                &interproceduralInitialization))
          return;
      }
      if (memorySSAInitialization) {
        memorySSAInitializationTranscript =
            memorySSAHeapInitializationRecord(
                *memorySSAInitialization, context);
        usesMemorySSAHeapInitialization = true;
      }
      if (dynamicByteLaneCover) {
        dynamicByteLaneCoverTranscript =
            dynamicByteLaneCoverRecord(
                *dynamicByteLaneCover, context);
        usesDynamicByteLaneCover = true;
      }
      if (loopMemoryPhiByteLane) {
        loopMemoryPhiByteLaneTranscript =
            loopMemoryPhiByteLaneRecord(
                *loopMemoryPhiByteLane, context);
        usesLoopMemoryPhiByteLaneInduction = true;
        usesStridedLoopMemoryPhiByteLaneInduction |=
            loopMemoryPhiByteLane->isStrided();
        usesConditionalLoopMemoryPhiByteLaneInduction |=
            loopMemoryPhiByteLane->isConditional();
      }
      if (multiLatchLoopMemoryPhi) {
        multiLatchLoopMemoryPhiTranscript =
            multiLatchLoopMemoryPhiRecord(
                *multiLatchLoopMemoryPhi, context);
        usesLoopMemoryPhiByteLaneInduction = true;
        usesStridedLoopMemoryPhiByteLaneInduction |=
            multiLatchLoopMemoryPhi->isStrided();
        if (multiLatchLoopMemoryPhi->nestedSummary) {
          usesNestedLoopMemoryPhiSummaryComposition = true;
          usesNestedLoopMemoryPhiLastWriteValueSummary |=
              multiLatchLoopMemoryPhi->nestedValueSummary;
          usesNestedLoopMemoryPhiTwoDimensionalAffineSummary |=
              multiLatchLoopMemoryPhi->nestedTwoDimensionalSummary;
          usesNestedLoopMemoryPhiAffineSymbolicValueSummary |=
              multiLatchLoopMemoryPhi->nestedSymbolicValueSummary;
          usesNestedLoopMemoryPhiPiecewiseAffineValueSummary |=
              multiLatchLoopMemoryPhi->nestedPiecewiseValueSummary ||
              multiLatchLoopMemoryPhi->nestedDecisionDagValueSummary;
          usesNestedLoopMemoryPhiDecisionDagValueSummary |=
              multiLatchLoopMemoryPhi->nestedDecisionDagValueSummary;
          if (multiLatchLoopMemoryPhi->executableMemoryTransfer) {
            bool alreadyRegistered = std::any_of(
                context.executableNestedLoopMemoryTransfers.begin(),
                context.executableNestedLoopMemoryTransfers.end(),
                [&](const ExecutableNestedLoopMemoryTransfer &transfer) {
                  return transfer.certificate.preheader ==
                             multiLatchLoopMemoryPhi->preheader &&
                         transfer.certificate.header ==
                             multiLatchLoopMemoryPhi->header;
                });
            if (!alreadyRegistered) {
              context.executableNestedLoopMemoryTransfers.push_back({
                  *multiLatchLoopMemoryPhi,
                  context.values.lookup(load)});
              usesExecutableNestedLoopMemoryTransfer = true;
            }
          }
        } else {
          usesMultiLatchLoopMemoryPhiFixedPoint = true;
          usesOrderedMultiLatchLoopMemoryPhiTransfer |=
              multiLatchLoopMemoryPhi->hasOrderedWriterTransfer();
        }
      }
      if (interproceduralInitialization) {
        if (interproceduralInitialization->size() == 1) {
          interproceduralInitializationTranscript =
              interproceduralHeapEffectRecord(
                  interproceduralInitialization->front(), context);
        } else {
          json::Array transcripts;
          for (const InterproceduralHeapEffectCertificate &certificate :
               *interproceduralInitialization)
            transcripts.push_back(
                interproceduralHeapEffectRecord(certificate, context));
          interproceduralInitializationTranscript =
              std::move(transcripts);
        }
        usesInterproceduralHeapEffects = true;
      }
      json::Object lowered;
      lowered["op"] = "load";
      lowered["dst"] = context.values.lookup(load);
      if (isUnion) {
        auto address = pointerOperand(
            load->getPointerOperand(), context, instruction);
        if (!address)
          return;
        lowered["address"] = std::move(*address);
        json::Array cases;
        uint64_t totalAliases = 0;
        for (const PointerAlternative &alternative : *alternatives) {
          const StaticPointer &pointer = alternative.pointer;
          if ((pointer.address == 0 &&
               pointer.dynamicIndex == nullptr) ||
              (pointer.dynamicIndex == nullptr &&
               (pointer.objectOffset < 0 ||
                static_cast<uint64_t>(pointer.objectOffset) >
                    pointer.objectSize ||
                bytes > pointer.objectSize -
                            static_cast<uint64_t>(pointer.objectOffset))))
            continue;
          auto aliases = memoryAliases(
              pointer, bytes, instruction, false);
          if (!aliases)
            return;
          totalAliases += aliases->addresses.size();
          if (totalAliases > aliasLimit) {
            reject(instruction,
                   "pointer union alias set exceeds continuation limit");
            return;
          }
          auto item = aliasCase(
              alternative, *aliases, context, instruction);
          if (!item)
            return;
          cases.push_back(std::move(*item));
        }
        if (cases.empty()) {
          reject(instruction, "pointer union load has no valid alternatives");
          return;
        }
        lowered["alias_cases"] = std::move(cases);
      } else {
        const StaticPointer &pointer = alternatives->front().pointer;
        auto aliases = memoryAliases(
            pointer, bytes, instruction, false);
        if (!aliases)
          return;
        if (pointer.dynamicIndex == nullptr) {
          lowered["address"] = integerConstant(
              static_cast<int64_t>(pointer.address),
              M.getDataLayout().getPointerSizeInBits());
        } else {
          auto address = pointerOperand(
              load->getPointerOperand(), context, instruction);
          auto aliasIndex = operand(
              pointer.dynamicIndex, context, instruction);
          if (!address || !aliasIndex)
            return;
          lowered["address"] = std::move(*address);
          lowered["aliases"] = aliasArray(aliases->addresses);
          lowered["alias_index_values"] =
              aliasIndexArray(aliases->indices);
          lowered["alias_index"] = std::move(*aliasIndex);
          lowered["alias_index_bits"] =
              static_cast<int64_t>(pointer.dynamicIndexBits);
          lowered["alias_index_min"] = aliases->minimumIndex;
          lowered["alias_index_max"] = aliases->maximumIndex;
        }
      }
      lowered["bits"] = static_cast<int64_t>(bits);
      lowered["bytes"] = static_cast<int64_t>(bytes);
      if (collectiveInitializationCertificate)
        lowered["initialization_bases"] =
            std::move(*collectiveInitializationCertificate);
      if (guardedInitializationTranscript)
        lowered["initialization_guard_tree"] =
            std::move(*guardedInitializationTranscript);
      if (memorySSAInitializationTranscript)
        lowered["initialization_memoryssa"] =
            std::move(*memorySSAInitializationTranscript);
      if (dynamicByteLaneCoverTranscript)
        lowered["initialization_dynamic_byte_lane"] =
            std::move(*dynamicByteLaneCoverTranscript);
      if (loopMemoryPhiByteLaneTranscript)
        lowered["initialization_loop_memoryphi"] =
            std::move(*loopMemoryPhiByteLaneTranscript);
      if (multiLatchLoopMemoryPhiTranscript)
        lowered["initialization_loop_memoryphi"] =
            std::move(*multiLatchLoopMemoryPhiTranscript);
      if (interproceduralInitializationTranscript)
        lowered["initialization_interprocedural"] =
            std::move(*interproceduralInitializationTranscript);
      output.push_back(std::move(lowered));
      usesPointerMemory |= pointerValue;
      if (!pointerValue) {
        auto byteLaneIndex =
            context.byteLaneCompositionIndices.find(
                load);
        if (
            byteLaneIndex !=
            context.byteLaneCompositionIndices.end()) {
          const auto &composition =
              context.byteLaneCompositions[
                  byteLaneIndex->second];
          std::vector<std::string> sources;
          for (const auto &lane :
               composition.lanes) {
            if (lane.store == nullptr)
              continue;
            auto defined =
                context.byteLaneStoreDefinedNames.find(
                    lane.store);
            if (
                defined ==
                    context.byteLaneStoreDefinedNames.end() ||
                std::find(
                    sources.begin(), sources.end(),
                    defined->second) != sources.end())
              continue;
            sources.push_back(defined->second);
          }
          if (sources.empty()) {
            reject(
                *load,
                "byte-lane memory composition has no "
                "deferred-poison source");
            return;
          }
          if (sources.size() == 1) {
            json::Object bind;
            bind["op"] = "unary";
            bind["operator"] = "identity";
            bind["dst"] = composition.defined;
            bind["value"] =
                variableOperand(sources.front());
            bind["bits"] = 1;
            output.push_back(std::move(bind));
          } else {
            std::string accumulated =
                sources.front();
            for (size_t index = 1;
                 index < sources.size(); ++index) {
              std::string destination =
                  index + 1 == sources.size()
                      ? composition.defined
                      : context.temporaryName(
                            "byte_lane_defined_");
              json::Object combine;
              combine["op"] = "binary";
              combine["operator"] = "and";
              combine["dst"] = destination;
              combine["left"] =
                  variableOperand(accumulated);
              combine["right"] =
                  variableOperand(sources[index]);
              combine["bits"] = 1;
              output.push_back(
                  std::move(combine));
              accumulated =
                  std::move(destination);
            }
          }
          context.poisonConditions[load] =
              composition.defined;
          usesByteLaneMemoryDefinedness = true;
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesCanonicalMemoryDeferredPoison = true;
          usesCrossBlockMemoryDeferredPoison |=
              composition.crossBlock;
          for (const auto &lane :
               composition.lanes) {
            if (lane.store == nullptr)
              continue;
            auto sinks =
                byteLanePoisonLoads.find(
                    lane.store);
            usesMultiAccessMemoryDeferredPoison |=
                sinks != byteLanePoisonLoads.end() &&
                sinks->second.size() > 1;
          }
        }
        auto byteLanePhiIndex =
            context.byteLanePhiIndices.find(load);
        if (
            byteLanePhiIndex !=
            context.byteLanePhiIndices.end()) {
          const auto &phi =
              context.byteLanePhis[
                  byteLanePhiIndex->second];
          context.poisonConditions[load] =
              phi.defined;
          usesByteLaneMemoryDefinednessPhi = true;
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesCanonicalMemoryDeferredPoison = true;
          usesCrossBlockMemoryDeferredPoison = true;
          usesBranchMemoryDeferredPoison = true;
          for (const auto &incoming :
               phi.incoming)
            for (const auto &lane :
                 incoming.lanes) {
              if (lane.store == nullptr)
                continue;
              auto sinks =
                  byteLanePoisonLoads.find(
                      lane.store);
              usesMultiAccessMemoryDeferredPoison |=
                  sinks !=
                      byteLanePoisonLoads.end() &&
                  sinks->second.size() > 1;
            }
        }
        auto cyclicByteLanePhiIndex =
            context.cyclicByteLanePhiIndices.find(
                load);
        if (
            cyclicByteLanePhiIndex !=
            context.cyclicByteLanePhiIndices.end()) {
          const auto &phi =
              context.cyclicByteLanePhis[
                  cyclicByteLanePhiIndex->second];
          std::string accumulated =
              phi.laneDefined.front();
          for (size_t index = 1;
               index < phi.laneDefined.size();
               ++index) {
            std::string destination =
                index + 1 ==
                        phi.laneDefined.size()
                    ? phi.defined
                    : context.temporaryName(
                          "cyclic_byte_lane_defined_");
            json::Object combine;
            combine["op"] = "binary";
            combine["operator"] = "and";
            combine["dst"] = destination;
            combine["left"] =
                variableOperand(accumulated);
            combine["right"] =
                variableOperand(
                    phi.laneDefined[index]);
            combine["bits"] = 1;
            output.push_back(std::move(combine));
            accumulated = std::move(destination);
          }
          context.poisonConditions[load] =
              phi.defined;
          if (phi.forwardedComposedTripleGroupsRecursiveConditional)
            usesForwardedComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.composedTripleGroupsRecursiveConditional)
            usesComposedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedDoubleComposedGroupsRecursiveConditional)
            usesForwardedDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.doubleComposedGroupsRecursiveConditional)
            usesDoubleComposedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedMixedGroupsRecursiveConditional)
            usesForwardedMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.mixedGroupsRecursiveConditional)
            usesMixedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedTripleGroupsRecursiveConditional)
            usesForwardedTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.tripleGroupsRecursiveConditional)
            usesTripleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedMultipleGroupsRecursiveConditional)
            usesForwardedMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.multipleGroupsRecursiveConditional)
            usesMultipleGroupsRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedMultiCarryRecursiveConditional)
            usesForwardedMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.multiCarryRecursiveConditional)
            usesMultiCarryComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedComposedRepeatedSourceRecursiveConditional)
            usesForwardedComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.composedRepeatedSourceRecursiveConditional)
            usesComposedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedRepeatedSourceRecursiveConditional)
            usesForwardedRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.repeatedSourceRecursiveConditional)
            usesRepeatedSourceRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedGroupedRecursiveConditional)
            usesForwardedGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.groupedRecursiveConditional)
            usesGroupedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedRecursiveConditional)
            usesForwardedRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.recursiveConditional)
            usesRecursiveConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedMultiArmConditional)
            usesForwardedMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.multiArmConditional)
            usesMultiArmConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.forwardedConditional)
            usesForwardedConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else if (phi.conditional)
            usesConditionalCyclicByteLaneMemoryDefinednessPhi =
                true;
          else
            usesCyclicByteLaneMemoryDefinednessPhi =
                true;
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesCanonicalMemoryDeferredPoison = true;
          usesCrossBlockMemoryDeferredPoison = true;
          usesBranchMemoryDeferredPoison = true;
          for (const auto &incoming :
               phi.incoming)
            for (const auto &lane :
                 incoming.lanes) {
              if (
                  lane.kind !=
                      FunctionContext::
                          CyclicByteLaneMemorySource::
                              Kind::Store ||
                  lane.store == nullptr)
                continue;
              auto sinks =
                  byteLanePoisonLoads.find(
                      lane.store);
              usesMultiAccessMemoryDeferredPoison |=
                  sinks !=
                      byteLanePoisonLoads.end() &&
                  sinks->second.size() > 1;
            }
          for (const auto &transfer :
               phi.conditionalTransfers) {
            auto sinks =
                byteLanePoisonLoads.find(
                    transfer.store);
            usesMultiAccessMemoryDeferredPoison |=
                sinks !=
                    byteLanePoisonLoads.end() &&
                sinks->second.size() > 1;
          }
          for (const auto &transfer :
               phi.multiArmTransfers)
            for (const auto &arm :
                 transfer.arms) {
              if (arm.store == nullptr)
                continue;
              auto sinks =
                  byteLanePoisonLoads.find(
                      arm.store);
              usesMultiAccessMemoryDeferredPoison |=
                  sinks !=
                      byteLanePoisonLoads.end() &&
                  sinks->second.size() > 1;
            }
          for (const auto &transfer :
               phi.recursiveTransfers)
            for (const auto &leaf :
                 transfer.leaves)
              for (const auto &lane :
                   leaf.lanes) {
                if (
                    lane.kind !=
                        FunctionContext::
                            CyclicByteLaneMemorySource::
                                Kind::Store ||
                    lane.store == nullptr)
                  continue;
                auto sinks =
                    byteLanePoisonLoads.find(
                        lane.store);
                usesMultiAccessMemoryDeferredPoison |=
                    sinks !=
                        byteLanePoisonLoads.end() &&
                    sinks->second.size() > 1;
              }
        }
        bool branchMerge = false;
        const StoreInst *source =
            cyclicByteLanePhiIndex ==
                    context
                        .cyclicByteLanePhiIndices
                        .end()
                ? boundedPoisonStoreBeforeLoad(
                      *load, &branchMerge)
                : nullptr;
        auto poison =
            source == nullptr
                ? context.poisonConditions.end()
                : context.poisonConditions.find(source);
        if (poison != context.poisonConditions.end()) {
          context.poisonConditions[load] = poison->second;
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesCrossBlockMemoryDeferredPoison |=
              source->getParent() != load->getParent();
          usesBranchMemoryDeferredPoison |=
              branchMerge;
          bool canonicalAddress = false;
          exactScalarMemoryPair(
              *source, *load, &canonicalAddress);
          usesCanonicalMemoryDeferredPoison |= canonicalAddress;
        }
        auto mergeIndex =
            context.memoryPoisonMergeIndices.find(load);
        if (
            mergeIndex !=
            context.memoryPoisonMergeIndices.end()) {
          const auto &merge =
              context.memoryPoisonMerges[
                  mergeIndex->second];
          context.poisonConditions[load] =
              merge.defined;
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesCrossBlockMemoryDeferredPoison = true;
          usesBranchMemoryDeferredPoison = true;
          usesMemoryDefinednessPhi = true;
          usesMultiCellMemoryDefinednessPhi |=
              merge.multiCell;
          usesIdentifiedObjectMultiCellMemoryDefinednessPhi |=
              merge.identifiedObjectMultiCell;
          usesFixedHeapObjectMultiCellMemoryDefinednessPhi |=
              merge.fixedHeapObjectMultiCell;
          usesFinitePointerDomainMultiCellMemoryDefinednessPhi |=
              merge.finitePointerDomainMultiCell;
          usesGuardCorrelatedPointerDomainMultiCellMemoryDefinednessPhi |=
              merge.guardCorrelatedPointerDomainMultiCell;
          usesPhiCorrelatedPointerDomainMultiCellMemoryDefinednessPhi |=
              merge.phiCorrelatedPointerDomainMultiCell;
          usesSymbolicIndexIntervalMultiCellMemoryDefinednessPhi |=
              merge.symbolicIndexIntervalMultiCell;
          usesInterproceduralMemoryDefinednessPhi |=
              merge.interprocedural;
          usesMultilevelMemoryDefinednessPhi |=
              merge.multilevel;
          usesCyclicMemoryDefinednessPhi |=
              merge.cyclic;
          usesCyclicMemoryDefinednessCarry |=
              merge.carry;
          usesConditionalMemoryDefinednessCarry |=
              merge.conditionalCarry;
          usesForwardedConditionalMemoryDefinednessCarry |=
              merge.forwardedConditionalCarry;
          usesMultiArmConditionalMemoryDefinednessCarry |=
              merge.multiArmConditionalCarry;
          usesEquivalentDefinedStoreMemoryCarry |=
              merge.equivalentDefinedStoreCarry;
          usesSharedPoisonStoreMemoryCarry |=
              merge.sharedPoisonStoreCarry;
          usesNestedConditionalMemoryDefinednessCarry |=
              merge.nestedConditionalCarry;
          usesRecursiveConditionalMemoryDefinednessCarry |=
              merge.recursiveConditionalCarry;
          usesGroupedRecursiveConditionalMemoryDefinednessCarry |=
              merge.groupedRecursiveConditionalCarry;
          usesRepeatedSourceRecursiveMemoryDefinednessCarry |=
              merge.repeatedSourceRecursiveConditionalCarry;
          usesMultiCarryRecursiveMemoryDefinednessCarry |=
              merge.multiCarryRecursiveConditionalCarry;
          usesInitialMemoryDefinednessMerge |=
              merge.initial;
          usesInitialSubobjectDefinednessMerge |=
              merge.initialSubobject;
          for (const auto &item : merge.incoming) {
            if (item.store == nullptr)
              continue;
            bool canonicalAddress = false;
            exactScalarMemoryPair(
                *item.store, *load,
                &canonicalAddress);
            usesCanonicalMemoryDeferredPoison |=
                canonicalAddress;
          }
        }
      }
      return;
    }
    if (auto *store = dyn_cast<StoreInst>(&instruction)) {
      bool pointerValue =
          store->getValueOperand()->getType()->isPointerTy();
      unsigned bits =
          pointerValue
              ? M.getDataLayout().getPointerSizeInBits(
                    store->getValueOperand()->getType()
                        ->getPointerAddressSpace())
              : integerBits(store->getValueOperand()->getType());
      uint64_t bytes =
          bits == 0
              ? 0
              : fixedStoreBytes(
                    M.getDataLayout(), store->getValueOperand()->getType());
      if (bits == 0 || bytes == 0 || bytes > 8 ||
          store->isVolatile() || store->isAtomic()) {
        reject(instruction,
               "store must be a non-atomic bounded scalar access");
        return;
      }
      auto alternatives = pointerAlternatives(
          store->getPointerOperand(), context, instruction);
      std::optional<json::Object> value;
      if (pointerValue) {
        if (isFunctionPointerValue(store->getValueOperand())) {
          value = functionPointerOperand(
              store->getValueOperand(), context, instruction);
          usesFunctionPointerMemory = true;
        } else {
          value = pointerOperand(
              store->getValueOperand(), context, instruction);
        }
      } else {
        value = operand(
            store->getValueOperand(), context, instruction);
      }
      if (!alternatives || alternatives->empty() || !value)
        return;
      bool isUnion = alternatives->size() != 1 ||
                     !alternatives->front().guards.empty();
      json::Object lowered;
      lowered["op"] = "store";
      if (isUnion) {
        auto address = pointerOperand(
            store->getPointerOperand(), context, instruction);
        if (!address)
          return;
        lowered["address"] = std::move(*address);
        json::Array cases;
        uint64_t totalAliases = 0;
        for (const PointerAlternative &alternative : *alternatives) {
          const StaticPointer &pointer = alternative.pointer;
          if ((pointer.address == 0 &&
               pointer.dynamicIndex == nullptr) ||
              pointer.readOnly ||
              (pointer.dynamicIndex == nullptr &&
               (pointer.objectOffset < 0 ||
                static_cast<uint64_t>(pointer.objectOffset) >
                    pointer.objectSize ||
                bytes > pointer.objectSize -
                            static_cast<uint64_t>(pointer.objectOffset))))
            continue;
          auto aliases = memoryAliases(
              pointer, bytes, instruction, true);
          if (!aliases)
            return;
          totalAliases += aliases->addresses.size();
          if (totalAliases > aliasLimit) {
            reject(instruction,
                   "pointer union alias set exceeds continuation limit");
            return;
          }
          auto item = aliasCase(
              alternative, *aliases, context, instruction);
          if (!item)
            return;
          cases.push_back(std::move(*item));
        }
        if (cases.empty()) {
          reject(instruction, "pointer union store has no valid alternatives");
          return;
        }
        lowered["alias_cases"] = std::move(cases);
      } else {
        const StaticPointer &pointer = alternatives->front().pointer;
        auto aliases = memoryAliases(
            pointer, bytes, instruction, true);
        if (!aliases)
          return;
        if (pointer.dynamicIndex == nullptr) {
          lowered["address"] = integerConstant(
              static_cast<int64_t>(pointer.address),
              M.getDataLayout().getPointerSizeInBits());
        } else {
          auto address = pointerOperand(
              store->getPointerOperand(), context, instruction);
          auto aliasIndex = operand(
              pointer.dynamicIndex, context, instruction);
          if (!address || !aliasIndex)
            return;
          lowered["address"] = std::move(*address);
          lowered["aliases"] = aliasArray(aliases->addresses);
          lowered["alias_index_values"] =
              aliasIndexArray(aliases->indices);
          lowered["alias_index"] = std::move(*aliasIndex);
          lowered["alias_index_bits"] =
              static_cast<int64_t>(pointer.dynamicIndexBits);
          lowered["alias_index_min"] = aliases->minimumIndex;
          lowered["alias_index_max"] = aliases->maximumIndex;
        }
      }
      lowered["value"] = std::move(*value);
      lowered["bits"] = static_cast<int64_t>(bits);
      lowered["bytes"] = static_cast<int64_t>(bytes);
      auto byteLaneStore =
          context.byteLaneStoreIds.find(store);
      if (
          byteLaneStore !=
          context.byteLaneStoreIds.end()) {
        lowered["byte_lane_store"] =
            byteLaneStore->second;
        auto defined =
            context.byteLaneStoreDefinedNames.find(
                store);
          if (
              defined !=
              context.byteLaneStoreDefinedNames.end()) {
            lowered["byte_lane_defined"] =
                defined->second;
            bool writerGraphStore =
                std::any_of(
                    context.byteLaneCompositions.begin(),
                    context.byteLaneCompositions.end(),
                    [&](const auto &composition) {
                      return std::any_of(
                          composition.lanes.begin(),
                          composition.lanes.end(),
                          [&](const auto &lane) {
                            return lane.store == store;
                          });
                    }) ||
                std::any_of(
                    context.byteLanePhis.begin(),
                    context.byteLanePhis.end(),
                    [&](const auto &phi) {
                      return std::any_of(
                          phi.incoming.begin(),
                          phi.incoming.end(),
                          [&](const auto &incoming) {
                            return std::any_of(
                                incoming.lanes.begin(),
                                incoming.lanes.end(),
                                [&](const auto &lane) {
                                  return lane.store == store;
                                });
                          });
                    }) ||
                std::any_of(
                    context.cyclicByteLanePhis.begin(),
                    context.cyclicByteLanePhis.end(),
                    [&](const auto &phi) {
                      if (phi.conditional)
                        return std::any_of(
                            phi.conditionalTransfers.begin(),
                            phi.conditionalTransfers.end(),
                            [&](const auto &transfer) {
                              return std::any_of(
                                  transfer.lanes.begin(),
                                  transfer.lanes.end(),
                                  [&](const auto &lane) {
                                    return
                                        lane.kind ==
                                            FunctionContext::
                                                CyclicByteLaneMemorySource::
                                                    Kind::Store &&
                                        lane.store == store;
                                  });
                            });
                      if (phi.multiArmConditional)
                        return std::any_of(
                            phi.multiArmTransfers.begin(),
                            phi.multiArmTransfers.end(),
                            [&](const auto &transfer) {
                              return std::any_of(
                                  transfer.arms.begin(),
                                  transfer.arms.end(),
                                  [&](const auto &arm) {
                                    return std::any_of(
                                        arm.lanes.begin(),
                                        arm.lanes.end(),
                                        [&](const auto &lane) {
                                          return
                                              lane.kind ==
                                                  FunctionContext::
                                                      CyclicByteLaneMemorySource::
                                                          Kind::Store &&
                                              lane.store == store;
                                        });
                                  });
                            });
                      if (phi.recursiveConditional)
                        return std::any_of(
                            phi.recursiveTransfers.begin(),
                            phi.recursiveTransfers.end(),
                            [&](const auto &transfer) {
                              if (
                                  transfer.grouped ||
                                  transfer.repeatedSource ||
                                  transfer.composedRepeatedSource ||
                                  transfer.multiCarry ||
                                  transfer.multipleGroups ||
                                  transfer.tripleGroups ||
                                  transfer.mixedGroups ||
                                  transfer.doubleComposedGroups ||
                                  transfer.composedTripleGroups)
                                return false;
                              return std::any_of(
                                  transfer.leaves.begin(),
                                  transfer.leaves.end(),
                                  [&](const auto &leaf) {
                                    return std::any_of(
                                        leaf.lanes.begin(),
                                        leaf.lanes.end(),
                                        [&](const auto &lane) {
                                          return
                                              lane.kind ==
                                                  FunctionContext::
                                                      CyclicByteLaneMemorySource::
                                                          Kind::Store &&
                                              lane.store == store;
                                        });
                                  });
                            });
                      return std::any_of(
                          phi.incoming.begin(),
                          phi.incoming.end(),
                          [&](const auto &incoming) {
                            return std::any_of(
                                incoming.lanes.begin(),
                                incoming.lanes.end(),
                                [&](const auto &lane) {
                                  return
                                      lane.kind ==
                                          FunctionContext::
                                              CyclicByteLaneMemorySource::
                                                  Kind::Store &&
                                      lane.store == store;
                                });
                          });
                    });
            if (writerGraphStore) {
              auto poison = context.poisonConditions.find(
                  store->getValueOperand());
              if (poison != context.poisonConditions.end())
                lowered["byte_lane_poison_source"] =
                    poison->second;
            }
          }
      }
      output.push_back(std::move(lowered));
      usesPointerMemory |= pointerValue;
      if (!pointerValue) {
        auto poison = context.poisonConditions.find(
            store->getValueOperand());
        auto sinks =
            boundedPoisonLoadsAfterStore(*store);
        if (!sinks)
          sinks =
              poisonMergeLoadsForStore(*store);
        if (
            !sinks &&
            cyclicByteLanePoisonStores.count(
                store) != 0) {
          auto cyclicLoads =
              byteLanePoisonLoads.find(store);
          if (
              cyclicLoads !=
                  byteLanePoisonLoads.end() &&
              !cyclicLoads->second.empty())
            sinks = cyclicLoads->second;
        }
        if (
            poison != context.poisonConditions.end() &&
            sinks) {
          auto byteLaneDefined =
              context.byteLaneStoreDefinedNames.find(
                  store);
          if (
              byteLaneDefined !=
              context.byteLaneStoreDefinedNames.end()) {
            json::Object bind;
            bind["op"] = "unary";
            bind["operator"] = "identity";
            bind["dst"] =
                byteLaneDefined->second;
            bind["value"] =
                variableOperand(poison->second);
            bind["bits"] = 1;
            output.push_back(std::move(bind));
            context.poisonConditions[store] =
                byteLaneDefined->second;
          } else {
            context.poisonConditions[store] =
                poison->second;
          }
          usesTransitiveDeferredPoison = true;
          usesMemoryDeferredPoison = true;
          usesMultiAccessMemoryDeferredPoison |=
              sinks->size() > 1;
          for (const LoadInst *sink : *sinks) {
            bool canonicalAddress = false;
            exactScalarMemoryPair(
                *store, *sink, &canonicalAddress);
            usesCanonicalMemoryDeferredPoison |=
                canonicalAddress;
            usesCrossBlockMemoryDeferredPoison |=
                store->getParent() != sink->getParent();
          }
        } else if (
            context.byteLaneStoreDefinedNames.find(
                store) !=
            context.byteLaneStoreDefinedNames.end()) {
          reject(
              *store,
              "byte-lane memory store has no "
              "deferred-poison sidecar");
          return;
        }
      }
      return;
    }
    if (auto *binary = dyn_cast<BinaryOperator>(&instruction)) {
      lowerDefinedBinary(*binary, context, output);
      return;
    }
    if (auto *comparison = dyn_cast<ICmpInst>(&instruction)) {
      auto operation = comparisonOperator(comparison->getPredicate());
      bool pointerComparison =
          comparison->getOperand(0)->getType()->isPointerTy();
      if (!operation ||
          (!pointerComparison &&
           integerBits(comparison->getOperand(0)->getType()) == 0) ||
          (pointerComparison &&
           comparison->getPredicate() != ICmpInst::ICMP_EQ &&
           comparison->getPredicate() != ICmpInst::ICMP_NE)) {
        reject(instruction, "unsupported integer comparison");
        return;
      }
      std::optional<json::Object> left;
      std::optional<json::Object> right;
      if (pointerComparison) {
        auto leftAlternatives = pointerAlternatives(
            comparison->getOperand(0), context, instruction);
        auto rightAlternatives = pointerAlternatives(
            comparison->getOperand(1), context, instruction);
        if (!leftAlternatives || !rightAlternatives)
          return;
        left = pointerOperand(
            comparison->getOperand(0), context, instruction);
        right = pointerOperand(
            comparison->getOperand(1), context, instruction);
      } else {
        left = operand(comparison->getOperand(0), context, instruction);
        right = operand(comparison->getOperand(1), context, instruction);
      }
      if (!left || !right)
        return;
      json::Object lowered;
      lowered["op"] = "binary";
      lowered["operator"] = *operation;
      lowered["dst"] = context.values.lookup(comparison);
      lowered["left"] = std::move(*left);
      lowered["right"] = std::move(*right);
      lowered["bits"] = 1;
      output.push_back(std::move(lowered));
      if (!pointerComparison) {
        std::vector<std::string> conditions;
        for (Value *operandValue : comparison->operands()) {
          auto poison =
              context.poisonConditions.find(operandValue);
          if (
              poison != context.poisonConditions.end() &&
              std::find(
                  conditions.begin(), conditions.end(),
                  poison->second) == conditions.end())
            conditions.push_back(poison->second);
        }
        if (!conditions.empty()) {
          std::string defined = conditions.front();
          for (size_t index = 1;
               index < conditions.size(); ++index)
            defined = appendBinaryTemporary(
                "llvm_comparison_defined_", "and",
                variableOperand(defined),
                variableOperand(conditions[index]), 1,
                context, output);
          context.poisonConditions[comparison] =
              std::move(defined);
          usesTransitiveDeferredPoison = true;
        }
      }
      return;
    }
    if (auto *cast = dyn_cast<CastInst>(&instruction)) {
      StringRef operation;
      switch (cast->getOpcode()) {
      case Instruction::Trunc:
        operation = "trunc";
        break;
      case Instruction::ZExt:
        operation = "zext";
        break;
      case Instruction::SExt:
        operation = "sext";
        break;
      case Instruction::BitCast:
        operation = "identity";
        break;
      default:
        reject(instruction, "unsupported cast");
        return;
      }
      unsigned bits = integerBits(cast->getType());
      auto value = operand(cast->getOperand(0), context, instruction);
      if (bits == 0 || !value) {
        reject(instruction, "cast is not between bounded integers");
        return;
      }
      json::Object lowered;
      lowered["op"] = "unary";
      lowered["operator"] = operation;
      lowered["dst"] = context.values.lookup(cast);
      lowered["value"] = std::move(*value);
      lowered["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(lowered));
      auto poison =
          context.poisonConditions.find(cast->getOperand(0));
      if (poison != context.poisonConditions.end()) {
        context.poisonConditions[cast] = poison->second;
        usesTransitiveDeferredPoison = true;
      }
      return;
    }
    if (auto *freeze = dyn_cast<FreezeInst>(&instruction)) {
      unsigned bits = integerBits(freeze->getType());
      auto poison =
          context.poisonConditions.find(freeze->getOperand(0));
      if (poison != context.poisonConditions.end()) {
        auto value = operand(
            freeze->getOperand(0), context, instruction);
        if (bits == 0 || !value) {
          reject(
              instruction,
              "deferred poison freeze requires a bounded integer");
          return;
        }
        std::string choice =
            context.temporaryName("deferred_poison_choice_");
        json::Object nondeterministic;
        nondeterministic["op"] = "nondet";
        nondeterministic["dst"] = choice;
        nondeterministic["bits"] = static_cast<int64_t>(bits);
        nondeterministic["site"] =
            std::to_string(stableSiteId(instruction));
        output.push_back(std::move(nondeterministic));
        json::Object lowered;
        lowered["op"] = "select";
        lowered["dst"] = context.values.lookup(freeze);
        lowered["condition"] = variableOperand(poison->second);
        lowered["true"] = std::move(*value);
        lowered["false"] = variableOperand(choice);
        lowered["bits"] = static_cast<int64_t>(bits);
        output.push_back(std::move(lowered));
        usesNondeterministicFreeze = true;
        return;
      }
      if (isa<UndefValue>(freeze->getOperand(0)) ||
          isa<PoisonValue>(freeze->getOperand(0))) {
        if (bits == 0) {
          reject(
              instruction,
              "freeze is only supported for bounded integers");
          return;
        }
        json::Object lowered;
        lowered["op"] = "nondet";
        lowered["dst"] = context.values.lookup(freeze);
        lowered["bits"] = static_cast<int64_t>(bits);
        lowered["site"] =
            std::to_string(stableSiteId(instruction));
        output.push_back(std::move(lowered));
        usesNondeterministicFreeze = true;
        return;
      }
      auto value = operand(freeze->getOperand(0), context, instruction);
      if (bits == 0 || !value) {
        reject(instruction, "freeze is only supported for bounded integers");
        return;
      }
      json::Object lowered;
      lowered["op"] = "unary";
      lowered["operator"] = "identity";
      lowered["dst"] = context.values.lookup(freeze);
      lowered["value"] = std::move(*value);
      lowered["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *select = dyn_cast<SelectInst>(&instruction)) {
      auto condition = operand(select->getCondition(), context, instruction);
      unsigned bits = integerBits(select->getType());
      std::optional<json::Object> whenTrue;
      std::optional<json::Object> whenFalse;
      if (select->getType()->isPointerTy()) {
        bits = M.getDataLayout().getPointerSizeInBits(
            select->getType()->getPointerAddressSpace());
        if (isFunctionPointerValue(select)) {
          whenTrue = functionPointerOperand(
              select->getTrueValue(), context, instruction);
          whenFalse = functionPointerOperand(
              select->getFalseValue(), context, instruction);
          auto alternatives = functionPointerAlternatives(
              select, context, instruction);
          if (!alternatives || alternatives->empty())
            return;
        } else {
          whenTrue = pointerOperand(
              select->getTrueValue(), context, instruction);
          whenFalse = pointerOperand(
              select->getFalseValue(), context, instruction);
          auto alternatives = pointerAlternatives(
              select, context, instruction);
          if (!alternatives)
            return;
        }
      } else {
        whenTrue = operand(
            select->getTrueValue(), context, instruction);
        whenFalse = operand(
            select->getFalseValue(), context, instruction);
      }
      if (bits == 0 || bits > 64 || !condition || !whenTrue || !whenFalse) {
        reject(instruction,
               "select is not a bounded integer or pointer operation");
        return;
      }
      json::Object lowered;
      lowered["op"] = "select";
      lowered["dst"] = context.values.lookup(select);
      lowered["condition"] = std::move(*condition);
      lowered["true"] = std::move(*whenTrue);
      lowered["false"] = std::move(*whenFalse);
      lowered["bits"] = static_cast<int64_t>(bits);
      output.push_back(std::move(lowered));
      if (!select->getType()->isPointerTy()) {
        auto conditionPoison =
            context.poisonConditions.find(select->getCondition());
        auto truePoison =
            context.poisonConditions.find(select->getTrueValue());
        auto falsePoison =
            context.poisonConditions.find(select->getFalseValue());
        if (
            conditionPoison != context.poisonConditions.end() ||
            truePoison != context.poisonConditions.end() ||
            falsePoison != context.poisonConditions.end()) {
          auto selector = operand(
              select->getCondition(), context, instruction);
          if (!selector)
            return;
          json::Object trueDefined =
              truePoison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(truePoison->second);
          json::Object falseDefined =
              falsePoison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(falsePoison->second);
          std::string selectedDefined = appendSelectTemporary(
              "llvm_select_chosen_defined_",
              std::move(*selector), std::move(trueDefined),
              std::move(falseDefined), 1, context, output);
          std::string defined = std::move(selectedDefined);
          if (
              conditionPoison !=
              context.poisonConditions.end())
            defined = appendBinaryTemporary(
                "llvm_select_condition_defined_", "and",
                variableOperand(conditionPoison->second),
                variableOperand(defined), 1, context, output);
          context.poisonConditions[select] = std::move(defined);
          usesTransitiveDeferredPoison = true;
          usesSelectDeferredPoison = true;
        }
      }
      return;
    }
    if (auto *branch = dyn_cast<BranchInst>(&instruction)) {
      if (branch->isUnconditional()) {
        json::Object lowered;
        lowered["op"] = "jump";
        lowered["target"] = context.edgeTarget(
            branch->getParent(), branch->getSuccessor(0));
        output.push_back(std::move(lowered));
        return;
      }
      auto condition = operand(branch->getCondition(), context, instruction);
      if (!condition)
        return;
      json::Object lowered;
      lowered["op"] = "branch";
      lowered["condition"] = std::move(*condition);
      lowered["true"] = context.edgeTarget(
          branch->getParent(), branch->getSuccessor(0));
      lowered["false"] = context.edgeTarget(
          branch->getParent(), branch->getSuccessor(1));
      lowered["site"] = std::to_string(stableSiteId(instruction));
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *switchInstruction = dyn_cast<SwitchInst>(&instruction)) {
      unsigned bits =
          integerBits(switchInstruction->getCondition()->getType());
      if (bits == 0)
        return;
      if (switchInstruction->getNumCases() == 0) {
        json::Object jump;
        jump["op"] = "jump";
        jump["target"] = context.edgeTarget(
            switchInstruction->getParent(),
            switchInstruction->getDefaultDest());
        output.push_back(std::move(jump));
        return;
      }
      std::vector<SwitchInst::CaseHandle> cases;
      for (auto item : switchInstruction->cases())
        cases.push_back(item);
      std::string first =
          context.temporaryName("switch_dispatch_");
      json::Object jump;
      jump["op"] = "jump";
      jump["target"] = first;
      output.push_back(std::move(jump));
      std::string current = first;
      for (size_t index = 0; index < cases.size(); ++index) {
        auto switchValue = operand(
            switchInstruction->getCondition(), context, instruction);
        if (!switchValue)
          return;
        json::Array dispatch;
        std::string comparison = context.temporaryName("switch_cmp_");
        json::Object compare;
        compare["op"] = "binary";
        compare["operator"] = "eq";
        compare["dst"] = comparison;
        compare["left"] = std::move(*switchValue);
        compare["right"] = constantOperand(*cases[index].getCaseValue());
        compare["bits"] = 1;
        dispatch.push_back(std::move(compare));
        json::Object branch;
        branch["op"] = "branch";
        branch["condition"] = variableOperand(comparison);
        branch["true"] = context.edgeTarget(
            switchInstruction->getParent(),
            cases[index].getCaseSuccessor());
        std::string next = index + 1 == cases.size()
                               ? context.edgeTarget(
                                     switchInstruction->getParent(),
                                     switchInstruction->getDefaultDest())
                               : context.temporaryName("switch_dispatch_");
        branch["false"] = next;
        branch["site"] = std::to_string(stableSiteId(instruction));
        dispatch.push_back(std::move(branch));
        extraBlocks.emplace(current, std::move(dispatch));
        current = next;
      }
      return;
    }
    if (auto *landing = dyn_cast<LandingPadInst>(&instruction)) {
      auto supportedUser = [this, landing](const User *user) {
        if (auto *resume = dyn_cast<ResumeInst>(user))
          return resume->getValue() == landing;
        auto *extract = dyn_cast<ExtractValueInst>(user);
        if (extract == nullptr ||
            extract->getAggregateOperand() != landing)
          return false;
        ArrayRef<unsigned> indices = extract->getIndices();
        return indices.size() == 1 &&
               ((indices.front() == 1 &&
                 extract->getType()->isIntegerTy(32)) ||
                (indices.front() == 0 &&
                 isExceptionTokenExtract(*extract)));
      };
      if (std::any_of(
              landing->user_begin(), landing->user_end(),
              [&](const User *user) { return !supportedUser(user); })) {
        reject(
            instruction,
            "landingpad has unsupported exception-object uses");
        return;
      }
      json::Array types;
      bool catchAll = false;
      for (unsigned index = 0; index < landing->getNumClauses(); ++index) {
        if (!landing->isCatch(index)) {
          reject(instruction, "landingpad filters are unsupported");
          return;
        }
        Constant *clause = landing->getClause(index);
        if (isa<ConstantPointerNull>(clause)) {
          catchAll = true;
          continue;
        }
        auto typeId = exceptionTypeId(clause);
        if (!typeId) {
          reject(
              instruction,
              "landingpad catch type is not a stable global typeinfo");
          return;
        }
        types.push_back(static_cast<int64_t>(*typeId));
      }
      if (!landing->isCleanup() && types.empty() && !catchAll) {
        reject(instruction, "landingpad has no supported clause");
        return;
      }
      json::Object lowered;
      lowered["op"] = "exception_match";
      lowered["types"] = std::move(types);
      lowered["cleanup"] = landing->isCleanup();
      lowered["catch_all"] = catchAll;
      output.push_back(std::move(lowered));
      usesCleanupExceptions = true;
      usesTypedExceptions |=
          landing->getNumClauses() != 0 || catchAll;
      return;
    }
    if (isa<ResumeInst>(&instruction)) {
      json::Object lowered;
      lowered["op"] = "throw";
      output.push_back(std::move(lowered));
      usesCleanupExceptions = true;
      return;
    }
    if (auto *call = dyn_cast<CallBase>(&instruction)) {
      auto *invoke = dyn_cast<InvokeInst>(call);
      if (!isa<CallInst>(call) && invoke == nullptr) {
        reject(instruction,
               "callbr control transfers are unsupported");
        return;
      }
      Function *callee = call->getCalledFunction();
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "__cxa_throw") {
        if (!isCxaThrowSummary(*call)) {
          reject(instruction, "__cxa_throw has an unsupported signature");
          return;
        }
        auto *destructor = dyn_cast<Constant>(call->getArgOperand(2));
        if (destructor == nullptr || !destructor->isNullValue()) {
          reject(
              instruction,
              "__cxa_throw currently requires a trivial null destructor");
          return;
        }
        Value *rawObject = call->getArgOperand(0)->stripPointerCasts();
        auto *allocation = dyn_cast<CallBase>(rawObject);
        if (allocation == nullptr ||
            !isCxaAllocateExceptionSummary(*allocation)) {
          reject(
              instruction,
              "__cxa_throw requires a direct bounded exception allocation");
          return;
        }
        auto pool = layoutHeap(*allocation);
        auto address = pointerOperand(
            call->getArgOperand(0), context, instruction);
        auto typeId = exceptionTypeId(call->getArgOperand(1));
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getArgOperand(0)->getType()->getPointerAddressSpace());
        if (!pool || !address || !typeId || pointerBits == 0 ||
            pointerBits > 64) {
          if (!typeId)
            reject(
                instruction,
                "__cxa_throw requires stable global typeinfo");
          return;
        }
        json::Object lowered;
        lowered["op"] = "exception_throw";
        lowered["address"] = std::move(*address);
        json::Array addresses;
        for (const StaticMemoryObject &slot : pool->slots)
          addresses.push_back(static_cast<int64_t>(slot.address));
        lowered["addresses"] = std::move(addresses);
        lowered["arena_site"] = pool->site;
        lowered["site"] = std::to_string(stableSiteId(instruction));
        lowered["bits"] = static_cast<int64_t>(pointerBits);
        lowered["type_id"] = static_cast<int64_t>(*typeId);
        if (invoke != nullptr)
          lowered["unwind"] = context.edgeTarget(
              invoke->getParent(), invoke->getUnwindDest());
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesTypedExceptions = true;
        usesExceptionCatchLifecycle = true;
        usesExceptionObjectArena = true;
        return;
      }
      if (isCxaBeginCatchSummary(*call)) {
        if (
            invoke != nullptr ||
            (!call->use_empty() &&
             !hasOnlyTrivialScalarCatchObjectLoads(*call))) {
          reject(
              instruction,
              "__cxa_begin_catch object materialization is unsupported");
          return;
        }
        Value *argument = call->getArgOperand(0)->stripPointerCasts();
        auto *token = dyn_cast<ExtractValueInst>(argument);
        if (token == nullptr || !isExceptionTokenExtract(*token)) {
          reject(
              instruction,
              "__cxa_begin_catch requires the current landingpad token");
          return;
        }
        unsigned bits = M.getDataLayout().getPointerSizeInBits(
            token->getType()->getPointerAddressSpace());
        if (bits == 0 || bits > 64) {
          reject(
              instruction,
              "__cxa_begin_catch token width is unsupported");
          return;
        }
        json::Object lowered;
        lowered["op"] = "exception_begin_catch";
        lowered["token"] = variableOperand(
            context.values.lookup(token));
        lowered["token_bits"] = static_cast<int64_t>(bits);
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesExceptionCatchLifecycle = true;
        return;
      }
      if (isCxaEndCatchSummary(*call)) {
        json::Object lowered;
        lowered["op"] = "exception_end_catch";
        if (invoke != nullptr)
          lowered["normal"] = context.edgeTarget(
              invoke->getParent(), invoke->getNormalDest());
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesExceptionCatchLifecycle = true;
        return;
      }
      if (isCxaRethrowSummary(*call)) {
        if (invoke == nullptr ||
            !isa<UnreachableInst>(
                invoke->getNormalDest()->getTerminator())) {
          reject(
              instruction,
              "__cxa_rethrow requires an invoke with unreachable normal edge");
          return;
        }
        json::Object lowered;
        lowered["op"] = "exception_rethrow";
        lowered["unwind"] = context.edgeTarget(
            invoke->getParent(), invoke->getUnwindDest());
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesExceptionCatchLifecycle = true;
        return;
      }
      if (
          invoke != nullptr &&
          isContinuationTypedThrowSummary(*call)) {
        auto condition = operand(
            call->getArgOperand(0), context, instruction);
        auto exception = operand(
            call->getArgOperand(1), context, instruction);
        auto typeId = exceptionTypeId(call->getArgOperand(2));
        unsigned exceptionBits = integerBits(
            call->getArgOperand(1)->getType());
        if (!condition || !exception || !typeId || exceptionBits == 0 ||
            exceptionBits > 64) {
          if (!typeId)
            reject(
                instruction,
                "typed exception requires a stable global typeinfo");
          return;
        }
        json::Object lowered;
        lowered["op"] = "throw_if";
        lowered["condition"] = std::move(*condition);
        lowered["exception"] = std::move(*exception);
        lowered["exception_bits"] =
            static_cast<int64_t>(exceptionBits);
        lowered["type_id"] = static_cast<int64_t>(*typeId);
        lowered["normal"] = context.edgeTarget(
            invoke->getParent(), invoke->getNormalDest());
        lowered["unwind"] = context.edgeTarget(
            invoke->getParent(), invoke->getUnwindDest());
        lowered["site"] = std::to_string(stableSiteId(instruction));
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        usesTypedExceptions = true;
        return;
      }
      if (invoke != nullptr && isContinuationThrowSummary(*call)) {
        auto condition = operand(
            call->getArgOperand(0), context, instruction);
        auto exception = operand(
            call->getArgOperand(1), context, instruction);
        unsigned exceptionBits = integerBits(
            call->getArgOperand(1)->getType());
        if (!condition || !exception || exceptionBits == 0 ||
            exceptionBits > 64)
          return;
        json::Object lowered;
        lowered["op"] = "throw_if";
        lowered["condition"] = std::move(*condition);
        lowered["exception"] = std::move(*exception);
        lowered["exception_bits"] =
            static_cast<int64_t>(exceptionBits);
        lowered["normal"] = context.edgeTarget(
            invoke->getParent(), invoke->getNormalDest());
        lowered["unwind"] = context.edgeTarget(
            invoke->getParent(), invoke->getUnwindDest());
        lowered["site"] = std::to_string(stableSiteId(instruction));
        output.push_back(std::move(lowered));
        usesCleanupExceptions = true;
        return;
      }
      bool nounwindInvoke =
          invoke != nullptr && isProvablyNoUnwind(*invoke);
      bool declarativePureExternal =
          callee != nullptr && callee->isDeclaration() &&
          callee->getFnAttribute(
              "symcc-continuation-model").isStringAttribute();
      if (invoke != nullptr && !nounwindInvoke) {
        std::vector<Function *> targets;
        if (callee != nullptr)
          targets.push_back(callee);
        else if (!discoverFunctionTargets(
                     call->getCalledOperand(), targets)) {
          reject(
              instruction,
              "unwinding invoke requires bounded internal targets");
          return;
        }
        if (targets.empty() || std::any_of(
                targets.begin(), targets.end(), [](const Function *target) {
                  return target == nullptr || target->isDeclaration() ||
                         target->isIntrinsic();
                })) {
          reject(
              instruction,
              "unwinding invoke requires bounded internal targets");
          return;
        }
        usesCleanupExceptions = true;
      }
      if (
          invoke != nullptr && nounwindInvoke && callee != nullptr &&
          (callee->isIntrinsic() ||
           (callee->isDeclaration() && !declarativePureExternal))) {
        reject(
            instruction,
            "nounwind invoke currently requires an internal target");
        return;
      }
      if (call->isMustTailCall()) {
        reject(instruction, "musttail calls are unsupported");
        return;
      }
      if (callee != nullptr && callee->isIntrinsic()) {
        if (callee->getIntrinsicID() == Intrinsic::eh_typeid_for) {
          auto typeId =
              call->arg_size() == 1
                  ? exceptionTypeId(call->getArgOperand(0))
                  : std::nullopt;
          if (!typeId || !call->getType()->isIntegerTy(32)) {
            reject(
                instruction,
                "exception typeid requires a stable global typeinfo");
            return;
          }
          json::Object lowered;
          lowered["op"] = "const";
          lowered["dst"] = context.values.lookup(call);
          lowered["value"] = static_cast<int64_t>(*typeId);
          lowered["bits"] = 32;
          output.push_back(std::move(lowered));
          usesTypedExceptions = true;
          usesCleanupExceptions = true;
          return;
        }
        if (auto *transfer = dyn_cast<MemTransferInst>(call)) {
          if (transfer->isVolatile()) {
            reject(
                instruction,
                "volatile memory transfer is unsupported");
            return;
          }
          StringRef operation =
              callee->getIntrinsicID() == Intrinsic::memmove
                  ? "memmove" : "memcpy";
          lowerBoundedRegionOperands(
              operation, transfer->getRawDest(),
              transfer->getRawSource(), transfer->getLength(),
              "", 0, context, instruction, output);
          return;
        }
        if (auto *memorySet = dyn_cast<MemSetInst>(call)) {
          if (memorySet->isVolatile()) {
            reject(
                instruction,
                "volatile memory set is unsupported");
            return;
          }
          lowerBoundedRegionOperands(
              "memset", memorySet->getRawDest(),
              memorySet->getValue(), memorySet->getLength(),
              "", 0, context, instruction, output);
          return;
        }
        if (callee->getIntrinsicID() == Intrinsic::assume) {
          auto condition = operand(call->getArgOperand(0), context, instruction);
          if (condition) {
            json::Object lowered;
            lowered["op"] = "assume";
            lowered["condition"] = std::move(*condition);
            output.push_back(std::move(lowered));
          }
          return;
        }
        if (callee->getIntrinsicID() == Intrinsic::bswap) {
          unsigned bits = integerBits(call->getType());
          if (call->arg_size() != 1) {
            reject(
                instruction,
                "byte-swap intrinsic has an unsupported signature");
            return;
          }
          if (!lowerByteSwap(
                  call->getArgOperand(0),
                  context.values.lookup(call), bits,
                  context, instruction, output))
            return;
          usesScalarExternalSummaries = true;
          return;
        }
        if (lowerBitCountIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerBitPermutationIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerSaturatingArithmeticIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerOverflowArithmeticIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerScalarSelectionIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerOptimizationHintIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerSsaCopyIntrinsic(
                *call, context, instruction, output))
          return;
        if (lowerObjectSizeIntrinsic(
                *call, context, instruction, output))
          return;
        if (callee->getIntrinsicID() == Intrinsic::lifetime_start ||
            callee->getIntrinsicID() == Intrinsic::lifetime_end ||
            isa<DbgInfoIntrinsic>(call))
          return;
        reject(instruction, "unsupported LLVM intrinsic");
        return;
      }
      if (lowerDeclarativePureExternal(
              *call, context, instruction, output))
        return;
      if (lowerBoundedScalarExternal(
              *call, context, instruction, output))
        return;
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "strcpy" ||
           callee->getName() == "strncpy")) {
        lowerBoundedStringCopy(
            *call, context, instruction, output);
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "memcmp" ||
           callee->getName() == "bcmp")) {
        lowerBoundedMemoryCompare(
            *call, context, instruction, output);
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "strlen" ||
           callee->getName() == "strcmp" ||
           callee->getName() == "strncmp")) {
        lowerBoundedStringSummary(
            *call, context, instruction, output);
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "memchr" ||
           callee->getName() == "strchr")) {
        lowerBoundedPointerSearch(
            *call, context, instruction, output);
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "memcpy" ||
           callee->getName() == "memmove" ||
           callee->getName() == "memset")) {
        lowerBoundedRegionEffect(
            *call, context, instruction, output);
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "realloc") {
        if (call->arg_size() != 2 ||
            !call->getType()->isPointerTy() ||
            call->getType()->getPointerAddressSpace() != 0 ||
            !call->getArgOperand(0)->getType()->isPointerTy()) {
          reject(instruction, "realloc has an unsupported signature");
          return;
        }
        unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
            call->getType()->getPointerAddressSpace());
        unsigned sizeBits =
            integerBits(call->getArgOperand(1)->getType());
        auto alternatives = pointerAlternatives(
            call->getArgOperand(0), context, instruction);
        auto address = pointerOperand(
            call->getArgOperand(0), context, instruction);
        auto size = operand(
            call->getArgOperand(1), context, instruction);
        if (!alternatives || alternatives->empty() ||
            !address || !size || pointerBits == 0 ||
            pointerBits > 64 || sizeBits == 0 || sizeBits > 64)
          return;
        std::set<uint64_t> bases;
        for (const PointerAlternative &alternative : *alternatives) {
          const StaticPointer &pointer = alternative.pointer;
          if (pointer.address == 0 || pointer.heapObject == nullptr ||
              pointer.dynamicIndex != nullptr ||
              pointer.objectOffset != 0 ||
              (pointer.heapObject->getCalledFunction() != nullptr &&
               pointer.heapObject->getCalledFunction()->getName() ==
                   "__cxa_allocate_exception")) {
            reject(
                instruction,
                "realloc requires a non-null supported heap object base");
            return;
          }
          bases.insert(pointer.address);
        }
        json::Object lowered;
        lowered["op"] = "heap_realloc";
        lowered["dst"] = context.values.lookup(call);
        lowered["address"] = std::move(*address);
        lowered["size"] = std::move(*size);
        lowered["bits"] = static_cast<int64_t>(pointerBits);
        lowered["size_bits"] = static_cast<int64_t>(sizeBits);
        lowered["strategy"] = "bounded-in-place";
        lowered["nullable"] = true;
        json::Array addresses;
        for (uint64_t base : bases)
          addresses.push_back(static_cast<int64_t>(base));
        lowered["addresses"] = std::move(addresses);
        output.push_back(std::move(lowered));
        usesReallocHeap = true;
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "__cxa_allocate_exception") {
        if (!isCxaAllocateExceptionSummary(*call)) {
          reject(
              instruction,
              "__cxa_allocate_exception has an unsupported signature");
          return;
        }
        auto pool = layoutHeap(*call);
        if (!pool)
          return;
        json::Object lowered;
        lowered["op"] = "exception_alloc";
        lowered["dst"] = context.values.lookup(call);
        json::Array addresses;
        for (const StaticMemoryObject &slot : pool->slots)
          addresses.push_back(static_cast<int64_t>(slot.address));
        lowered["addresses"] = std::move(addresses);
        lowered["capacity"] =
            static_cast<int64_t>(pool->slots.size());
        lowered["size"] = static_cast<int64_t>(pool->objectSize);
        lowered["site"] = pool->site;
        lowered["bits"] = static_cast<int64_t>(
            M.getDataLayout().getPointerSizeInBits());
        output.push_back(std::move(lowered));
        usesExceptionObjectArena = true;
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          (callee->getName() == "malloc" ||
           callee->getName() == "calloc")) {
        auto pool = layoutHeap(*call);
        if (!pool)
          return;
        json::Object lowered;
        lowered["op"] = "heap_alloc";
        lowered["dst"] = context.values.lookup(call);
        json::Array addresses;
        for (const StaticMemoryObject &slot : pool->slots)
          addresses.push_back(static_cast<int64_t>(slot.address));
        lowered["addresses"] = std::move(addresses);
        lowered["capacity"] =
            static_cast<int64_t>(pool->slots.size());
        if (pool->nullable) {
          if (pool->allocator == "calloc") {
            auto count = operand(
                call->getArgOperand(0), context, instruction);
            auto elementSize = operand(
                call->getArgOperand(1), context, instruction);
            if (!count || !elementSize)
              return;
            lowered["count"] = std::move(*count);
            lowered["element_size"] = std::move(*elementSize);
            lowered["zero_initialize"] = true;
          } else {
            auto requestedSize = operand(
                call->getArgOperand(0), context, instruction);
            if (!requestedSize)
              return;
            lowered["size"] = std::move(*requestedSize);
          }
          lowered["size_bits"] =
              static_cast<int64_t>(pool->sizeBits);
          lowered["max_size"] =
              static_cast<int64_t>(pool->objectSize);
          lowered["nullable"] = true;
        } else {
          lowered["size"] = static_cast<int64_t>(pool->objectSize);
        }
        lowered["site"] = pool->site;
        lowered["allocator"] = pool->allocator;
        lowered["bits"] = static_cast<int64_t>(
            M.getDataLayout().getPointerSizeInBits());
        output.push_back(std::move(lowered));
        return;
      }
      if (callee != nullptr && callee->isDeclaration() &&
          callee->getName() == "free") {
        if (call->arg_size() != 1 || !call->getType()->isVoidTy()) {
          reject(instruction, "free has an unsupported signature");
          return;
        }
        Value *argument = call->getArgOperand(0);
        json::Object lowered;
        lowered["op"] = "heap_free";
        if (isa<ConstantPointerNull>(argument)) {
          lowered["address"] = integerConstant(
              0, M.getDataLayout().getPointerSizeInBits());
        } else {
          auto alternatives = pointerAlternatives(
              argument, context, instruction);
          if (!alternatives || alternatives->empty())
            return;
          std::set<uint64_t> bases;
          for (const PointerAlternative &alternative : *alternatives) {
            const StaticPointer &pointer = alternative.pointer;
            if (pointer.address == 0) {
              if (pointer.heapObject != nullptr ||
                  pointer.dynamicIndex != nullptr ||
                  pointer.objectOffset != 0) {
                reject(instruction,
                       "free has an invalid null provenance alternative");
                return;
              }
              continue;
            }
            if (pointer.heapObject == nullptr ||
                pointer.dynamicIndex != nullptr ||
                pointer.objectOffset != 0 ||
                (pointer.heapObject->getCalledFunction() != nullptr &&
                 pointer.heapObject->getCalledFunction()->getName() ==
                     "__cxa_allocate_exception")) {
              reject(instruction,
                     "free requires a supported heap object base");
              return;
            }
            bases.insert(pointer.address);
          }
          auto address = pointerOperand(
              argument, context, instruction);
          if (!address)
            return;
          lowered["address"] = std::move(*address);
          unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
              argument->getType()->getPointerAddressSpace());
          if (pointerBits == 0 || pointerBits > 64) {
            reject(instruction, "free pointer width exceeds 64 bits");
            return;
          }
          json::Array addresses;
          for (uint64_t base : bases)
            addresses.push_back(static_cast<int64_t>(base));
          lowered["addresses"] = std::move(addresses);
          lowered["bits"] = static_cast<int64_t>(pointerBits);
          usesHeapLifetimePointerUnions = true;
        }
        output.push_back(std::move(lowered));
        return;
      }
      std::optional<std::vector<FunctionAlternative>>
          indirectAlternatives;
      if (callee == nullptr) {
        indirectAlternatives = functionPointerAlternatives(
            call->getCalledOperand(), context, instruction);
        if (!indirectAlternatives || indirectAlternatives->empty())
          return;
        usesIndirectCalls = true;
      } else if (callee->isDeclaration()) {
        reject(instruction, "external calls require virtualization");
        return;
      }
      auto compatibleTarget = [&](Function *target) {
        if (target == nullptr || target->isDeclaration() ||
            target->isIntrinsic() || target->isVarArg() ||
            call->arg_size() != target->arg_size() ||
            call->getType() != target->getReturnType())
          return false;
        unsigned index = 0;
        for (Argument &parameter : target->args())
          if (parameter.getType() !=
              call->getArgOperand(index++)->getType())
            return false;
        return true;
      };
      if (callee != nullptr && !compatibleTarget(callee)) {
        reject(instruction, "direct call signature is unsupported");
        return;
      }
      if (indirectAlternatives &&
          std::any_of(
              indirectAlternatives->begin(),
              indirectAlternatives->end(),
              [&](const FunctionAlternative &alternative) {
                return !compatibleTarget(alternative.function);
              })) {
        reject(
            instruction,
            "indirect-call targets do not share an exact signature");
        return;
      }
      json::Array arguments;
      json::Array pointerArguments;
      std::vector<std::pair<unsigned, json::Object>> pointerDomains;
      for (unsigned index = 0; index < call->arg_size(); ++index) {
        Value *argument = call->getArgOperand(index);
        std::optional<json::Object> loweredArgument;
        if (argument->getType()->isPointerTy()) {
          loweredArgument =
              pointerOperand(argument, context, instruction);
          auto alternatives =
              pointerAlternatives(argument, context, instruction);
          unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
              argument->getType()->getPointerAddressSpace());
          if (!alternatives || pointerBits == 0 || pointerBits > 64)
            return;
          auto domain = pointerDomainOperand(
              *alternatives, context, instruction, output);
          if (!domain)
            return;
          pointerDomains.emplace_back(
              index, std::move(*domain));
          usesCrossFunctionDomains = true;
          json::Object metadata;
          metadata["index"] = static_cast<int64_t>(index);
          metadata["bits"] = static_cast<int64_t>(pointerBits);
          pointerArguments.push_back(std::move(metadata));
          usesCrossFunctionPointers = true;
        } else {
          loweredArgument = operand(argument, context, instruction);
        }
        if (!loweredArgument)
          return;
        arguments.push_back(std::move(*loweredArgument));
      }
      json::Array pointerDomainArguments;
      for (auto &domain : pointerDomains) {
        json::Object metadata;
        metadata["index"] = static_cast<int64_t>(domain.first);
        metadata["argument"] =
            static_cast<int64_t>(arguments.size());
        pointerDomainArguments.push_back(std::move(metadata));
        arguments.push_back(std::move(domain.second));
      }
      json::Array definedArguments;
      if (callee != nullptr) {
        for (unsigned index = 0;
             index < call->arg_size(); ++index) {
          Argument *parameter = callee->getArg(index);
          if (
              parameter == nullptr ||
              !argumentHasDeferredPoison(*parameter))
            continue;
          Value *argument = call->getArgOperand(index);
          auto poison =
              context.poisonConditions.find(argument);
          json::Object metadata;
          metadata["index"] =
              static_cast<int64_t>(index);
          metadata["argument"] =
              static_cast<int64_t>(arguments.size());
          definedArguments.push_back(std::move(metadata));
          arguments.push_back(
              poison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(poison->second));
          usesTransitiveDeferredPoison = true;
          usesCrossFunctionArgumentPoison = true;
          auto calls = directCallsites(*callee);
          usesMultiCallsiteDeferredPoison |=
              calls && calls->size() > 1;
        }
      }
      json::Object lowered;
      if (indirectAlternatives) {
        auto target = functionPointerOperand(
            call->getCalledOperand(), context, instruction);
        unsigned targetBits = M.getDataLayout().getPointerSizeInBits(
            call->getCalledOperand()->getType()->getPointerAddressSpace());
        if (!target || targetBits == 0 || targetBits > 64)
          return;
        lowered["op"] = "indirect_call";
        lowered["target"] = std::move(*target);
        lowered["target_bits"] =
            static_cast<int64_t>(targetBits);
        json::Array targets;
        for (const FunctionAlternative &alternative :
             *indirectAlternatives) {
          auto identifier = functionIds.find(alternative.function);
          auto guards = guardArray(
              alternative.guards, context, instruction);
          if (identifier == functionIds.end() || !guards)
            return;
          json::Object targetCase;
          targetCase["function"] =
              alternative.function->getName();
          targetCase["id"] =
              static_cast<int64_t>(identifier->second);
          targetCase["guards"] = std::move(*guards);
          targets.push_back(std::move(targetCase));
        }
        lowered["targets"] = std::move(targets);
      } else {
        lowered["op"] = "call";
        lowered["function"] = callee->getName();
      }
      lowered["args"] = std::move(arguments);
      if (!pointerArguments.empty())
        lowered["pointer_args"] = std::move(pointerArguments);
      if (!pointerDomainArguments.empty())
        lowered["pointer_domains"] =
            std::move(pointerDomainArguments);
      if (!definedArguments.empty())
        lowered["defined_args"] =
            std::move(definedArguments);
      if (!call->getType()->isVoidTy()) {
        if (call->getType()->isPointerTy()) {
          unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
              call->getType()->getPointerAddressSpace());
          auto alternatives =
              pointerAlternatives(call, context, instruction);
          if (!alternatives || pointerBits == 0 || pointerBits > 64)
            return;
          lowered["pointer_result_bits"] =
              static_cast<int64_t>(pointerBits);
          lowered["result_bits"] =
              static_cast<int64_t>(pointerBits);
          lowered["pointer_domain_dst"] =
              context.values.lookup(call) + "__domain";
          usesCrossFunctionPointers = true;
          usesCrossFunctionDomains = true;
        } else if (integerBits(call->getType()) == 0) {
          reject(instruction,
                 "call result is not a bounded integer or pointer");
          return;
        } else {
          lowered["result_bits"] =
              static_cast<int64_t>(integerBits(call->getType()));
          if (
              callee != nullptr &&
              functionHasDeferredPoisonReturn(*callee)) {
            std::string definedDestination =
                context.values.lookup(call) + "__defined";
            lowered["defined_dst"] = definedDestination;
            context.poisonConditions[call] =
                std::move(definedDestination);
            usesTransitiveDeferredPoison = true;
            usesCrossFunctionDeferredPoison = true;
            auto calls = directCallsites(*callee);
            usesMultiCallsiteDeferredPoison |=
                calls && calls->size() > 1;
          }
        }
        lowered["dst"] = context.values.lookup(call);
      }
      if (invoke != nullptr) {
        lowered["normal_target"] = context.edgeTarget(
            invoke->getParent(), invoke->getNormalDest());
        if (nounwindInvoke) {
          usesNoUnwindInvokes = true;
        } else {
          lowered["unwind_target"] = context.edgeTarget(
              invoke->getParent(), invoke->getUnwindDest());
          usesCleanupExceptions = true;
        }
      }
      output.push_back(std::move(lowered));
      return;
    }
    if (auto *returnInstruction = dyn_cast<ReturnInst>(&instruction)) {
      json::Object lowered;
      lowered["op"] = "return";
      if (Value *value = returnInstruction->getReturnValue()) {
        std::optional<json::Object> result;
        if (value->getType()->isPointerTy()) {
          unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
              value->getType()->getPointerAddressSpace());
          auto alternatives =
              pointerAlternatives(value, context, instruction);
          if (!alternatives || pointerBits == 0 || pointerBits > 64)
            return;
          if (std::any_of(
                  alternatives->begin(), alternatives->end(),
                  [&](const PointerAlternative &alternative) {
                    const AllocaInst *stack =
                        alternative.pointer.stackObject;
                    return stack != nullptr &&
                           stack->getFunction() ==
                               returnInstruction->getFunction();
                  })) {
            reject(
                instruction,
                "pointer return escapes the callee stack frame");
            return;
          }
          result = pointerOperand(value, context, instruction);
          auto domain = pointerDomainOperand(
              *alternatives, context, instruction, output);
          if (!domain)
            return;
          lowered["pointer_domain"] = std::move(*domain);
          lowered["pointer_bits"] =
              static_cast<int64_t>(pointerBits);
          usesCrossFunctionPointers = true;
          usesCrossFunctionDomains = true;
        } else {
          result = operand(value, context, instruction);
        }
        if (!result)
          return;
        lowered["value"] = std::move(*result);
        if (
            !value->getType()->isPointerTy() &&
            functionHasDeferredPoisonReturn(
                *returnInstruction->getFunction())) {
          auto poison =
              context.poisonConditions.find(value);
          lowered["defined"] =
              poison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(poison->second);
          usesTransitiveDeferredPoison = true;
          usesCrossFunctionDeferredPoison = true;
          auto calls = directCallsites(
              *returnInstruction->getFunction());
          usesMultiCallsiteDeferredPoison |=
              calls && calls->size() > 1;
        }
      } else {
        lowered["value"] = integerConstant(0, 1);
      }
      output.push_back(std::move(lowered));
      return;
    }
    if (isa<UnreachableInst>(instruction)) {
      json::Object assume;
      assume["op"] = "assume";
      assume["condition"] = integerConstant(0, 1);
      output.push_back(std::move(assume));
      json::Object lowered;
      lowered["op"] = "halt";
      lowered["value"] = integerConstant(0, 1);
      output.push_back(std::move(lowered));
      return;
    }
    reject(instruction, "instruction is outside the conservative LLVM subset");
  }

  void addPhiEdgeBlocks(
      FunctionContext &context, json::Object &blocks,
      const std::set<const BasicBlock *> &liveBlocks) {
    for (const auto &edge : context.edgeBlocks) {
      const BasicBlock *predecessor = edge.first.first;
      const BasicBlock *successor = edge.first.second;
      if (
          liveBlocks.count(predecessor) == 0 ||
          liveBlocks.count(successor) == 0)
        continue;
      json::Array instructions;
      std::set<std::string> emittedPointerTags;
      struct StagedPhi {
        const PHINode *phi = nullptr;
        std::string temporary;
        std::string definedTemporary;
        unsigned bits = 0;
      };
      std::vector<StagedPhi> staged;
      for (const Instruction &instruction : *successor) {
        auto *phi = dyn_cast<PHINode>(&instruction);
        if (phi == nullptr)
          break;
        Value *incoming = phi->getIncomingValueForBlock(
            const_cast<BasicBlock *>(predecessor));
        unsigned bits = integerBits(phi->getType());
        std::optional<json::Object> value;
        if (phi->getType()->isPointerTy()) {
          bits = M.getDataLayout().getPointerSizeInBits(
              phi->getType()->getPointerAddressSpace());
          if (isFunctionPointerValue(
                  const_cast<PHINode *>(phi))) {
            value = functionPointerOperand(
                incoming, context, *successor->getFirstNonPHI());
            auto alternatives = functionPointerAlternatives(
                const_cast<PHINode *>(phi), context,
                *successor->getFirstNonPHI());
            if (!alternatives || alternatives->empty())
              continue;
          } else {
            value = pointerOperand(
                incoming, context, *successor->getFirstNonPHI());
            auto alternatives = pointerAlternatives(
                const_cast<PHINode *>(phi), context,
                *successor->getFirstNonPHI());
            if (!alternatives)
              continue;
          }
          if (phi->getBasicBlockIndex(
                  const_cast<BasicBlock *>(predecessor)) < 0) {
            reject(*phi, "pointer PHI predecessor is inconsistent");
            continue;
          }
          std::string tagName = context.pointerTags.lookup(phi);
          if (emittedPointerTags.insert(tagName).second) {
            json::Object tag;
            tag["op"] = "const";
            tag["dst"] = tagName;
            tag["value"] = static_cast<int64_t>(
                context.blockIds.lookup(predecessor));
            tag["bits"] = 32;
            instructions.push_back(std::move(tag));
          }
        } else {
          value = operand(
              incoming, context, *successor->getFirstNonPHI());
        }
        if (!value || bits == 0) {
          reject(*phi, "PHI incoming value is not bounded");
          continue;
        }
        std::string temporary = context.temporaryName("phi_in_");
        json::Object stage;
        stage["op"] = "unary";
        stage["operator"] = "identity";
        stage["dst"] = temporary;
        stage["value"] = std::move(*value);
        stage["bits"] = static_cast<int64_t>(bits);
        instructions.push_back(std::move(stage));
        std::string definedTemporary;
        auto phiDefined = context.poisonConditions.find(phi);
        if (
            !phi->getType()->isPointerTy() &&
            phiDefined != context.poisonConditions.end()) {
          auto incomingDefined =
              context.poisonConditions.find(incoming);
          json::Object definedValue =
              incomingDefined == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(incomingDefined->second);
          definedTemporary =
              context.temporaryName("phi_defined_in_");
          json::Object definedStage;
          definedStage["op"] = "unary";
          definedStage["operator"] = "identity";
          definedStage["dst"] = definedTemporary;
          definedStage["value"] = std::move(definedValue);
          definedStage["bits"] = 1;
          instructions.push_back(std::move(definedStage));
          if (
              incomingDefined !=
              context.poisonConditions.end()) {
            usesTransitiveDeferredPoison = true;
            usesPhiDeferredPoison = true;
          }
        }
        staged.push_back(StagedPhi{
            phi, std::move(temporary),
            std::move(definedTemporary), bits});
      }
      for (const auto &item : staged) {
        json::Object commit;
        commit["op"] = "unary";
        commit["operator"] = "identity";
        commit["dst"] = context.values.lookup(item.phi);
        commit["value"] = variableOperand(item.temporary);
        commit["bits"] = static_cast<int64_t>(item.bits);
        instructions.push_back(std::move(commit));
        if (!item.definedTemporary.empty()) {
          json::Object definedCommit;
          definedCommit["op"] = "unary";
          definedCommit["operator"] = "identity";
          definedCommit["dst"] =
              context.poisonConditions.lookup(item.phi);
          definedCommit["value"] =
              variableOperand(item.definedTemporary);
          definedCommit["bits"] = 1;
          instructions.push_back(std::move(definedCommit));
        }
      }
      for (const auto &merge :
           context.memoryPoisonMerges) {
        if (merge.load->getParent() != successor)
          continue;
        auto incoming = std::find_if(
            merge.incoming.begin(), merge.incoming.end(),
            [&](const auto &item) {
              return item.block == predecessor;
            });
        if (incoming == merge.incoming.end())
          continue;
        auto poison =
            incoming->store == nullptr
                ? context.poisonConditions.end()
                : context.poisonConditions.find(
                      incoming->store);
        json::Object assignment;
        assignment["dst"] = merge.defined;
        if (
            incoming->kind ==
            FunctionContext::MemoryPoisonIncoming::
                Kind::RecursiveConditionalTree) {
          using TreeNode =
              FunctionContext::MemoryDefinednessTreeNode;
          std::set<int> activeNodes;
          std::function<std::optional<json::Object>(
              int, bool, unsigned)>
              lowerTree =
                  [&](int nodeIndex, bool root,
                      unsigned depth)
                      -> std::optional<json::Object> {
                    if (
                        nodeIndex < 0 ||
                        static_cast<size_t>(nodeIndex) >=
                            incoming->conditionTree.size() ||
                        depth > 6 ||
                        !activeNodes.insert(nodeIndex).second)
                      return std::nullopt;
                    const TreeNode &node =
                        incoming->conditionTree[nodeIndex];
                    if (node.kind == TreeNode::Kind::Carry) {
                      activeNodes.erase(nodeIndex);
                      return variableOperand(merge.defined);
                    }
                    if (node.kind == TreeNode::Kind::Store) {
                      auto storePoison =
                          node.store == nullptr
                              ? context.poisonConditions.end()
                              : context.poisonConditions.find(
                                    node.store);
                      activeNodes.erase(nodeIndex);
                      return
                          storePoison ==
                                  context.poisonConditions.end()
                              ? integerConstant(1, 1)
                              : variableOperand(
                                    storePoison->second);
                    }
                    auto condition = operand(
                        const_cast<Value *>(node.condition),
                        context,
                        *predecessor->getTerminator());
                    auto whenTrue = lowerTree(
                        node.trueNode, false, depth + 1);
                    auto whenFalse = lowerTree(
                        node.falseNode, false, depth + 1);
                    if (
                        !condition || !whenTrue ||
                        !whenFalse) {
                      activeNodes.erase(nodeIndex);
                      return std::nullopt;
                    }
                    std::string destination =
                        root
                            ? merge.defined
                            : context.temporaryName(
                                  "recursive_memory_defined_");
                    json::Object select;
                    select["op"] = "select";
                    select["dst"] = destination;
                    select["condition"] =
                        std::move(*condition);
                    select["true"] =
                        std::move(*whenTrue);
                    select["false"] =
                        std::move(*whenFalse);
                    select["bits"] = 1;
                    if (root)
                      assignment = std::move(select);
                    else
                      instructions.push_back(
                          std::move(select));
                    activeNodes.erase(nodeIndex);
                    return variableOperand(destination);
                  };
          auto root = lowerTree(
              incoming->conditionTreeRoot, true, 0);
          if (!root) {
            reject(
                *predecessor->getTerminator(),
                "recursive conditional memory "
                "definedness tree is malformed");
            continue;
          }
        } else if (
            incoming->kind ==
            FunctionContext::MemoryPoisonIncoming::
                Kind::NestedConditionalStoresCarry) {
          auto outerCondition = operand(
              const_cast<Value *>(incoming->condition),
              context, *predecessor->getTerminator());
          auto innerCondition = operand(
              const_cast<Value *>(
                  incoming->innerCondition),
              context, *predecessor->getTerminator());
          if (!outerCondition || !innerCondition) {
            reject(
                *predecessor->getTerminator(),
                "nested conditional memory definedness "
                "carry has no bounded condition");
            continue;
          }
          auto secondaryPoison =
              incoming->secondaryStore == nullptr
                  ? context.poisonConditions.end()
                  : context.poisonConditions.find(
                        incoming->secondaryStore);
          json::Object firstDefined =
              poison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(poison->second);
          json::Object secondDefined =
              secondaryPoison ==
                      context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(
                        secondaryPoison->second);
          std::string nestedDefined =
              context.temporaryName(
                  "nested_memory_defined_");
          json::Object nested;
          nested["op"] = "select";
          nested["dst"] = nestedDefined;
          nested["condition"] =
              std::move(*innerCondition);
          nested["true"] =
              incoming->firstStoreWhenTrue
                  ? std::move(firstDefined)
                  : std::move(secondDefined);
          nested["false"] =
              incoming->firstStoreWhenTrue
                  ? std::move(secondDefined)
                  : std::move(firstDefined);
          nested["bits"] = 1;
          instructions.push_back(std::move(nested));
          json::Object stored =
              variableOperand(nestedDefined);
          json::Object carried =
              variableOperand(merge.defined);
          assignment["op"] = "select";
          assignment["condition"] =
              std::move(*outerCondition);
          assignment["true"] =
              incoming->storeWhenTrue
                  ? std::move(stored)
                  : std::move(carried);
          assignment["false"] =
              incoming->storeWhenTrue
                  ? std::move(carried)
                  : std::move(stored);
        } else if (
            incoming->kind ==
            FunctionContext::MemoryPoisonIncoming::
                Kind::ConditionalStoreCarry) {
          auto condition = operand(
              const_cast<Value *>(incoming->condition), context,
              *predecessor->getTerminator());
          if (!condition) {
            reject(
                *predecessor->getTerminator(),
                "conditional memory definedness carry "
                "has no bounded condition");
            continue;
          }
          json::Object storeDefined =
              poison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(poison->second);
          json::Object carried =
              variableOperand(merge.defined);
          assignment["op"] = "select";
          assignment["condition"] =
              std::move(*condition);
          assignment["true"] =
              incoming->storeWhenTrue
                  ? std::move(storeDefined)
                  : std::move(carried);
          assignment["false"] =
              incoming->storeWhenTrue
                  ? std::move(carried)
                  : std::move(storeDefined);
        } else {
          assignment["op"] = "unary";
          assignment["operator"] = "identity";
          assignment["value"] =
              incoming->kind ==
                      FunctionContext::MemoryPoisonIncoming::
                          Kind::Carry
                  ? variableOperand(merge.defined)
                  : poison == context.poisonConditions.end()
                  ? integerConstant(1, 1)
                  : variableOperand(poison->second);
        }
        assignment["bits"] = 1;
        instructions.push_back(
            std::move(assignment));
      }
      for (const auto &phi :
           context.byteLanePhis) {
        if (phi.load->getParent() != successor)
          continue;
        auto incoming = std::find_if(
            phi.incoming.begin(),
            phi.incoming.end(),
            [&](const auto &item) {
              return item.block == predecessor;
            });
        if (incoming == phi.incoming.end())
          continue;
        std::vector<std::string> sources;
        for (const auto &lane :
             incoming->lanes) {
          if (lane.store == nullptr)
            continue;
          auto defined =
              context.byteLaneStoreDefinedNames.find(
                  lane.store);
          if (
              defined ==
                  context.byteLaneStoreDefinedNames.end() ||
              std::find(
                  sources.begin(), sources.end(),
                  defined->second) != sources.end())
            continue;
          sources.push_back(defined->second);
        }
        if (sources.empty()) {
          json::Object assignment;
          assignment["op"] = "unary";
          assignment["operator"] = "identity";
          assignment["dst"] = phi.defined;
          assignment["value"] =
              integerConstant(1, 1);
          assignment["bits"] = 1;
          instructions.push_back(
              std::move(assignment));
          continue;
        }
        if (sources.size() == 1) {
          json::Object assignment;
          assignment["op"] = "unary";
          assignment["operator"] = "identity";
          assignment["dst"] = phi.defined;
          assignment["value"] =
              variableOperand(sources.front());
          assignment["bits"] = 1;
          instructions.push_back(
              std::move(assignment));
          continue;
        }
        std::string accumulated =
            sources.front();
        for (size_t index = 1;
             index < sources.size(); ++index) {
          std::string destination =
              index + 1 == sources.size()
                  ? phi.defined
                  : context.temporaryName(
                        "byte_lane_phi_defined_");
          json::Object combine;
          combine["op"] = "binary";
          combine["operator"] = "and";
          combine["dst"] = destination;
          combine["left"] =
              variableOperand(accumulated);
          combine["right"] =
              variableOperand(sources[index]);
          combine["bits"] = 1;
          instructions.push_back(
              std::move(combine));
          accumulated =
              std::move(destination);
        }
      }
      for (const auto &phi :
           context.cyclicByteLanePhis) {
        if (phi.load->getParent() != successor)
          continue;
        auto incoming = std::find_if(
            phi.incoming.begin(),
            phi.incoming.end(),
            [&](const auto &item) {
              return item.block == predecessor;
            });
        if (incoming == phi.incoming.end())
          continue;
        for (size_t laneIndex = 0;
             laneIndex < incoming->lanes.size();
             ++laneIndex) {
          const auto &lane =
              incoming->lanes[laneIndex];
          json::Object assignment;
          assignment["op"] = "unary";
          assignment["operator"] = "identity";
          assignment["dst"] =
              phi.laneDefined[laneIndex];
          if (
              lane.kind ==
              FunctionContext::
                  CyclicByteLaneMemorySource::
                      Kind::Carry) {
            assignment["value"] =
                variableOperand(
                    phi.laneDefined[laneIndex]);
          } else if (
              lane.kind ==
                  FunctionContext::
                      CyclicByteLaneMemorySource::
                          Kind::Store &&
              lane.store != nullptr) {
            auto defined =
                context
                    .byteLaneStoreDefinedNames
                    .find(lane.store);
            assignment["value"] =
                defined ==
                        context
                            .byteLaneStoreDefinedNames
                            .end()
                    ? integerConstant(1, 1)
                    : variableOperand(
                          defined->second);
          } else {
            assignment["value"] =
                integerConstant(1, 1);
          }
          assignment["bits"] = 1;
          instructions.push_back(
              std::move(assignment));
        }
      }
      for (const auto &phi :
           context.cyclicByteLanePhis)
        for (const auto &transfer :
             phi.conditionalTransfers) {
          bool storeEdge =
              predecessor ==
                  transfer.storeArm &&
              successor == transfer.join;
          bool carryEdge =
              predecessor ==
                  transfer.carryArm &&
              successor == transfer.join;
          if (!storeEdge && !carryEdge)
            continue;
          for (size_t laneIndex = 0;
               laneIndex < transfer.lanes.size();
               ++laneIndex) {
            const auto &lane =
                transfer.lanes[laneIndex];
            json::Object assignment;
            assignment["op"] = "unary";
            assignment["operator"] =
                "identity";
            assignment["dst"] =
                phi.laneDefined[laneIndex];
            if (
                storeEdge &&
                lane.kind ==
                    FunctionContext::
                        CyclicByteLaneMemorySource::
                            Kind::Store &&
                transfer.store != nullptr) {
              auto defined =
                  context
                      .byteLaneStoreDefinedNames
                      .find(transfer.store);
              assignment["value"] =
                  defined ==
                          context
                              .byteLaneStoreDefinedNames
                              .end()
                      ? integerConstant(1, 1)
                      : variableOperand(
                            defined->second);
            } else {
              assignment["value"] =
                  variableOperand(
                      phi.laneDefined[
                          laneIndex]);
            }
            assignment["bits"] = 1;
            instructions.push_back(
                std::move(assignment));
          }
        }
      for (const auto &phi :
           context.cyclicByteLanePhis)
        for (const auto &transfer :
             phi.multiArmTransfers)
          for (const auto &arm :
               transfer.arms) {
            if (
                predecessor != arm.block ||
                successor != transfer.join)
              continue;
            for (size_t laneIndex = 0;
                 laneIndex < arm.lanes.size();
                 ++laneIndex) {
              const auto &lane =
                  arm.lanes[laneIndex];
              json::Object assignment;
              assignment["op"] = "unary";
              assignment["operator"] =
                  "identity";
              assignment["dst"] =
                  phi.laneDefined[laneIndex];
              if (
                  lane.kind ==
                      FunctionContext::
                          CyclicByteLaneMemorySource::
                              Kind::Store &&
                  arm.store != nullptr) {
                auto defined =
                    context
                        .byteLaneStoreDefinedNames
                        .find(arm.store);
                assignment["value"] =
                    defined ==
                            context
                                .byteLaneStoreDefinedNames
                                .end()
                        ? integerConstant(1, 1)
                        : variableOperand(
                              defined->second);
              } else {
                assignment["value"] =
                    variableOperand(
                        phi.laneDefined[
                            laneIndex]);
              }
              assignment["bits"] = 1;
              instructions.push_back(
                  std::move(assignment));
            }
          }
      for (const auto &phi :
           context.cyclicByteLanePhis)
        for (const auto &transfer :
             phi.recursiveTransfers)
          for (const auto &leaf :
               transfer.leaves) {
            if (
                predecessor != leaf.block ||
                successor != transfer.join)
              continue;
            for (size_t laneIndex = 0;
                 laneIndex < leaf.lanes.size();
                 ++laneIndex) {
              const auto &lane =
                  leaf.lanes[laneIndex];
              json::Object assignment;
              assignment["op"] = "unary";
              assignment["operator"] =
                  "identity";
              assignment["dst"] =
                  phi.laneDefined[laneIndex];
              if (
                  lane.kind ==
                      FunctionContext::
                          CyclicByteLaneMemorySource::
                              Kind::Store &&
                  lane.store != nullptr) {
                auto defined =
                    context
                        .byteLaneStoreDefinedNames
                        .find(lane.store);
                assignment["value"] =
                    defined ==
                            context
                                .byteLaneStoreDefinedNames
                                .end()
                        ? integerConstant(1, 1)
                        : variableOperand(
                              defined->second);
              } else {
                assignment["value"] =
                    variableOperand(
                        phi.laneDefined[
                            laneIndex]);
              }
              assignment["bits"] = 1;
              instructions.push_back(
                  std::move(assignment));
            }
          }
      auto executableTransfer = std::find_if(
          context.executableNestedLoopMemoryTransfers.begin(),
          context.executableNestedLoopMemoryTransfers.end(),
          [&](const ExecutableNestedLoopMemoryTransfer &transfer) {
            return transfer.certificate.preheader == predecessor &&
                   transfer.certificate.header == successor;
          });
      if (executableTransfer !=
          context.executableNestedLoopMemoryTransfers.end()) {
        const MultiLatchLoopMemoryPhiCertificate &certificate =
            executableTransfer->certificate;
        json::Object summaryTransfer;
        summaryTransfer["op"] = "loop_summary_transfer";
        summaryTransfer["schema"] = "symcc-loop-summary-transfer-v1";
        summaryTransfer["mode"] = "explicit-opt-in";
        summaryTransfer["fallback"] = context.blocks.lookup(successor);
        summaryTransfer["target"] = context.blocks.lookup(certificate.exit);
        summaryTransfer["source_load"] = executableTransfer->sourceLoad;
        summaryTransfer["transcript"] =
            nestedLoopMemoryPhiSummaryRecord(certificate, context);
        json::Object proof;
        proof["effect"] = "closed-memory-only";
        proof["memory_kind"] = "stack";
        proof["memory_base"] = static_cast<int64_t>(certificate.base);
        proof["live_outs"] = 0;
        int64_t storeCount = 0;
        for (const MultiLatchLoopMemoryPhiTransfer &transfer :
             certificate.transfers)
          storeCount += static_cast<int64_t>(transfer.writers.size());
        proof["store_count"] = storeCount;
        json::Array loopBlocks;
        loopBlocks.push_back(context.blocks.lookup(certificate.header));
        loopBlocks.push_back(context.blocks.lookup(certificate.root));
        loopBlocks.push_back(context.blocks.lookup(certificate.innerHeader));
        loopBlocks.push_back(context.blocks.lookup(certificate.innerBody));
        loopBlocks.push_back(context.blocks.lookup(certificate.outerLatch));
        proof["loop_blocks"] = std::move(loopBlocks);
        summaryTransfer["proof"] = std::move(proof);
        instructions.push_back(std::move(summaryTransfer));
      } else {
        json::Object jump;
        jump["op"] = "jump";
        jump["target"] = context.blocks.lookup(successor);
        instructions.push_back(std::move(jump));
      }
      blocks[edge.second] = std::move(instructions);
    }
  }

  void lowerFunction(Function &function, bool isEntry,
                     json::Object &functions) {
    LiveFunctionAnalyses analyses =
        analysisProvider ? analysisProvider(function)
                         : LiveFunctionAnalyses{};
    FunctionContext context(
        function, analyses.aliasAnalysis, analyses.memorySSA);
    bool deferredPoisonReturn =
        functionHasDeferredPoisonReturn(function);
    bool transitiveArgumentPoisonReturn =
        functionHasTransitiveArgumentPoisonReturn(function);
    for (Argument &argument : function.args())
      if (argumentHasDeferredPoison(argument)) {
        context.poisonConditions[&argument] =
            context.values.lookup(&argument) + "__defined";
      }
    prepareByteLaneMemoryCompositions(
        function, context);
    prepareByteLaneMemoryPhis(
        function, context);
    prepareCyclicByteLaneMemoryPhis(
        function, context);
    prepareMemoryPoisonMerges(function, context);
    if (transitiveArgumentPoisonReturn)
      usesTransitiveCallDeferredPoison = true;
    for (Instruction &instruction : instructions(function)) {
      auto *phi = dyn_cast<PHINode>(&instruction);
      if (
          phi != nullptr && integerBits(phi->getType()) != 0 &&
          hasOnlyDeferredPoisonFreezeSinks(phi))
        context.poisonConditions[phi] =
            context.values.lookup(phi) + "__defined";
    }
    json::Object loweredFunction;
    loweredFunction["entry"] = context.blocks.lookup(
        &function.getEntryBlock());
    auto functionIdentifier = functionIds.find(&function);
    if (functionIdentifier != functionIds.end())
      loweredFunction["function_id"] =
          static_cast<int64_t>(functionIdentifier->second);
    json::Array parameters;
    json::Array parameterBits;
    json::Array pointerParameters;
    json::Array pointerDomains;
    json::Array definedParameters;
    std::vector<unsigned> pointerDomainIndices;
    if (!isEntry) {
      unsigned argumentIndex = 0;
      for (Argument &argument : function.args()) {
        if (argument.getType()->isPointerTy()) {
          unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
              argument.getType()->getPointerAddressSpace());
          if (argument.getType()->getPointerAddressSpace() != 0 ||
              pointerBits == 0 || pointerBits > 64) {
            errors.push_back(
                function.getName().str() +
                ": pointer parameter has an unsupported address space");
          } else {
            json::Object metadata;
            metadata["index"] =
                static_cast<int64_t>(argumentIndex);
            metadata["bits"] = static_cast<int64_t>(pointerBits);
            pointerParameters.push_back(std::move(metadata));
            pointerDomainIndices.push_back(argumentIndex);
            usesCrossFunctionPointers = true;
          }
          parameterBits.push_back(
              static_cast<int64_t>(pointerBits));
        } else if (integerBits(argument.getType()) == 0) {
          errors.push_back(function.getName().str() +
                           ": parameters must be bounded integers or pointers");
          parameterBits.push_back(1);
        } else {
          parameterBits.push_back(static_cast<int64_t>(
              integerBits(argument.getType())));
        }
        parameters.push_back(context.values.lookup(&argument));
        ++argumentIndex;
      }
      for (unsigned pointerIndex : pointerDomainIndices) {
        json::Object metadata;
        metadata["index"] = static_cast<int64_t>(pointerIndex);
        metadata["parameter"] =
            static_cast<int64_t>(parameters.size());
        pointerDomains.push_back(std::move(metadata));
        parameters.push_back(
            pointerDomainParameter(pointerIndex));
        parameterBits.push_back(1);
      }
      unsigned definedArgumentIndex = 0;
      for (Argument &argument : function.args()) {
        if (!argumentHasDeferredPoison(argument)) {
          ++definedArgumentIndex;
          continue;
        }
        json::Object metadata;
        metadata["index"] =
            static_cast<int64_t>(definedArgumentIndex);
        metadata["parameter"] =
            static_cast<int64_t>(parameters.size());
        definedParameters.push_back(std::move(metadata));
        parameters.push_back(
            context.poisonConditions.lookup(&argument));
        parameterBits.push_back(1);
        usesTransitiveDeferredPoison = true;
        usesCrossFunctionArgumentPoison = true;
        ++definedArgumentIndex;
      }
    }
    loweredFunction["params"] = std::move(parameters);
    loweredFunction["param_bits"] = std::move(parameterBits);
    if (!pointerParameters.empty())
      loweredFunction["pointer_params"] =
          std::move(pointerParameters);
    if (!pointerDomains.empty())
      loweredFunction["pointer_domains"] =
          std::move(pointerDomains);
    if (!definedParameters.empty())
      loweredFunction["defined_params"] =
          std::move(definedParameters);
    if (function.getReturnType()->isPointerTy()) {
      unsigned pointerBits = M.getDataLayout().getPointerSizeInBits(
          function.getReturnType()->getPointerAddressSpace());
      if (function.getReturnType()->getPointerAddressSpace() != 0 ||
          pointerBits == 0 || pointerBits > 64) {
        errors.push_back(
            function.getName().str() +
            ": pointer return has an unsupported address space");
      } else {
        loweredFunction["pointer_return_bits"] =
            static_cast<int64_t>(pointerBits);
        loweredFunction["return_bits"] =
            static_cast<int64_t>(pointerBits);
        loweredFunction["pointer_return_domain"] = true;
        usesCrossFunctionPointers = true;
        usesCrossFunctionDomains = true;
      }
    } else if (function.getReturnType()->isVoidTy()) {
      loweredFunction["return_bits"] = 0;
    } else {
      unsigned returnBits = integerBits(function.getReturnType());
      if (returnBits == 0) {
        errors.push_back(
            function.getName().str() +
            ": return type must be a bounded integer, pointer, or void");
      } else {
        loweredFunction["return_bits"] =
            static_cast<int64_t>(returnBits);
      }
    }
    if (deferredPoisonReturn)
      loweredFunction["return_defined"] = true;
    json::Object blocks;
    std::map<std::string, json::Array> extraBlocks;
    const auto liveBlocks =
        continuationReachableBlocks(function);
    for (BasicBlock &block : function) {
      if (liveBlocks.count(&block) == 0)
        continue;
      json::Array instructions;
      if (isEntry && &block == &function.getEntryBlock())
        appendEntryInputs(function, context, instructions);
      for (Instruction &instruction : block)
        lowerInstruction(instruction, context, instructions, extraBlocks);
      if (instructions.empty()) {
        reject(block.back(), "lowered basic block is empty");
        json::Object halt;
        halt["op"] = "halt";
        halt["value"] = integerConstant(0, 1);
        instructions.push_back(std::move(halt));
      }
      blocks[context.blocks.lookup(&block)] = std::move(instructions);
    }
    for (auto &extra : extraBlocks)
      blocks[extra.first] = std::move(extra.second);
    addPhiEdgeBlocks(context, blocks, liveBlocks);
    if (!context.memoryPoisonMerges.empty()) {
      json::Array contracts;
      for (const auto &merge :
           context.memoryPoisonMerges) {
        json::Object contract;
        contract["load"] =
            context.values.lookup(merge.load);
        contract["defined"] = merge.defined;
        contract["block"] =
            context.blocks.lookup(
                merge.load->getParent());
        if (merge.interprocedural)
          contract["interprocedural"] = true;
        if (merge.multiCell)
          contract["multicell"] = true;
        if (merge.identifiedObjectMultiCell)
          contract["identified_objects"] = true;
        if (merge.fixedHeapObjectMultiCell)
          contract["fixed_heap_objects"] = true;
        if (merge.finitePointerDomainMultiCell)
          contract["finite_pointer_domains"] = true;
        if (merge.guardCorrelatedPointerDomainMultiCell)
          contract["guard_correlated_pointer_domains"] = true;
        if (merge.phiCorrelatedPointerDomainMultiCell)
          contract["phi_correlated_pointer_domains"] = true;
        if (merge.symbolicIndexIntervalMultiCell)
          contract["symbolic_index_intervals"] = true;
        if (!merge.aliasGraphNeighbors.empty()) {
          json::Array neighbors;
          for (const LoadInst *neighbor :
               merge.aliasGraphNeighbors)
            neighbors.push_back(
                context.values.lookup(neighbor));
          contract["alias_graph_neighbors"] =
              std::move(neighbors);
        }
        if (merge.multilevel)
          contract["multilevel"] = true;
        if (merge.cyclic)
          contract["cyclic"] = true;
        if (merge.initial)
          contract["initial"] = true;
        if (merge.initialSubobject)
          contract["initial_subobject"] = true;
        if (merge.carry)
          contract["carry"] = true;
        if (merge.conditionalCarry)
          contract["conditional_carry"] = true;
        if (merge.forwardedConditionalCarry)
          contract["forwarded_conditional_carry"] = true;
        if (merge.multiArmConditionalCarry)
          contract["multiarm_conditional_carry"] = true;
        if (merge.equivalentDefinedStoreCarry)
          contract["equivalent_defined_stores"] = true;
        if (merge.sharedPoisonStoreCarry)
          contract["shared_poison_stores"] = true;
        if (merge.nestedConditionalCarry)
          contract["nested_conditional_carry"] = true;
        if (merge.recursiveConditionalCarry) {
          contract["recursive_conditional_carry"] = true;
          contract["condition_tree_depth"] =
              static_cast<int64_t>(
                  merge.conditionTreeDepth);
          contract["condition_tree_leaves"] =
              static_cast<int64_t>(
                  merge.conditionTreeLeaves);
          contract["condition_tree_carry_leaves"] =
              static_cast<int64_t>(
                  merge.conditionTreeCarryLeaves);
        }
        if (merge.groupedRecursiveConditionalCarry)
          contract["grouped_recursive_conditional_carry"] =
              true;
        if (merge.repeatedSourceRecursiveConditionalCarry)
          contract["repeated_source_recursive_conditional_carry"] =
              true;
        if (merge.multiCarryRecursiveConditionalCarry)
          contract["multicarry_recursive_conditional_carry"] =
              true;
        json::Array incoming;
        for (const auto &item : merge.incoming) {
          json::Object endpoint;
          endpoint["block"] =
              context.edgeBlocks.at({
                  item.block,
                  merge.load->getParent()});
          if (
              item.kind ==
              FunctionContext::MemoryPoisonIncoming::
                  Kind::Carry)
            endpoint["carry"] = true;
          if (
              item.kind ==
              FunctionContext::MemoryPoisonIncoming::
                  Kind::ConditionalStoreCarry) {
            endpoint["carry"] = true;
            endpoint["conditional_carry"] = true;
            if (item.conditionalForwarded)
              endpoint["forwarded_conditional_carry"] =
                  true;
            if (item.multiArmConditional)
              endpoint["multiarm_conditional_carry"] =
                  true;
            if (item.equivalentDefinedStores)
              endpoint["equivalent_defined_stores"] =
                  true;
            if (item.sharedPoisonStores)
              endpoint["shared_poison_stores"] = true;
          }
          if (
              item.kind ==
              FunctionContext::MemoryPoisonIncoming::
                  Kind::NestedConditionalStoresCarry) {
            endpoint["carry"] = true;
            endpoint["conditional_carry"] = true;
            endpoint["forwarded_conditional_carry"] =
                true;
            endpoint["multiarm_conditional_carry"] =
                true;
            endpoint["nested_conditional_carry"] =
                true;
          }
          if (
              item.kind ==
              FunctionContext::MemoryPoisonIncoming::
                  Kind::RecursiveConditionalTree) {
            endpoint["carry"] = true;
            endpoint["conditional_carry"] = true;
            endpoint["forwarded_conditional_carry"] =
                true;
            endpoint["multiarm_conditional_carry"] =
                true;
            endpoint["nested_conditional_carry"] =
                true;
            endpoint["recursive_conditional_carry"] =
                true;
            endpoint["condition_tree_depth"] =
                static_cast<int64_t>(
                    item.conditionTreeDepth);
            endpoint["condition_tree_leaves"] =
                static_cast<int64_t>(
                    std::count_if(
                        item.conditionTree.begin(),
                        item.conditionTree.end(),
                        [](const auto &node) {
                          return
                              node.kind !=
                              FunctionContext::
                                  MemoryDefinednessTreeNode::
                                      Kind::Select;
                        }));
            endpoint["condition_tree_carry_leaves"] =
                static_cast<int64_t>(
                    std::count_if(
                        item.conditionTree.begin(),
                        item.conditionTree.end(),
                        [](const auto &node) {
                          return
                              node.kind ==
                              FunctionContext::
                                  MemoryDefinednessTreeNode::
                                      Kind::Carry;
                        }));
            if (item.groupedRecursiveConditional)
              endpoint[
                  "grouped_recursive_conditional_carry"] =
                  true;
            if (item.repeatedSourceRecursiveConditional)
              endpoint[
                  "repeated_source_recursive_conditional_carry"] =
                  true;
            if (item.multiCarryRecursiveConditional)
              endpoint[
                  "multicarry_recursive_conditional_carry"] =
                  true;
          }
          incoming.push_back(
              std::move(endpoint));
        }
        contract["incoming"] =
            std::move(incoming);
        contracts.push_back(
            std::move(contract));
      }
      loweredFunction["memory_defined_phis"] =
          std::move(contracts);
    }
    if (!context.byteLaneCompositions.empty()) {
      usesByteLaneWriterGraph = true;
      json::Array contracts;
      for (const auto &composition :
           context.byteLaneCompositions) {
        json::Object contract;
        contract["load"] =
            context.values.lookup(
                composition.load);
        contract["defined"] =
            composition.defined;
        contract["block"] =
            context.blocks.lookup(
                composition.load->getParent());
        contract["bytes"] =
            static_cast<int64_t>(
                composition.bytes);
        contract["writer_graph"] = true;
        if (composition.initial)
          contract["initial"] = true;
        if (composition.crossBlock)
          contract["cross_block"] = true;
        json::Array lanes;
        for (unsigned lane = 0;
             lane < composition.lanes.size();
             ++lane) {
          const auto &source =
              composition.lanes[lane];
          json::Object metadata;
          metadata["lane"] =
              static_cast<int64_t>(lane);
          if (source.store == nullptr) {
            metadata["source"] = "initial";
          } else {
            metadata["source"] = "store";
            metadata["store"] =
                context.byteLaneStoreIds.lookup(
                    source.store);
            metadata["store_byte"] =
                static_cast<int64_t>(
                    source.storeByte);
            metadata["store_bytes"] =
                static_cast<int64_t>(
                    fixedStoreBytes(
                        M.getDataLayout(),
                        source.store
                            ->getValueOperand()
                            ->getType()));
            auto defined =
                context.byteLaneStoreDefinedNames.find(
                    source.store);
            if (
                defined !=
                context.byteLaneStoreDefinedNames.end())
              metadata["defined"] =
                  defined->second;
          }
          lanes.push_back(
              std::move(metadata));
        }
        contract["lanes"] =
            std::move(lanes);
        contracts.push_back(
            std::move(contract));
      }
      loweredFunction[
          "byte_lane_memory_definedness"] =
          std::move(contracts);
    }
    if (!context.byteLanePhis.empty()) {
      usesByteLanePhiWriterGraph = true;
      json::Array contracts;
      for (const auto &phi :
           context.byteLanePhis) {
        json::Object contract;
        contract["load"] =
            context.values.lookup(phi.load);
        contract["defined"] =
            phi.defined;
        contract["block"] =
            context.blocks.lookup(
                phi.load->getParent());
        contract["bytes"] =
            static_cast<int64_t>(
                phi.bytes);
        contract["writer_graph"] = true;
        json::Array incomingMetadata;
        for (const auto &incoming :
             phi.incoming) {
          json::Object endpoint;
          endpoint["block"] =
              context.edgeBlocks.at({
                  incoming.block,
                  phi.load->getParent()});
          if (incoming.initial)
            endpoint["initial"] = true;
          json::Array lanes;
          for (unsigned lane = 0;
               lane < incoming.lanes.size();
               ++lane) {
            const auto &source =
                incoming.lanes[lane];
            json::Object metadata;
            metadata["lane"] =
                static_cast<int64_t>(lane);
            if (source.store == nullptr) {
              metadata["source"] =
                  "initial";
            } else {
              metadata["source"] =
                  "store";
              metadata["store"] =
                  context.byteLaneStoreIds.lookup(
                      source.store);
              metadata["store_byte"] =
                  static_cast<int64_t>(
                      source.storeByte);
              metadata["store_bytes"] =
                  static_cast<int64_t>(
                      fixedStoreBytes(
                          M.getDataLayout(),
                          source.store
                              ->getValueOperand()
                              ->getType()));
              auto defined =
                  context.byteLaneStoreDefinedNames.find(
                      source.store);
              if (
                  defined !=
                  context.byteLaneStoreDefinedNames.end())
                metadata["defined"] =
                    defined->second;
            }
            lanes.push_back(
                std::move(metadata));
          }
          endpoint["lanes"] =
              std::move(lanes);
          incomingMetadata.push_back(
              std::move(endpoint));
        }
        contract["incoming"] =
            std::move(incomingMetadata);
        contracts.push_back(
            std::move(contract));
      }
      loweredFunction[
          "byte_lane_memory_definedness_phis"] =
          std::move(contracts);
    }
    if (!context.cyclicByteLanePhis.empty()) {
      json::Array contracts;
      json::Array conditionalContracts;
      json::Array forwardedConditionalContracts;
      json::Array multiArmConditionalContracts;
      json::Array forwardedMultiArmConditionalContracts;
      json::Array recursiveConditionalContracts;
      json::Array forwardedRecursiveConditionalContracts;
      json::Array groupedRecursiveConditionalContracts;
      json::Array forwardedGroupedRecursiveConditionalContracts;
      json::Array repeatedSourceRecursiveConditionalContracts;
      json::Array forwardedRepeatedSourceRecursiveConditionalContracts;
      json::Array composedRepeatedSourceRecursiveConditionalContracts;
      json::Array forwardedComposedRepeatedSourceRecursiveConditionalContracts;
      json::Array multiCarryComposedRepeatedSourceRecursiveConditionalContracts;
      json::Array forwardedMultiCarryComposedRepeatedSourceRecursiveConditionalContracts;
      json::Array multipleGroupsRecursiveConditionalContracts;
      json::Array forwardedMultipleGroupsRecursiveConditionalContracts;
      json::Array tripleGroupsRecursiveConditionalContracts;
      json::Array forwardedTripleGroupsRecursiveConditionalContracts;
      json::Array mixedMultipleGroupsRecursiveConditionalContracts;
      json::Array forwardedMixedMultipleGroupsRecursiveConditionalContracts;
      json::Array doubleComposedMultipleGroupsRecursiveConditionalContracts;
      json::Array forwardedDoubleComposedMultipleGroupsRecursiveConditionalContracts;
      json::Array composedTripleGroupsRecursiveConditionalContracts;
      json::Array forwardedComposedTripleGroupsRecursiveConditionalContracts;
      for (const auto &phi :
           context.cyclicByteLanePhis) {
        json::Object contract;
        contract["load"] =
            context.values.lookup(phi.load);
        contract["defined"] = phi.defined;
        contract["block"] =
            context.blocks.lookup(
                phi.load->getParent());
        contract["bytes"] =
            static_cast<int64_t>(phi.bytes);
        bool writerGraph =
            !phi.conditional &&
            !phi.multiArmConditional &&
            !phi.recursiveConditional;
        bool conditionalWriterGraph =
            phi.conditional &&
            !phi.multiArmConditional &&
            !phi.recursiveConditional;
        bool multiArmWriterGraph =
            phi.multiArmConditional &&
            !phi.recursiveConditional;
        bool recursiveWriterGraph =
            phi.recursiveConditional &&
            !phi.groupedRecursiveConditional &&
            !phi.repeatedSourceRecursiveConditional &&
            !phi.composedRepeatedSourceRecursiveConditional &&
            !phi.multiCarryRecursiveConditional &&
            !phi.multipleGroupsRecursiveConditional &&
            !phi.tripleGroupsRecursiveConditional &&
            !phi.mixedGroupsRecursiveConditional &&
            !phi.doubleComposedGroupsRecursiveConditional &&
            !phi.composedTripleGroupsRecursiveConditional;
        if (writerGraph) {
          contract["writer_graph"] = true;
          usesCyclicByteLaneWriterGraph = true;
        }
        if (conditionalWriterGraph) {
          contract["writer_graph"] = true;
          usesConditionalCyclicByteLaneWriterGraph = true;
        }
        if (multiArmWriterGraph) {
          contract["writer_graph"] = true;
          usesMultiArmCyclicByteLaneWriterGraph = true;
        }
        if (recursiveWriterGraph) {
          contract["writer_graph"] = true;
          usesRecursiveCyclicByteLaneWriterGraph = true;
        }
        json::Array laneDefined;
        for (const std::string &name :
             phi.laneDefined)
          laneDefined.push_back(name);
        contract["lane_defined"] =
            std::move(laneDefined);
        json::Array incomingMetadata;
        for (const auto &incoming :
             phi.incoming) {
          json::Object endpoint;
          endpoint["block"] =
              context.edgeBlocks.at({
                  incoming.block,
                  phi.load->getParent()});
          json::Array lanes;
          for (unsigned laneIndex = 0;
               laneIndex < incoming.lanes.size();
               ++laneIndex) {
            const auto &source =
                incoming.lanes[laneIndex];
            json::Object metadata;
            metadata["lane"] =
                static_cast<int64_t>(
                    laneIndex);
            if (
                source.kind ==
                FunctionContext::
                    CyclicByteLaneMemorySource::
                        Kind::Carry) {
              metadata["source"] = "carry";
            } else if (
                source.kind ==
                    FunctionContext::
                        CyclicByteLaneMemorySource::
                            Kind::Store &&
                source.store != nullptr) {
              metadata["source"] = "store";
              metadata["store"] =
                  context.byteLaneStoreIds.lookup(
                      source.store);
              metadata["store_byte"] =
                  static_cast<int64_t>(
                      source.storeByte);
              metadata["store_bytes"] =
                  static_cast<int64_t>(
                      fixedStoreBytes(
                          M.getDataLayout(),
                          source.store
                              ->getValueOperand()
                              ->getType()));
              auto defined =
                  context
                      .byteLaneStoreDefinedNames
                      .find(source.store);
              if (
                  defined !=
                  context
                      .byteLaneStoreDefinedNames
                      .end())
                metadata["defined"] =
                    defined->second;
            } else {
              metadata["source"] = "initial";
            }
            lanes.push_back(
                std::move(metadata));
          }
          endpoint["lanes"] =
              std::move(lanes);
          incomingMetadata.push_back(
              std::move(endpoint));
        }
        contract["incoming"] =
            std::move(incomingMetadata);
        if (phi.recursiveConditional) {
          json::Array transfers;
          for (const auto &transfer :
               phi.recursiveTransfers) {
            json::Object metadata;
            metadata["root"] =
                context.blocks.lookup(
                    transfer.rootBranch
                        ->getParent());
            metadata["join"] =
                context.blocks.lookup(
                    transfer.join);
            metadata["depth"] =
                static_cast<int64_t>(
                    transfer.depth);
            if (transfer.forwarded)
              metadata["forwarded"] = true;
            if (transfer.grouped)
              metadata["grouped"] = true;
            if (transfer.repeatedSource)
              metadata["repeated_source"] = true;
            if (transfer.composedRepeatedSource)
              metadata["composed_repeated_source"] = true;
            if (transfer.multiCarry) {
              metadata["multicarry"] = true;
              metadata["carry_leaves"] =
                  static_cast<int64_t>(
                      transfer.carryLeaves);
            }
            if (
                transfer.multipleGroups ||
                transfer.tripleGroups) {
              metadata["multiple_groups"] = true;
              metadata["group_count"] =
                  static_cast<int64_t>(
                      transfer.groupCount);
            }
            if (transfer.tripleGroups)
              metadata["triple_groups"] = true;
            if (transfer.mixedGroups) {
              metadata["mixed_groups"] = true;
              metadata["composed_group_count"] =
                  static_cast<int64_t>(
                      transfer.composedGroupCount);
            }
            if (transfer.doubleComposedGroups) {
              metadata["mixed_groups"] = true;
              metadata["double_composed_groups"] =
                  true;
              metadata["composed_group_count"] =
                  static_cast<int64_t>(
                      transfer.composedGroupCount);
            }
            if (transfer.composedTripleGroups) {
              metadata["mixed_groups"] = true;
              metadata["composed_triple_groups"] =
                  true;
              metadata["composed_group_count"] =
                  static_cast<int64_t>(
                      transfer.composedGroupCount);
            }
            json::Array branches;
            for (const auto &node :
                 transfer.branches) {
              json::Object branch;
              branch["block"] =
                  context.blocks.lookup(
                      node.branch
                          ->getParent());
              branch["true"] =
                  context.blocks.lookup(
                      node.branch
                          ->getSuccessor(0));
              branch["false"] =
                  context.blocks.lookup(
                      node.branch
                          ->getSuccessor(1));
              branches.push_back(
                  std::move(branch));
            }
            metadata["branches"] =
                std::move(branches);
            json::Array leaves;
            for (const auto &leaf :
                 transfer.leaves) {
              json::Object leafMetadata;
              leafMetadata["block"] =
                  context.blocks.lookup(
                      leaf.block);
              leafMetadata["successor"] =
                  context.blocks.lookup(
                      leaf.successor);
              leafMetadata["edge"] =
                  context.edgeBlocks.at({
                      leaf.block,
                      transfer.join});
              json::Array lanes;
              for (unsigned laneIndex = 0;
                   laneIndex <
                       leaf.lanes.size();
                   ++laneIndex) {
                const auto &source =
                    leaf.lanes[laneIndex];
                json::Object lane;
                lane["lane"] =
                    static_cast<int64_t>(
                        laneIndex);
                if (
                    source.kind ==
                        FunctionContext::
                            CyclicByteLaneMemorySource::
                                Kind::Store &&
                    source.store != nullptr) {
                  lane["source"] = "store";
                  lane["store"] =
                      context
                          .byteLaneStoreIds
                          .lookup(source.store);
                  lane["store_byte"] =
                      static_cast<int64_t>(
                          source.storeByte);
                  lane["store_bytes"] =
                      static_cast<int64_t>(
                          fixedStoreBytes(
                              M.getDataLayout(),
                              source.store
                                  ->getValueOperand()
                                  ->getType()));
                  auto defined =
                      context
                          .byteLaneStoreDefinedNames
                          .find(source.store);
                  if (
                      defined !=
                      context
                          .byteLaneStoreDefinedNames
                          .end())
                    lane["defined"] =
                        defined->second;
                } else {
                  lane["source"] = "carry";
                }
                lanes.push_back(
                    std::move(lane));
              }
              leafMetadata["lanes"] =
                  std::move(lanes);
              leaves.push_back(
                  std::move(leafMetadata));
            }
            metadata["leaves"] =
                std::move(leaves);
            transfers.push_back(
                std::move(metadata));
          }
          contract["recursive_transfers"] =
              std::move(transfers);
          if (phi.forwardedComposedTripleGroupsRecursiveConditional)
            forwardedComposedTripleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.composedTripleGroupsRecursiveConditional)
            composedTripleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedDoubleComposedGroupsRecursiveConditional)
            forwardedDoubleComposedMultipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.doubleComposedGroupsRecursiveConditional)
            doubleComposedMultipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedMixedGroupsRecursiveConditional)
            forwardedMixedMultipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.mixedGroupsRecursiveConditional)
            mixedMultipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedTripleGroupsRecursiveConditional)
            forwardedTripleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.tripleGroupsRecursiveConditional)
            tripleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedMultipleGroupsRecursiveConditional)
            forwardedMultipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.multipleGroupsRecursiveConditional)
            multipleGroupsRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedMultiCarryRecursiveConditional)
            forwardedMultiCarryComposedRepeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.multiCarryRecursiveConditional)
            multiCarryComposedRepeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedComposedRepeatedSourceRecursiveConditional)
            forwardedComposedRepeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.composedRepeatedSourceRecursiveConditional)
            composedRepeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedRepeatedSourceRecursiveConditional)
            forwardedRepeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.repeatedSourceRecursiveConditional)
            repeatedSourceRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedGroupedRecursiveConditional)
            forwardedGroupedRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.groupedRecursiveConditional)
            groupedRecursiveConditionalContracts.push_back(
                std::move(contract));
          else if (phi.forwardedRecursiveConditional)
            forwardedRecursiveConditionalContracts.push_back(
                std::move(contract));
          else
            recursiveConditionalContracts.push_back(
                std::move(contract));
        } else if (phi.multiArmConditional) {
          json::Array transfers;
          for (const auto &transfer :
               phi.multiArmTransfers) {
            json::Object metadata;
            metadata["root_branch"] =
                context.blocks.lookup(
                    transfer.rootBranch
                        ->getParent());
            metadata["inner_branch"] =
                context.blocks.lookup(
                    transfer.innerBranch
                        ->getParent());
            metadata["join"] =
                context.blocks.lookup(
                    transfer.join);
            if (transfer.forwarded)
              metadata["forwarded"] = true;
            json::Array arms;
            for (const auto &arm :
                 transfer.arms) {
              json::Object armMetadata;
              switch (arm.route) {
              case FunctionContext::
                  MultiArmCyclicByteLaneArm::
                      Route::Root:
                armMetadata["route"] = "root";
                break;
              case FunctionContext::
                  MultiArmCyclicByteLaneArm::
                      Route::InnerTrue:
                armMetadata["route"] =
                    "inner_true";
                break;
              case FunctionContext::
                  MultiArmCyclicByteLaneArm::
                      Route::InnerFalse:
                armMetadata["route"] =
                    "inner_false";
                break;
              default:
                llvm_unreachable(
                    "invalid multi-arm byte-lane route");
              }
              armMetadata["block"] =
                  context.blocks.lookup(
                      arm.block);
              armMetadata["successor"] =
                  context.blocks.lookup(
                      arm.successor);
              armMetadata["edge"] =
                  context.edgeBlocks.at({
                      arm.block,
                      transfer.join});
              json::Array lanes;
              for (unsigned laneIndex = 0;
                   laneIndex <
                       arm.lanes.size();
                   ++laneIndex) {
                const auto &source =
                    arm.lanes[laneIndex];
                json::Object lane;
                lane["lane"] =
                    static_cast<int64_t>(
                        laneIndex);
                if (
                    source.kind ==
                        FunctionContext::
                            CyclicByteLaneMemorySource::
                                Kind::Store &&
                    source.store != nullptr) {
                  lane["source"] = "store";
                  lane["store"] =
                      context
                          .byteLaneStoreIds
                          .lookup(source.store);
                  lane["store_byte"] =
                      static_cast<int64_t>(
                          source.storeByte);
                  lane["store_bytes"] =
                      static_cast<int64_t>(
                          fixedStoreBytes(
                              M.getDataLayout(),
                              source.store
                                  ->getValueOperand()
                                  ->getType()));
                  auto defined =
                      context
                          .byteLaneStoreDefinedNames
                          .find(source.store);
                  if (
                      defined !=
                      context
                          .byteLaneStoreDefinedNames
                          .end())
                    lane["defined"] =
                        defined->second;
                } else {
                  lane["source"] = "carry";
                }
                lanes.push_back(
                    std::move(lane));
              }
              armMetadata["lanes"] =
                  std::move(lanes);
              arms.push_back(
                  std::move(armMetadata));
            }
            metadata["arms"] =
                std::move(arms);
            transfers.push_back(
                std::move(metadata));
          }
          contract["multiarm_transfers"] =
              std::move(transfers);
          if (phi.forwardedMultiArmConditional)
            forwardedMultiArmConditionalContracts.push_back(
                std::move(contract));
          else
            multiArmConditionalContracts.push_back(
                std::move(contract));
        } else if (phi.conditional) {
          json::Array transfers;
          for (const auto &transfer :
               phi.conditionalTransfers) {
            json::Object metadata;
            metadata["branch"] =
                context.blocks.lookup(
                    transfer.branch
                        ->getParent());
            metadata["store_arm"] =
                context.blocks.lookup(
                    transfer.storeArm);
            metadata["carry_arm"] =
                context.blocks.lookup(
                    transfer.carryArm);
            metadata["store_successor"] =
                context.blocks.lookup(
                    transfer.storeSuccessor);
            metadata["carry_successor"] =
                context.blocks.lookup(
                    transfer.carrySuccessor);
            metadata["join"] =
                context.blocks.lookup(
                    transfer.join);
            metadata["store_edge"] =
                context.edgeBlocks.at({
                    transfer.storeArm,
                    transfer.join});
            metadata["carry_edge"] =
                context.edgeBlocks.at({
                    transfer.carryArm,
                    transfer.join});
            metadata["store_when_true"] =
                transfer.storeWhenTrue;
            if (transfer.forwarded)
              metadata["forwarded"] = true;
            json::Array lanes;
            for (unsigned laneIndex = 0;
                 laneIndex <
                     transfer.lanes.size();
                 ++laneIndex) {
              const auto &source =
                  transfer.lanes[laneIndex];
              json::Object lane;
              lane["lane"] =
                  static_cast<int64_t>(
                      laneIndex);
              if (
                  source.kind ==
                      FunctionContext::
                          CyclicByteLaneMemorySource::
                              Kind::Store &&
                  source.store != nullptr) {
                lane["source"] = "store";
                lane["store"] =
                    context
                        .byteLaneStoreIds
                        .lookup(source.store);
                lane["store_byte"] =
                    static_cast<int64_t>(
                        source.storeByte);
                lane["store_bytes"] =
                    static_cast<int64_t>(
                        fixedStoreBytes(
                            M.getDataLayout(),
                            source.store
                                ->getValueOperand()
                                ->getType()));
                auto defined =
                    context
                        .byteLaneStoreDefinedNames
                        .find(source.store);
                if (
                    defined !=
                    context
                        .byteLaneStoreDefinedNames
                        .end())
                  lane["defined"] =
                      defined->second;
              } else {
                lane["source"] = "carry";
              }
              lanes.push_back(
                  std::move(lane));
            }
            metadata["lanes"] =
                std::move(lanes);
            transfers.push_back(
                std::move(metadata));
          }
          contract["conditional_transfers"] =
              std::move(transfers);
          if (phi.forwardedConditional)
            forwardedConditionalContracts.push_back(
                std::move(contract));
          else
            conditionalContracts.push_back(
                std::move(contract));
        } else {
          contracts.push_back(
              std::move(contract));
        }
      }
      if (!contracts.empty())
        loweredFunction[
            "cyclic_byte_lane_memory_definedness_phis"] =
            std::move(contracts);
      if (!conditionalContracts.empty())
        loweredFunction[
            "conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(conditionalContracts);
      if (!forwardedConditionalContracts.empty())
        loweredFunction[
            "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedConditionalContracts);
      if (!multiArmConditionalContracts.empty())
        loweredFunction[
            "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                multiArmConditionalContracts);
      if (!forwardedMultiArmConditionalContracts.empty())
        loweredFunction[
            "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedMultiArmConditionalContracts);
      if (!recursiveConditionalContracts.empty())
        loweredFunction[
            "recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                recursiveConditionalContracts);
      if (!forwardedRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedRecursiveConditionalContracts);
      if (!groupedRecursiveConditionalContracts.empty())
        loweredFunction[
            "grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                groupedRecursiveConditionalContracts);
      if (!forwardedGroupedRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedGroupedRecursiveConditionalContracts);
      if (!repeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                repeatedSourceRecursiveConditionalContracts);
      if (!forwardedRepeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedRepeatedSourceRecursiveConditionalContracts);
      if (!composedRepeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                composedRepeatedSourceRecursiveConditionalContracts);
      if (!forwardedComposedRepeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedComposedRepeatedSourceRecursiveConditionalContracts);
      if (!multiCarryComposedRepeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                multiCarryComposedRepeatedSourceRecursiveConditionalContracts);
      if (!forwardedMultiCarryComposedRepeatedSourceRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedMultiCarryComposedRepeatedSourceRecursiveConditionalContracts);
      if (!multipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                multipleGroupsRecursiveConditionalContracts);
      if (!forwardedMultipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedMultipleGroupsRecursiveConditionalContracts);
      if (!tripleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                tripleGroupsRecursiveConditionalContracts);
      if (!forwardedTripleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedTripleGroupsRecursiveConditionalContracts);
      if (!mixedMultipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                mixedMultipleGroupsRecursiveConditionalContracts);
      if (!forwardedMixedMultipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedMixedMultipleGroupsRecursiveConditionalContracts);
      if (!doubleComposedMultipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                doubleComposedMultipleGroupsRecursiveConditionalContracts);
      if (!forwardedDoubleComposedMultipleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedDoubleComposedMultipleGroupsRecursiveConditionalContracts);
      if (!composedTripleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                composedTripleGroupsRecursiveConditionalContracts);
      if (!forwardedComposedTripleGroupsRecursiveConditionalContracts.empty())
        loweredFunction[
            "forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"] =
            std::move(
                forwardedComposedTripleGroupsRecursiveConditionalContracts);
    }
    if (!context.pointerBlockTags.empty()) {
      json::Array discriminators;
      for (BasicBlock &block : function) {
        std::string tag = context.pointerBlockTags.lookup(&block);
        if (tag.empty() || liveBlocks.count(&block) == 0)
          continue;
        json::Object contract;
        contract["block"] = context.blocks.lookup(&block);
        contract["tag"] = tag;
        contract["bits"] = 32;
        json::Array incoming;
        std::set<const BasicBlock *> seenPredecessors;
        for (BasicBlock *predecessor : predecessors(&block)) {
          if (
              liveBlocks.count(predecessor) == 0 ||
              !seenPredecessors.insert(predecessor).second)
            continue;
          json::Object endpoint;
          endpoint["edge"] = context.edgeBlocks.at({predecessor, &block});
          endpoint["predecessor"] = context.blocks.lookup(predecessor);
          endpoint["value"] = static_cast<int64_t>(
              context.blockIds.lookup(predecessor));
          incoming.push_back(std::move(endpoint));
        }
        if (!incoming.empty()) {
          contract["incoming"] = std::move(incoming);
          discriminators.push_back(std::move(contract));
        }
      }
      if (!discriminators.empty()) {
        loweredFunction["phi_edge_discriminators"] =
            std::move(discriminators);
        usesSharedPhiEdgeDiscriminator = true;
      }
    }
    loweredFunction["blocks"] = std::move(blocks);
    functions[function.getName()] = std::move(loweredFunction);
  }

  void writeReport(json::Object &report) const {
    report["schema"] = kReportSchema;
    report["status"] = "rejected";
    report["source_module"] = M.getModuleIdentifier();
    report["entry"] = entryName;
    report["llvm_version"] = LLVM_VERSION_STRING;
    json::Array diagnostics;
    for (const std::string &error : errors)
      diagnostics.push_back(error);
    report["diagnostics"] = std::move(diagnostics);
  }
};

void writeJson(StringRef path, json::Object object) {
  std::error_code error;
  raw_fd_ostream output(path, error, sys::fs::OF_Text);
  if (error)
    report_fatal_error(
        Twine("cannot write live continuation artifact: ") + error.message());
  output << formatv("{0:2}\n", json::Value(std::move(object)));
}

} // namespace

char LiveContinuationExportLegacyPass::ID = 0;

bool exportLiveContinuationWithAnalyses(
    Module &M, LiveFunctionAnalysisProvider analysisProvider,
    bool promoteAllocas) {
  const char *outputPath = std::getenv("SYMCC_LIVE_PROGRAM_OUT");
  if (outputPath == nullptr || *outputPath == '\0')
    return false;
  const char *rawEntry = std::getenv("SYMCC_LIVE_ENTRY");
  StringRef entry =
      rawEntry == nullptr || *rawEntry == '\0' ? StringRef("main")
                                               : StringRef(rawEntry);
  initializeStableSiteIds(M);
  json::Object program;
  json::Object report;
  bool lowered = ContinuationLowerer(
                     M, entry, std::move(analysisProvider),
                     promoteAllocas)
                     .run(program, report);
  writeJson(outputPath, lowered ? std::move(program) : std::move(report));
  if (!lowered && enabled(std::getenv("SYMCC_LIVE_STRICT")))
    report_fatal_error("LLVM-to-continuation lowering rejected the module");
  return true;
}

bool exportLiveContinuation(Module &M) {
  return exportLiveContinuationWithAnalyses(M, {}, true);
}

bool LiveContinuationExportLegacyPass::runOnModule(Module &M) {
  return exportLiveContinuation(M);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses
LiveContinuationExportPass::run(
    Module &M, ModuleAnalysisManager &analyses) {
  const char *outputPath = std::getenv("SYMCC_LIVE_PROGRAM_OUT");
  if (outputPath == nullptr || *outputPath == '\0')
    return PreservedAnalyses::all();

  bool promoted = promoteLiveScalarAllocas(M);
  auto &functionAnalyses =
      analyses.getResult<FunctionAnalysisManagerModuleProxy>(M)
          .getManager();
  if (promoted)
    for (Function &function : M)
      if (!function.isDeclaration())
        functionAnalyses.invalidate(
            function, PreservedAnalyses::none());
  LiveFunctionAnalysisProvider provider =
      [&](Function &function) -> LiveFunctionAnalyses {
    return {
        &functionAnalyses.getResult<AAManager>(function),
        &functionAnalyses.getResult<MemorySSAAnalysis>(function)
             .getMSSA(),
    };
  };
  (void)exportLiveContinuationWithAnalyses(
      M, std::move(provider), false);
  return PreservedAnalyses::none();
}
#endif

} // namespace symcc
