// This file is part of SymCC.
//
// Recover scalar memory live-outs across an F260 continuation dispatch.
// MemorySSA identifies the state reaching every capture edge, while AA proves
// that a bounded def-chain ends in an equal-width MustAlias store.

#include "IFSSContinuationMemory.h"

#include "ManifestWriter.h"
#include "SiteId.h"

#include <llvm/ADT/APInt.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/Analysis/AliasAnalysis.h>
#include <llvm/Analysis/MemoryLocation.h>
#include <llvm/Analysis/MemorySSA.h>
#include <llvm/Analysis/ValueTracking.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/DataLayout.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/FormatVariadic.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/ModRef.h>
#include <llvm/Support/raw_ostream.h>

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iterator>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr unsigned kMaxContinuationExits = 8;
constexpr unsigned kMaxContinuationMemorySlots = 4;
constexpr unsigned kMaxMemoryDefChain = 8;
constexpr unsigned kMaxNestedMemoryPhis = 4;
constexpr unsigned kMaxNestedPhiIncoming = 4;
constexpr unsigned kMaxProvenanceNodes = 16;
constexpr unsigned kMaxByteLaneWidth = 8;
constexpr unsigned kMaxByteLaneDefinitions = 16;
constexpr unsigned kMaxCyclicLatches = 4;
constexpr unsigned kMaxCyclicPredicateNodes = 3;
constexpr unsigned kMaxCyclicPredicateLeaves = 4;
constexpr unsigned kMaxCyclicSymbolicRegionWriters = 4;
constexpr unsigned kLegacyOrderedWriterLayers = 4;
constexpr unsigned kLegacyOrderedPointerPartitions = 2;
constexpr unsigned kLegacyOrderedGuardedWriters = 2;
constexpr unsigned kMaxOrderedWriterLayers = 8;
constexpr char kContinuationSchema[] = "bounded-continuation-tuple-v1";
constexpr char kMemorySchema[] =
    "must-alias-continuation-memory-tuple-v1";
constexpr char kMemoryInitialSchema[] =
    "must-alias-or-live-on-entry-continuation-memory-tuple-v2";
constexpr char kMemoryNestedSchema[] =
    "bounded-acyclic-memoryphi-continuation-memory-tuple-v3";
constexpr char kMemoryByteLaneSchema[] =
    "byte-lane-continuation-memory-tuple-v4";
constexpr char kMemoryGuardedByteLaneSchema[] =
    "guarded-byte-lane-continuation-memory-tuple-v5";
constexpr char kMemoryCyclicByteLaneSchema[] =
    "cyclic-byte-lane-continuation-memory-tuple-v6";
constexpr char kMemoryPointerPartitionSchema[] =
    "finite-pointer-union-continuation-memory-tuple-v7";
constexpr char kMemoryGuardedPrioritySchema[] =
    "guarded-write-priority-continuation-memory-tuple-v8";
constexpr char kMemoryConditionalCyclicByteLaneSchema[] =
    "conditional-cyclic-byte-lane-continuation-memory-tuple-v9";
constexpr char kMemoryMultiLatchCyclicByteLaneSchema[] =
    "multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10";
constexpr char kMemoryPointerPartitionPrioritySchema[] =
    "pointer-union-priority-continuation-memory-tuple-v11";
constexpr char kMemoryConditionalMultiLatchCyclicByteLaneSchema[] =
    "conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12";
constexpr char kMemoryBoundedMultiLatchCyclicByteLaneSchema[] =
    "bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13";
constexpr char kMemoryNestedPredicateCyclicByteLaneSchema[] =
    "nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14";
constexpr char kMemoryOrderedWriterGraphSchema[] =
    "ordered-writer-graph-continuation-memory-tuple-v15";
constexpr char kMemorySymbolicRegionWriterGraphSchema[] =
    "symbolic-region-writer-graph-continuation-memory-tuple-v16";
constexpr char kMemorySymbolicRegionCyclicByteLaneSchema[] =
    "symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17";
constexpr char kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema[] =
    "symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18";
constexpr char kMemoryOrderedSymbolicRegionCyclicByteLaneSchema[] =
    "ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19";
constexpr char kManifestSchema[] =
    "symcc-ifss-continuation-manifest-v1";

struct ExitProof {
  unsigned ordinal = 0;
  unsigned destinationOrdinal = 0;
  uint64_t sourceTerminatorSite = 0;
  unsigned successorIndex = 0;
};

struct ContinuationSummary {
  uint64_t controllerSite = 0;
  unsigned exitCount = 0;
  unsigned destinationCount = 0;
  unsigned scalarSlotCount = 0;
  unsigned blockCount = 0;
  unsigned pathCount = 0;
};

struct ScalarSlotProof {
  unsigned ordinal = 0;
  unsigned destinationOrdinal = 0;
  uint64_t originalPhiSite = 0;
};

struct CaptureState {
  BasicBlock *block = nullptr;
  unsigned ordinal = 0;
  unsigned destinationOrdinal = 0;
  uint64_t sourceTerminatorSite = 0;
  unsigned successorIndex = 0;
};

enum class MemoryStateKind : uint64_t {
  Store = 0,
  LiveOnEntry = 1,
  MemoryPhi = 2,
  ByteComposition = 3,
};

struct GuardedByteOverlay {
  unsigned priority = 0;
  StoreInst *store = nullptr;
  Instruction *guard = nullptr;
  bool storeWhenTrue = false;
  unsigned sourceByte = 0;
  unsigned sourceWidth = 0;
};

struct ByteLaneSource {
  unsigned ordinal = 0;
  StoreInst *store = nullptr;
  unsigned sourceByte = 0;
  unsigned sourceWidth = 0;
  SmallVector<GuardedByteOverlay, 2> guarded;
  bool carry = false;

  MemoryStateKind kind() const {
    assert(!carry && "cyclic carry has no scalar memory-state kind");
    return store == nullptr ? MemoryStateKind::LiveOnEntry
                            : MemoryStateKind::Store;
  }

  bool hasGuardedSource() const {
    return !guarded.empty();
  }
};

struct PointerPartitionChild {
  bool leaf = false;
  unsigned index = 0;
};

struct PointerPartitionNode {
  SelectInst *select = nullptr;
  Instruction *guard = nullptr;
  PointerPartitionChild trueChild;
  PointerPartitionChild falseChild;
};

struct PointerPartitionLeaf {
  SmallVector<int, kMaxByteLaneWidth> sourceBytes;
};

struct PointerPartition {
  StoreInst *store = nullptr;
  unsigned storeWidth = 0;
  SmallVector<PointerPartitionNode, 3> nodes;
  SmallVector<PointerPartitionLeaf, 4> leaves;
};

enum class OrderedWriterKind : uint64_t {
  Guarded = 0,
  PointerPartition = 1,
  SymbolicRegion = 2,
};

struct SymbolicRegionCase {
  int64_t indexValue = 0;
  unsigned sourceByte = 0;
};

struct OrderedWriterLayer {
  unsigned ordinal = 0;
  OrderedWriterKind kind = OrderedWriterKind::Guarded;
  StoreInst *store = nullptr;
  unsigned storeWidth = 0;
  Instruction *guard = nullptr;
  SmallVector<int, kMaxByteLaneWidth> sourceBytes;
  SmallVector<int, kMaxByteLaneWidth> storePolarities;
  std::shared_ptr<PointerPartition> pointerPartition;
  Instruction *regionBase = nullptr;
  uint64_t regionExtent = 0;
  Instruction *symbolicIndex = nullptr;
  unsigned symbolicIndexBits = 0;
  int64_t symbolicBaseOffset = 0;
  SmallVector<
      SmallVector<SymbolicRegionCase, kMaxByteLaneWidth>,
      kMaxByteLaneWidth>
      symbolicLaneCases;
};

struct CyclicPredicateChild {
  bool leaf = false;
  unsigned index = 0;
};

struct CyclicPredicateNode {
  BasicBlock *block = nullptr;
  Instruction *guard = nullptr;
  CyclicPredicateChild trueChild;
  CyclicPredicateChild falseChild;
};

struct CyclicPredicateLeaf {
  BasicBlock *block = nullptr;
  SmallVector<Instruction *, kMaxMemoryDefChain> skipped;
  SmallVector<ByteLaneSource, kMaxByteLaneWidth> byteLanes;
};

struct CyclicBackedgeTransfer {
  BasicBlock *block = nullptr;
  BasicBlock *branch = nullptr;
  BasicBlock *storeArm = nullptr;
  BasicBlock *carryArm = nullptr;
  Instruction *guard = nullptr;
  bool storeWhenTrue = false;
  SmallVector<Instruction *, kMaxMemoryDefChain> skipped;
  SmallVector<ByteLaneSource, kMaxByteLaneWidth> byteLanes;
  SmallVector<CyclicPredicateNode, kMaxCyclicPredicateNodes>
      predicateNodes;
  SmallVector<CyclicPredicateLeaf, kMaxCyclicPredicateLeaves>
      predicateLeaves;
  std::shared_ptr<OrderedWriterLayer> symbolicRegion;

  bool hasPredicateTree() const {
    return !predicateNodes.empty();
  }
};

struct ReachingState {
  MemoryStateKind kind = MemoryStateKind::Store;
  BasicBlock *point = nullptr;
  StoreInst *store = nullptr;
  MemoryPhi *memoryPhi = nullptr;
  SmallVector<Instruction *, kMaxMemoryDefChain> skipped;
  SmallVector<BasicBlock *, kMaxNestedPhiIncoming> incomingBlocks;
  std::vector<std::shared_ptr<ReachingState>> incomingStates;
  SmallVector<ByteLaneSource, kMaxByteLaneWidth> byteLanes;
  unsigned byteWidth = 0;
  bool littleEndian = true;
  BasicBlock *cycleHeader = nullptr;
  BasicBlock *cycleEntry = nullptr;
  BasicBlock *cycleBackedge = nullptr;
  BasicBlock *cycleBranch = nullptr;
  BasicBlock *cycleStoreArm = nullptr;
  BasicBlock *cycleCarryArm = nullptr;
  Instruction *cycleGuard = nullptr;
  bool cycleStoreWhenTrue = false;
  std::shared_ptr<ReachingState> cycleEntryState;
  SmallVector<Instruction *, kMaxMemoryDefChain> cycleSkipped;
  SmallVector<CyclicBackedgeTransfer, kMaxCyclicLatches> cycleTransfers;
  std::shared_ptr<OrderedWriterLayer> cycleSymbolicRegion;
  SmallVector<
      OrderedWriterLayer, kMaxCyclicSymbolicRegionWriters>
      cycleSymbolicRegions;
  std::shared_ptr<PointerPartition> pointerPartition;
  SmallVector<OrderedWriterLayer, kMaxOrderedWriterLayers> writerLayers;

  bool requiresOrderedWriterGraph() const {
    unsigned partitions = 0;
    unsigned guards = 0;
    unsigned symbolicRegions = 0;
    for (const OrderedWriterLayer &layer : writerLayers) {
      partitions +=
          layer.kind == OrderedWriterKind::PointerPartition ? 1 : 0;
      guards += layer.kind == OrderedWriterKind::Guarded ? 1 : 0;
      symbolicRegions +=
          layer.kind == OrderedWriterKind::SymbolicRegion ? 1 : 0;
    }
    if (symbolicRegions != 0 ||
        writerLayers.size() > kLegacyOrderedWriterLayers ||
        partitions > kLegacyOrderedPointerPartitions ||
        guards > kLegacyOrderedGuardedWriters)
      return true;
    if (partitions == 0)
      return false;
    const bool legacyPointerPriority =
        writerLayers.size() == 2 && partitions == 1 && guards == 1 &&
        writerLayers[0].kind == OrderedWriterKind::PointerPartition &&
        writerLayers[1].kind == OrderedWriterKind::Guarded;
    return !(writerLayers.size() == 1 && partitions == 1) &&
           !legacyPointerPriority;
  }

  bool requiresSymbolicRegionWriterGraph() const {
    unsigned partitions = 0;
    unsigned guards = 0;
    for (const OrderedWriterLayer &layer : writerLayers) {
      if (layer.kind == OrderedWriterKind::SymbolicRegion)
        return true;
      partitions +=
          layer.kind == OrderedWriterKind::PointerPartition ? 1 : 0;
      guards += layer.kind == OrderedWriterKind::Guarded ? 1 : 0;
    }
    return writerLayers.size() > kLegacyOrderedWriterLayers ||
           partitions > kLegacyOrderedPointerPartitions ||
           guards > kLegacyOrderedGuardedWriters;
  }

  bool hasKind(MemoryStateKind target) const {
    if (kind == target)
      return true;
    if (kind == MemoryStateKind::ByteComposition &&
        cycleEntryState == nullptr &&
        (target == MemoryStateKind::Store ||
         target == MemoryStateKind::LiveOnEntry))
      return std::any_of(
          byteLanes.begin(), byteLanes.end(),
          [&](const ByteLaneSource &lane) {
            return (!lane.carry && lane.kind() == target) ||
                   (target == MemoryStateKind::Store &&
                    lane.hasGuardedSource());
          });
    if (cycleEntryState != nullptr) {
      if (cycleEntryState->hasKind(target))
        return true;
      if (target == MemoryStateKind::Store &&
          (cycleSymbolicRegion != nullptr ||
           !cycleSymbolicRegions.empty() ||
           std::any_of(
               byteLanes.begin(), byteLanes.end(),
               [](const ByteLaneSource &lane) {
                 return lane.store != nullptr;
               }) ||
           std::any_of(
               cycleTransfers.begin(), cycleTransfers.end(),
               [](const CyclicBackedgeTransfer &transfer) {
                 if (transfer.symbolicRegion != nullptr)
                   return true;
                 if (std::any_of(
                         transfer.byteLanes.begin(),
                         transfer.byteLanes.end(),
                         [](const ByteLaneSource &lane) {
                           return lane.store != nullptr;
                         }))
                   return true;
                 return std::any_of(
                     transfer.predicateLeaves.begin(),
                     transfer.predicateLeaves.end(),
                     [](const CyclicPredicateLeaf &leaf) {
                       return std::any_of(
                           leaf.byteLanes.begin(),
                           leaf.byteLanes.end(),
                           [](const ByteLaneSource &lane) {
                             return lane.store != nullptr;
                           });
                     });
               })))
        return true;
    }
    if (target == MemoryStateKind::Store &&
        pointerPartition != nullptr)
      return true;
    return std::any_of(
        incomingStates.begin(), incomingStates.end(),
        [&](const std::shared_ptr<ReachingState> &state) {
          return state->hasKind(target);
        });
  }
};

struct MemoryCandidate {
  LoadInst *load = nullptr;
  PHINode *exitId = nullptr;
  MemoryPhi *memoryPhi = nullptr;
  uint64_t controllerSite = 0;
  unsigned destinationOrdinal = 0;
  SmallVector<CaptureState, kMaxContinuationExits> captures;
  SmallVector<ReachingState, kMaxContinuationExits> states;

  bool hasLiveOnEntry() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.hasKind(MemoryStateKind::LiveOnEntry);
        });
  }

  bool hasNestedMemoryPhi() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.hasKind(MemoryStateKind::MemoryPhi);
        });
  }

  bool hasByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.kind == MemoryStateKind::ByteComposition ||
                 state.cycleEntryState != nullptr;
        });
  }

  bool hasCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleEntryState != nullptr;
        });
  }

  bool hasConditionalCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleGuard != nullptr;
        });
  }

  bool hasMultiLatchCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return !state.cycleTransfers.empty();
        });
  }

  bool hasConditionalMultiLatchCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleTransfers.size() == 2 &&
                 std::any_of(
              state.cycleTransfers.begin(),
              state.cycleTransfers.end(),
              [](const CyclicBackedgeTransfer &transfer) {
                return transfer.guard != nullptr;
              });
        });
  }

  bool hasNestedPredicateCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return std::any_of(
              state.cycleTransfers.begin(),
              state.cycleTransfers.end(),
              [](const CyclicBackedgeTransfer &transfer) {
                return transfer.hasPredicateTree();
              });
        });
  }

  bool hasOrderedWriterGraphComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.requiresOrderedWriterGraph();
        });
  }

  bool hasSymbolicRegionWriterGraphComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.requiresSymbolicRegionWriterGraph();
        });
  }

  bool hasSymbolicRegionCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleEntryState != nullptr &&
                 (state.cycleSymbolicRegion != nullptr ||
                  !state.cycleSymbolicRegions.empty());
        });
  }

  bool hasOrderedSymbolicRegionCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleEntryState != nullptr &&
                 state.cycleSymbolicRegions.size() >= 2;
        });
  }

  bool hasSymbolicRegionMultiLatchCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleEntryState != nullptr &&
                 !state.cycleTransfers.empty() &&
                 std::any_of(
                     state.cycleTransfers.begin(),
                     state.cycleTransfers.end(),
                     [](const CyclicBackedgeTransfer &transfer) {
                       return transfer.symbolicRegion != nullptr;
                     });
        });
  }

  bool hasBoundedMultiLatchCyclicByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.cycleTransfers.size() > 2;
        });
  }

  bool hasPointerPartitionPriorityComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.pointerPartition != nullptr &&
                 std::any_of(
                     state.byteLanes.begin(),
                     state.byteLanes.end(),
                     [](const ByteLaneSource &lane) {
                       return lane.hasGuardedSource();
                     });
        });
  }

  bool hasPointerPartition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.pointerPartition != nullptr;
        });
  }

  bool hasGuardedWritePriority() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return std::any_of(
              state.byteLanes.begin(), state.byteLanes.end(),
              [](const ByteLaneSource &lane) {
                return lane.guarded.size() > 1;
              });
        });
  }

  bool hasGuardedByteLaneComposition() const {
    return std::any_of(
        states.begin(), states.end(), [](const ReachingState &state) {
          return state.kind == MemoryStateKind::ByteComposition &&
                 std::any_of(
                     state.byteLanes.begin(), state.byteLanes.end(),
                     [](const ByteLaneSource &lane) {
                       return lane.hasGuardedSource();
                     });
        });
  }
};

struct ProvenanceBudget {
  unsigned phiCount = 0;
  unsigned nodeCount = 0;
  SmallPtrSet<MemoryAccess *, kMaxNestedMemoryPhis> active;
};

struct ManifestMemorySlot {
  unsigned ordinal = 0;
  uint64_t loadSite = 0;
  MemoryCandidate candidate;
};

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

Metadata *integerMetadata(LLVMContext &context, unsigned bits,
                          uint64_t value) {
  return ConstantAsMetadata::get(
      ConstantInt::get(IntegerType::get(context, bits), value));
}

bool metadataInteger(const MDNode &node, unsigned index, uint64_t &value) {
  if (index >= node.getNumOperands())
    return false;
  auto *constant =
      mdconst::dyn_extract<ConstantInt>(node.getOperand(index));
  if (constant == nullptr)
    return false;
  value = constant->getZExtValue();
  return true;
}

bool hasSchema(const MDNode &node, StringRef schema) {
  if (node.getNumOperands() == 0)
    return false;
  auto *text = dyn_cast<MDString>(node.getOperand(0));
  return text != nullptr && text->getString() == schema;
}

bool parseExitProof(const Instruction &instruction, uint64_t &controllerSite,
                    ExitProof &result) {
  const MDNode *proof =
      instruction.getMetadata("symcc.ifss_continuation_exit");
  if (proof == nullptr || proof->getNumOperands() != 6 ||
      !hasSchema(*proof, kContinuationSchema))
    return false;

  uint64_t ordinal = 0;
  uint64_t sourceSite = 0;
  uint64_t successorIndex = 0;
  uint64_t destination = 0;
  if (!metadataInteger(*proof, 1, controllerSite) ||
      !metadataInteger(*proof, 2, ordinal) ||
      !metadataInteger(*proof, 3, sourceSite) ||
      !metadataInteger(*proof, 4, successorIndex) ||
      !metadataInteger(*proof, 5, destination) ||
      ordinal >= kMaxContinuationExits ||
      successorIndex >= 2 || destination >= kMaxContinuationExits)
    return false;
  result.ordinal = static_cast<unsigned>(ordinal);
  result.sourceTerminatorSite = sourceSite;
  result.successorIndex = static_cast<unsigned>(successorIndex);
  result.destinationOrdinal = static_cast<unsigned>(destination);
  return true;
}

bool parseContinuationSummary(const PHINode &phi,
                              ContinuationSummary &result) {
  const MDNode *proof = phi.getMetadata("symcc.ifss_continuation");
  if (proof == nullptr || proof->getNumOperands() != 7 ||
      !hasSchema(*proof, kContinuationSchema))
    return false;

  uint64_t controller = 0;
  uint64_t exits = 0;
  uint64_t destinations = 0;
  uint64_t scalarSlots = 0;
  uint64_t blocks = 0;
  uint64_t paths = 0;
  if (!metadataInteger(*proof, 1, controller) ||
      !metadataInteger(*proof, 2, exits) ||
      !metadataInteger(*proof, 3, destinations) ||
      !metadataInteger(*proof, 4, scalarSlots) ||
      !metadataInteger(*proof, 5, blocks) ||
      !metadataInteger(*proof, 6, paths) || exits < 2 ||
      exits > kMaxContinuationExits || destinations < 2 ||
      destinations > exits || scalarSlots > 8 || blocks > 32 ||
      paths == 0 || paths > 64)
    return false;
  result.controllerSite = controller;
  result.exitCount = static_cast<unsigned>(exits);
  result.destinationCount = static_cast<unsigned>(destinations);
  result.scalarSlotCount = static_cast<unsigned>(scalarSlots);
  result.blockCount = static_cast<unsigned>(blocks);
  result.pathCount = static_cast<unsigned>(paths);
  return true;
}

PHINode *findExitId(BasicBlock &dispatch, uint64_t &controllerSite,
                    unsigned &exitCount) {
  for (PHINode &phi : dispatch.phis()) {
    ContinuationSummary summary;
    if (phi.getType()->isIntegerTy(8) &&
        parseContinuationSummary(phi, summary)) {
      controllerSite = summary.controllerSite;
      exitCount = summary.exitCount;
      return &phi;
    }
  }
  return nullptr;
}

MemoryAccess *incomingAccessForBlock(MemoryPhi &phi, BasicBlock *block) {
  MemoryAccess *result = nullptr;
  for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
    if (phi.getIncomingBlock(index) != block)
      continue;
    if (result != nullptr)
      return nullptr;
    result = phi.getIncomingValue(index);
  }
  return result;
}

bool valueDominates(const Value *value, Instruction *point,
                    DominatorTree &dominators) {
  if (isa<Constant, Argument>(value))
    return true;
  auto *instruction = dyn_cast<Instruction>(value);
  return instruction != nullptr && dominators.dominates(instruction, point);
}

bool collectCaptures(BasicBlock &dispatch, uint64_t controllerSite,
                     unsigned exitCount,
                     SmallVectorImpl<CaptureState> &captures) {
  SmallVector<CaptureState, kMaxContinuationExits> byOrdinal(exitCount);
  SmallVector<bool, kMaxContinuationExits> seen(exitCount, false);
  unsigned predecessorEdges = 0;
  for (BasicBlock *predecessor : predecessors(&dispatch)) {
    ++predecessorEdges;
    auto *branch = dyn_cast<BranchInst>(predecessor->getTerminator());
    uint64_t exitController = 0;
    ExitProof proof;
    if (branch == nullptr || !branch->isUnconditional() ||
        branch->getSuccessor(0) != &dispatch ||
        !parseExitProof(*branch, exitController, proof) ||
        exitController != controllerSite || proof.ordinal >= exitCount ||
        seen[proof.ordinal])
      return false;
    seen[proof.ordinal] = true;
    byOrdinal[proof.ordinal] = {
        predecessor, proof.ordinal, proof.destinationOrdinal,
        proof.sourceTerminatorSite, proof.successorIndex};
  }
  if (predecessorEdges != exitCount ||
      std::find(seen.begin(), seen.end(), false) != seen.end())
    return false;
  captures.append(byOrdinal.begin(), byOrdinal.end());
  return true;
}

bool destinationMatchesExits(
    BasicBlock &destination, uint64_t controllerSite,
    ArrayRef<CaptureState> captures, unsigned &destinationOrdinal) {
  SmallVector<bool, kMaxContinuationExits> seen(captures.size(), false);
  bool haveDestination = false;
  unsigned predecessorEdges = 0;
  for (BasicBlock *predecessor : predecessors(&destination)) {
    ++predecessorEdges;
    auto *branch = dyn_cast<BranchInst>(predecessor->getTerminator());
    uint64_t exitController = 0;
    ExitProof proof;
    if (branch == nullptr || !branch->isUnconditional() ||
        branch->getSuccessor(0) != &destination ||
        !parseExitProof(*branch, exitController, proof) ||
        exitController != controllerSite ||
        proof.ordinal >= captures.size() || seen[proof.ordinal] ||
        captures[proof.ordinal].destinationOrdinal !=
            proof.destinationOrdinal)
      return false;
    if (!haveDestination) {
      destinationOrdinal = proof.destinationOrdinal;
      haveDestination = true;
    } else if (destinationOrdinal != proof.destinationOrdinal) {
      return false;
    }
    seen[proof.ordinal] = true;
  }
  if (!haveDestination || predecessorEdges == 0)
    return false;

  for (const CaptureState &capture : captures)
    if (seen[capture.ordinal] !=
        (capture.destinationOrdinal == destinationOrdinal))
      return false;
  return true;
}

bool destinationPrefixHasNoWrite(const LoadInst &load) {
  for (const Instruction &instruction : *load.getParent()) {
    if (&instruction == &load)
      return true;
    if (instruction.mayWriteToMemory())
      return false;
  }
  return false;
}

bool findReachingState(MemoryAccess *access, MemoryAccess *liveOnEntry,
                       MemoryPhi *dispatchPhi, const LoadInst &load,
                       AAResults &aliasAnalysis,
                       DominatorTree &dominators, BasicBlock *point,
                       ReachingState &result, ProvenanceBudget &budget) {
  if (access == nullptr || ++budget.nodeCount > kMaxProvenanceNodes)
    return false;
  result.point = point;
  MemoryLocation loadLocation = MemoryLocation::get(&load);
  while (access != liveOnEntry) {
    if (auto *phi = dyn_cast<MemoryPhi>(access)) {
      if (phi == dispatchPhi || phi->getNumIncomingValues() < 2 ||
          phi->getNumIncomingValues() > kMaxNestedPhiIncoming ||
          budget.phiCount >= kMaxNestedMemoryPhis ||
          budget.active.count(phi) != 0 ||
          !dominators.dominates(phi->getBlock(), point))
        return false;
      SmallPtrSet<BasicBlock *, kMaxNestedPhiIncoming> seenBlocks;
      for (unsigned index = 0; index < phi->getNumIncomingValues();
           ++index)
        if (!seenBlocks.insert(phi->getIncomingBlock(index)).second)
          return false;

      ++budget.phiCount;
      budget.active.insert(phi);
      result.kind = MemoryStateKind::MemoryPhi;
      result.memoryPhi = phi;
      for (unsigned index = 0; index < phi->getNumIncomingValues();
           ++index) {
        BasicBlock *incomingBlock = phi->getIncomingBlock(index);
        auto incoming = std::make_shared<ReachingState>();
        if (!findReachingState(
                phi->getIncomingValue(index), liveOnEntry, dispatchPhi,
                load, aliasAnalysis, dominators, incomingBlock, *incoming,
                budget))
          return false;
        result.incomingBlocks.push_back(incomingBlock);
        result.incomingStates.push_back(std::move(incoming));
      }
      budget.active.erase(phi);
      return true;
    }

    auto *definition = dyn_cast_or_null<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    auto *store = dyn_cast_or_null<StoreInst>(instruction);
    if ((store != nullptr && !store->isSimple()) ||
        isa_and_nonnull<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(
            instruction))
      return false;
    if (store != nullptr &&
        aliasAnalysis.alias(loadLocation, MemoryLocation::get(store)) ==
            AliasResult::MustAlias) {
      Value *storedValue = store->getValueOperand();
      if (!store->isSimple() || storedValue->getType() != load.getType() ||
          isa<UndefValue, PoisonValue>(storedValue) ||
          !valueDominates(storedValue, point->getTerminator(), dominators) ||
          !dominators.dominates(store, point->getTerminator()))
        return false;
      result.kind = MemoryStateKind::Store;
      result.store = store;
      return true;
    }
    if (instruction == nullptr ||
        isModSet(aliasAnalysis.getModRefInfo(instruction, loadLocation)) ||
        result.skipped.size() >= kMaxMemoryDefChain)
      return false;
    result.skipped.push_back(instruction);
    access = definition->getDefiningAccess();
  }
  if (access != liveOnEntry ||
      !valueDominates(
          load.getPointerOperand(), point->getTerminator(), dominators))
    return false;
  result.kind = MemoryStateKind::LiveOnEntry;
  return true;
}

std::optional<unsigned> fixedIntegerByteWidth(Type *type) {
  auto *integer = dyn_cast<IntegerType>(type);
  if (integer == nullptr || integer->getBitWidth() % 8 != 0)
    return std::nullopt;
  const unsigned width = integer->getBitWidth() / 8;
  if (width == 0 || width > kMaxByteLaneWidth)
    return std::nullopt;
  return width;
}

bool exactInBoundsAddress(const Value *pointer, const DataLayout &layout,
                          const Value *&base, int64_t &offset) {
  offset = 0;
  base = GetPointerBaseWithConstantOffset(
      pointer, offset, layout, false);
  return base != nullptr;
}

bool byteIntervalsOverlap(int64_t leftOffset, unsigned leftWidth,
                          int64_t rightOffset, unsigned rightWidth) {
  const __int128 leftBegin = leftOffset;
  const __int128 leftEnd = leftBegin + leftWidth;
  const __int128 rightBegin = rightOffset;
  const __int128 rightEnd = rightBegin + rightWidth;
  return leftBegin < rightEnd && rightBegin < leftEnd;
}

bool classifySelectStoreArm(
    Value *armPointer, unsigned storeWidth, const Value *loadBase,
    int64_t loadOffset, unsigned loadWidth, const DataLayout &layout,
    MemoryLocation loadLocation, MemoryLocation storeLocation,
    AAResults &aliasAnalysis,
    SmallVectorImpl<int> &sourceBytes) {
  sourceBytes.assign(loadWidth, -1);
  const Value *armBase = nullptr;
  int64_t armOffset = 0;
  if (!exactInBoundsAddress(
          armPointer, layout, armBase, armOffset))
    return false;
  if (armBase != loadBase)
    return aliasAnalysis.alias(
               loadLocation,
               storeLocation.getWithNewPtr(armPointer)) ==
           AliasResult::NoAlias;
  if (!byteIntervalsOverlap(
          loadOffset, loadWidth, armOffset, storeWidth))
    return true;

  const __int128 overlapBegin =
      std::max<__int128>(loadOffset, armOffset);
  const __int128 overlapEnd = std::min<__int128>(
      static_cast<__int128>(loadOffset) + loadWidth,
      static_cast<__int128>(armOffset) + storeWidth);
  for (__int128 address = overlapBegin; address < overlapEnd; ++address)
    sourceBytes[static_cast<unsigned>(address - loadOffset)] =
        static_cast<int>(address - armOffset);
  return true;
}

std::shared_ptr<PointerPartition> buildPointerPartition(
    StoreInst &store, unsigned storeWidth, const Value *loadBase,
    int64_t loadOffset, unsigned loadWidth, const DataLayout &layout,
    MemoryLocation loadLocation, MemoryLocation storeLocation,
    AAResults &aliasAnalysis, DominatorTree &dominators,
    Instruction *captureTerminator) {
  auto partition = std::make_shared<PointerPartition>();
  partition->store = &store;
  partition->storeWidth = storeWidth;
  SmallPtrSet<SelectInst *, 4> seenSelects;

  std::function<std::optional<PointerPartitionChild>(Value *, unsigned)>
      visit = [&](Value *pointer, unsigned depth)
      -> std::optional<PointerPartitionChild> {
    if (auto *select = dyn_cast<SelectInst>(pointer)) {
      if (depth >= 2 || partition->nodes.size() >= 3 ||
          !seenSelects.insert(select).second)
        return std::nullopt;
      auto *guard = dyn_cast<Instruction>(select->getCondition());
      if (guard == nullptr || !guard->getType()->isIntegerTy(1) ||
          !valueDominates(
              guard, captureTerminator, dominators))
        return std::nullopt;
      const unsigned nodeIndex = partition->nodes.size();
      partition->nodes.push_back({select, guard, {}, {}});
      auto trueChild = visit(select->getTrueValue(), depth + 1);
      auto falseChild = visit(select->getFalseValue(), depth + 1);
      if (!trueChild || !falseChild)
        return std::nullopt;
      partition->nodes[nodeIndex].trueChild = *trueChild;
      partition->nodes[nodeIndex].falseChild = *falseChild;
      return PointerPartitionChild{false, nodeIndex};
    }

    if (partition->leaves.size() >= 4)
      return std::nullopt;
    SmallVector<int, kMaxByteLaneWidth> sourceBytes;
    if (!classifySelectStoreArm(
            pointer, storeWidth, loadBase, loadOffset, loadWidth,
            layout, loadLocation, storeLocation, aliasAnalysis,
            sourceBytes))
      return std::nullopt;
    const unsigned leafIndex = partition->leaves.size();
    partition->leaves.push_back({std::move(sourceBytes)});
    return PointerPartitionChild{true, leafIndex};
  };

  auto root = visit(store.getPointerOperand(), 0);
  if (!root || root->leaf || root->index != 0 ||
      partition->nodes.size() < 2 ||
      partition->leaves.size() != partition->nodes.size() + 1)
    return nullptr;

  bool hasOverlap = false;
  for (unsigned lane = 0; lane < loadWidth; ++lane) {
    bool hasFallback = false;
    for (const PointerPartitionLeaf &leaf : partition->leaves) {
      if (leaf.sourceBytes[lane] < 0)
        hasFallback = true;
      else
        hasOverlap = true;
    }
    if (!hasFallback)
      return nullptr;
  }
  return hasOverlap ? partition : nullptr;
}

std::optional<uint64_t> constantHeapRegionExtent(Instruction *base) {
  auto *call = dyn_cast_or_null<CallBase>(base);
  Function *callee = call == nullptr ? nullptr : call->getCalledFunction();
  if (callee == nullptr || !callee->isDeclaration() ||
      !call->getType()->isPointerTy() ||
      call->getType()->getPointerAddressSpace() != 0)
    return std::nullopt;
  const unsigned sizeBits =
      base->getModule()->getDataLayout().getIndexSizeInBits(0);
  if (sizeBits == 0 || sizeBits > 64 || callee->isVarArg())
    return std::nullopt;
  const StringRef name = callee->getName();
  uint64_t extent = 0;
  if (name == "malloc") {
    auto *size =
        call->arg_size() == 1 && callee->arg_size() == 1 &&
                call->getArgOperand(0)->getType()->isIntegerTy(sizeBits)
            ? dyn_cast<ConstantInt>(call->getArgOperand(0))
            : nullptr;
    if (size == nullptr || size->getValue().getActiveBits() > 63)
      return std::nullopt;
    extent = size->getZExtValue();
  } else if (name == "calloc") {
    const bool validArguments =
        call->arg_size() == 2 && callee->arg_size() == 2 &&
        call->getArgOperand(0)->getType()->isIntegerTy(sizeBits) &&
        call->getArgOperand(1)->getType()->isIntegerTy(sizeBits) &&
        call->getArgOperand(0)->getType() ==
            call->getArgOperand(1)->getType();
    auto *count =
        validArguments
            ? dyn_cast<ConstantInt>(call->getArgOperand(0))
            : nullptr;
    auto *size =
        validArguments
            ? dyn_cast<ConstantInt>(call->getArgOperand(1))
            : nullptr;
    if (count == nullptr || size == nullptr ||
        count->getValue().getActiveBits() > 63 ||
        size->getValue().getActiveBits() > 63)
      return std::nullopt;
    const __int128 product =
        static_cast<__int128>(count->getZExtValue()) *
        size->getZExtValue();
    if (product > std::numeric_limits<uint64_t>::max())
      return std::nullopt;
    extent = static_cast<uint64_t>(product);
  } else {
    return std::nullopt;
  }
  if (extent == 0 || extent > (1ULL << 32))
    return std::nullopt;
  return extent;
}

std::optional<OrderedWriterLayer> buildSymbolicRegionWriter(
    StoreInst &store, unsigned storeWidth, const Value *loadBase,
    int64_t loadOffset, unsigned loadWidth, const DataLayout &layout,
    DominatorTree &dominators, Instruction *captureTerminator) {
  auto *gep = dyn_cast<GetElementPtrInst>(store.getPointerOperand());
  if (gep == nullptr || !gep->isInBounds() ||
      !gep->getSourceElementType()->isIntegerTy(8) ||
      gep->getNumIndices() != 1)
    return std::nullopt;
  Value *indexValue = *gep->idx_begin();
  auto *index = dyn_cast<Instruction>(indexValue);
  auto *indexType = dyn_cast<IntegerType>(indexValue->getType());
  if (index == nullptr || indexType == nullptr ||
      isa<ConstantInt, UndefValue, PoisonValue>(indexValue) ||
      indexType->getBitWidth() == 0 ||
      indexType->getBitWidth() > 64 ||
      indexType->getBitWidth() !=
          layout.getIndexSizeInBits(gep->getPointerAddressSpace()) ||
      !valueDominates(index, captureTerminator, dominators))
    return std::nullopt;

  const Value *regionBaseValue = nullptr;
  int64_t symbolicBaseOffset = 0;
  if (!exactInBoundsAddress(
          gep->getPointerOperand(), layout, regionBaseValue,
          symbolicBaseOffset) ||
      regionBaseValue != loadBase)
    return std::nullopt;
  auto *regionBase =
      dyn_cast<Instruction>(const_cast<Value *>(regionBaseValue));
  const std::optional<uint64_t> regionExtent =
      constantHeapRegionExtent(regionBase);
  if (regionBase == nullptr || !regionExtent ||
      symbolicBaseOffset < 0 ||
      static_cast<uint64_t>(symbolicBaseOffset) > *regionExtent ||
      loadOffset < 0 ||
      static_cast<__int128>(loadOffset) + loadWidth >
          *regionExtent)
    return std::nullopt;

  OrderedWriterLayer layer;
  layer.kind = OrderedWriterKind::SymbolicRegion;
  layer.store = &store;
  layer.storeWidth = storeWidth;
  layer.regionBase = regionBase;
  layer.regionExtent = *regionExtent;
  layer.symbolicIndex = index;
  layer.symbolicIndexBits = indexType->getBitWidth();
  layer.symbolicBaseOffset = symbolicBaseOffset;
  layer.symbolicLaneCases.resize(loadWidth);
  bool hasEffect = false;
  for (unsigned lane = 0; lane < loadWidth; ++lane) {
    for (unsigned sourceByte = 0; sourceByte < storeWidth;
         ++sourceByte) {
      const __int128 target =
          static_cast<__int128>(loadOffset) + lane -
          sourceByte - symbolicBaseOffset;
      if (target < std::numeric_limits<int64_t>::min() ||
          target > std::numeric_limits<int64_t>::max())
        continue;
      const int64_t signedTarget = static_cast<int64_t>(target);
      APInt targetValue(
          64, static_cast<uint64_t>(signedTarget), true);
      if (!targetValue.isSignedIntN(layer.symbolicIndexBits))
        continue;
      layer.symbolicLaneCases[lane].push_back(
          {signedTarget, sourceByte});
      hasEffect = true;
    }
  }
  return hasEffect ? std::optional<OrderedWriterLayer>(std::move(layer))
                   : std::nullopt;
}

bool findByteLaneState(MemoryAccess *access, MemoryAccess *liveOnEntry,
                       MemoryPhi *dispatchPhi, const LoadInst &load,
                       AAResults &aliasAnalysis,
                       DominatorTree &dominators, BasicBlock *point,
                       ReachingState &result) {
  const std::optional<unsigned> loadWidth =
      fixedIntegerByteWidth(load.getType());
  if (access == nullptr || !loadWidth || *loadWidth < 2)
    return false;

  const DataLayout &layout = load.getModule()->getDataLayout();
  const Value *loadBase = nullptr;
  int64_t loadOffset = 0;
  if (!exactInBoundsAddress(
          load.getPointerOperand(), layout, loadBase, loadOffset))
    return false;

  result.kind = MemoryStateKind::ByteComposition;
  result.point = point;
  result.byteWidth = *loadWidth;
  result.littleEndian = layout.isLittleEndian();
  result.byteLanes.resize(*loadWidth);
  SmallVector<bool, kMaxByteLaneWidth> assigned(*loadWidth, false);
  unsigned assignedCount = 0;
  unsigned definitions = 0;
  bool haveStoreSource = false;
  SmallVector<StoreInst *, 2> guardedStoresSeen;
  unsigned pointerPartitionsSeen = 0;
  MemoryLocation loadLocation = MemoryLocation::get(&load);
  Instruction *captureTerminator = point->getTerminator();

  while (access != liveOnEntry) {
    if (access == dispatchPhi || isa<MemoryPhi>(access) ||
        ++definitions > kMaxByteLaneDefinitions)
      return false;
    auto *definition = dyn_cast<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    auto *store = dyn_cast_or_null<StoreInst>(instruction);
    if (instruction == nullptr ||
        (store != nullptr && !store->isSimple()) ||
        isa<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(instruction))
      return false;

    bool consumedStore = false;
    if (store != nullptr) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      auto *pointerSelect =
          dyn_cast<SelectInst>(store->getPointerOperand());
      const bool nestedPointerSelect =
          pointerSelect != nullptr &&
          (isa<SelectInst>(pointerSelect->getTrueValue()) ||
           isa<SelectInst>(pointerSelect->getFalseValue()));
      if (storeWidth && nestedPointerSelect) {
        Value *storedValue = store->getValueOperand();
        if (result.writerLayers.size() >= kMaxOrderedWriterLayers ||
            pointerPartitionsSeen >= kMaxOrderedWriterLayers ||
            isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, captureTerminator, dominators) ||
            !dominators.dominates(store, captureTerminator))
          return false;
        std::shared_ptr<PointerPartition> partition =
            buildPointerPartition(
                *store, *storeWidth, loadBase, loadOffset,
                *loadWidth, layout, loadLocation,
                MemoryLocation::get(store), aliasAnalysis,
                dominators, captureTerminator);
        if (partition == nullptr)
          return false;
        bool hasVisibleEffect = false;
        for (unsigned lane = 0; lane < *loadWidth; ++lane) {
          if (assigned[lane])
            for (PointerPartitionLeaf &leaf : partition->leaves)
              leaf.sourceBytes[lane] = -1;
          else
            hasVisibleEffect |= std::any_of(
                partition->leaves.begin(), partition->leaves.end(),
                [&](const PointerPartitionLeaf &leaf) {
                  return leaf.sourceBytes[lane] >= 0;
                });
        }
        if (hasVisibleEffect) {
          if (result.pointerPartition == nullptr)
            result.pointerPartition = partition;
          OrderedWriterLayer layer;
          layer.ordinal =
              static_cast<unsigned>(result.writerLayers.size());
          layer.kind = OrderedWriterKind::PointerPartition;
          layer.store = store;
          layer.storeWidth = *storeWidth;
          layer.pointerPartition = std::move(partition);
          result.writerLayers.push_back(std::move(layer));
          ++pointerPartitionsSeen;
          haveStoreSource = true;
        }
        consumedStore = true;
      }
      if (storeWidth && pointerSelect != nullptr &&
          !nestedPointerSelect) {
        auto *guard =
            dyn_cast<Instruction>(pointerSelect->getCondition());
        SmallVector<int, kMaxByteLaneWidth> trueBytes;
        SmallVector<int, kMaxByteLaneWidth> falseBytes;
        Value *storedValue = store->getValueOperand();
        const MemoryLocation storeLocation =
            MemoryLocation::get(store);
        if (guard == nullptr ||
            !guard->getType()->isIntegerTy(1) ||
            isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                guard, captureTerminator, dominators) ||
            !valueDominates(
                storedValue, captureTerminator, dominators) ||
            !dominators.dominates(store, captureTerminator) ||
            !classifySelectStoreArm(
                pointerSelect->getTrueValue(), *storeWidth, loadBase,
                loadOffset, *loadWidth, layout, loadLocation,
                storeLocation, aliasAnalysis, trueBytes) ||
            !classifySelectStoreArm(
                pointerSelect->getFalseValue(), *storeWidth, loadBase,
                loadOffset, *loadWidth, layout, loadLocation,
                storeLocation, aliasAnalysis, falseBytes))
          return false;

        bool hasOverlap = false;
        bool hasConditionalEffect = false;
        SmallVector<int, kMaxByteLaneWidth> writerSourceBytes(
            *loadWidth, -1);
        SmallVector<int, kMaxByteLaneWidth> writerPolarities(
            *loadWidth, -1);
        for (unsigned lane = 0; lane < *loadWidth; ++lane) {
          const int trueByte = trueBytes[lane];
          const int falseByte = falseBytes[lane];
          if (trueByte < 0 && falseByte < 0)
            continue;
          hasOverlap = true;
          if (assigned[lane])
            continue;
          if (trueByte >= 0 && falseByte >= 0) {
            if (trueByte != falseByte)
              return false;
            ByteLaneSource &source = result.byteLanes[lane];
            source.ordinal = lane;
            source.store = store;
            source.sourceByte = static_cast<unsigned>(trueByte);
            source.sourceWidth = *storeWidth;
            assigned[lane] = true;
            ++assignedCount;
            haveStoreSource = true;
            continue;
          }
          ByteLaneSource &source = result.byteLanes[lane];
          auto knownStore = std::find(
              guardedStoresSeen.begin(),
              guardedStoresSeen.end(), store);
          if (knownStore == guardedStoresSeen.end()) {
            if (guardedStoresSeen.size() >=
                kMaxOrderedWriterLayers)
              return false;
            guardedStoresSeen.push_back(store);
            knownStore = std::prev(guardedStoresSeen.end());
          }
          if (source.guarded.size() >= 2)
            return false;
          source.ordinal = lane;
          source.guarded.push_back({
              static_cast<unsigned>(
                  std::distance(
                      guardedStoresSeen.begin(), knownStore)),
              store, guard, trueByte >= 0,
              static_cast<unsigned>(
                  trueByte >= 0 ? trueByte : falseByte),
              *storeWidth});
          writerSourceBytes[lane] =
              trueByte >= 0 ? trueByte : falseByte;
          writerPolarities[lane] = trueByte >= 0 ? 1 : 0;
          hasConditionalEffect = true;
          haveStoreSource = true;
        }
        if (hasConditionalEffect) {
          if (result.writerLayers.size() >=
              kMaxOrderedWriterLayers)
            return false;
          OrderedWriterLayer layer;
          layer.ordinal =
              static_cast<unsigned>(result.writerLayers.size());
          layer.kind = OrderedWriterKind::Guarded;
          layer.store = store;
          layer.storeWidth = *storeWidth;
          layer.guard = guard;
          layer.sourceBytes = std::move(writerSourceBytes);
          layer.storePolarities =
              std::move(writerPolarities);
          result.writerLayers.push_back(std::move(layer));
        }
        consumedStore = hasOverlap;
      }

      if (!consumedStore && storeWidth &&
          pointerSelect == nullptr &&
          isa<GetElementPtrInst>(store->getPointerOperand())) {
        Value *storedValue = store->getValueOperand();
        if (result.writerLayers.size() >= kMaxOrderedWriterLayers ||
            isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, captureTerminator, dominators) ||
            !dominators.dominates(store, captureTerminator))
          return false;
        std::optional<OrderedWriterLayer> symbolic =
            buildSymbolicRegionWriter(
                *store, *storeWidth, loadBase, loadOffset,
                *loadWidth, layout, dominators,
                captureTerminator);
        if (symbolic) {
          bool hasVisibleEffect = false;
          for (unsigned lane = 0; lane < *loadWidth; ++lane) {
            if (assigned[lane])
              symbolic->symbolicLaneCases[lane].clear();
            hasVisibleEffect |=
                !symbolic->symbolicLaneCases[lane].empty();
          }
          if (hasVisibleEffect) {
            symbolic->ordinal =
                static_cast<unsigned>(result.writerLayers.size());
            result.writerLayers.push_back(std::move(*symbolic));
            haveStoreSource = true;
          }
          consumedStore = true;
        }
      }

      const Value *storeBase = nullptr;
      int64_t storeOffset = 0;
      if (!consumedStore && pointerSelect == nullptr && storeWidth &&
          exactInBoundsAddress(
              store->getPointerOperand(), layout, storeBase,
              storeOffset) &&
          storeBase == loadBase &&
          byteIntervalsOverlap(
              loadOffset, *loadWidth, storeOffset, *storeWidth)) {
        Value *storedValue = store->getValueOperand();
        if (isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, captureTerminator, dominators) ||
            !dominators.dominates(store, captureTerminator) ||
            aliasAnalysis.alias(
                loadLocation, MemoryLocation::get(store)) ==
                AliasResult::NoAlias)
          return false;

        const __int128 overlapBegin =
            std::max<__int128>(loadOffset, storeOffset);
        const __int128 overlapEnd = std::min<__int128>(
            static_cast<__int128>(loadOffset) + *loadWidth,
            static_cast<__int128>(storeOffset) + *storeWidth);
        for (__int128 address = overlapBegin; address < overlapEnd;
             ++address) {
          const unsigned lane =
              static_cast<unsigned>(address - loadOffset);
          if (assigned[lane])
            continue;
          ByteLaneSource &source = result.byteLanes[lane];
          source.ordinal = lane;
          source.store = store;
          source.sourceByte =
              static_cast<unsigned>(address - storeOffset);
          source.sourceWidth = *storeWidth;
          assigned[lane] = true;
          ++assignedCount;
          haveStoreSource = true;
        }
        consumedStore = true;
      }
    }

    if (!consumedStore) {
      if (isModSet(
              aliasAnalysis.getModRefInfo(instruction, loadLocation)) ||
          result.skipped.size() >= kMaxMemoryDefChain)
        return false;
      result.skipped.push_back(instruction);
    }
    if (assignedCount == *loadWidth)
      return true;
    access = definition->getDefiningAccess();
  }

  if (access != liveOnEntry ||
      !valueDominates(
          load.getPointerOperand(), captureTerminator, dominators))
    return false;
  for (unsigned lane = 0; lane < *loadWidth; ++lane) {
    if (assigned[lane])
      continue;
    ByteLaneSource &source = result.byteLanes[lane];
    source.ordinal = lane;
    source.store = nullptr;
    source.sourceByte = lane;
    source.sourceWidth = *loadWidth;
    assigned[lane] = true;
  }
  // A pure live-on-entry composition is not a useful continuation tuple.
  return haveStoreSource;
}

bool findCyclicPredicateLeafState(
    MemoryAccess *access, MemoryAccess *liveOnEntry,
    MemoryPhi *dispatchPhi, MemoryPhi *cyclePhi,
    const LoadInst &load, AAResults &aliasAnalysis,
    DominatorTree &dominators, BasicBlock *leafBlock,
    CyclicPredicateLeaf &result, bool &hasStore) {
  const std::optional<unsigned> loadWidth =
      fixedIntegerByteWidth(load.getType());
  if (access == nullptr || !loadWidth || *loadWidth < 2 ||
      leafBlock == nullptr)
    return false;
  const DataLayout &layout = load.getModule()->getDataLayout();
  const Value *loadBase = nullptr;
  int64_t loadOffset = 0;
  if (!exactInBoundsAddress(
          load.getPointerOperand(), layout, loadBase, loadOffset))
    return false;
  const MemoryLocation loadLocation = MemoryLocation::get(&load);

  result.block = leafBlock;
  result.byteLanes.resize(*loadWidth);
  if (access == cyclePhi) {
    for (unsigned lane = 0; lane < *loadWidth; ++lane) {
      ByteLaneSource &source = result.byteLanes[lane];
      source.ordinal = lane;
      source.sourceByte = lane;
      source.sourceWidth = *loadWidth;
      source.carry = true;
    }
    hasStore = false;
    return true;
  }

  SmallVector<bool, kMaxByteLaneWidth> assigned(*loadWidth, false);
  unsigned assignedCount = 0;
  unsigned definitions = 0;
  hasStore = false;
  Instruction *leafTerminator = leafBlock->getTerminator();
  while (access != cyclePhi) {
    if (access == nullptr || access == liveOnEntry ||
        access == dispatchPhi || isa<MemoryPhi>(access) ||
        ++definitions > kMaxByteLaneDefinitions)
      return false;
    auto *definition = dyn_cast<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    auto *store = dyn_cast_or_null<StoreInst>(instruction);
    if (instruction == nullptr ||
        (store != nullptr && !store->isSimple()) ||
        isa<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(instruction))
      return false;

    bool consumedStore = false;
    if (store != nullptr &&
        !isa<SelectInst>(store->getPointerOperand())) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      const Value *storeBase = nullptr;
      int64_t storeOffset = 0;
      if (storeWidth &&
          exactInBoundsAddress(
              store->getPointerOperand(), layout,
              storeBase, storeOffset) &&
          storeBase == loadBase &&
          byteIntervalsOverlap(
              loadOffset, *loadWidth, storeOffset, *storeWidth)) {
        Value *storedValue = store->getValueOperand();
        if (isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, leafTerminator, dominators) ||
            !dominators.dominates(store, leafTerminator) ||
            aliasAnalysis.alias(
                loadLocation, MemoryLocation::get(store)) ==
                AliasResult::NoAlias)
          return false;
        const __int128 overlapBegin =
            std::max<__int128>(loadOffset, storeOffset);
        const __int128 overlapEnd = std::min<__int128>(
            static_cast<__int128>(loadOffset) + *loadWidth,
            static_cast<__int128>(storeOffset) + *storeWidth);
        for (__int128 address = overlapBegin;
             address < overlapEnd; ++address) {
          const unsigned lane =
              static_cast<unsigned>(address - loadOffset);
          if (assigned[lane])
            continue;
          ByteLaneSource &source = result.byteLanes[lane];
          source.ordinal = lane;
          source.store = store;
          source.sourceByte =
              static_cast<unsigned>(address - storeOffset);
          source.sourceWidth = *storeWidth;
          assigned[lane] = true;
          ++assignedCount;
        }
        consumedStore = true;
        hasStore = true;
      }
    }
    if (!consumedStore) {
      if (isModSet(
              aliasAnalysis.getModRefInfo(
                  instruction, loadLocation)) ||
          result.skipped.size() >= kMaxMemoryDefChain)
        return false;
      result.skipped.push_back(instruction);
    }
    access = definition->getDefiningAccess();
  }

  for (unsigned lane = 0; lane < *loadWidth; ++lane) {
    if (assigned[lane])
      continue;
    ByteLaneSource &source = result.byteLanes[lane];
    source.ordinal = lane;
    source.sourceByte = lane;
    source.sourceWidth = *loadWidth;
    source.carry = true;
  }
  return hasStore && assignedCount != 0 &&
         assignedCount != *loadWidth;
}

bool findNestedPredicateCyclicTransfer(
    MemoryPhi &transferPhi, MemoryAccess *liveOnEntry,
    MemoryPhi *dispatchPhi, MemoryPhi *cyclePhi,
    const LoadInst &load, AAResults &aliasAnalysis,
    DominatorTree &dominators, BasicBlock *backedgeBlock,
    CyclicBackedgeTransfer &result) {
  if (&transferPhi == dispatchPhi ||
      transferPhi.getBlock() != backedgeBlock ||
      transferPhi.getNumIncomingValues() < 3 ||
      transferPhi.getNumIncomingValues() >
          kMaxCyclicPredicateLeaves ||
      pred_size(backedgeBlock) !=
          transferPhi.getNumIncomingValues())
    return false;

  result.block = backedgeBlock;
  DenseMap<BasicBlock *, unsigned> leafOrdinals;
  bool haveStoreLeaf = false;
  BasicBlock *root = nullptr;
  for (unsigned index = 0;
       index < transferPhi.getNumIncomingValues(); ++index) {
    BasicBlock *leafBlock = transferPhi.getIncomingBlock(index);
    auto *leafBranch =
        dyn_cast<BranchInst>(leafBlock->getTerminator());
    if (leafBlock == backedgeBlock ||
        !leafOrdinals.try_emplace(leafBlock, index).second ||
        leafBranch == nullptr || !leafBranch->isUnconditional() ||
        leafBranch->getSuccessor(0) != backedgeBlock ||
        pred_size(leafBlock) != 1 ||
        !dominators.dominates(cyclePhi->getBlock(), leafBlock))
      return false;
    CyclicPredicateLeaf leaf;
    bool leafHasStore = false;
    if (!findCyclicPredicateLeafState(
            transferPhi.getIncomingValue(index), liveOnEntry,
            dispatchPhi, cyclePhi, load, aliasAnalysis,
            dominators, leafBlock, leaf, leafHasStore))
      return false;
    haveStoreLeaf |= leafHasStore;
    result.predicateLeaves.push_back(std::move(leaf));
    root = root == nullptr
               ? leafBlock
               : dominators.findNearestCommonDominator(
                     root, leafBlock);
  }
  if (!haveStoreLeaf || root == nullptr ||
      root == cyclePhi->getBlock() || root == backedgeBlock ||
      leafOrdinals.count(root) != 0)
    return false;

  SmallPtrSet<BasicBlock *, 8> visitedBlocks;
  SmallVector<bool, kMaxCyclicPredicateLeaves> visitedLeaves(
      result.predicateLeaves.size(), false);
  bool valid = true;
  std::function<CyclicPredicateChild(BasicBlock *)> collect =
      [&](BasicBlock *block) -> CyclicPredicateChild {
    auto leaf = leafOrdinals.find(block);
    if (leaf != leafOrdinals.end()) {
      if (!visitedBlocks.insert(block).second)
        valid = false;
      visitedLeaves[leaf->second] = true;
      return {true, leaf->second};
    }
    if (!valid || block == cyclePhi->getBlock() ||
        block == backedgeBlock ||
        result.predicateNodes.size() >=
            kMaxCyclicPredicateNodes ||
        !visitedBlocks.insert(block).second ||
        !dominators.dominates(cyclePhi->getBlock(), block)) {
      valid = false;
      return {};
    }
    auto *branch =
        dyn_cast<BranchInst>(block->getTerminator());
    auto *guard =
        branch == nullptr || !branch->isConditional()
            ? nullptr
            : dyn_cast<Instruction>(branch->getCondition());
    if (guard == nullptr || !guard->getType()->isIntegerTy(1) ||
        branch->getSuccessor(0) == branch->getSuccessor(1) ||
        branch->getSuccessor(0)->getSinglePredecessor() != block ||
        branch->getSuccessor(1)->getSinglePredecessor() != block) {
      valid = false;
      return {};
    }
    const unsigned ordinal = result.predicateNodes.size();
    result.predicateNodes.push_back(
        {block, guard, {}, {}});
    CyclicPredicateChild trueChild =
        collect(branch->getSuccessor(0));
    CyclicPredicateChild falseChild =
        collect(branch->getSuccessor(1));
    result.predicateNodes[ordinal].trueChild = trueChild;
    result.predicateNodes[ordinal].falseChild = falseChild;
    return {false, ordinal};
  };
  CyclicPredicateChild treeRoot = collect(root);
  if (!valid || treeRoot.leaf || treeRoot.index != 0 ||
      result.predicateNodes.size() + 1 !=
          result.predicateLeaves.size() ||
      std::any_of(
          visitedLeaves.begin(), visitedLeaves.end(),
          [](bool visited) { return !visited; }))
    return false;
  return true;
}

bool findMultiLatchCyclicTransfer(
    MemoryAccess *access, MemoryAccess *liveOnEntry,
    MemoryPhi *dispatchPhi, MemoryPhi *cyclePhi,
    const LoadInst &load, AAResults &aliasAnalysis,
    DominatorTree &dominators, BasicBlock *backedgeBlock,
    CyclicBackedgeTransfer &result) {
  const std::optional<unsigned> loadWidth =
      fixedIntegerByteWidth(load.getType());
  if (access == nullptr || !loadWidth || *loadWidth < 2 ||
      backedgeBlock == nullptr)
    return false;
  const DataLayout &layout = load.getModule()->getDataLayout();
  const Value *loadBase = nullptr;
  int64_t loadOffset = 0;
  if (!exactInBoundsAddress(
          load.getPointerOperand(), layout, loadBase, loadOffset))
    return false;
  const MemoryLocation loadLocation = MemoryLocation::get(&load);

  result.block = backedgeBlock;
  if (auto *transferPhi = dyn_cast<MemoryPhi>(access)) {
    if (transferPhi->getNumIncomingValues() >= 3)
      return findNestedPredicateCyclicTransfer(
          *transferPhi, liveOnEntry, dispatchPhi, cyclePhi,
          load, aliasAnalysis, dominators, backedgeBlock,
          result);
    if (transferPhi == dispatchPhi ||
        transferPhi->getBlock() != backedgeBlock ||
        transferPhi->getNumIncomingValues() != 2 ||
        pred_size(backedgeBlock) != 2)
      return false;

    BasicBlock *storeArm = nullptr;
    BasicBlock *carryArm = nullptr;
    MemoryAccess *storeAccess = nullptr;
    for (unsigned index = 0;
         index < transferPhi->getNumIncomingValues(); ++index) {
      BasicBlock *arm = transferPhi->getIncomingBlock(index);
      MemoryAccess *incoming = transferPhi->getIncomingValue(index);
      auto *armBranch = dyn_cast<BranchInst>(arm->getTerminator());
      if (armBranch == nullptr || !armBranch->isUnconditional() ||
          armBranch->getSuccessor(0) != backedgeBlock ||
          pred_size(arm) != 1 ||
          !dominators.dominates(cyclePhi->getBlock(), arm))
        return false;
      if (incoming == cyclePhi) {
        if (carryArm != nullptr)
          return false;
        carryArm = arm;
      } else {
        if (storeArm != nullptr)
          return false;
        storeArm = arm;
        storeAccess = incoming;
      }
    }
    if (storeArm == nullptr || carryArm == nullptr ||
        storeAccess == nullptr)
      return false;

    BasicBlock *branchBlock = *pred_begin(storeArm);
    if (*pred_begin(carryArm) != branchBlock)
      return false;
    auto *branch =
        dyn_cast<BranchInst>(branchBlock->getTerminator());
    auto *guard =
        branch == nullptr || !branch->isConditional()
            ? nullptr
            : dyn_cast<Instruction>(branch->getCondition());
    SmallPtrSet<BasicBlock *, 5> topologyBlocks;
    topologyBlocks.insert(cyclePhi->getBlock());
    topologyBlocks.insert(backedgeBlock);
    topologyBlocks.insert(branchBlock);
    topologyBlocks.insert(storeArm);
    topologyBlocks.insert(carryArm);
    if (guard == nullptr || !guard->getType()->isIntegerTy(1) ||
        topologyBlocks.size() != 5 ||
        !dominators.dominates(cyclePhi->getBlock(), branchBlock) ||
        !dominators.dominates(branchBlock, storeArm) ||
        !dominators.dominates(branchBlock, carryArm) ||
        !valueDominates(
            guard, backedgeBlock->getTerminator(), dominators))
      return false;
    const bool storeWhenTrue =
        branch->getSuccessor(0) == storeArm &&
        branch->getSuccessor(1) == carryArm;
    const bool storeWhenFalse =
        branch->getSuccessor(1) == storeArm &&
        branch->getSuccessor(0) == carryArm;
    if (!storeWhenTrue && !storeWhenFalse)
      return false;

    result.branch = branchBlock;
    result.storeArm = storeArm;
    result.carryArm = carryArm;
    result.guard = guard;
    result.storeWhenTrue = storeWhenTrue;
    access = storeAccess;
  }
  result.byteLanes.resize(*loadWidth);
  SmallVector<bool, kMaxByteLaneWidth> assigned(*loadWidth, false);
  unsigned assignedCount = 0;
  unsigned definitions = 0;
  bool haveStore = false;
  std::shared_ptr<OrderedWriterLayer> symbolicRegion;
  Instruction *backedgeTerminator = backedgeBlock->getTerminator();
  while (access != cyclePhi) {
    if (access == nullptr || access == liveOnEntry ||
        access == dispatchPhi || isa<MemoryPhi>(access) ||
        ++definitions > kMaxByteLaneDefinitions)
      return false;
    auto *definition = dyn_cast<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    auto *store = dyn_cast_or_null<StoreInst>(instruction);
    if (instruction == nullptr ||
        (store != nullptr && !store->isSimple()) ||
        isa<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(instruction))
      return false;

    bool consumedStore = false;
    if (store != nullptr &&
        !isa<SelectInst>(store->getPointerOperand())) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      const Value *storeBase = nullptr;
      int64_t storeOffset = 0;
      if (storeWidth &&
          exactInBoundsAddress(
              store->getPointerOperand(), layout,
              storeBase, storeOffset) &&
          storeBase == loadBase &&
          byteIntervalsOverlap(
              loadOffset, *loadWidth, storeOffset, *storeWidth)) {
        Value *storedValue = store->getValueOperand();
        if (isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, backedgeTerminator, dominators) ||
            !(result.guard != nullptr
                  ? dominators.dominates(
                        store,
                        result.storeArm->getTerminator())
                  : dominators.dominates(
                        store, backedgeTerminator)) ||
            aliasAnalysis.alias(
                loadLocation, MemoryLocation::get(store)) ==
                AliasResult::NoAlias)
          return false;
        const __int128 overlapBegin =
            std::max<__int128>(loadOffset, storeOffset);
        const __int128 overlapEnd = std::min<__int128>(
            static_cast<__int128>(loadOffset) + *loadWidth,
            static_cast<__int128>(storeOffset) + *storeWidth);
        for (__int128 address = overlapBegin;
             address < overlapEnd; ++address) {
          const unsigned lane =
              static_cast<unsigned>(address - loadOffset);
          if (assigned[lane])
            continue;
          ByteLaneSource &source = result.byteLanes[lane];
          source.ordinal = lane;
          source.store = store;
          source.sourceByte =
              static_cast<unsigned>(address - storeOffset);
          source.sourceWidth = *storeWidth;
          assigned[lane] = true;
          ++assignedCount;
        }
        consumedStore = true;
        haveStore = true;
      }
    }
    if (!consumedStore && store != nullptr &&
        symbolicRegion == nullptr) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      std::optional<OrderedWriterLayer> layer =
          storeWidth
              ? buildSymbolicRegionWriter(
                    *store, *storeWidth, loadBase, loadOffset,
                    *loadWidth, layout, dominators,
                    backedgeTerminator)
              : std::nullopt;
      Value *storedValue = store->getValueOperand();
      const bool storeDominatesTransfer =
          result.guard != nullptr
              ? dominators.dominates(
                    store, result.storeArm->getTerminator())
              : dominators.dominates(store, backedgeTerminator);
      if (layer &&
          !isa<UndefValue, PoisonValue>(storedValue) &&
          valueDominates(
              storedValue, backedgeTerminator, dominators) &&
          storeDominatesTransfer) {
        layer->ordinal = 0;
        symbolicRegion =
            std::make_shared<OrderedWriterLayer>(std::move(*layer));
        consumedStore = true;
        haveStore = true;
      }
    }
    if (!consumedStore) {
      if (isModSet(
              aliasAnalysis.getModRefInfo(
                  instruction, loadLocation)) ||
          result.skipped.size() >= kMaxMemoryDefChain)
        return false;
      result.skipped.push_back(instruction);
    }
    access = definition->getDefiningAccess();
  }

  bool haveCarry = false;
  for (unsigned lane = 0; lane < *loadWidth; ++lane) {
    if (assigned[lane])
      continue;
    ByteLaneSource &source = result.byteLanes[lane];
    source.ordinal = lane;
    source.sourceByte = lane;
    source.sourceWidth = *loadWidth;
    source.carry = true;
    haveCarry = true;
  }
  if (symbolicRegion != nullptr) {
    if (!haveStore || !haveCarry || assignedCount != 0)
      return false;
    result.symbolicRegion = std::move(symbolicRegion);
    return true;
  }
  return haveStore && haveCarry && assignedCount != 0 &&
         assignedCount != *loadWidth;
}

bool findCyclicByteLaneState(
    MemoryAccess *access, MemoryAccess *liveOnEntry,
    MemoryPhi *dispatchPhi, const LoadInst &load,
    AAResults &aliasAnalysis, DominatorTree &dominators,
    BasicBlock *point, ReachingState &result) {
  const std::optional<unsigned> loadWidth =
      fixedIntegerByteWidth(load.getType());
  if (access == nullptr || !loadWidth || *loadWidth < 2)
    return false;

  const DataLayout &layout = load.getModule()->getDataLayout();
  const Value *loadBase = nullptr;
  int64_t loadOffset = 0;
  if (!exactInBoundsAddress(
          load.getPointerOperand(), layout, loadBase, loadOffset))
    return false;
  const MemoryLocation loadLocation = MemoryLocation::get(&load);

  result.kind = MemoryStateKind::ByteComposition;
  result.point = point;
  result.byteWidth = *loadWidth;
  result.littleEndian = layout.isLittleEndian();

  unsigned prefixDefinitions = 0;
  while (!isa<MemoryPhi>(access)) {
    if (access == liveOnEntry || access == dispatchPhi ||
        ++prefixDefinitions > kMaxByteLaneDefinitions)
      return false;
    auto *definition = dyn_cast<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    if (instruction == nullptr ||
        isModSet(aliasAnalysis.getModRefInfo(
            instruction, loadLocation)) ||
        result.skipped.size() >= kMaxMemoryDefChain)
      return false;
    result.skipped.push_back(instruction);
    access = definition->getDefiningAccess();
  }

  auto *cyclePhi = cast<MemoryPhi>(access);
  BasicBlock *header = cyclePhi->getBlock();
  if (cyclePhi == dispatchPhi ||
      cyclePhi->getNumIncomingValues() < 2 ||
      cyclePhi->getNumIncomingValues() >
          kMaxCyclicLatches + 1 ||
      pred_size(header) != cyclePhi->getNumIncomingValues() ||
      !dominators.dominates(header, point))
    return false;

  if (cyclePhi->getNumIncomingValues() >= 3) {
    BasicBlock *entryBlock = nullptr;
    MemoryAccess *entryAccess = nullptr;
    SmallVector<
        std::pair<BasicBlock *, MemoryAccess *>,
        kMaxCyclicLatches>
        backedges;
    SmallPtrSet<BasicBlock *, 8> incomingBlocks;
    for (unsigned index = 0;
         index < cyclePhi->getNumIncomingValues(); ++index) {
      BasicBlock *block = cyclePhi->getIncomingBlock(index);
      MemoryAccess *incoming =
          cyclePhi->getIncomingValue(index);
      auto *branch =
          dyn_cast<BranchInst>(block->getTerminator());
      if (!incomingBlocks.insert(block).second ||
          branch == nullptr || !branch->isUnconditional() ||
          branch->getSuccessor(0) != header)
        return false;
      if (dominators.dominates(header, block)) {
        backedges.push_back({block, incoming});
      } else {
        if (entryBlock != nullptr)
          return false;
        entryBlock = block;
        entryAccess = incoming;
      }
    }
    if (entryBlock == nullptr || entryAccess == nullptr ||
        backedges.size() < 2 ||
        backedges.size() > kMaxCyclicLatches ||
        backedges.size() + 1 !=
            cyclePhi->getNumIncomingValues() ||
        dominators.dominates(header, entryBlock))
      return false;
    for (const auto &backedge : backedges)
      if (!dominators.dominates(header, backedge.first))
        return false;

    auto entryState = std::make_shared<ReachingState>();
    if (!findByteLaneState(
            entryAccess, liveOnEntry, dispatchPhi, load,
            aliasAnalysis, dominators, entryBlock, *entryState) ||
        entryState->cycleEntryState != nullptr ||
        std::any_of(
            entryState->byteLanes.begin(),
            entryState->byteLanes.end(),
            [](const ByteLaneSource &lane) {
              return lane.hasGuardedSource() || lane.carry;
            }))
      return false;

    for (const auto &backedge : backedges) {
      CyclicBackedgeTransfer transfer;
      if (!findMultiLatchCyclicTransfer(
              backedge.second, liveOnEntry, dispatchPhi,
              cyclePhi, load, aliasAnalysis, dominators,
              backedge.first, transfer))
        return false;
      result.cycleTransfers.push_back(std::move(transfer));
    }
    result.memoryPhi = cyclePhi;
    result.cycleHeader = header;
    result.cycleEntry = entryBlock;
    result.cycleEntryState = std::move(entryState);
    return true;
  }

  BasicBlock *entryBlock = nullptr;
  BasicBlock *backedgeBlock = nullptr;
  MemoryAccess *entryAccess = nullptr;
  MemoryAccess *backedgeAccess = nullptr;
  for (unsigned index = 0;
       index < cyclePhi->getNumIncomingValues(); ++index) {
    BasicBlock *block = cyclePhi->getIncomingBlock(index);
    auto *branch = dyn_cast<BranchInst>(block->getTerminator());
    if (branch == nullptr || !branch->isUnconditional() ||
        branch->getSuccessor(0) != header)
      return false;
    if (dominators.dominates(header, block)) {
      if (backedgeBlock != nullptr)
        return false;
      backedgeBlock = block;
      backedgeAccess = cyclePhi->getIncomingValue(index);
    } else {
      if (entryBlock != nullptr)
        return false;
      entryBlock = block;
      entryAccess = cyclePhi->getIncomingValue(index);
    }
  }
  if (entryBlock == nullptr || backedgeBlock == nullptr ||
      entryAccess == nullptr || backedgeAccess == nullptr ||
      !dominators.dominates(header, backedgeBlock) ||
      dominators.dominates(header, entryBlock))
    return false;

  if (auto *transferPhi = dyn_cast<MemoryPhi>(backedgeAccess);
      transferPhi != nullptr &&
      transferPhi->getNumIncomingValues() >= 3) {
    auto entryState = std::make_shared<ReachingState>();
    CyclicBackedgeTransfer transfer;
    if (!findByteLaneState(
            entryAccess, liveOnEntry, dispatchPhi, load,
            aliasAnalysis, dominators, entryBlock, *entryState) ||
        entryState->cycleEntryState != nullptr ||
        std::any_of(
            entryState->byteLanes.begin(),
            entryState->byteLanes.end(),
            [](const ByteLaneSource &lane) {
              return lane.hasGuardedSource() || lane.carry;
            }) ||
        !findNestedPredicateCyclicTransfer(
            *transferPhi, liveOnEntry, dispatchPhi, cyclePhi,
            load, aliasAnalysis, dominators, backedgeBlock,
            transfer))
      return false;
    result.memoryPhi = cyclePhi;
    result.cycleHeader = header;
    result.cycleEntry = entryBlock;
    result.cycleEntryState = std::move(entryState);
    result.cycleTransfers.push_back(std::move(transfer));
    return true;
  }

  MemoryAccess *transferAccess = backedgeAccess;
  if (auto *transferPhi = dyn_cast<MemoryPhi>(backedgeAccess)) {
    if (transferPhi == dispatchPhi ||
        transferPhi->getBlock() != backedgeBlock ||
        transferPhi->getNumIncomingValues() != 2 ||
        pred_size(backedgeBlock) != 2)
      return false;

    BasicBlock *storeArm = nullptr;
    BasicBlock *carryArm = nullptr;
    MemoryAccess *storeAccess = nullptr;
    for (unsigned index = 0;
         index < transferPhi->getNumIncomingValues(); ++index) {
      BasicBlock *arm = transferPhi->getIncomingBlock(index);
      MemoryAccess *incoming = transferPhi->getIncomingValue(index);
      auto *armBranch = dyn_cast<BranchInst>(arm->getTerminator());
      if (armBranch == nullptr || !armBranch->isUnconditional() ||
          armBranch->getSuccessor(0) != backedgeBlock ||
          pred_size(arm) != 1 || !dominators.dominates(header, arm))
        return false;
      if (incoming == cyclePhi) {
        if (carryArm != nullptr)
          return false;
        carryArm = arm;
      } else {
        if (storeArm != nullptr)
          return false;
        storeArm = arm;
        storeAccess = incoming;
      }
    }
    if (storeArm == nullptr || carryArm == nullptr ||
        storeAccess == nullptr)
      return false;

    BasicBlock *branchBlock = *pred_begin(storeArm);
    if (*pred_begin(carryArm) != branchBlock)
      return false;
    auto *branch =
        dyn_cast<BranchInst>(branchBlock->getTerminator());
    if (branch == nullptr || !branch->isConditional())
      return false;
    auto *guard =
        dyn_cast<Instruction>(branch->getCondition());
    SmallPtrSet<BasicBlock *, 6> topologyBlocks;
    topologyBlocks.insert(header);
    topologyBlocks.insert(entryBlock);
    topologyBlocks.insert(backedgeBlock);
    topologyBlocks.insert(branchBlock);
    topologyBlocks.insert(storeArm);
    topologyBlocks.insert(carryArm);
    if (guard == nullptr || !guard->getType()->isIntegerTy(1) ||
        topologyBlocks.size() != 6 ||
        pred_size(storeArm) != 1 || pred_size(carryArm) != 1 ||
        !dominators.dominates(header, branchBlock) ||
        !dominators.dominates(branchBlock, storeArm) ||
        !dominators.dominates(branchBlock, carryArm) ||
        !valueDominates(
            guard, backedgeBlock->getTerminator(), dominators))
      return false;
    const bool storeWhenTrue =
        branch->getSuccessor(0) == storeArm &&
        branch->getSuccessor(1) == carryArm;
    const bool storeWhenFalse =
        branch->getSuccessor(1) == storeArm &&
        branch->getSuccessor(0) == carryArm;
    if (!storeWhenTrue && !storeWhenFalse)
      return false;

    result.cycleBranch = branchBlock;
    result.cycleStoreArm = storeArm;
    result.cycleCarryArm = carryArm;
    result.cycleGuard = guard;
    result.cycleStoreWhenTrue = storeWhenTrue;
    transferAccess = storeAccess;
  }

  auto entryState = std::make_shared<ReachingState>();
  if (!findByteLaneState(
          entryAccess, liveOnEntry, dispatchPhi, load,
          aliasAnalysis, dominators, entryBlock, *entryState) ||
      entryState->cycleEntryState != nullptr ||
      std::any_of(
          entryState->byteLanes.begin(),
          entryState->byteLanes.end(),
          [](const ByteLaneSource &lane) {
            return lane.hasGuardedSource() || lane.carry;
          }))
    return false;

  result.byteLanes.resize(*loadWidth);
  SmallVector<bool, kMaxByteLaneWidth> assigned(*loadWidth, false);
  unsigned assignedCount = 0;
  unsigned definitions = 0;
  bool haveStore = false;
  SmallVector<
      OrderedWriterLayer, kMaxCyclicSymbolicRegionWriters>
      symbolicRegions;
  Instruction *backedgeTerminator = backedgeBlock->getTerminator();
  access = transferAccess;
  while (access != cyclePhi) {
    if (access == nullptr || access == liveOnEntry ||
        access == dispatchPhi || isa<MemoryPhi>(access) ||
        ++definitions > kMaxByteLaneDefinitions)
      return false;
    auto *definition = dyn_cast<MemoryDef>(access);
    if (definition == nullptr)
      return false;
    Instruction *instruction = definition->getMemoryInst();
    auto *store = dyn_cast_or_null<StoreInst>(instruction);
    if (instruction == nullptr ||
        (store != nullptr && !store->isSimple()) ||
        isa<AtomicRMWInst, AtomicCmpXchgInst, FenceInst>(instruction))
      return false;

    bool consumedStore = false;
    if (store != nullptr &&
        !isa<SelectInst>(store->getPointerOperand())) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      const Value *storeBase = nullptr;
      int64_t storeOffset = 0;
      if (storeWidth &&
          exactInBoundsAddress(
              store->getPointerOperand(), layout,
              storeBase, storeOffset) &&
          storeBase == loadBase &&
          byteIntervalsOverlap(
              loadOffset, *loadWidth, storeOffset, *storeWidth)) {
        Value *storedValue = store->getValueOperand();
        if (isa<UndefValue, PoisonValue>(storedValue) ||
            !valueDominates(
                storedValue, backedgeTerminator, dominators) ||
            !(result.cycleGuard != nullptr
                  ? dominators.dominates(
                        store,
                        result.cycleStoreArm->getTerminator())
                  : dominators.dominates(
                        store, backedgeTerminator)) ||
            aliasAnalysis.alias(
                loadLocation, MemoryLocation::get(store)) ==
                AliasResult::NoAlias)
          return false;
        const __int128 overlapBegin =
            std::max<__int128>(loadOffset, storeOffset);
        const __int128 overlapEnd = std::min<__int128>(
            static_cast<__int128>(loadOffset) + *loadWidth,
            static_cast<__int128>(storeOffset) + *storeWidth);
        for (__int128 address = overlapBegin;
             address < overlapEnd; ++address) {
          const unsigned lane =
              static_cast<unsigned>(address - loadOffset);
          if (assigned[lane])
            continue;
          ByteLaneSource &source = result.byteLanes[lane];
          source.ordinal = lane;
          source.store = store;
          source.sourceByte =
              static_cast<unsigned>(address - storeOffset);
          source.sourceWidth = *storeWidth;
          assigned[lane] = true;
          ++assignedCount;
        }
        consumedStore = true;
        haveStore = true;
      }
    }
    if (!consumedStore && store != nullptr &&
        result.cycleGuard == nullptr &&
        symbolicRegions.size() <
            kMaxCyclicSymbolicRegionWriters) {
      const std::optional<unsigned> storeWidth =
          fixedIntegerByteWidth(store->getValueOperand()->getType());
      std::optional<OrderedWriterLayer> layer =
          storeWidth
              ? buildSymbolicRegionWriter(
                    *store, *storeWidth, loadBase, loadOffset,
                    *loadWidth, layout, dominators,
                    backedgeTerminator)
              : std::nullopt;
      Value *storedValue = store->getValueOperand();
      if (layer &&
          !isa<UndefValue, PoisonValue>(storedValue) &&
          valueDominates(
              storedValue, backedgeTerminator, dominators) &&
          dominators.dominates(store, backedgeTerminator) &&
          aliasAnalysis.alias(
              loadLocation, MemoryLocation::get(store)) !=
              AliasResult::NoAlias &&
          (symbolicRegions.empty() ||
           (layer->regionBase ==
                symbolicRegions.front().regionBase &&
            layer->regionExtent ==
                symbolicRegions.front().regionExtent))) {
        layer->ordinal =
            static_cast<unsigned>(symbolicRegions.size());
        symbolicRegions.push_back(std::move(*layer));
        consumedStore = true;
        haveStore = true;
      }
    }
    if (!consumedStore) {
      if (isModSet(
              aliasAnalysis.getModRefInfo(
                  instruction, loadLocation)) ||
          result.cycleSkipped.size() >= kMaxMemoryDefChain)
        return false;
      result.cycleSkipped.push_back(instruction);
    }
    access = definition->getDefiningAccess();
  }

  bool haveCarry = false;
  for (unsigned lane = 0; lane < *loadWidth; ++lane) {
    if (assigned[lane])
      continue;
    ByteLaneSource &source = result.byteLanes[lane];
    source.ordinal = lane;
    source.sourceByte = lane;
    source.sourceWidth = *loadWidth;
    source.carry = true;
    haveCarry = true;
  }
  if (!symbolicRegions.empty()) {
    if (!haveStore || !haveCarry || assignedCount != 0 ||
        result.cycleGuard != nullptr)
      return false;
  } else if (!haveStore || !haveCarry || assignedCount == 0 ||
             assignedCount == *loadWidth) {
    return false;
  }

  result.memoryPhi = cyclePhi;
  result.cycleHeader = header;
  result.cycleEntry = entryBlock;
  result.cycleBackedge = backedgeBlock;
  result.cycleEntryState = std::move(entryState);
  if (symbolicRegions.size() == 1)
    result.cycleSymbolicRegion =
        std::make_shared<OrderedWriterLayer>(
            std::move(symbolicRegions.front()));
  else
    result.cycleSymbolicRegions = std::move(symbolicRegions);
  return true;
}

bool collectCandidate(LoadInst &load, AAResults &aliasAnalysis,
                      MemorySSA &memorySSA, DominatorTree &dominators,
                      MemoryCandidate &result,
                      bool requireUse = true) {
  if (!load.isSimple() || (requireUse && load.use_empty()) ||
      !(load.getType()->isIntegerTy() ||
        load.getType()->isFloatingPointTy() ||
        load.getType()->isPointerTy()) ||
      !destinationPrefixHasNoWrite(load))
    return false;

  auto *use =
      dyn_cast_or_null<MemoryUse>(memorySSA.getMemoryAccess(&load));
  auto *memoryPhi =
      use == nullptr ? nullptr : dyn_cast<MemoryPhi>(use->getDefiningAccess());
  if (memoryPhi == nullptr || memoryPhi->getBlock() == load.getParent())
    return false;

  BasicBlock *dispatch = memoryPhi->getBlock();
  uint64_t controllerSite = 0;
  unsigned exitCount = 0;
  PHINode *exitId = findExitId(*dispatch, controllerSite, exitCount);
  if (exitId == nullptr ||
      memoryPhi->getNumIncomingValues() != exitCount ||
      !dominators.dominates(dispatch, load.getParent()))
    return false;

  SmallVector<CaptureState, kMaxContinuationExits> captures;
  if (!collectCaptures(
          *dispatch, controllerSite, exitCount, captures))
    return false;

  unsigned destinationOrdinal = 0;
  if (!destinationMatchesExits(
          *load.getParent(), controllerSite, captures,
          destinationOrdinal))
    return false;

  SmallVector<ReachingState, kMaxContinuationExits> states(exitCount);
  bool standardProof = true;
  for (const CaptureState &capture : captures) {
    if (capture.destinationOrdinal != destinationOrdinal)
      continue;
    MemoryAccess *access =
        incomingAccessForBlock(*memoryPhi, capture.block);
    ProvenanceBudget budget;
    if (access == nullptr ||
        !findReachingState(
            access, memorySSA.getLiveOnEntryDef(), memoryPhi, load,
            aliasAnalysis, dominators, capture.block,
            states[capture.ordinal], budget)) {
      standardProof = false;
      break;
    }
  }
  if (!standardProof) {
    states.assign(exitCount, ReachingState{});
    for (const CaptureState &capture : captures) {
      if (capture.destinationOrdinal != destinationOrdinal)
        continue;
      MemoryAccess *access =
          incomingAccessForBlock(*memoryPhi, capture.block);
      ReachingState byteState;
      if (findByteLaneState(
              access, memorySSA.getLiveOnEntryDef(), memoryPhi, load,
              aliasAnalysis, dominators, capture.block, byteState)) {
        states[capture.ordinal] = std::move(byteState);
      } else {
        ReachingState cyclicState;
        if (!findCyclicByteLaneState(
                access, memorySSA.getLiveOnEntryDef(), memoryPhi, load,
                aliasAnalysis, dominators, capture.block,
                cyclicState))
          return false;
        states[capture.ordinal] = std::move(cyclicState);
      }
    }
    const bool cyclic = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.cycleEntryState != nullptr;
        });
    const bool conditionalCyclic = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.cycleGuard != nullptr;
        });
    const bool multiLatchCyclic = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return !state.cycleTransfers.empty();
        });
    const bool unconditionalCyclic = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.cycleEntryState != nullptr &&
                 state.cycleGuard == nullptr &&
                 state.cycleTransfers.empty();
        });
    const bool guarded = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return std::any_of(
              state.byteLanes.begin(), state.byteLanes.end(),
              [](const ByteLaneSource &lane) {
                return lane.hasGuardedSource();
              });
        });
    const bool partitioned = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.pointerPartition != nullptr;
        });
    const bool pointerPartitionPriority = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.pointerPartition != nullptr &&
                 std::any_of(
                     state.byteLanes.begin(),
                     state.byteLanes.end(),
                     [](const ByteLaneSource &lane) {
                       return lane.hasGuardedSource();
                     });
        });
    const bool guardedWritePriority = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return std::any_of(
              state.byteLanes.begin(), state.byteLanes.end(),
              [](const ByteLaneSource &lane) {
                return lane.guarded.size() > 1;
              });
        });
    const bool orderedWriterGraph = std::any_of(
        states.begin(), states.end(),
        [](const ReachingState &state) {
          return state.requiresOrderedWriterGraph();
        });
    if ((multiLatchCyclic &&
         (conditionalCyclic || unconditionalCyclic)) ||
        (conditionalCyclic && unconditionalCyclic) ||
        (cyclic && guarded) ||
        (partitioned && cyclic) ||
        (!orderedWriterGraph &&
         ((partitioned && guardedWritePriority) ||
          (partitioned && guarded &&
           !pointerPartitionPriority))))
      return false;
  }

  const bool haveStore = std::any_of(
      states.begin(), states.end(), [](const ReachingState &state) {
        return state.hasKind(MemoryStateKind::Store);
      });
  // A destination whose every path reads the same live-on-entry state does
  // not need a synthesized tuple.
  if (!haveStore)
    return false;

  result.load = &load;
  result.exitId = exitId;
  result.memoryPhi = memoryPhi;
  result.controllerSite = controllerSite;
  result.destinationOrdinal = destinationOrdinal;
  result.captures = std::move(captures);
  result.states = std::move(states);
  return true;
}

bool parseScalarSlotProof(const PHINode &phi, uint64_t controllerSite,
                          ScalarSlotProof &result) {
  const MDNode *proof =
      phi.getMetadata("symcc.ifss_continuation_liveout");
  if (proof == nullptr || proof->getNumOperands() != 5 ||
      !hasSchema(*proof, kContinuationSchema))
    return false;

  uint64_t proofController = 0;
  uint64_t ordinal = 0;
  uint64_t destination = 0;
  uint64_t originalPhiSite = 0;
  if (!metadataInteger(*proof, 1, proofController) ||
      !metadataInteger(*proof, 2, ordinal) ||
      !metadataInteger(*proof, 3, destination) ||
      !metadataInteger(*proof, 4, originalPhiSite) ||
      proofController != controllerSite || ordinal >= 8 ||
      destination >= kMaxContinuationExits)
    return false;
  result.ordinal = static_cast<unsigned>(ordinal);
  result.destinationOrdinal = static_cast<unsigned>(destination);
  result.originalPhiSite = originalPhiSite;
  return true;
}

bool parseMemorySlotHeader(const PHINode &phi, uint64_t controllerSite,
                           unsigned &ordinal, unsigned &destinationOrdinal,
                           uint64_t &loadSite, unsigned &exitCount,
                           unsigned &relevantExitCount,
                           bool &allowsLiveOnEntry,
                           bool &allowsNestedMemoryPhi,
                           bool &allowsByteComposition,
                           bool &allowsGuardedByteComposition,
                           bool &allowsCyclicByteComposition,
                           bool &allowsSymbolicRegionCyclicByteComposition,
                           bool &allowsSymbolicRegionMultiLatchCyclicByteComposition,
                           bool &allowsConditionalCyclicByteComposition,
                           bool &allowsMultiLatchCyclicByteComposition,
                           bool &allowsConditionalMultiLatchCyclicByteComposition,
                           bool &allowsBoundedMultiLatchCyclicByteComposition,
                           bool &allowsNestedPredicateCyclicByteComposition,
                           bool &allowsOrderedWriterGraph,
                           bool &allowsSymbolicRegionWriterGraph,
                           bool &allowsPointerPartitionPriority,
                           bool &allowsPointerPartition,
                           bool &allowsGuardedWritePriority) {
  const MDNode *proof =
      phi.getMetadata("symcc.ifss_continuation_memory");
  if (proof == nullptr || proof->getNumOperands() < 7)
    return false;
  allowsGuardedWritePriority =
      hasSchema(*proof, kMemoryGuardedPrioritySchema);
  allowsSymbolicRegionCyclicByteComposition =
      hasSchema(
          *proof, kMemorySymbolicRegionCyclicByteLaneSchema) ||
      hasSchema(
          *proof, kMemoryOrderedSymbolicRegionCyclicByteLaneSchema);
  allowsSymbolicRegionMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema);
  allowsConditionalCyclicByteComposition =
      hasSchema(*proof, kMemoryConditionalCyclicByteLaneSchema);
  allowsConditionalMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemoryConditionalMultiLatchCyclicByteLaneSchema);
  allowsBoundedMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemoryBoundedMultiLatchCyclicByteLaneSchema);
  allowsNestedPredicateCyclicByteComposition =
      hasSchema(
          *proof, kMemoryNestedPredicateCyclicByteLaneSchema);
  allowsSymbolicRegionWriterGraph =
      hasSchema(*proof, kMemorySymbolicRegionWriterGraphSchema);
  const bool allowsLegacyOrderedWriterGraph =
      hasSchema(*proof, kMemoryOrderedWriterGraphSchema);
  allowsOrderedWriterGraph =
      allowsSymbolicRegionWriterGraph ||
      allowsLegacyOrderedWriterGraph;
  allowsMultiLatchCyclicByteComposition =
      allowsSymbolicRegionMultiLatchCyclicByteComposition ||
      allowsNestedPredicateCyclicByteComposition ||
      allowsBoundedMultiLatchCyclicByteComposition ||
      allowsConditionalMultiLatchCyclicByteComposition ||
      hasSchema(*proof, kMemoryMultiLatchCyclicByteLaneSchema);
  allowsPointerPartitionPriority =
      hasSchema(*proof, kMemoryPointerPartitionPrioritySchema);
  allowsGuardedByteComposition =
      allowsGuardedWritePriority ||
      allowsPointerPartitionPriority ||
      hasSchema(*proof, kMemoryGuardedByteLaneSchema);
  allowsCyclicByteComposition =
      allowsSymbolicRegionCyclicByteComposition ||
      allowsMultiLatchCyclicByteComposition ||
      allowsConditionalCyclicByteComposition ||
      hasSchema(*proof, kMemoryCyclicByteLaneSchema);
  allowsPointerPartition =
      allowsLegacyOrderedWriterGraph ||
      allowsPointerPartitionPriority ||
      hasSchema(*proof, kMemoryPointerPartitionSchema);
  allowsByteComposition =
      allowsOrderedWriterGraph || allowsPointerPartition ||
      allowsCyclicByteComposition ||
      allowsGuardedByteComposition ||
      hasSchema(*proof, kMemoryByteLaneSchema);
  allowsNestedMemoryPhi = hasSchema(*proof, kMemoryNestedSchema);
  allowsLiveOnEntry =
      allowsNestedMemoryPhi || allowsByteComposition ||
      hasSchema(*proof, kMemoryInitialSchema);
  if (!allowsLiveOnEntry && !hasSchema(*proof, kMemorySchema))
    return false;

  uint64_t proofController = 0;
  uint64_t slot = 0;
  uint64_t destination = 0;
  uint64_t exits = 0;
  uint64_t relevant = 0;
  if (!metadataInteger(*proof, 1, proofController) ||
      !metadataInteger(*proof, 2, slot) ||
      !metadataInteger(*proof, 3, destination) ||
      !metadataInteger(*proof, 4, loadSite) ||
      !metadataInteger(*proof, 5, exits) ||
      !metadataInteger(*proof, 6, relevant) ||
      proofController != controllerSite ||
      slot >= kMaxContinuationMemorySlots ||
      destination >= kMaxContinuationExits || exits < 2 ||
      exits > kMaxContinuationExits || relevant == 0 ||
      relevant > exits)
    return false;
  ordinal = static_cast<unsigned>(slot);
  destinationOrdinal = static_cast<unsigned>(destination);
  exitCount = static_cast<unsigned>(exits);
  relevantExitCount = static_cast<unsigned>(relevant);
  return true;
}

Value *incomingValueForBlock(const PHINode &phi, BasicBlock *block) {
  Value *result = nullptr;
  for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
    if (phi.getIncomingBlock(index) != block)
      continue;
    if (result != nullptr)
      return nullptr;
    result = phi.getIncomingValue(index);
  }
  return result;
}

bool snapshotMatches(const LoadInst &snapshot, const LoadInst &source,
                     BasicBlock *capture, const MDNode *proof) {
  if (!snapshot.isSimple() || snapshot.getParent() != capture ||
      snapshot.getType() != source.getType() ||
      snapshot.getPointerOperand() != source.getPointerOperand() ||
      snapshot.getAlign() != source.getAlign() ||
      snapshot.getMetadata(
          "symcc.ifss_continuation_memory_initial") != proof)
    return false;
  for (const Instruction *instruction = snapshot.getNextNode();
       instruction != nullptr &&
       instruction != capture->getTerminator();
       instruction = instruction->getNextNode())
    if (instruction->mayWriteToMemory())
      return false;
  return snapshot.comesBefore(capture->getTerminator());
}

using ProvenanceNodeList =
    SmallVector<const ReachingState *, kMaxProvenanceNodes>;

void flattenProvenance(const ReachingState &state,
                       ProvenanceNodeList &nodes) {
  nodes.push_back(&state);
  for (const std::shared_ptr<ReachingState> &incoming :
       state.incomingStates)
    flattenProvenance(*incoming, nodes);
}

uint64_t provenanceSourceSite(const ReachingState &state) {
  if (state.kind == MemoryStateKind::Store)
    return stableSiteId(*state.store);
  if (state.kind == MemoryStateKind::MemoryPhi)
    return stableSiteId(*state.memoryPhi->getBlock()->getTerminator());
  return 0;
}

bool provenanceValueMatches(const ReachingState &state, Value *value,
                            const LoadInst &source,
                            const MDNode *proof) {
  if (state.kind == MemoryStateKind::Store)
    return state.store != nullptr &&
           value == state.store->getValueOperand();
  if (state.kind == MemoryStateKind::LiveOnEntry) {
    auto *snapshot = dyn_cast<LoadInst>(value);
    return snapshot != nullptr && state.point != nullptr &&
           snapshotMatches(*snapshot, source, state.point, proof);
  }

  auto *phi = dyn_cast<PHINode>(value);
  if (phi == nullptr || state.memoryPhi == nullptr ||
      phi->getParent() != state.memoryPhi->getBlock() ||
      phi->getMetadata(
          "symcc.ifss_continuation_memory_nested") != proof ||
      phi->getNumIncomingValues() != state.incomingStates.size() ||
      state.incomingBlocks.size() != state.incomingStates.size())
    return false;
  for (unsigned index = 0; index < state.incomingStates.size(); ++index) {
    BasicBlock *block = state.incomingBlocks[index];
    Value *incoming = incomingValueForBlock(*phi, block);
    if (incoming == nullptr ||
        !provenanceValueMatches(
            *state.incomingStates[index], incoming, source, proof))
      return false;
  }
  return true;
}

bool nestedProofStateMatches(const MDNode &proof, unsigned &index,
                             unsigned exitOrdinal,
                             const ReachingState &root) {
  ProvenanceNodeList nodes;
  flattenProvenance(root, nodes);
  if (nodes.empty() || nodes.size() > kMaxProvenanceNodes)
    return false;
  DenseMap<const ReachingState *, unsigned> indices;
  for (unsigned ordinal = 0; ordinal < nodes.size(); ++ordinal)
    indices[nodes[ordinal]] = ordinal;

  uint64_t proofExit = 0;
  uint64_t nodeCount = 0;
  uint64_t rootIndex = 0;
  if (!metadataInteger(proof, index++, proofExit) ||
      !metadataInteger(proof, index++, nodeCount) ||
      !metadataInteger(proof, index++, rootIndex) ||
      proofExit != exitOrdinal || nodeCount != nodes.size() ||
      rootIndex != 0)
    return false;
  for (const ReachingState *state : nodes) {
    uint64_t proofKind = 0;
    uint64_t proofSource = 0;
    uint64_t skippedCount = 0;
    if (!metadataInteger(proof, index++, proofKind) ||
        !metadataInteger(proof, index++, proofSource) ||
        !metadataInteger(proof, index++, skippedCount) ||
        proofKind != static_cast<uint64_t>(state->kind) ||
        proofSource != provenanceSourceSite(*state) ||
        skippedCount != state->skipped.size())
      return false;
    for (Instruction *instruction : state->skipped) {
      uint64_t skippedSite = 0;
      if (!metadataInteger(proof, index++, skippedSite) ||
          skippedSite != stableSiteId(*instruction))
        return false;
    }
    uint64_t incomingCount = 0;
    if (!metadataInteger(proof, index++, incomingCount) ||
        incomingCount != state->incomingStates.size() ||
        incomingCount != state->incomingBlocks.size())
      return false;
    for (unsigned incoming = 0; incoming < incomingCount; ++incoming) {
      uint64_t blockSite = 0;
      uint64_t childIndex = 0;
      const ReachingState *child =
          state->incomingStates[incoming].get();
      if (!metadataInteger(proof, index++, blockSite) ||
          !metadataInteger(proof, index++, childIndex) ||
          blockSite != stableSiteId(
                           *state->incomingBlocks[incoming]
                                ->getTerminator()) ||
          childIndex != indices.lookup(child))
        return false;
    }
  }
  return true;
}

void appendNestedProofState(SmallVectorImpl<Metadata *> &proof,
                            LLVMContext &context, unsigned exitOrdinal,
                            const ReachingState &root) {
  ProvenanceNodeList nodes;
  flattenProvenance(root, nodes);
  DenseMap<const ReachingState *, unsigned> indices;
  for (unsigned ordinal = 0; ordinal < nodes.size(); ++ordinal)
    indices[nodes[ordinal]] = ordinal;

  proof.push_back(integerMetadata(context, 32, exitOrdinal));
  proof.push_back(integerMetadata(context, 32, nodes.size()));
  proof.push_back(integerMetadata(context, 32, 0));
  for (const ReachingState *state : nodes) {
    proof.push_back(integerMetadata(
        context, 32, static_cast<uint64_t>(state->kind)));
    proof.push_back(
        integerMetadata(context, 64, provenanceSourceSite(*state)));
    proof.push_back(
        integerMetadata(context, 32, state->skipped.size()));
    for (Instruction *instruction : state->skipped)
      proof.push_back(
          integerMetadata(context, 64, stableSiteId(*instruction)));
    proof.push_back(
        integerMetadata(context, 32, state->incomingStates.size()));
    for (unsigned incoming = 0;
         incoming < state->incomingStates.size(); ++incoming) {
      proof.push_back(integerMetadata(
          context, 64,
          stableSiteId(
              *state->incomingBlocks[incoming]->getTerminator())));
      proof.push_back(integerMetadata(
          context, 32,
          indices.lookup(state->incomingStates[incoming].get())));
    }
  }
}

unsigned byteBitShift(bool littleEndian, unsigned width,
                      unsigned byteOrdinal) {
  return 8 * (littleEndian ? byteOrdinal : width - 1 - byteOrdinal);
}

bool constantIntegerEquals(Value *value, uint64_t expected) {
  auto *constant = dyn_cast<ConstantInt>(value);
  return constant != nullptr && constant->getZExtValue() == expected;
}

Value *recoveredByteSource(Value *byte, unsigned sourceWidth,
                           unsigned sourceByte, bool littleEndian,
                           BasicBlock *point) {
  const unsigned sourceShift =
      byteBitShift(littleEndian, sourceWidth, sourceByte);
  if (sourceWidth == 1)
    return sourceShift == 0 ? byte : nullptr;
  auto *truncate = dyn_cast<TruncInst>(byte);
  if (truncate == nullptr || truncate->getParent() != point)
    return nullptr;
  Value *extracted = truncate->getOperand(0);
  if (sourceShift == 0)
    return extracted;
  auto *shift = dyn_cast<BinaryOperator>(extracted);
  if (shift == nullptr || shift->getOpcode() != Instruction::LShr ||
      shift->getParent() != point ||
      !constantIntegerEquals(shift->getOperand(1), sourceShift))
    return nullptr;
  return shift->getOperand(0);
}

bool byteCompositionValueMatches(const ReachingState &state, Value *value,
                                 const LoadInst &source,
                                 const MDNode *proof) {
  if (state.kind != MemoryStateKind::ByteComposition ||
      state.point == nullptr || state.byteWidth < 2 ||
      state.byteWidth > kMaxByteLaneWidth ||
      state.byteLanes.size() != state.byteWidth ||
      source.getType()->getIntegerBitWidth() != 8 * state.byteWidth)
    return false;

  Value *accumulator = value;
  LoadInst *liveSnapshot = nullptr;
  for (unsigned reverse = state.byteWidth; reverse != 0; --reverse) {
    const ByteLaneSource &lane = state.byteLanes[reverse - 1];
    if (lane.ordinal != reverse - 1 || lane.sourceWidth == 0 ||
        lane.sourceWidth > kMaxByteLaneWidth ||
        lane.sourceByte >= lane.sourceWidth ||
        lane.guarded.size() > 2 ||
        std::any_of(
            lane.guarded.begin(), lane.guarded.end(),
            [](const GuardedByteOverlay &overlay) {
              return overlay.priority >= 2 ||
                     overlay.store == nullptr ||
                     overlay.guard == nullptr ||
                     overlay.sourceWidth == 0 ||
                     overlay.sourceWidth >
                         kMaxByteLaneWidth ||
                     overlay.sourceByte >=
                         overlay.sourceWidth;
            }) ||
        (lane.guarded.size() == 2 &&
         lane.guarded[0].priority >=
             lane.guarded[1].priority))
      return false;

    auto *combine = dyn_cast<BinaryOperator>(accumulator);
    if (combine == nullptr || combine->getOpcode() != Instruction::Or ||
        combine->getParent() != state.point ||
        combine->getType() != source.getType())
      return false;
    accumulator = combine->getOperand(0);
    Value *component = combine->getOperand(1);

    const unsigned destinationShift = byteBitShift(
        state.littleEndian, state.byteWidth, lane.ordinal);
    if (destinationShift != 0) {
      auto *shift = dyn_cast<BinaryOperator>(component);
      if (shift == nullptr ||
          shift->getOpcode() != Instruction::Shl ||
          shift->getParent() != state.point ||
          !constantIntegerEquals(
              shift->getOperand(1), destinationShift))
        return false;
      component = shift->getOperand(0);
    }

    auto *extend = dyn_cast<ZExtInst>(component);
    if (extend == nullptr || extend->getParent() != state.point ||
        extend->getType() != source.getType() ||
        !extend->getOperand(0)->getType()->isIntegerTy(8))
      return false;
    Value *baseByte = extend->getOperand(0);
    for (const GuardedByteOverlay &overlay :
         lane.guarded) {
      auto *select = dyn_cast<SelectInst>(baseByte);
      if (select == nullptr || select->getParent() != state.point ||
          select->getCondition() != overlay.guard ||
          !select->getType()->isIntegerTy(8))
        return false;
      Value *guardedByte = nullptr;
      if (overlay.storeWhenTrue) {
        guardedByte = select->getTrueValue();
        baseByte = select->getFalseValue();
      } else {
        guardedByte = select->getFalseValue();
        baseByte = select->getTrueValue();
      }
      Value *guardedSource = recoveredByteSource(
          guardedByte, overlay.sourceWidth,
          overlay.sourceByte, state.littleEndian,
          state.point);
      if (guardedSource == nullptr ||
          guardedSource != overlay.store->getValueOperand())
        return false;
      const std::optional<unsigned> guardedWidth =
          fixedIntegerByteWidth(guardedSource->getType());
      if (!guardedWidth ||
          *guardedWidth != overlay.sourceWidth)
        return false;
    }

    Value *sourceValue = recoveredByteSource(
        baseByte, lane.sourceWidth, lane.sourceByte,
        state.littleEndian, state.point);
    if (sourceValue == nullptr ||
        (lane.store != nullptr &&
         sourceValue != lane.store->getValueOperand()))
      return false;
    const std::optional<unsigned> actualSourceWidth =
        fixedIntegerByteWidth(sourceValue->getType());
    if (!actualSourceWidth || *actualSourceWidth != lane.sourceWidth)
      return false;
    if (lane.store == nullptr) {
      auto *snapshot = dyn_cast<LoadInst>(sourceValue);
      if (snapshot == nullptr ||
          !snapshotMatches(
              *snapshot, source, state.point, proof) ||
          (liveSnapshot != nullptr && liveSnapshot != snapshot))
        return false;
      liveSnapshot = snapshot;
    }
  }
  auto *neutral = dyn_cast<ConstantInt>(accumulator);
  return neutral != nullptr && neutral->isZero() &&
         neutral->getType() == source.getType();
}

bool symbolicRegionByteMatches(
    const OrderedWriterLayer &layer, unsigned lane, Value *value,
    Value *&baseByte, bool littleEndian, BasicBlock *point);

bool cyclicBackedgeValueMatches(
    const ReachingState &state, Value *value, PHINode *cycleValue,
    const LoadInst &source) {
  if (state.cycleBackedge == nullptr ||
      state.byteWidth < 2 ||
      state.byteWidth > kMaxByteLaneWidth ||
      state.byteLanes.size() != state.byteWidth)
    return false;
  Value *accumulator = value;
  for (unsigned reverse = state.byteWidth; reverse != 0; --reverse) {
    const ByteLaneSource &lane = state.byteLanes[reverse - 1];
    if (lane.ordinal != reverse - 1 ||
        lane.sourceWidth == 0 ||
        lane.sourceWidth > kMaxByteLaneWidth ||
        lane.sourceByte >= lane.sourceWidth ||
        lane.hasGuardedSource() ||
        (lane.carry && lane.store != nullptr) ||
        (!lane.carry && lane.store == nullptr))
      return false;
    auto *combine = dyn_cast<BinaryOperator>(accumulator);
    if (combine == nullptr ||
        combine->getOpcode() != Instruction::Or ||
        combine->getParent() != state.cycleBackedge ||
        combine->getType() != source.getType())
      return false;
    accumulator = combine->getOperand(0);
    Value *component = combine->getOperand(1);
    const unsigned destinationShift = byteBitShift(
        state.littleEndian, state.byteWidth, lane.ordinal);
    if (destinationShift != 0) {
      auto *shift = dyn_cast<BinaryOperator>(component);
      if (shift == nullptr ||
          shift->getOpcode() != Instruction::Shl ||
          shift->getParent() != state.cycleBackedge ||
          !constantIntegerEquals(
              shift->getOperand(1), destinationShift))
        return false;
      component = shift->getOperand(0);
    }
    auto *extend = dyn_cast<ZExtInst>(component);
    if (extend == nullptr ||
        extend->getParent() != state.cycleBackedge ||
        extend->getType() != source.getType() ||
        !extend->getOperand(0)->getType()->isIntegerTy(8))
      return false;
    Value *byteValue = extend->getOperand(0);
    if (state.cycleSymbolicRegion != nullptr ||
        !state.cycleSymbolicRegions.empty()) {
      if (!lane.carry)
        return false;
      Value *regionValue = byteValue;
      if (state.cycleGuard != nullptr) {
        auto *select = dyn_cast<SelectInst>(regionValue);
        if (select == nullptr ||
            select->getParent() != state.cycleBackedge ||
            select->getCondition() != state.cycleGuard ||
            !select->getType()->isIntegerTy(8))
          return false;
        Value *carriedValue =
            state.cycleStoreWhenTrue
                ? select->getFalseValue()
                : select->getTrueValue();
        Value *carriedSource = recoveredByteSource(
            carriedValue, state.byteWidth, lane.ordinal,
            state.littleEndian, state.cycleBackedge);
        if (carriedSource != cycleValue)
          return false;
        regionValue =
            state.cycleStoreWhenTrue
                ? select->getTrueValue()
                : select->getFalseValue();
      }
      Value *carriedByte = regionValue;
      if (state.cycleSymbolicRegion != nullptr) {
        if (!state.cycleSymbolicRegions.empty() ||
            !symbolicRegionByteMatches(
                *state.cycleSymbolicRegion, lane.ordinal,
                regionValue, carriedByte, state.littleEndian,
                state.cycleBackedge))
          return false;
      } else {
        if (state.cycleSymbolicRegions.size() < 2 ||
            state.cycleSymbolicRegions.size() >
                kMaxCyclicSymbolicRegionWriters ||
            state.cycleGuard != nullptr)
          return false;
        for (const OrderedWriterLayer &layer :
             state.cycleSymbolicRegions) {
          Value *nextBase = nullptr;
          if (!symbolicRegionByteMatches(
                  layer, lane.ordinal, carriedByte, nextBase,
                  state.littleEndian, state.cycleBackedge))
            return false;
          carriedByte = nextBase;
        }
      }
      if (carriedByte == nullptr ||
          recoveredByteSource(
              carriedByte, state.byteWidth, lane.ordinal,
              state.littleEndian, state.cycleBackedge) != cycleValue)
        return false;
      continue;
    }
    if (state.cycleGuard != nullptr && !lane.carry) {
      auto *select = dyn_cast<SelectInst>(byteValue);
      if (select == nullptr ||
          select->getParent() != state.cycleBackedge ||
          select->getCondition() != state.cycleGuard ||
          !select->getType()->isIntegerTy(8))
        return false;
      Value *storedByte =
          state.cycleStoreWhenTrue
              ? select->getTrueValue()
              : select->getFalseValue();
      Value *carriedByte =
          state.cycleStoreWhenTrue
              ? select->getFalseValue()
              : select->getTrueValue();
      Value *storedSource = recoveredByteSource(
          storedByte, lane.sourceWidth, lane.sourceByte,
          state.littleEndian, state.cycleBackedge);
      Value *carriedSource = recoveredByteSource(
          carriedByte, state.byteWidth, lane.ordinal,
          state.littleEndian, state.cycleBackedge);
      if (storedSource != lane.store->getValueOperand() ||
          carriedSource != cycleValue)
        return false;
      continue;
    }
    Value *sourceValue = recoveredByteSource(
        byteValue, lane.sourceWidth,
        lane.sourceByte, state.littleEndian,
        state.cycleBackedge);
    if (sourceValue == nullptr ||
        (lane.carry ? sourceValue != cycleValue
                    : sourceValue !=
                          lane.store->getValueOperand()))
      return false;
  }
  auto *neutral = dyn_cast<ConstantInt>(accumulator);
  return neutral != nullptr && neutral->isZero() &&
         neutral->getType() == source.getType();
}

bool cyclicByteCompositionValueMatches(
    const ReachingState &state, Value *value,
    const LoadInst &source, const MDNode *proof) {
  auto *phi = dyn_cast<PHINode>(value);
  if (phi == nullptr || state.memoryPhi == nullptr ||
      state.cycleHeader == nullptr ||
      state.cycleEntry == nullptr ||
      state.cycleBackedge == nullptr ||
      state.cycleEntryState == nullptr ||
      phi->getParent() != state.cycleHeader ||
      phi->getType() != source.getType() ||
      phi->getNumIncomingValues() != 2 ||
      phi->getMetadata(
          "symcc.ifss_continuation_memory_cycle") != proof)
    return false;
  Value *entry =
      incomingValueForBlock(*phi, state.cycleEntry);
  Value *backedge =
      incomingValueForBlock(*phi, state.cycleBackedge);
  return entry != nullptr && backedge != nullptr &&
         byteCompositionValueMatches(
             *state.cycleEntryState, entry, source, proof) &&
         cyclicBackedgeValueMatches(
             state, backedge, phi, source);
}

bool predicateCyclicBackedgeValueMatches(
    const CyclicBackedgeTransfer &transfer, Value *value,
    PHINode *cycleValue, const ReachingState &state,
    const LoadInst &source, const MDNode *proof) {
  auto *phi = dyn_cast<PHINode>(value);
  if (!transfer.hasPredicateTree() ||
      transfer.predicateLeaves.size() < 3 ||
      transfer.predicateLeaves.size() >
          kMaxCyclicPredicateLeaves ||
      transfer.predicateNodes.size() + 1 !=
          transfer.predicateLeaves.size() ||
      phi == nullptr || phi->getParent() != transfer.block ||
      phi->getType() != source.getType() ||
      phi->getNumIncomingValues() !=
          transfer.predicateLeaves.size() ||
      phi->getMetadata(
          "symcc.ifss_continuation_memory_predicate_cycle") !=
          proof)
    return false;
  for (const CyclicPredicateLeaf &leaf :
       transfer.predicateLeaves) {
    Value *leafValue = incomingValueForBlock(*phi, leaf.block);
    if (leafValue == nullptr)
      return false;
    ReachingState leafState;
    leafState.byteWidth = state.byteWidth;
    leafState.littleEndian = state.littleEndian;
    leafState.cycleBackedge = leaf.block;
    leafState.byteLanes = leaf.byteLanes;
    if (!cyclicBackedgeValueMatches(
            leafState, leafValue, cycleValue, source))
      return false;
  }
  return true;
}

bool multiLatchCyclicByteCompositionValueMatches(
    const ReachingState &state, Value *value,
    const LoadInst &source, const MDNode *proof) {
  auto *phi = dyn_cast<PHINode>(value);
  const bool hasPredicateTree = std::any_of(
      state.cycleTransfers.begin(), state.cycleTransfers.end(),
      [](const CyclicBackedgeTransfer &transfer) {
        return transfer.hasPredicateTree();
      });
  if (phi == nullptr || state.memoryPhi == nullptr ||
      state.cycleHeader == nullptr ||
      state.cycleEntry == nullptr ||
      state.cycleEntryState == nullptr ||
      state.cycleTransfers.size() <
          (hasPredicateTree ? 1U : 2U) ||
      state.cycleTransfers.size() > kMaxCyclicLatches ||
      phi->getParent() != state.cycleHeader ||
      phi->getType() != source.getType() ||
      phi->getNumIncomingValues() !=
          state.cycleTransfers.size() + 1 ||
      phi->getMetadata(
          "symcc.ifss_continuation_memory_multi_cycle") != proof)
    return false;
  Value *entry =
      incomingValueForBlock(*phi, state.cycleEntry);
  if (entry == nullptr ||
      !byteCompositionValueMatches(
          *state.cycleEntryState, entry, source, proof))
    return false;
  for (const CyclicBackedgeTransfer &transfer :
       state.cycleTransfers) {
    Value *backedge =
        incomingValueForBlock(*phi, transfer.block);
    if (backedge == nullptr)
      return false;
    if (transfer.hasPredicateTree()) {
      if (!predicateCyclicBackedgeValueMatches(
              transfer, backedge, phi, state, source, proof))
        return false;
      continue;
    }
    ReachingState transferState;
    transferState.byteWidth = state.byteWidth;
    transferState.littleEndian = state.littleEndian;
    transferState.cycleBackedge = transfer.block;
    transferState.cycleBranch = transfer.branch;
    transferState.cycleStoreArm = transfer.storeArm;
    transferState.cycleCarryArm = transfer.carryArm;
    transferState.cycleGuard = transfer.guard;
    transferState.cycleStoreWhenTrue =
        transfer.storeWhenTrue;
    transferState.byteLanes = transfer.byteLanes;
    transferState.cycleSymbolicRegion =
        transfer.symbolicRegion;
    if (!cyclicBackedgeValueMatches(
            transferState, backedge, phi, source))
      return false;
  }
  return true;
}

bool pointerPartitionTreeMatches(
    const PointerPartition &partition,
    PointerPartitionChild child, unsigned lane,
    Value *value, Value *&baseByte, bool littleEndian,
    BasicBlock *point) {
  if (child.leaf) {
    if (child.index >= partition.leaves.size() ||
        lane >= partition.leaves[child.index].sourceBytes.size())
      return false;
    const int sourceByte =
        partition.leaves[child.index].sourceBytes[lane];
    if (sourceByte < 0) {
      if (baseByte != nullptr && baseByte != value)
        return false;
      baseByte = value;
      return true;
    }
    Value *source = recoveredByteSource(
        value, partition.storeWidth,
        static_cast<unsigned>(sourceByte),
        littleEndian, point);
    return source != nullptr &&
           source == partition.store->getValueOperand();
  }
  if (child.index >= partition.nodes.size())
    return false;
  const PointerPartitionNode &node =
      partition.nodes[child.index];
  auto *select = dyn_cast<SelectInst>(value);
  return select != nullptr && select->getParent() == point &&
         select->getCondition() == node.guard &&
         pointerPartitionTreeMatches(
             partition, node.trueChild, lane,
             select->getTrueValue(), baseByte,
             littleEndian, point) &&
         pointerPartitionTreeMatches(
             partition, node.falseChild, lane,
             select->getFalseValue(), baseByte,
             littleEndian, point);
}

bool symbolicRegionByteMatches(
    const OrderedWriterLayer &layer, unsigned lane, Value *value,
    Value *&baseByte, bool littleEndian, BasicBlock *point) {
  if (layer.kind != OrderedWriterKind::SymbolicRegion ||
      layer.store == nullptr || layer.symbolicIndex == nullptr ||
      layer.symbolicIndexBits == 0 ||
      layer.symbolicIndexBits > 64 ||
      lane >= layer.symbolicLaneCases.size())
    return false;
  Value *current = value;
  for (const SymbolicRegionCase &item :
       layer.symbolicLaneCases[lane]) {
    auto *select = dyn_cast<SelectInst>(current);
    auto *condition =
        select == nullptr
            ? nullptr
            : dyn_cast<ICmpInst>(select->getCondition());
    auto *constant =
        condition == nullptr
            ? nullptr
            : dyn_cast<ConstantInt>(condition->getOperand(1));
    if (select == nullptr || select->getParent() != point ||
        !select->getType()->isIntegerTy(8) ||
        condition == nullptr || condition->getParent() != point ||
        condition->getPredicate() != ICmpInst::ICMP_EQ ||
        condition->getOperand(0) != layer.symbolicIndex ||
        constant == nullptr ||
        constant->getBitWidth() != layer.symbolicIndexBits ||
        constant->getSExtValue() != item.indexValue)
      return false;
    Value *storedSource = recoveredByteSource(
        select->getTrueValue(), layer.storeWidth,
        item.sourceByte, littleEndian, point);
    if (storedSource != layer.store->getValueOperand())
      return false;
    current = select->getFalseValue();
  }
  baseByte = current;
  return true;
}

bool pointerPartitionValueMatches(
    const ReachingState &state, Value *value,
    const LoadInst &source, const MDNode *proof) {
  if (state.pointerPartition == nullptr ||
      state.point == nullptr || state.byteWidth < 2 ||
      state.byteWidth > kMaxByteLaneWidth ||
      state.byteLanes.size() != state.byteWidth ||
      state.pointerPartition->nodes.empty())
    return false;
  Value *accumulator = value;
  LoadInst *liveSnapshot = nullptr;
  for (unsigned reverse = state.byteWidth; reverse != 0; --reverse) {
    const ByteLaneSource &lane = state.byteLanes[reverse - 1];
    if (lane.ordinal != reverse - 1 || lane.carry ||
        lane.guarded.size() > 1 || lane.sourceWidth == 0 ||
        lane.sourceWidth > kMaxByteLaneWidth ||
        lane.sourceByte >= lane.sourceWidth)
      return false;
    auto *combine = dyn_cast<BinaryOperator>(accumulator);
    if (combine == nullptr ||
        combine->getOpcode() != Instruction::Or ||
        combine->getParent() != state.point ||
        combine->getType() != source.getType())
      return false;
    accumulator = combine->getOperand(0);
    Value *component = combine->getOperand(1);
    const unsigned destinationShift = byteBitShift(
        state.littleEndian, state.byteWidth, lane.ordinal);
    if (destinationShift != 0) {
      auto *shift = dyn_cast<BinaryOperator>(component);
      if (shift == nullptr ||
          shift->getOpcode() != Instruction::Shl ||
          shift->getParent() != state.point ||
          !constantIntegerEquals(
              shift->getOperand(1), destinationShift))
        return false;
      component = shift->getOperand(0);
    }
    auto *extend = dyn_cast<ZExtInst>(component);
    if (extend == nullptr || extend->getParent() != state.point ||
        extend->getType() != source.getType() ||
        !extend->getOperand(0)->getType()->isIntegerTy(8))
      return false;
    Value *baseByte = nullptr;
    if (!pointerPartitionTreeMatches(
            *state.pointerPartition, {false, 0}, lane.ordinal,
            extend->getOperand(0), baseByte, state.littleEndian,
            state.point) ||
        baseByte == nullptr)
      return false;
    for (const GuardedByteOverlay &overlay :
         lane.guarded) {
      auto *select = dyn_cast<SelectInst>(baseByte);
      if (select == nullptr ||
          select->getParent() != state.point ||
          select->getCondition() != overlay.guard ||
          !select->getType()->isIntegerTy(8))
        return false;
      Value *guardedByte = nullptr;
      if (overlay.storeWhenTrue) {
        guardedByte = select->getTrueValue();
        baseByte = select->getFalseValue();
      } else {
        guardedByte = select->getFalseValue();
        baseByte = select->getTrueValue();
      }
      Value *guardedSource = recoveredByteSource(
          guardedByte, overlay.sourceWidth,
          overlay.sourceByte, state.littleEndian,
          state.point);
      if (guardedSource == nullptr ||
          guardedSource != overlay.store->getValueOperand())
        return false;
    }
    Value *baseSource = recoveredByteSource(
        baseByte, lane.sourceWidth, lane.sourceByte,
        state.littleEndian, state.point);
    if (baseSource == nullptr ||
        (lane.store != nullptr &&
         baseSource != lane.store->getValueOperand()))
      return false;
    const std::optional<unsigned> actualWidth =
        fixedIntegerByteWidth(baseSource->getType());
    if (!actualWidth || *actualWidth != lane.sourceWidth)
      return false;
    if (lane.store == nullptr) {
      auto *snapshot = dyn_cast<LoadInst>(baseSource);
      if (snapshot == nullptr ||
          !snapshotMatches(
              *snapshot, source, state.point, proof) ||
          (liveSnapshot != nullptr &&
           liveSnapshot != snapshot))
        return false;
      liveSnapshot = snapshot;
    }
  }
  auto *neutral = dyn_cast<ConstantInt>(accumulator);
  return neutral != nullptr && neutral->isZero() &&
         neutral->getType() == source.getType();
}

bool orderedWriterGraphValueMatches(
    const ReachingState &state, Value *value,
    const LoadInst &source, const MDNode *proof) {
  if (state.point == nullptr || state.byteWidth < 2 ||
      state.byteWidth > kMaxByteLaneWidth ||
      state.byteLanes.size() != state.byteWidth ||
      state.writerLayers.size() > kMaxOrderedWriterLayers)
    return false;
  Value *accumulator = value;
  LoadInst *liveSnapshot = nullptr;
  for (unsigned reverse = state.byteWidth; reverse != 0; --reverse) {
    const ByteLaneSource &lane = state.byteLanes[reverse - 1];
    if (lane.ordinal != reverse - 1 || lane.carry ||
        lane.sourceWidth == 0 ||
        lane.sourceWidth > kMaxByteLaneWidth ||
        lane.sourceByte >= lane.sourceWidth)
      return false;
    auto *combine = dyn_cast<BinaryOperator>(accumulator);
    if (combine == nullptr ||
        combine->getOpcode() != Instruction::Or ||
        combine->getParent() != state.point ||
        combine->getType() != source.getType())
      return false;
    accumulator = combine->getOperand(0);
    Value *component = combine->getOperand(1);
    const unsigned destinationShift = byteBitShift(
        state.littleEndian, state.byteWidth, lane.ordinal);
    if (destinationShift != 0) {
      auto *shift = dyn_cast<BinaryOperator>(component);
      if (shift == nullptr ||
          shift->getOpcode() != Instruction::Shl ||
          shift->getParent() != state.point ||
          !constantIntegerEquals(
              shift->getOperand(1), destinationShift))
        return false;
      component = shift->getOperand(0);
    }
    auto *extend = dyn_cast<ZExtInst>(component);
    if (extend == nullptr || extend->getParent() != state.point ||
        extend->getType() != source.getType() ||
        !extend->getOperand(0)->getType()->isIntegerTy(8))
      return false;
    Value *baseByte = extend->getOperand(0);
    for (unsigned layerIndex = 0;
         layerIndex < state.writerLayers.size(); ++layerIndex) {
      const OrderedWriterLayer &layer =
          state.writerLayers[layerIndex];
      if (layer.ordinal != layerIndex || layer.store == nullptr ||
          layer.storeWidth == 0 ||
          layer.storeWidth > kMaxByteLaneWidth)
        return false;
      if (layer.kind == OrderedWriterKind::PointerPartition) {
        if (layer.pointerPartition == nullptr ||
            layer.pointerPartition->store != layer.store ||
            layer.pointerPartition->storeWidth != layer.storeWidth)
          return false;
        Value *fallback = nullptr;
        if (!pointerPartitionTreeMatches(
                *layer.pointerPartition, {false, 0}, lane.ordinal,
                baseByte, fallback, state.littleEndian, state.point) ||
            fallback == nullptr)
          return false;
        baseByte = fallback;
        continue;
      }
      if (layer.kind == OrderedWriterKind::SymbolicRegion) {
        Value *fallback = nullptr;
        if (!symbolicRegionByteMatches(
                layer, lane.ordinal, baseByte, fallback,
                state.littleEndian, state.point) ||
            fallback == nullptr)
          return false;
        baseByte = fallback;
        continue;
      }
      if (layer.kind != OrderedWriterKind::Guarded ||
          layer.guard == nullptr ||
          layer.pointerPartition != nullptr ||
          layer.sourceBytes.size() != state.byteWidth ||
          layer.storePolarities.size() != state.byteWidth)
        return false;
      const int sourceByte = layer.sourceBytes[lane.ordinal];
      const int polarity = layer.storePolarities[lane.ordinal];
      if (sourceByte < 0) {
        if (polarity != -1)
          return false;
        continue;
      }
      if (polarity < 0 || polarity > 1 ||
          static_cast<unsigned>(sourceByte) >= layer.storeWidth)
        return false;
      auto *select = dyn_cast<SelectInst>(baseByte);
      if (select == nullptr ||
          select->getParent() != state.point ||
          select->getCondition() != layer.guard ||
          !select->getType()->isIntegerTy(8))
        return false;
      Value *storedByte =
          polarity != 0 ? select->getTrueValue()
                        : select->getFalseValue();
      baseByte = polarity != 0 ? select->getFalseValue()
                               : select->getTrueValue();
      Value *storedSource = recoveredByteSource(
          storedByte, layer.storeWidth,
          static_cast<unsigned>(sourceByte), state.littleEndian,
          state.point);
      if (storedSource != layer.store->getValueOperand())
        return false;
    }
    Value *baseSource = recoveredByteSource(
        baseByte, lane.sourceWidth, lane.sourceByte,
        state.littleEndian, state.point);
    if (baseSource == nullptr ||
        (lane.store != nullptr &&
         baseSource != lane.store->getValueOperand()))
      return false;
    const std::optional<unsigned> actualWidth =
        fixedIntegerByteWidth(baseSource->getType());
    if (!actualWidth || *actualWidth != lane.sourceWidth)
      return false;
    if (lane.store == nullptr) {
      auto *snapshot = dyn_cast<LoadInst>(baseSource);
      if (snapshot == nullptr ||
          !snapshotMatches(
              *snapshot, source, state.point, proof) ||
          (liveSnapshot != nullptr && liveSnapshot != snapshot))
        return false;
      liveSnapshot = snapshot;
    }
  }
  auto *neutral = dyn_cast<ConstantInt>(accumulator);
  return neutral != nullptr && neutral->isZero() &&
         neutral->getType() == source.getType();
}

bool byteProofStateMatches(const MDNode &proof, unsigned &index,
                           unsigned exitOrdinal,
                           const ReachingState &state, Value *incoming,
                           const LoadInst &source,
                           bool guardedSchema,
                           bool matchExpression = true,
                           bool prioritySchema = false,
                           bool allowUnencodedGuarded = false) {
  uint64_t proofExit = 0;
  uint64_t byteWidth = 0;
  uint64_t endianness = 0;
  uint64_t skippedCount = 0;
  if (!metadataInteger(proof, index++, proofExit) ||
      !metadataInteger(proof, index++, byteWidth) ||
      !metadataInteger(proof, index++, endianness) ||
      !metadataInteger(proof, index++, skippedCount) ||
      proofExit != exitOrdinal ||
      state.kind != MemoryStateKind::ByteComposition ||
      byteWidth != state.byteWidth ||
      endianness != (state.littleEndian ? 0 : 1) ||
      skippedCount != state.skipped.size())
    return false;
  for (Instruction *instruction : state.skipped) {
    uint64_t skippedSite = 0;
    if (!metadataInteger(proof, index++, skippedSite) ||
        skippedSite != stableSiteId(*instruction))
      return false;
  }

  uint64_t laneCount = 0;
  if (!metadataInteger(proof, index++, laneCount) ||
      laneCount != state.byteLanes.size() ||
      laneCount != state.byteWidth)
    return false;
  for (const ByteLaneSource &lane : state.byteLanes) {
    uint64_t ordinal = 0;
    uint64_t kind = 0;
    uint64_t sourceSite = 0;
    uint64_t sourceByte = 0;
    uint64_t sourceWidth = 0;
    if (!metadataInteger(proof, index++, ordinal) ||
        !metadataInteger(proof, index++, kind) ||
        !metadataInteger(proof, index++, sourceSite) ||
        !metadataInteger(proof, index++, sourceByte) ||
        !metadataInteger(proof, index++, sourceWidth) ||
        ordinal != lane.ordinal ||
        kind != static_cast<uint64_t>(lane.kind()) ||
        sourceSite !=
            (lane.store == nullptr ? 0 : stableSiteId(*lane.store)) ||
        sourceByte != lane.sourceByte ||
        sourceWidth != lane.sourceWidth)
      return false;
    if (guardedSchema) {
      uint64_t guardedCount = 0;
      const uint64_t expectedCount =
          prioritySchema ? lane.guarded.size()
                         : (lane.hasGuardedSource() ? 1 : 0);
      if (!metadataInteger(proof, index++, guardedCount) ||
          guardedCount != expectedCount ||
          (!prioritySchema && lane.guarded.size() > 1))
        return false;
      for (unsigned guardedIndex = 0;
           guardedIndex < guardedCount; ++guardedIndex) {
        const GuardedByteOverlay &overlay =
            lane.guarded[guardedIndex];
        uint64_t priority = overlay.priority;
        uint64_t guardSite = 0;
        uint64_t storeWhenTrue = 0;
        uint64_t storeSite = 0;
        uint64_t guardedSourceByte = 0;
        uint64_t guardedSourceWidth = 0;
        if ((prioritySchema &&
             (!metadataInteger(proof, index++, priority) ||
              priority != overlay.priority)) ||
            !metadataInteger(proof, index++, guardSite) ||
            !metadataInteger(proof, index++, storeWhenTrue) ||
            !metadataInteger(proof, index++, storeSite) ||
            !metadataInteger(proof, index++, guardedSourceByte) ||
            !metadataInteger(proof, index++, guardedSourceWidth) ||
            guardSite != stableSiteId(*overlay.guard) ||
            storeWhenTrue !=
                (overlay.storeWhenTrue ? 1 : 0) ||
            storeSite != stableSiteId(*overlay.store) ||
            guardedSourceByte != overlay.sourceByte ||
            guardedSourceWidth != overlay.sourceWidth)
          return false;
      }
    } else if (
        lane.hasGuardedSource() && !allowUnencodedGuarded) {
      return false;
    }
  }
  return !matchExpression ||
         byteCompositionValueMatches(
             state, incoming, source, &proof);
}

bool symbolicRegionProofRecordMatches(
    const MDNode &proof, unsigned &index,
    const OrderedWriterLayer &layer, unsigned byteWidth) {
  uint64_t storeSite = 0;
  uint64_t storeWidth = 0;
  uint64_t regionBaseSite = 0;
  uint64_t regionExtent = 0;
  uint64_t indexSite = 0;
  uint64_t indexBits = 0;
  uint64_t baseOffset = 0;
  uint64_t laneCount = 0;
  if (layer.kind != OrderedWriterKind::SymbolicRegion ||
      layer.pointerPartition != nullptr ||
      layer.store == nullptr || layer.regionBase == nullptr ||
      layer.symbolicIndex == nullptr || layer.guard != nullptr ||
      !metadataInteger(proof, index++, storeSite) ||
      !metadataInteger(proof, index++, storeWidth) ||
      !metadataInteger(proof, index++, regionBaseSite) ||
      !metadataInteger(proof, index++, regionExtent) ||
      !metadataInteger(proof, index++, indexSite) ||
      !metadataInteger(proof, index++, indexBits) ||
      !metadataInteger(proof, index++, baseOffset) ||
      !metadataInteger(proof, index++, laneCount) ||
      storeSite != stableSiteId(*layer.store) ||
      storeWidth != layer.storeWidth ||
      regionBaseSite != stableSiteId(*layer.regionBase) ||
      regionExtent != layer.regionExtent ||
      indexSite != stableSiteId(*layer.symbolicIndex) ||
      indexBits != layer.symbolicIndexBits ||
      baseOffset !=
          static_cast<uint64_t>(layer.symbolicBaseOffset) ||
      laneCount != byteWidth ||
      layer.symbolicLaneCases.size() != laneCount)
    return false;
  bool hasEffect = false;
  for (unsigned lane = 0; lane < laneCount; ++lane) {
    uint64_t caseCount = 0;
    if (!metadataInteger(proof, index++, caseCount) ||
        caseCount != layer.symbolicLaneCases[lane].size() ||
        caseCount > kMaxByteLaneWidth)
      return false;
    for (const SymbolicRegionCase &item :
         layer.symbolicLaneCases[lane]) {
      uint64_t indexValue = 0;
      uint64_t sourceByte = 0;
      if (!metadataInteger(proof, index++, indexValue) ||
          !metadataInteger(proof, index++, sourceByte) ||
          indexValue != static_cast<uint64_t>(item.indexValue) ||
          sourceByte != item.sourceByte ||
          sourceByte >= layer.storeWidth)
        return false;
      hasEffect = true;
    }
  }
  return hasEffect;
}

bool cyclicByteProofStateMatches(
    const MDNode &proof, unsigned &index, unsigned exitOrdinal,
    const ReachingState &state, Value *incoming,
    const LoadInst &source, bool conditionalSchema) {
  auto *cycleValue = dyn_cast<PHINode>(incoming);
  if (cycleValue == nullptr)
    return false;
  uint64_t byteWidth = 0;
  uint64_t endianness = 0;
  uint64_t skippedCount = 0;
  uint64_t headerSite = 0;
  uint64_t entrySite = 0;
  uint64_t backedgeSite = 0;
  if (!metadataInteger(proof, index++, byteWidth) ||
      !metadataInteger(proof, index++, endianness) ||
      !metadataInteger(proof, index++, skippedCount) ||
      state.cycleEntryState == nullptr ||
      state.memoryPhi == nullptr ||
      state.cycleHeader == nullptr ||
      state.cycleEntry == nullptr ||
      state.cycleBackedge == nullptr ||
      byteWidth != state.byteWidth ||
      endianness != (state.littleEndian ? 0 : 1) ||
      skippedCount != state.skipped.size())
    return false;
  for (Instruction *instruction : state.skipped) {
    uint64_t site = 0;
    if (!metadataInteger(proof, index++, site) ||
        site != stableSiteId(*instruction))
      return false;
  }
  if (!metadataInteger(proof, index++, headerSite) ||
      !metadataInteger(proof, index++, entrySite) ||
      !metadataInteger(proof, index++, backedgeSite) ||
      headerSite != stableSiteId(
                        *state.cycleHeader->getTerminator()) ||
      entrySite != stableSiteId(
                       *state.cycleEntry->getTerminator()) ||
      backedgeSite != stableSiteId(
                          *state.cycleBackedge->getTerminator()) ||
      conditionalSchema != (state.cycleGuard != nullptr))
    return false;
  if (conditionalSchema) {
    uint64_t branchSite = 0;
    uint64_t storeArmSite = 0;
    uint64_t carryArmSite = 0;
    uint64_t guardSite = 0;
    uint64_t storeWhenTrue = 0;
    if (state.cycleBranch == nullptr ||
        state.cycleStoreArm == nullptr ||
        state.cycleCarryArm == nullptr ||
        !metadataInteger(proof, index++, branchSite) ||
        !metadataInteger(proof, index++, storeArmSite) ||
        !metadataInteger(proof, index++, carryArmSite) ||
        !metadataInteger(proof, index++, guardSite) ||
        !metadataInteger(proof, index++, storeWhenTrue) ||
        branchSite != stableSiteId(
                          *state.cycleBranch->getTerminator()) ||
        storeArmSite != stableSiteId(
                           *state.cycleStoreArm->getTerminator()) ||
        carryArmSite != stableSiteId(
                           *state.cycleCarryArm->getTerminator()) ||
        guardSite != stableSiteId(*state.cycleGuard) ||
        storeWhenTrue != (state.cycleStoreWhenTrue ? 1 : 0))
      return false;
  }
  if (!byteProofStateMatches(
          proof, index, exitOrdinal, *state.cycleEntryState,
          incomingValueForBlock(
              *cycleValue, state.cycleEntry),
          source, false))
    return false;
  uint64_t cycleSkippedCount = 0;
  if (!metadataInteger(proof, index++, cycleSkippedCount) ||
      cycleSkippedCount != state.cycleSkipped.size())
    return false;
  for (Instruction *instruction : state.cycleSkipped) {
    uint64_t site = 0;
    if (!metadataInteger(proof, index++, site) ||
        site != stableSiteId(*instruction))
      return false;
  }
  uint64_t laneCount = 0;
  if (!metadataInteger(proof, index++, laneCount) ||
      laneCount != state.byteLanes.size() ||
      laneCount != state.byteWidth)
    return false;
  for (const ByteLaneSource &lane : state.byteLanes) {
    uint64_t ordinal = 0;
    uint64_t kind = 0;
    uint64_t sourceSite = 0;
    uint64_t sourceByte = 0;
    uint64_t sourceWidth = 0;
    if (!metadataInteger(proof, index++, ordinal) ||
        !metadataInteger(proof, index++, kind) ||
        !metadataInteger(proof, index++, sourceSite) ||
        !metadataInteger(proof, index++, sourceByte) ||
        !metadataInteger(proof, index++, sourceWidth) ||
        ordinal != lane.ordinal ||
        kind != (lane.carry ? 2 : 0) ||
        sourceSite !=
            (lane.carry ? 0 : stableSiteId(*lane.store)) ||
        sourceByte != lane.sourceByte ||
        sourceWidth != lane.sourceWidth)
      return false;
  }
  const bool orderedSymbolicRegionSchema =
      hasSchema(
          proof, kMemoryOrderedSymbolicRegionCyclicByteLaneSchema);
  const bool symbolicRegionSchema =
      hasSchema(proof, kMemorySymbolicRegionCyclicByteLaneSchema);
  if (orderedSymbolicRegionSchema) {
    uint64_t writerCount = 0;
    if (symbolicRegionSchema ||
        state.cycleSymbolicRegion != nullptr ||
        state.cycleSymbolicRegions.size() < 2 ||
        state.cycleSymbolicRegions.size() >
            kMaxCyclicSymbolicRegionWriters ||
        !metadataInteger(proof, index++, writerCount) ||
        writerCount != state.cycleSymbolicRegions.size())
      return false;
    const OrderedWriterLayer &newest =
        state.cycleSymbolicRegions.front();
    for (unsigned writerIndex = 0;
         writerIndex < state.cycleSymbolicRegions.size();
         ++writerIndex) {
      const OrderedWriterLayer &writer =
          state.cycleSymbolicRegions[writerIndex];
      if (writer.ordinal != writerIndex ||
          writer.regionBase != newest.regionBase ||
          writer.regionExtent != newest.regionExtent ||
          !symbolicRegionProofRecordMatches(
              proof, index, writer, state.byteWidth))
        return false;
    }
  } else {
    if (!state.cycleSymbolicRegions.empty() ||
        (state.cycleSymbolicRegion != nullptr) !=
            symbolicRegionSchema)
      return false;
    if (state.cycleSymbolicRegion != nullptr &&
        !symbolicRegionProofRecordMatches(
            proof, index, *state.cycleSymbolicRegion,
            state.byteWidth))
      return false;
  }
  return cyclicByteCompositionValueMatches(
      state, incoming, source, &proof);
}

bool multiLatchCyclicByteProofStateMatches(
    const MDNode &proof, unsigned &index, unsigned exitOrdinal,
    const ReachingState &state, Value *incoming,
    const LoadInst &source, bool conditionalSchema,
    bool nestedPredicateSchema, bool symbolicRegionSchema) {
  uint64_t byteWidth = 0;
  uint64_t endianness = 0;
  uint64_t skippedCount = 0;
  uint64_t headerSite = 0;
  uint64_t entrySite = 0;
  if (!metadataInteger(proof, index++, byteWidth) ||
      !metadataInteger(proof, index++, endianness) ||
      !metadataInteger(proof, index++, skippedCount) ||
      state.cycleEntryState == nullptr ||
      state.memoryPhi == nullptr ||
      state.cycleHeader == nullptr ||
      state.cycleEntry == nullptr ||
      state.cycleTransfers.size() <
          (nestedPredicateSchema ? 1U : 2U) ||
      state.cycleTransfers.size() > kMaxCyclicLatches ||
      byteWidth != state.byteWidth ||
      endianness != (state.littleEndian ? 0 : 1) ||
      skippedCount != state.skipped.size())
    return false;
  for (Instruction *instruction : state.skipped) {
    uint64_t site = 0;
    if (!metadataInteger(proof, index++, site) ||
        site != stableSiteId(*instruction))
      return false;
  }
  auto *cycleValue = dyn_cast<PHINode>(incoming);
  if (cycleValue == nullptr ||
      !metadataInteger(proof, index++, headerSite) ||
      !metadataInteger(proof, index++, entrySite) ||
      headerSite != stableSiteId(
                        *state.cycleHeader->getTerminator()) ||
      entrySite != stableSiteId(
                       *state.cycleEntry->getTerminator()) ||
      !byteProofStateMatches(
          proof, index, exitOrdinal, *state.cycleEntryState,
          incomingValueForBlock(
              *cycleValue, state.cycleEntry),
          source, false))
    return false;
  uint64_t transferCount = 0;
  if (!metadataInteger(proof, index++, transferCount) ||
      transferCount != state.cycleTransfers.size())
    return false;
  bool hasSymbolicRegion = false;
  for (const CyclicBackedgeTransfer &transfer :
       state.cycleTransfers) {
    uint64_t backedgeSite = 0;
    uint64_t transferSkippedCount = 0;
    if (!metadataInteger(proof, index++, backedgeSite) ||
        backedgeSite != stableSiteId(
                            *transfer.block->getTerminator()))
      return false;
    if (nestedPredicateSchema) {
      uint64_t kind = 0;
      const uint64_t expectedKind =
          transfer.hasPredicateTree()
              ? 2
              : (transfer.guard != nullptr ? 1 : 0);
      if (!metadataInteger(proof, index++, kind) ||
          kind != expectedKind)
        return false;
      if (kind == 2) {
        uint64_t nodeCount = 0;
        uint64_t leafCount = 0;
        if (!metadataInteger(proof, index++, nodeCount) ||
            !metadataInteger(proof, index++, leafCount) ||
            nodeCount != transfer.predicateNodes.size() ||
            leafCount != transfer.predicateLeaves.size())
          return false;
        for (const CyclicPredicateNode &node :
             transfer.predicateNodes) {
          uint64_t branchSite = 0;
          uint64_t guardSite = 0;
          uint64_t trueKind = 0;
          uint64_t trueIndex = 0;
          uint64_t falseKind = 0;
          uint64_t falseIndex = 0;
          if (!metadataInteger(proof, index++, branchSite) ||
              !metadataInteger(proof, index++, guardSite) ||
              !metadataInteger(proof, index++, trueKind) ||
              !metadataInteger(proof, index++, trueIndex) ||
              !metadataInteger(proof, index++, falseKind) ||
              !metadataInteger(proof, index++, falseIndex) ||
              branchSite != stableSiteId(
                                *node.block->getTerminator()) ||
              guardSite != stableSiteId(*node.guard) ||
              trueKind != (node.trueChild.leaf ? 1 : 0) ||
              trueIndex != node.trueChild.index ||
              falseKind != (node.falseChild.leaf ? 1 : 0) ||
              falseIndex != node.falseChild.index)
            return false;
        }
        for (unsigned leafIndex = 0;
             leafIndex < transfer.predicateLeaves.size();
             ++leafIndex) {
          const CyclicPredicateLeaf &leaf =
              transfer.predicateLeaves[leafIndex];
          uint64_t ordinal = 0;
          uint64_t leafSite = 0;
          uint64_t leafSkippedCount = 0;
          uint64_t laneCount = 0;
          if (!metadataInteger(proof, index++, ordinal) ||
              !metadataInteger(proof, index++, leafSite) ||
              ordinal != leafIndex ||
              leafSite != stableSiteId(
                              *leaf.block->getTerminator()) ||
              !metadataInteger(
                  proof, index++, leafSkippedCount) ||
              leafSkippedCount != leaf.skipped.size())
            return false;
          for (Instruction *instruction : leaf.skipped) {
            uint64_t site = 0;
            if (!metadataInteger(proof, index++, site) ||
                site != stableSiteId(*instruction))
              return false;
          }
          if (!metadataInteger(proof, index++, laneCount) ||
              laneCount != leaf.byteLanes.size() ||
              laneCount != state.byteWidth)
            return false;
          for (const ByteLaneSource &lane : leaf.byteLanes) {
            uint64_t ordinalValue = 0;
            uint64_t laneKind = 0;
            uint64_t sourceSite = 0;
            uint64_t sourceByte = 0;
            uint64_t sourceWidth = 0;
            if (!metadataInteger(
                    proof, index++, ordinalValue) ||
                !metadataInteger(proof, index++, laneKind) ||
                !metadataInteger(proof, index++, sourceSite) ||
                !metadataInteger(proof, index++, sourceByte) ||
                !metadataInteger(proof, index++, sourceWidth) ||
                ordinalValue != lane.ordinal ||
                laneKind != (lane.carry ? 2 : 0) ||
                sourceSite !=
                    (lane.carry ? 0
                                : stableSiteId(*lane.store)) ||
                sourceByte != lane.sourceByte ||
                sourceWidth != lane.sourceWidth)
              return false;
          }
        }
        continue;
      }
      if (kind == 1) {
        uint64_t branchSite = 0;
        uint64_t storeArmSite = 0;
        uint64_t carryArmSite = 0;
        uint64_t guardSite = 0;
        uint64_t storeWhenTrue = 0;
        if (transfer.branch == nullptr ||
            transfer.storeArm == nullptr ||
            transfer.carryArm == nullptr ||
            !metadataInteger(proof, index++, branchSite) ||
            !metadataInteger(proof, index++, storeArmSite) ||
            !metadataInteger(proof, index++, carryArmSite) ||
            !metadataInteger(proof, index++, guardSite) ||
            !metadataInteger(proof, index++, storeWhenTrue) ||
            branchSite != stableSiteId(
                              *transfer.branch->getTerminator()) ||
            storeArmSite != stableSiteId(
                               *transfer.storeArm->getTerminator()) ||
            carryArmSite != stableSiteId(
                               *transfer.carryArm->getTerminator()) ||
            guardSite != stableSiteId(*transfer.guard) ||
            storeWhenTrue !=
                (transfer.storeWhenTrue ? 1 : 0))
          return false;
      }
    } else if (conditionalSchema) {
      uint64_t conditional = 0;
      if (!metadataInteger(proof, index++, conditional) ||
          conditional != (transfer.guard != nullptr ? 1 : 0))
        return false;
      if (conditional != 0) {
        uint64_t branchSite = 0;
        uint64_t storeArmSite = 0;
        uint64_t carryArmSite = 0;
        uint64_t guardSite = 0;
        uint64_t storeWhenTrue = 0;
        if (transfer.branch == nullptr ||
            transfer.storeArm == nullptr ||
            transfer.carryArm == nullptr ||
            !metadataInteger(proof, index++, branchSite) ||
            !metadataInteger(proof, index++, storeArmSite) ||
            !metadataInteger(proof, index++, carryArmSite) ||
            !metadataInteger(proof, index++, guardSite) ||
            !metadataInteger(proof, index++, storeWhenTrue) ||
            branchSite != stableSiteId(
                              *transfer.branch->getTerminator()) ||
            storeArmSite != stableSiteId(
                               *transfer.storeArm->getTerminator()) ||
            carryArmSite != stableSiteId(
                               *transfer.carryArm->getTerminator()) ||
            guardSite != stableSiteId(*transfer.guard) ||
            storeWhenTrue !=
                (transfer.storeWhenTrue ? 1 : 0))
          return false;
      }
    } else if (transfer.guard != nullptr ||
               transfer.hasPredicateTree()) {
      return false;
    }
    if (
        !metadataInteger(
            proof, index++, transferSkippedCount) ||
        transferSkippedCount != transfer.skipped.size())
      return false;
    for (Instruction *instruction : transfer.skipped) {
      uint64_t site = 0;
      if (!metadataInteger(proof, index++, site) ||
          site != stableSiteId(*instruction))
        return false;
    }
    uint64_t laneCount = 0;
    if (!metadataInteger(proof, index++, laneCount) ||
        laneCount != transfer.byteLanes.size() ||
        laneCount != state.byteWidth)
      return false;
    for (const ByteLaneSource &lane :
         transfer.byteLanes) {
      uint64_t ordinal = 0;
      uint64_t kind = 0;
      uint64_t sourceSite = 0;
      uint64_t sourceByte = 0;
      uint64_t sourceWidth = 0;
      if (!metadataInteger(proof, index++, ordinal) ||
          !metadataInteger(proof, index++, kind) ||
          !metadataInteger(proof, index++, sourceSite) ||
          !metadataInteger(proof, index++, sourceByte) ||
          !metadataInteger(proof, index++, sourceWidth) ||
          ordinal != lane.ordinal ||
          kind != (lane.carry ? 2 : 0) ||
          sourceSite !=
              (lane.carry ? 0 : stableSiteId(*lane.store)) ||
          sourceByte != lane.sourceByte ||
          sourceWidth != lane.sourceWidth)
        return false;
    }
    if (symbolicRegionSchema) {
      uint64_t hasWriter = 0;
      if (!metadataInteger(proof, index++, hasWriter) ||
          hasWriter != (transfer.symbolicRegion != nullptr ? 1 : 0))
        return false;
      if (transfer.symbolicRegion != nullptr) {
        if (!std::all_of(
                transfer.byteLanes.begin(),
                transfer.byteLanes.end(),
                [](const ByteLaneSource &lane) {
                  return lane.carry && lane.store == nullptr;
                }) ||
            !symbolicRegionProofRecordMatches(
                proof, index, *transfer.symbolicRegion,
                state.byteWidth))
          return false;
        hasSymbolicRegion = true;
      }
    } else if (transfer.symbolicRegion != nullptr) {
      return false;
    }
  }
  if (symbolicRegionSchema != hasSymbolicRegion)
    return false;
  return multiLatchCyclicByteCompositionValueMatches(
      state, incoming, source, &proof);
}

void appendByteProofState(SmallVectorImpl<Metadata *> &proof,
                          LLVMContext &context, unsigned exitOrdinal,
                          const ReachingState &state,
                          bool guardedSchema,
                          bool prioritySchema = false) {
  proof.push_back(integerMetadata(context, 32, exitOrdinal));
  proof.push_back(integerMetadata(context, 32, state.byteWidth));
  proof.push_back(integerMetadata(
      context, 32, state.littleEndian ? 0 : 1));
  proof.push_back(
      integerMetadata(context, 32, state.skipped.size()));
  for (Instruction *instruction : state.skipped)
    proof.push_back(
        integerMetadata(context, 64, stableSiteId(*instruction)));
  proof.push_back(
      integerMetadata(context, 32, state.byteLanes.size()));
  for (const ByteLaneSource &lane : state.byteLanes) {
    proof.push_back(integerMetadata(context, 32, lane.ordinal));
    proof.push_back(integerMetadata(
        context, 32, static_cast<uint64_t>(lane.kind())));
    proof.push_back(integerMetadata(
        context, 64,
        lane.store == nullptr ? 0 : stableSiteId(*lane.store)));
    proof.push_back(integerMetadata(context, 32, lane.sourceByte));
    proof.push_back(integerMetadata(context, 32, lane.sourceWidth));
    if (guardedSchema) {
      proof.push_back(integerMetadata(
          context, 32,
          prioritySchema ? lane.guarded.size()
                         : (lane.hasGuardedSource() ? 1 : 0)));
      const unsigned guardedCount =
          prioritySchema ? lane.guarded.size()
                         : (lane.hasGuardedSource() ? 1 : 0);
      for (unsigned guardedIndex = 0;
           guardedIndex < guardedCount; ++guardedIndex) {
        const GuardedByteOverlay &overlay =
            lane.guarded[guardedIndex];
        if (prioritySchema)
          proof.push_back(integerMetadata(
              context, 32, overlay.priority));
        proof.push_back(integerMetadata(
            context, 64, stableSiteId(*overlay.guard)));
        proof.push_back(integerMetadata(
            context, 32,
            overlay.storeWhenTrue ? 1 : 0));
        proof.push_back(integerMetadata(
            context, 64, stableSiteId(*overlay.store)));
        proof.push_back(integerMetadata(
            context, 32, overlay.sourceByte));
        proof.push_back(integerMetadata(
            context, 32, overlay.sourceWidth));
      }
    }
  }
}

void appendSymbolicRegionProofRecord(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    const OrderedWriterLayer &layer) {
  proof.push_back(integerMetadata(
      context, 64, stableSiteId(*layer.store)));
  proof.push_back(integerMetadata(
      context, 32, layer.storeWidth));
  proof.push_back(integerMetadata(
      context, 64, stableSiteId(*layer.regionBase)));
  proof.push_back(integerMetadata(
      context, 64, layer.regionExtent));
  proof.push_back(integerMetadata(
      context, 64, stableSiteId(*layer.symbolicIndex)));
  proof.push_back(integerMetadata(
      context, 32, layer.symbolicIndexBits));
  proof.push_back(integerMetadata(
      context, 64,
      static_cast<uint64_t>(layer.symbolicBaseOffset)));
  proof.push_back(integerMetadata(
      context, 32, layer.symbolicLaneCases.size()));
  for (const auto &laneCases : layer.symbolicLaneCases) {
    proof.push_back(integerMetadata(
        context, 32, laneCases.size()));
    for (const SymbolicRegionCase &item : laneCases) {
      proof.push_back(integerMetadata(
          context, 64,
          static_cast<uint64_t>(item.indexValue)));
      proof.push_back(integerMetadata(
          context, 32, item.sourceByte));
    }
  }
}

void appendCyclicByteProofState(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    unsigned exitOrdinal, const ReachingState &state,
    bool conditionalSchema) {
  proof.push_back(
      integerMetadata(context, 32, state.byteWidth));
  proof.push_back(integerMetadata(
      context, 32, state.littleEndian ? 0 : 1));
  proof.push_back(
      integerMetadata(context, 32, state.skipped.size()));
  for (Instruction *instruction : state.skipped)
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*instruction)));
  proof.push_back(integerMetadata(
      context, 64,
      stableSiteId(*state.cycleHeader->getTerminator())));
  proof.push_back(integerMetadata(
      context, 64,
      stableSiteId(*state.cycleEntry->getTerminator())));
  proof.push_back(integerMetadata(
      context, 64,
      stableSiteId(*state.cycleBackedge->getTerminator())));
  if (conditionalSchema) {
    proof.push_back(integerMetadata(
        context, 64,
        stableSiteId(*state.cycleBranch->getTerminator())));
    proof.push_back(integerMetadata(
        context, 64,
        stableSiteId(*state.cycleStoreArm->getTerminator())));
    proof.push_back(integerMetadata(
        context, 64,
        stableSiteId(*state.cycleCarryArm->getTerminator())));
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*state.cycleGuard)));
    proof.push_back(integerMetadata(
        context, 32, state.cycleStoreWhenTrue ? 1 : 0));
  }
  appendByteProofState(
      proof, context, exitOrdinal,
      *state.cycleEntryState, false);
  proof.push_back(integerMetadata(
      context, 32, state.cycleSkipped.size()));
  for (Instruction *instruction : state.cycleSkipped)
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*instruction)));
  proof.push_back(integerMetadata(
      context, 32, state.byteLanes.size()));
  for (const ByteLaneSource &lane : state.byteLanes) {
    proof.push_back(
        integerMetadata(context, 32, lane.ordinal));
    proof.push_back(integerMetadata(
        context, 32, lane.carry ? 2 : 0));
    proof.push_back(integerMetadata(
        context, 64,
        lane.carry ? 0 : stableSiteId(*lane.store)));
    proof.push_back(
        integerMetadata(context, 32, lane.sourceByte));
    proof.push_back(
        integerMetadata(context, 32, lane.sourceWidth));
  }
  if (!state.cycleSymbolicRegions.empty()) {
    proof.push_back(integerMetadata(
        context, 32, state.cycleSymbolicRegions.size()));
    for (const OrderedWriterLayer &writer :
         state.cycleSymbolicRegions)
      appendSymbolicRegionProofRecord(
          proof, context, writer);
  } else if (state.cycleSymbolicRegion != nullptr) {
    appendSymbolicRegionProofRecord(
        proof, context, *state.cycleSymbolicRegion);
  }
}

void appendMultiLatchCyclicByteProofState(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    unsigned exitOrdinal, const ReachingState &state,
    bool conditionalSchema, bool nestedPredicateSchema,
    bool symbolicRegionSchema) {
  proof.push_back(
      integerMetadata(context, 32, state.byteWidth));
  proof.push_back(integerMetadata(
      context, 32, state.littleEndian ? 0 : 1));
  proof.push_back(
      integerMetadata(context, 32, state.skipped.size()));
  for (Instruction *instruction : state.skipped)
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*instruction)));
  proof.push_back(integerMetadata(
      context, 64,
      stableSiteId(*state.cycleHeader->getTerminator())));
  proof.push_back(integerMetadata(
      context, 64,
      stableSiteId(*state.cycleEntry->getTerminator())));
  appendByteProofState(
      proof, context, exitOrdinal,
      *state.cycleEntryState, false);
  proof.push_back(integerMetadata(
      context, 32, state.cycleTransfers.size()));
  for (const CyclicBackedgeTransfer &transfer :
       state.cycleTransfers) {
    proof.push_back(integerMetadata(
        context, 64,
        stableSiteId(*transfer.block->getTerminator())));
    if (nestedPredicateSchema) {
      const uint64_t kind =
          transfer.hasPredicateTree()
              ? 2
              : (transfer.guard != nullptr ? 1 : 0);
      proof.push_back(integerMetadata(context, 32, kind));
      if (kind == 2) {
        proof.push_back(integerMetadata(
            context, 32, transfer.predicateNodes.size()));
        proof.push_back(integerMetadata(
            context, 32, transfer.predicateLeaves.size()));
        for (const CyclicPredicateNode &node :
             transfer.predicateNodes) {
          proof.push_back(integerMetadata(
              context, 64,
              stableSiteId(*node.block->getTerminator())));
          proof.push_back(integerMetadata(
              context, 64, stableSiteId(*node.guard)));
          proof.push_back(integerMetadata(
              context, 32, node.trueChild.leaf ? 1 : 0));
          proof.push_back(integerMetadata(
              context, 32, node.trueChild.index));
          proof.push_back(integerMetadata(
              context, 32, node.falseChild.leaf ? 1 : 0));
          proof.push_back(integerMetadata(
              context, 32, node.falseChild.index));
        }
        for (unsigned leafIndex = 0;
             leafIndex < transfer.predicateLeaves.size();
             ++leafIndex) {
          const CyclicPredicateLeaf &leaf =
              transfer.predicateLeaves[leafIndex];
          proof.push_back(
              integerMetadata(context, 32, leafIndex));
          proof.push_back(integerMetadata(
              context, 64,
              stableSiteId(*leaf.block->getTerminator())));
          proof.push_back(integerMetadata(
              context, 32, leaf.skipped.size()));
          for (Instruction *instruction : leaf.skipped)
            proof.push_back(integerMetadata(
                context, 64, stableSiteId(*instruction)));
          proof.push_back(integerMetadata(
              context, 32, leaf.byteLanes.size()));
          for (const ByteLaneSource &lane : leaf.byteLanes) {
            proof.push_back(integerMetadata(
                context, 32, lane.ordinal));
            proof.push_back(integerMetadata(
                context, 32, lane.carry ? 2 : 0));
            proof.push_back(integerMetadata(
                context, 64,
                lane.carry ? 0 : stableSiteId(*lane.store)));
            proof.push_back(integerMetadata(
                context, 32, lane.sourceByte));
            proof.push_back(integerMetadata(
                context, 32, lane.sourceWidth));
          }
        }
        continue;
      }
      if (kind == 1) {
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.branch->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.storeArm->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.carryArm->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64, stableSiteId(*transfer.guard)));
        proof.push_back(integerMetadata(
            context, 32,
            transfer.storeWhenTrue ? 1 : 0));
      }
    } else if (conditionalSchema) {
      proof.push_back(integerMetadata(
          context, 32, transfer.guard != nullptr ? 1 : 0));
      if (transfer.guard != nullptr) {
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.branch->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.storeArm->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64,
            stableSiteId(*transfer.carryArm->getTerminator())));
        proof.push_back(integerMetadata(
            context, 64, stableSiteId(*transfer.guard)));
        proof.push_back(integerMetadata(
            context, 32,
            transfer.storeWhenTrue ? 1 : 0));
      }
    }
    proof.push_back(integerMetadata(
        context, 32, transfer.skipped.size()));
    for (Instruction *instruction : transfer.skipped)
      proof.push_back(integerMetadata(
          context, 64, stableSiteId(*instruction)));
    proof.push_back(integerMetadata(
        context, 32, transfer.byteLanes.size()));
    for (const ByteLaneSource &lane :
         transfer.byteLanes) {
      proof.push_back(
          integerMetadata(context, 32, lane.ordinal));
      proof.push_back(integerMetadata(
          context, 32, lane.carry ? 2 : 0));
      proof.push_back(integerMetadata(
          context, 64,
          lane.carry ? 0 : stableSiteId(*lane.store)));
      proof.push_back(integerMetadata(
          context, 32, lane.sourceByte));
      proof.push_back(integerMetadata(
          context, 32, lane.sourceWidth));
    }
    if (symbolicRegionSchema) {
      proof.push_back(integerMetadata(
          context, 32,
          transfer.symbolicRegion != nullptr ? 1 : 0));
      if (transfer.symbolicRegion != nullptr)
        appendSymbolicRegionProofRecord(
            proof, context, *transfer.symbolicRegion);
    }
  }
}

bool pointerPartitionProofRecordMatches(
    const MDNode &proof, unsigned &index,
    const PointerPartition &partition) {
  uint64_t storeSite = 0;
  uint64_t storeWidth = 0;
  uint64_t nodeCount = 0;
  uint64_t leafCount = 0;
  if (!metadataInteger(proof, index++, storeSite) ||
      !metadataInteger(proof, index++, storeWidth) ||
      !metadataInteger(proof, index++, nodeCount) ||
      !metadataInteger(proof, index++, leafCount) ||
      storeSite != stableSiteId(*partition.store) ||
      storeWidth != partition.storeWidth ||
      nodeCount != partition.nodes.size() ||
      leafCount != partition.leaves.size())
    return false;
  for (const PointerPartitionNode &node : partition.nodes) {
    uint64_t selectSite = 0;
    uint64_t guardSite = 0;
    uint64_t trueLeaf = 0;
    uint64_t trueIndex = 0;
    uint64_t falseLeaf = 0;
    uint64_t falseIndex = 0;
    if (!metadataInteger(proof, index++, selectSite) ||
        !metadataInteger(proof, index++, guardSite) ||
        !metadataInteger(proof, index++, trueLeaf) ||
        !metadataInteger(proof, index++, trueIndex) ||
        !metadataInteger(proof, index++, falseLeaf) ||
        !metadataInteger(proof, index++, falseIndex) ||
        selectSite != stableSiteId(*node.select) ||
        guardSite != stableSiteId(*node.guard) ||
        trueLeaf != (node.trueChild.leaf ? 1 : 0) ||
        trueIndex != node.trueChild.index ||
        falseLeaf != (node.falseChild.leaf ? 1 : 0) ||
        falseIndex != node.falseChild.index)
      return false;
  }
  for (unsigned leafIndex = 0;
       leafIndex < partition.leaves.size(); ++leafIndex) {
    uint64_t ordinal = 0;
    if (!metadataInteger(proof, index++, ordinal) ||
        ordinal != leafIndex)
      return false;
    const PointerPartitionLeaf &leaf =
        partition.leaves[leafIndex];
    for (int sourceByte : leaf.sourceBytes) {
      uint64_t encoded = 0;
      if (!metadataInteger(proof, index++, encoded) ||
          encoded != static_cast<uint64_t>(sourceByte + 1))
        return false;
    }
  }
  return true;
}

bool pointerPartitionProofMatches(
    const MDNode &proof, unsigned &index,
    const ReachingState &state, Value *incoming,
    const LoadInst &source) {
  return state.pointerPartition != nullptr &&
         pointerPartitionProofRecordMatches(
             proof, index, *state.pointerPartition) &&
         pointerPartitionValueMatches(
             state, incoming, source, &proof);
}

void appendPointerPartitionProofRecord(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    const PointerPartition &partition) {
  proof.push_back(integerMetadata(
      context, 64, stableSiteId(*partition.store)));
  proof.push_back(integerMetadata(
      context, 32, partition.storeWidth));
  proof.push_back(integerMetadata(
      context, 32, partition.nodes.size()));
  proof.push_back(integerMetadata(
      context, 32, partition.leaves.size()));
  for (const PointerPartitionNode &node : partition.nodes) {
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*node.select)));
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*node.guard)));
    proof.push_back(integerMetadata(
        context, 32, node.trueChild.leaf ? 1 : 0));
    proof.push_back(integerMetadata(
        context, 32, node.trueChild.index));
    proof.push_back(integerMetadata(
        context, 32, node.falseChild.leaf ? 1 : 0));
    proof.push_back(integerMetadata(
        context, 32, node.falseChild.index));
  }
  for (unsigned leafIndex = 0;
       leafIndex < partition.leaves.size(); ++leafIndex) {
    proof.push_back(
        integerMetadata(context, 32, leafIndex));
    for (int sourceByte :
         partition.leaves[leafIndex].sourceBytes)
      proof.push_back(integerMetadata(
          context, 32,
          static_cast<uint64_t>(sourceByte + 1)));
  }
}

void appendPointerPartitionProof(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    const ReachingState &state) {
  appendPointerPartitionProofRecord(
      proof, context, *state.pointerPartition);
}

void appendOrderedWriterGraphProof(
    SmallVectorImpl<Metadata *> &proof, LLVMContext &context,
    unsigned exitOrdinal, const ReachingState &state) {
  appendByteProofState(
      proof, context, exitOrdinal, state, false);
  proof.push_back(integerMetadata(
      context, 32, state.writerLayers.size()));
  for (const OrderedWriterLayer &layer : state.writerLayers) {
    proof.push_back(
        integerMetadata(context, 32, layer.ordinal));
    proof.push_back(integerMetadata(
        context, 32, static_cast<uint64_t>(layer.kind)));
    if (layer.kind == OrderedWriterKind::PointerPartition) {
      appendPointerPartitionProofRecord(
          proof, context, *layer.pointerPartition);
      continue;
    }
    if (layer.kind == OrderedWriterKind::SymbolicRegion) {
      appendSymbolicRegionProofRecord(proof, context, layer);
      continue;
    }
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*layer.store)));
    proof.push_back(
        integerMetadata(context, 32, layer.storeWidth));
    proof.push_back(integerMetadata(
        context, 64, stableSiteId(*layer.guard)));
    proof.push_back(integerMetadata(
        context, 32, layer.sourceBytes.size()));
    for (unsigned lane = 0; lane < layer.sourceBytes.size(); ++lane) {
      proof.push_back(integerMetadata(
          context, 32,
          static_cast<uint64_t>(layer.sourceBytes[lane] + 1)));
      proof.push_back(integerMetadata(
          context, 32,
          static_cast<uint64_t>(
              layer.storePolarities[lane] + 1)));
    }
  }
}

bool orderedWriterGraphProofMatches(
    const MDNode &proof, unsigned &index, unsigned exitOrdinal,
    const ReachingState &state, Value *incoming,
    const LoadInst &source) {
  if (!byteProofStateMatches(
          proof, index, exitOrdinal, state, incoming, source,
          false, false, false, true))
    return false;
  uint64_t layerCount = 0;
  if (!metadataInteger(proof, index++, layerCount) ||
      layerCount != state.writerLayers.size() ||
      layerCount > kMaxOrderedWriterLayers)
    return false;
  unsigned partitions = 0;
  unsigned guards = 0;
  unsigned symbolicRegions = 0;
  for (unsigned layerIndex = 0;
       layerIndex < state.writerLayers.size(); ++layerIndex) {
    const OrderedWriterLayer &layer =
        state.writerLayers[layerIndex];
    uint64_t ordinal = 0;
    uint64_t kind = 0;
    if (!metadataInteger(proof, index++, ordinal) ||
        !metadataInteger(proof, index++, kind) ||
        ordinal != layerIndex || ordinal != layer.ordinal ||
        kind != static_cast<uint64_t>(layer.kind))
      return false;
    if (layer.kind == OrderedWriterKind::PointerPartition) {
      ++partitions;
      if (layer.pointerPartition == nullptr ||
          layer.pointerPartition->store != layer.store ||
          layer.pointerPartition->storeWidth != layer.storeWidth ||
          !pointerPartitionProofRecordMatches(
              proof, index, *layer.pointerPartition))
        return false;
      continue;
    }
    if (layer.kind == OrderedWriterKind::SymbolicRegion) {
      ++symbolicRegions;
      uint64_t storeSite = 0;
      uint64_t storeWidth = 0;
      uint64_t regionBaseSite = 0;
      uint64_t regionExtent = 0;
      uint64_t indexSite = 0;
      uint64_t indexBits = 0;
      uint64_t baseOffset = 0;
      uint64_t laneCount = 0;
      if (layer.pointerPartition != nullptr ||
          layer.store == nullptr || layer.regionBase == nullptr ||
          layer.symbolicIndex == nullptr || layer.guard != nullptr ||
          !metadataInteger(proof, index++, storeSite) ||
          !metadataInteger(proof, index++, storeWidth) ||
          !metadataInteger(proof, index++, regionBaseSite) ||
          !metadataInteger(proof, index++, regionExtent) ||
          !metadataInteger(proof, index++, indexSite) ||
          !metadataInteger(proof, index++, indexBits) ||
          !metadataInteger(proof, index++, baseOffset) ||
          !metadataInteger(proof, index++, laneCount) ||
          storeSite != stableSiteId(*layer.store) ||
          storeWidth != layer.storeWidth ||
          regionBaseSite != stableSiteId(*layer.regionBase) ||
          regionExtent != layer.regionExtent ||
          indexSite != stableSiteId(*layer.symbolicIndex) ||
          indexBits != layer.symbolicIndexBits ||
          baseOffset !=
              static_cast<uint64_t>(layer.symbolicBaseOffset) ||
          laneCount != state.byteWidth ||
          layer.symbolicLaneCases.size() != laneCount)
        return false;
      bool hasEffect = false;
      for (unsigned lane = 0; lane < laneCount; ++lane) {
        uint64_t caseCount = 0;
        if (!metadataInteger(proof, index++, caseCount) ||
            caseCount != layer.symbolicLaneCases[lane].size() ||
            caseCount > kMaxByteLaneWidth)
          return false;
        for (const SymbolicRegionCase &item :
             layer.symbolicLaneCases[lane]) {
          uint64_t indexValue = 0;
          uint64_t sourceByte = 0;
          if (!metadataInteger(proof, index++, indexValue) ||
              !metadataInteger(proof, index++, sourceByte) ||
              indexValue !=
                  static_cast<uint64_t>(item.indexValue) ||
              sourceByte != item.sourceByte ||
              sourceByte >= layer.storeWidth)
            return false;
          hasEffect = true;
        }
      }
      if (!hasEffect)
        return false;
      continue;
    }
    ++guards;
    uint64_t storeSite = 0;
    uint64_t storeWidth = 0;
    uint64_t guardSite = 0;
    uint64_t laneCount = 0;
    if (layer.kind != OrderedWriterKind::Guarded ||
        layer.pointerPartition != nullptr ||
        layer.store == nullptr || layer.guard == nullptr ||
        !metadataInteger(proof, index++, storeSite) ||
        !metadataInteger(proof, index++, storeWidth) ||
        !metadataInteger(proof, index++, guardSite) ||
        !metadataInteger(proof, index++, laneCount) ||
        storeSite != stableSiteId(*layer.store) ||
        storeWidth != layer.storeWidth ||
        guardSite != stableSiteId(*layer.guard) ||
        laneCount != state.byteWidth ||
        layer.sourceBytes.size() != laneCount ||
        layer.storePolarities.size() != laneCount)
      return false;
    bool hasEffect = false;
    for (unsigned lane = 0; lane < laneCount; ++lane) {
      uint64_t sourceByte = 0;
      uint64_t polarity = 0;
      if (!metadataInteger(proof, index++, sourceByte) ||
          !metadataInteger(proof, index++, polarity) ||
          sourceByte != static_cast<uint64_t>(
                            layer.sourceBytes[lane] + 1) ||
          polarity != static_cast<uint64_t>(
                          layer.storePolarities[lane] + 1))
        return false;
      if (layer.sourceBytes[lane] < 0) {
        if (layer.storePolarities[lane] != -1)
          return false;
      } else {
        if (layer.storePolarities[lane] < 0 ||
            layer.storePolarities[lane] > 1 ||
            static_cast<unsigned>(layer.sourceBytes[lane]) >=
                layer.storeWidth)
          return false;
        hasEffect = true;
      }
    }
    if (!hasEffect)
      return false;
  }
  const bool extendedGraph =
      state.requiresSymbolicRegionWriterGraph();
  return partitions <=
             (extendedGraph ? kMaxOrderedWriterLayers
                            : kLegacyOrderedPointerPartitions) &&
         guards <=
             (extendedGraph ? kMaxOrderedWriterLayers
                            : kLegacyOrderedGuardedWriters) &&
         symbolicRegions <= kMaxOrderedWriterLayers &&
         orderedWriterGraphValueMatches(
             state, incoming, source, &proof);
}

bool memoryProofMatches(const PHINode &phi,
                        const MemoryCandidate &candidate,
                        unsigned ordinal, uint64_t loadSite) {
  const MDNode *proof =
      phi.getMetadata("symcc.ifss_continuation_memory");
  if (proof == nullptr)
    return false;
  const bool symbolicRegionWriterGraph =
      hasSchema(*proof, kMemorySymbolicRegionWriterGraphSchema);
  const bool orderedWriterGraph =
      symbolicRegionWriterGraph ||
      hasSchema(*proof, kMemoryOrderedWriterGraphSchema);
  const bool pointerPartition =
      (!symbolicRegionWriterGraph && orderedWriterGraph) ||
      hasSchema(*proof, kMemoryPointerPartitionSchema) ||
      hasSchema(*proof, kMemoryPointerPartitionPrioritySchema);
  const bool pointerPartitionPriority =
      hasSchema(*proof, kMemoryPointerPartitionPrioritySchema);
  const bool guardedWritePriority =
      hasSchema(*proof, kMemoryGuardedPrioritySchema);
  const bool orderedSymbolicRegionCyclicByteComposition =
      hasSchema(
          *proof, kMemoryOrderedSymbolicRegionCyclicByteLaneSchema);
  const bool symbolicRegionCyclicByteComposition =
      orderedSymbolicRegionCyclicByteComposition ||
      hasSchema(
          *proof, kMemorySymbolicRegionCyclicByteLaneSchema);
  const bool symbolicRegionMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema);
  const bool conditionalCyclicByteComposition =
      hasSchema(*proof, kMemoryConditionalCyclicByteLaneSchema);
  const bool conditionalMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemoryConditionalMultiLatchCyclicByteLaneSchema);
  const bool boundedMultiLatchCyclicByteComposition =
      hasSchema(
          *proof,
          kMemoryBoundedMultiLatchCyclicByteLaneSchema);
  const bool nestedPredicateCyclicByteComposition =
      hasSchema(
          *proof, kMemoryNestedPredicateCyclicByteLaneSchema);
  const bool multiLatchCyclicByteComposition =
      symbolicRegionMultiLatchCyclicByteComposition ||
      nestedPredicateCyclicByteComposition ||
      boundedMultiLatchCyclicByteComposition ||
      conditionalMultiLatchCyclicByteComposition ||
      hasSchema(*proof, kMemoryMultiLatchCyclicByteLaneSchema);
  const bool cyclicByteComposition =
      symbolicRegionCyclicByteComposition ||
      multiLatchCyclicByteComposition ||
      conditionalCyclicByteComposition ||
      hasSchema(*proof, kMemoryCyclicByteLaneSchema);
  const bool guardedByteComposition =
      guardedWritePriority ||
      pointerPartitionPriority ||
      hasSchema(*proof, kMemoryGuardedByteLaneSchema);
  const bool byteComposition =
      orderedWriterGraph || pointerPartition ||
      cyclicByteComposition ||
      guardedByteComposition ||
      hasSchema(*proof, kMemoryByteLaneSchema);
  const bool nested = hasSchema(*proof, kMemoryNestedSchema);
  const bool extended =
      nested || hasSchema(*proof, kMemoryInitialSchema);
  if (byteComposition != candidate.hasByteLaneComposition() ||
      cyclicByteComposition !=
          candidate.hasCyclicByteLaneComposition() ||
      symbolicRegionCyclicByteComposition !=
          candidate.hasSymbolicRegionCyclicByteLaneComposition() ||
      symbolicRegionMultiLatchCyclicByteComposition !=
          candidate
              .hasSymbolicRegionMultiLatchCyclicByteLaneComposition() ||
      conditionalCyclicByteComposition !=
          candidate.hasConditionalCyclicByteLaneComposition() ||
      multiLatchCyclicByteComposition !=
          candidate.hasMultiLatchCyclicByteLaneComposition() ||
      (!nestedPredicateCyclicByteComposition &&
       !boundedMultiLatchCyclicByteComposition &&
       !symbolicRegionMultiLatchCyclicByteComposition &&
       conditionalMultiLatchCyclicByteComposition !=
           candidate
               .hasConditionalMultiLatchCyclicByteLaneComposition()) ||
      (!nestedPredicateCyclicByteComposition &&
       !symbolicRegionMultiLatchCyclicByteComposition &&
       boundedMultiLatchCyclicByteComposition !=
           candidate
               .hasBoundedMultiLatchCyclicByteLaneComposition()) ||
      nestedPredicateCyclicByteComposition !=
          candidate
              .hasNestedPredicateCyclicByteLaneComposition() ||
      (!orderedWriterGraph &&
       pointerPartition != candidate.hasPointerPartition()) ||
      orderedWriterGraph !=
          candidate.hasOrderedWriterGraphComposition() ||
      symbolicRegionWriterGraph !=
          candidate.hasSymbolicRegionWriterGraphComposition() ||
      (!orderedWriterGraph &&
       pointerPartitionPriority !=
           candidate.hasPointerPartitionPriorityComposition()) ||
      (!orderedWriterGraph &&
       guardedWritePriority !=
           candidate.hasGuardedWritePriority()) ||
      (!orderedWriterGraph &&
       guardedByteComposition !=
           candidate.hasGuardedByteLaneComposition()) ||
      nested != candidate.hasNestedMemoryPhi() ||
      (!byteComposition &&
       extended !=
           (candidate.hasLiveOnEntry() ||
            candidate.hasNestedMemoryPhi())))
    return false;
  unsigned index = 7;
  unsigned relevant = 0;
  for (const CaptureState &capture : candidate.captures) {
    Value *incoming = incomingValueForBlock(phi, capture.block);
    if (incoming == nullptr)
      return false;
    if (capture.destinationOrdinal != candidate.destinationOrdinal) {
      if (incoming != Constant::getNullValue(phi.getType()))
        return false;
      continue;
    }
    ++relevant;
    const ReachingState &state =
        candidate.states[capture.ordinal];
    if (byteComposition) {
      if (orderedWriterGraph) {
        if (!orderedWriterGraphProofMatches(
                *proof, index, capture.ordinal, state, incoming,
                *candidate.load))
          return false;
        continue;
      }
      if (pointerPartition) {
        uint64_t stateKind = 0;
        if (!metadataInteger(*proof, index++, stateKind) ||
            stateKind !=
                (state.pointerPartition != nullptr ? 1 : 0))
          return false;
        if (!byteProofStateMatches(
                *proof, index, capture.ordinal, state,
                incoming, *candidate.load,
                pointerPartitionPriority,
                stateKind == 0))
          return false;
        if (stateKind != 0 &&
            !pointerPartitionProofMatches(
                *proof, index, state, incoming,
                *candidate.load))
          return false;
        continue;
      }
      if (cyclicByteComposition) {
        uint64_t stateKind = 0;
        if (!metadataInteger(*proof, index++, stateKind) ||
            stateKind !=
                (state.cycleEntryState != nullptr ? 1 : 0))
          return false;
        if (stateKind != 0) {
          uint64_t proofExit = 0;
          if (!metadataInteger(*proof, index++, proofExit) ||
              proofExit != capture.ordinal)
            return false;
          if (multiLatchCyclicByteComposition) {
            if (!multiLatchCyclicByteProofStateMatches(
                    *proof, index, capture.ordinal, state,
                    incoming, *candidate.load,
                    conditionalMultiLatchCyclicByteComposition ||
                        boundedMultiLatchCyclicByteComposition ||
                        symbolicRegionMultiLatchCyclicByteComposition,
                    nestedPredicateCyclicByteComposition,
                    symbolicRegionMultiLatchCyclicByteComposition))
              return false;
          } else if (!cyclicByteProofStateMatches(
                         *proof, index, capture.ordinal, state,
                         incoming, *candidate.load,
                         conditionalCyclicByteComposition)) {
            return false;
          }
          continue;
        }
      }
      if (!byteProofStateMatches(
              *proof, index, capture.ordinal, state, incoming,
              *candidate.load, guardedByteComposition, true,
              guardedWritePriority))
        return false;
      continue;
    }
    if (nested) {
      if (!nestedProofStateMatches(
              *proof, index, capture.ordinal, state) ||
          !provenanceValueMatches(
              state, incoming, *candidate.load, proof))
        return false;
      continue;
    }
    uint64_t proofExit = 0;
    uint64_t proofKind =
        static_cast<uint64_t>(MemoryStateKind::Store);
    uint64_t proofSource = 0;
    uint64_t skippedCount = 0;
    if (!metadataInteger(*proof, index++, proofExit))
      return false;
    if (extended && !metadataInteger(*proof, index++, proofKind))
      return false;
    if (!metadataInteger(*proof, index++, proofSource) ||
        !metadataInteger(*proof, index++, skippedCount) ||
        proofExit != capture.ordinal ||
        proofKind != static_cast<uint64_t>(state.kind) ||
        skippedCount != state.skipped.size())
      return false;
    if (state.kind == MemoryStateKind::Store) {
      if (state.store == nullptr ||
          proofSource != stableSiteId(*state.store) ||
          incoming != state.store->getValueOperand())
        return false;
    } else {
      auto *snapshot = dyn_cast<LoadInst>(incoming);
      if (state.store != nullptr || proofSource != 0 || snapshot == nullptr ||
          !snapshotMatches(
              *snapshot, *candidate.load, capture.block, proof))
        return false;
    }
    for (Instruction *instruction : state.skipped) {
      uint64_t skippedSite = 0;
      if (!metadataInteger(*proof, index++, skippedSite) ||
          skippedSite != stableSiteId(*instruction))
        return false;
    }
  }
  unsigned headerOrdinal = 0;
  unsigned headerDestination = 0;
  uint64_t headerLoadSite = 0;
  unsigned headerExits = 0;
  unsigned headerRelevant = 0;
  bool headerAllowsLiveOnEntry = false;
  bool headerAllowsNestedMemoryPhi = false;
  bool headerAllowsByteComposition = false;
  bool headerAllowsGuardedByteComposition = false;
  bool headerAllowsCyclicByteComposition = false;
  bool headerAllowsSymbolicRegionCyclicByteComposition = false;
  bool headerAllowsSymbolicRegionMultiLatchCyclicByteComposition =
      false;
  bool headerAllowsConditionalCyclicByteComposition = false;
  bool headerAllowsMultiLatchCyclicByteComposition = false;
  bool headerAllowsConditionalMultiLatchCyclicByteComposition =
      false;
  bool headerAllowsBoundedMultiLatchCyclicByteComposition =
      false;
  bool headerAllowsNestedPredicateCyclicByteComposition =
      false;
  bool headerAllowsOrderedWriterGraph = false;
  bool headerAllowsSymbolicRegionWriterGraph = false;
  bool headerAllowsPointerPartitionPriority = false;
  bool headerAllowsPointerPartition = false;
  bool headerAllowsGuardedWritePriority = false;
  return index == proof->getNumOperands() &&
         parseMemorySlotHeader(
             phi, candidate.controllerSite, headerOrdinal,
             headerDestination, headerLoadSite, headerExits,
             headerRelevant, headerAllowsLiveOnEntry,
             headerAllowsNestedMemoryPhi,
             headerAllowsByteComposition,
             headerAllowsGuardedByteComposition,
             headerAllowsCyclicByteComposition,
             headerAllowsSymbolicRegionCyclicByteComposition,
             headerAllowsSymbolicRegionMultiLatchCyclicByteComposition,
             headerAllowsConditionalCyclicByteComposition,
             headerAllowsMultiLatchCyclicByteComposition,
             headerAllowsConditionalMultiLatchCyclicByteComposition,
             headerAllowsBoundedMultiLatchCyclicByteComposition,
             headerAllowsNestedPredicateCyclicByteComposition,
             headerAllowsOrderedWriterGraph,
             headerAllowsSymbolicRegionWriterGraph,
             headerAllowsPointerPartitionPriority,
             headerAllowsPointerPartition,
             headerAllowsGuardedWritePriority) &&
         headerOrdinal == ordinal &&
         headerDestination == candidate.destinationOrdinal &&
         headerLoadSite == loadSite &&
         headerExits == candidate.captures.size() &&
         headerRelevant == relevant &&
         headerAllowsByteComposition ==
             candidate.hasByteLaneComposition() &&
         (headerAllowsOrderedWriterGraph ||
          headerAllowsGuardedByteComposition ==
              candidate.hasGuardedByteLaneComposition()) &&
         headerAllowsCyclicByteComposition ==
             candidate.hasCyclicByteLaneComposition() &&
         headerAllowsSymbolicRegionCyclicByteComposition ==
             candidate
                 .hasSymbolicRegionCyclicByteLaneComposition() &&
         headerAllowsSymbolicRegionMultiLatchCyclicByteComposition ==
             candidate
                 .hasSymbolicRegionMultiLatchCyclicByteLaneComposition() &&
         headerAllowsConditionalCyclicByteComposition ==
             candidate.hasConditionalCyclicByteLaneComposition() &&
         headerAllowsMultiLatchCyclicByteComposition ==
             candidate.hasMultiLatchCyclicByteLaneComposition() &&
         (headerAllowsNestedPredicateCyclicByteComposition ||
          headerAllowsBoundedMultiLatchCyclicByteComposition ||
          headerAllowsSymbolicRegionMultiLatchCyclicByteComposition ||
          headerAllowsConditionalMultiLatchCyclicByteComposition ==
              candidate
                  .hasConditionalMultiLatchCyclicByteLaneComposition()) &&
         (headerAllowsNestedPredicateCyclicByteComposition ||
          headerAllowsSymbolicRegionMultiLatchCyclicByteComposition ||
          headerAllowsBoundedMultiLatchCyclicByteComposition ==
              candidate
                  .hasBoundedMultiLatchCyclicByteLaneComposition()) &&
         headerAllowsNestedPredicateCyclicByteComposition ==
             candidate
                 .hasNestedPredicateCyclicByteLaneComposition() &&
         headerAllowsOrderedWriterGraph ==
             candidate.hasOrderedWriterGraphComposition() &&
         headerAllowsSymbolicRegionWriterGraph ==
             candidate.hasSymbolicRegionWriterGraphComposition() &&
         (headerAllowsOrderedWriterGraph ||
          headerAllowsPointerPartitionPriority ==
              candidate.hasPointerPartitionPriorityComposition()) &&
         (headerAllowsOrderedWriterGraph ||
          headerAllowsPointerPartition ==
              candidate.hasPointerPartition()) &&
         (headerAllowsOrderedWriterGraph ||
          headerAllowsGuardedWritePriority ==
              candidate.hasGuardedWritePriority()) &&
         (headerAllowsByteComposition ||
          headerAllowsLiveOnEntry ==
              (candidate.hasLiveOnEntry() ||
               candidate.hasNestedMemoryPhi())) &&
         headerAllowsNestedMemoryPhi ==
             candidate.hasNestedMemoryPhi();
}

MDNode *memoryProof(LLVMContext &context, const MemoryCandidate &candidate,
                    unsigned slotOrdinal) {
  SmallVector<Metadata *, 64> proof;
  unsigned relevantExits = 0;
  for (const CaptureState &capture : candidate.captures)
    relevantExits +=
        capture.destinationOrdinal == candidate.destinationOrdinal ? 1 : 0;

  const bool guardedByteComposition =
      candidate.hasGuardedByteLaneComposition();
  const bool guardedWritePriority =
      candidate.hasGuardedWritePriority();
  const bool pointerPartition =
      candidate.hasPointerPartition();
  const bool pointerPartitionPriority =
      candidate.hasPointerPartitionPriorityComposition();
  const bool cyclicByteComposition =
      candidate.hasCyclicByteLaneComposition();
  const bool symbolicRegionCyclicByteComposition =
      candidate.hasSymbolicRegionCyclicByteLaneComposition();
  const bool orderedSymbolicRegionCyclicByteComposition =
      candidate
          .hasOrderedSymbolicRegionCyclicByteLaneComposition();
  const bool symbolicRegionMultiLatchCyclicByteComposition =
      candidate
          .hasSymbolicRegionMultiLatchCyclicByteLaneComposition();
  const bool conditionalCyclicByteComposition =
      candidate.hasConditionalCyclicByteLaneComposition();
  const bool multiLatchCyclicByteComposition =
      candidate.hasMultiLatchCyclicByteLaneComposition();
  const bool conditionalMultiLatchCyclicByteComposition =
      candidate.hasConditionalMultiLatchCyclicByteLaneComposition();
  const bool boundedMultiLatchCyclicByteComposition =
      candidate.hasBoundedMultiLatchCyclicByteLaneComposition();
  const bool nestedPredicateCyclicByteComposition =
      candidate.hasNestedPredicateCyclicByteLaneComposition();
  const bool orderedWriterGraph =
      candidate.hasOrderedWriterGraphComposition();
  const bool symbolicRegionWriterGraph =
      candidate.hasSymbolicRegionWriterGraphComposition();
  const bool byteComposition = candidate.hasByteLaneComposition();
  const bool nested = candidate.hasNestedMemoryPhi();
  const bool extended = candidate.hasLiveOnEntry();
  StringRef proofSchema = kMemorySchema;
  if (orderedSymbolicRegionCyclicByteComposition)
    proofSchema =
        kMemoryOrderedSymbolicRegionCyclicByteLaneSchema;
  else if (symbolicRegionMultiLatchCyclicByteComposition)
    proofSchema =
        kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema;
  else if (symbolicRegionCyclicByteComposition)
    proofSchema = kMemorySymbolicRegionCyclicByteLaneSchema;
  else if (symbolicRegionWriterGraph)
    proofSchema = kMemorySymbolicRegionWriterGraphSchema;
  else if (orderedWriterGraph)
    proofSchema = kMemoryOrderedWriterGraphSchema;
  else if (guardedWritePriority)
    proofSchema = kMemoryGuardedPrioritySchema;
  else if (pointerPartitionPriority)
    proofSchema = kMemoryPointerPartitionPrioritySchema;
  else if (pointerPartition)
    proofSchema = kMemoryPointerPartitionSchema;
  else if (nestedPredicateCyclicByteComposition)
    proofSchema = kMemoryNestedPredicateCyclicByteLaneSchema;
  else if (boundedMultiLatchCyclicByteComposition)
    proofSchema = kMemoryBoundedMultiLatchCyclicByteLaneSchema;
  else if (conditionalMultiLatchCyclicByteComposition)
    proofSchema =
        kMemoryConditionalMultiLatchCyclicByteLaneSchema;
  else if (multiLatchCyclicByteComposition)
    proofSchema = kMemoryMultiLatchCyclicByteLaneSchema;
  else if (conditionalCyclicByteComposition)
    proofSchema = kMemoryConditionalCyclicByteLaneSchema;
  else if (cyclicByteComposition)
    proofSchema = kMemoryCyclicByteLaneSchema;
  else if (guardedByteComposition)
    proofSchema = kMemoryGuardedByteLaneSchema;
  else if (byteComposition)
    proofSchema = kMemoryByteLaneSchema;
  else if (nested)
    proofSchema = kMemoryNestedSchema;
  else if (extended)
    proofSchema = kMemoryInitialSchema;
  proof.push_back(MDString::get(context, proofSchema));
  proof.push_back(
      integerMetadata(context, 64, candidate.controllerSite));
  proof.push_back(integerMetadata(context, 32, slotOrdinal));
  proof.push_back(
      integerMetadata(context, 32, candidate.destinationOrdinal));
  proof.push_back(
      integerMetadata(context, 64, stableSiteId(*candidate.load)));
  proof.push_back(
      integerMetadata(context, 32, candidate.captures.size()));
  proof.push_back(integerMetadata(context, 32, relevantExits));
  for (const CaptureState &capture : candidate.captures) {
    if (capture.destinationOrdinal != candidate.destinationOrdinal)
      continue;
    const ReachingState &state =
        candidate.states[capture.ordinal];
    if (byteComposition) {
      if (orderedWriterGraph) {
        appendOrderedWriterGraphProof(
            proof, context, capture.ordinal, state);
        continue;
      }
      if (pointerPartition) {
        proof.push_back(integerMetadata(
            context, 32,
            state.pointerPartition != nullptr ? 1 : 0));
        appendByteProofState(
            proof, context, capture.ordinal, state,
            pointerPartitionPriority);
        if (state.pointerPartition != nullptr)
          appendPointerPartitionProof(
              proof, context, state);
        continue;
      }
      if (cyclicByteComposition) {
        proof.push_back(integerMetadata(
            context, 32,
            state.cycleEntryState != nullptr ? 1 : 0));
        if (state.cycleEntryState != nullptr) {
          proof.push_back(integerMetadata(
              context, 32, capture.ordinal));
          if (multiLatchCyclicByteComposition)
            appendMultiLatchCyclicByteProofState(
                proof, context, capture.ordinal, state,
                conditionalMultiLatchCyclicByteComposition ||
                    boundedMultiLatchCyclicByteComposition ||
                    symbolicRegionMultiLatchCyclicByteComposition,
                nestedPredicateCyclicByteComposition,
                symbolicRegionMultiLatchCyclicByteComposition);
          else
            appendCyclicByteProofState(
                proof, context, capture.ordinal, state,
                conditionalCyclicByteComposition);
          continue;
        }
      }
      appendByteProofState(
          proof, context, capture.ordinal, state,
          guardedByteComposition, guardedWritePriority);
      continue;
    }
    if (nested) {
      appendNestedProofState(
          proof, context, capture.ordinal, state);
      continue;
    }
    proof.push_back(integerMetadata(context, 32, capture.ordinal));
    if (extended)
      proof.push_back(integerMetadata(
          context, 32, static_cast<uint64_t>(state.kind)));
    proof.push_back(integerMetadata(
        context, 64,
        state.kind == MemoryStateKind::Store
            ? stableSiteId(*state.store)
            : 0));
    proof.push_back(
        integerMetadata(context, 32, state.skipped.size()));
    for (Instruction *instruction : state.skipped)
      proof.push_back(
          integerMetadata(context, 64, stableSiteId(*instruction)));
  }
  return MDNode::get(context, proof);
}

Value *materializeSourceByte(Value *sourceValue, unsigned sourceWidth,
                             unsigned sourceByte, bool littleEndian,
                             Type *byteType,
                             Instruction *insertBefore) {
  Value *byte = sourceValue;
  const unsigned sourceShift =
      byteBitShift(littleEndian, sourceWidth, sourceByte);
  if (sourceShift != 0)
    byte = BinaryOperator::CreateLShr(
        byte, ConstantInt::get(
                  cast<IntegerType>(byte->getType()), sourceShift),
        "ifss.cont.byte.extract", insertBefore);
  if (sourceWidth != 1)
    byte = new TruncInst(
        byte, byteType, "ifss.cont.byte", insertBefore);
  return byte;
}

Value *materializePointerPartitionByte(
    const PointerPartition &partition,
    PointerPartitionChild child, unsigned lane,
    Value *baseByte, bool littleEndian, Type *byteType,
    Instruction *insertBefore) {
  if (child.leaf) {
    const int sourceByte =
        partition.leaves[child.index].sourceBytes[lane];
    return sourceByte < 0
               ? baseByte
               : materializeSourceByte(
                     partition.store->getValueOperand(),
                     partition.storeWidth,
                     static_cast<unsigned>(sourceByte),
                     littleEndian, byteType, insertBefore);
  }
  const PointerPartitionNode &node =
      partition.nodes[child.index];
  Value *trueValue = materializePointerPartitionByte(
      partition, node.trueChild, lane, baseByte,
      littleEndian, byteType, insertBefore);
  Value *falseValue = materializePointerPartitionByte(
      partition, node.falseChild, lane, baseByte,
      littleEndian, byteType, insertBefore);
  return SelectInst::Create(
      node.guard, trueValue, falseValue,
      "ifss.cont.byte.partition", insertBefore);
}

Value *materializeSymbolicRegionByte(
    const OrderedWriterLayer &layer, unsigned lane, Value *baseByte,
    bool littleEndian, Type *byteType, Instruction *insertBefore) {
  assert(
      layer.kind == OrderedWriterKind::SymbolicRegion &&
      lane < layer.symbolicLaneCases.size());
  Value *byte = baseByte;
  for (auto item = layer.symbolicLaneCases[lane].rbegin();
       item != layer.symbolicLaneCases[lane].rend(); ++item) {
    Value *storedByte = materializeSourceByte(
        layer.store->getValueOperand(), layer.storeWidth,
        item->sourceByte, littleEndian, byteType, insertBefore);
    auto *condition = new ICmpInst(
        insertBefore, ICmpInst::ICMP_EQ, layer.symbolicIndex,
        ConstantInt::getSigned(
            cast<IntegerType>(layer.symbolicIndex->getType()),
            item->indexValue),
        "ifss.cont.region.hit");
    byte = SelectInst::Create(
        condition, storedByte, byte,
        "ifss.cont.byte.region", insertBefore);
  }
  return byte;
}

Value *materializeByteComposition(ReachingState &state, LoadInst &source,
                                  MDNode *proof) {
  Instruction *insertBefore = state.point->getTerminator();
  LLVMContext &context = source.getContext();
  IntegerType *resultType = cast<IntegerType>(source.getType());
  Type *byteType = Type::getInt8Ty(context);
  LoadInst *liveSnapshot = nullptr;
  Value *result = ConstantInt::get(resultType, 0);

  for (const ByteLaneSource &lane : state.byteLanes) {
    Value *sourceValue = nullptr;
    if (lane.store != nullptr) {
      sourceValue = lane.store->getValueOperand();
    } else {
      if (liveSnapshot == nullptr) {
        liveSnapshot = new LoadInst(
            source.getType(), source.getPointerOperand(),
            "ifss.cont.initial", insertBefore);
        liveSnapshot->setAlignment(source.getAlign());
        liveSnapshot->setMetadata(
            "symcc.ifss_continuation_memory_initial", proof);
      }
      sourceValue = liveSnapshot;
    }

    Value *byte = materializeSourceByte(
        sourceValue, lane.sourceWidth, lane.sourceByte,
        state.littleEndian, byteType, insertBefore);
    if (state.requiresOrderedWriterGraph()) {
      for (auto layer = state.writerLayers.rbegin();
           layer != state.writerLayers.rend(); ++layer) {
        if (layer->kind ==
            OrderedWriterKind::PointerPartition) {
          byte = materializePointerPartitionByte(
              *layer->pointerPartition, {false, 0}, lane.ordinal,
              byte, state.littleEndian, byteType, insertBefore);
          continue;
        }
        if (layer->kind == OrderedWriterKind::SymbolicRegion) {
          byte = materializeSymbolicRegionByte(
              *layer, lane.ordinal, byte, state.littleEndian,
              byteType, insertBefore);
          continue;
        }
        const int sourceByte =
            layer->sourceBytes[lane.ordinal];
        if (sourceByte < 0)
          continue;
        Value *guardedByte = materializeSourceByte(
            layer->store->getValueOperand(), layer->storeWidth,
            static_cast<unsigned>(sourceByte),
            state.littleEndian, byteType, insertBefore);
        const bool storeWhenTrue =
            layer->storePolarities[lane.ordinal] != 0;
        byte = SelectInst::Create(
            layer->guard,
            storeWhenTrue ? guardedByte : byte,
            storeWhenTrue ? byte : guardedByte,
            "ifss.cont.byte.writer", insertBefore);
      }
    } else {
      for (auto overlay = lane.guarded.rbegin();
           overlay != lane.guarded.rend(); ++overlay) {
        Value *guardedByte = materializeSourceByte(
            overlay->store->getValueOperand(),
            overlay->sourceWidth, overlay->sourceByte,
            state.littleEndian, byteType, insertBefore);
        byte = SelectInst::Create(
            overlay->guard,
            overlay->storeWhenTrue ? guardedByte : byte,
            overlay->storeWhenTrue ? byte : guardedByte,
            "ifss.cont.byte.alias", insertBefore);
      }
      if (state.pointerPartition != nullptr)
        byte = materializePointerPartitionByte(
            *state.pointerPartition, {false, 0},
            lane.ordinal, byte, state.littleEndian,
            byteType, insertBefore);
    }

    Value *component = new ZExtInst(
        byte, resultType, "ifss.cont.byte.extend", insertBefore);
    const unsigned destinationShift = byteBitShift(
        state.littleEndian, state.byteWidth, lane.ordinal);
    if (destinationShift != 0)
      component = BinaryOperator::CreateShl(
          component, ConstantInt::get(resultType, destinationShift),
          "ifss.cont.byte.position", insertBefore);
    result = BinaryOperator::CreateOr(
        result, component, "ifss.cont.byte.compose", insertBefore);
  }
  return result;
}

Value *materializeProvenance(
    ReachingState &state, LoadInst &source, MDNode *proof,
    DenseMap<ReachingState *, Value *> &values) {
  auto known = values.find(&state);
  if (known != values.end())
    return known->second;
  Value *value = nullptr;
  if (state.kind == MemoryStateKind::Store) {
    value = state.store->getValueOperand();
  } else if (state.kind == MemoryStateKind::LiveOnEntry) {
    auto *snapshot = new LoadInst(
        source.getType(), source.getPointerOperand(),
        "ifss.cont.initial", state.point->getTerminator());
    snapshot->setAlignment(source.getAlign());
    snapshot->setMetadata(
        "symcc.ifss_continuation_memory_initial", proof);
    value = snapshot;
  } else if (state.cycleEntryState != nullptr) {
    auto *phi = PHINode::Create(
        source.getType(), 1 + std::max<size_t>(
                                1, state.cycleTransfers.size()),
        state.cycleTransfers.empty()
            ? "ifss.cont.memory.cycle"
            : "ifss.cont.memory.multi.cycle",
        state.cycleHeader->getFirstNonPHI());
    phi->setMetadata(
        state.cycleTransfers.empty()
            ? "symcc.ifss_continuation_memory_cycle"
            : "symcc.ifss_continuation_memory_multi_cycle",
        proof);
    values[&state] = phi;

    Value *entryValue = materializeProvenance(
        *state.cycleEntryState, source, proof, values);
    IntegerType *resultType =
        cast<IntegerType>(source.getType());
    Type *byteType = Type::getInt8Ty(source.getContext());
    auto materializeTransfer =
        [&](ArrayRef<ByteLaneSource> lanes,
            BasicBlock *backedge, Instruction *guard,
            bool storeWhenTrue,
            const OrderedWriterLayer *symbolicRegion,
            ArrayRef<OrderedWriterLayer> symbolicRegions) {
          Instruction *insertBefore =
              backedge->getTerminator();
          Value *backedgeValue =
              ConstantInt::get(resultType, 0);
          for (const ByteLaneSource &lane : lanes) {
            Value *byte = nullptr;
            if (lane.carry) {
              byte = materializeSourceByte(
                  phi, lane.sourceWidth, lane.sourceByte,
                  state.littleEndian, byteType, insertBefore);
            } else {
              Value *storedByte = materializeSourceByte(
                  lane.store->getValueOperand(),
                  lane.sourceWidth, lane.sourceByte,
                  state.littleEndian, byteType, insertBefore);
              if (guard == nullptr) {
                byte = storedByte;
              } else {
                Value *carriedByte = materializeSourceByte(
                    phi, state.byteWidth, lane.ordinal,
                    state.littleEndian, byteType,
                    insertBefore);
                byte = SelectInst::Create(
                    guard,
                    storeWhenTrue ? storedByte : carriedByte,
                    storeWhenTrue ? carriedByte : storedByte,
                    "ifss.cont.byte.cycle.guard",
                    insertBefore);
              }
            }
            if (symbolicRegion != nullptr ||
                !symbolicRegions.empty()) {
              Value *baseByte = byte;
              Value *regionByte = baseByte;
              if (symbolicRegion != nullptr) {
                regionByte = materializeSymbolicRegionByte(
                    *symbolicRegion, lane.ordinal, regionByte,
                    state.littleEndian, byteType, insertBefore);
              } else {
                for (auto writer = symbolicRegions.rbegin();
                     writer != symbolicRegions.rend(); ++writer)
                  regionByte = materializeSymbolicRegionByte(
                      *writer, lane.ordinal, regionByte,
                      state.littleEndian, byteType, insertBefore);
              }
              byte =
                  guard == nullptr
                      ? regionByte
                      : SelectInst::Create(
                            guard,
                            storeWhenTrue ? regionByte : baseByte,
                            storeWhenTrue ? baseByte : regionByte,
                            "ifss.cont.byte.region.guard",
                            insertBefore);
            }
            Value *component = new ZExtInst(
                byte, resultType, "ifss.cont.byte.extend",
                insertBefore);
            const unsigned destinationShift = byteBitShift(
                state.littleEndian, state.byteWidth,
                lane.ordinal);
            if (destinationShift != 0)
              component = BinaryOperator::CreateShl(
                  component,
                  ConstantInt::get(
                      resultType, destinationShift),
                  "ifss.cont.byte.position",
                  insertBefore);
            backedgeValue = BinaryOperator::CreateOr(
                backedgeValue, component,
                "ifss.cont.byte.compose", insertBefore);
          }
          return backedgeValue;
        };
    phi->addIncoming(entryValue, state.cycleEntry);
    if (state.cycleTransfers.empty()) {
      Value *backedgeValue = materializeTransfer(
          state.byteLanes, state.cycleBackedge,
          state.cycleGuard, state.cycleStoreWhenTrue,
          state.cycleSymbolicRegion.get(),
          state.cycleSymbolicRegions);
      phi->addIncoming(backedgeValue, state.cycleBackedge);
    } else {
      for (const CyclicBackedgeTransfer &transfer :
           state.cycleTransfers) {
        Value *backedgeValue = nullptr;
        if (transfer.hasPredicateTree()) {
          auto *predicatePhi = PHINode::Create(
              source.getType(), transfer.predicateLeaves.size(),
              "ifss.cont.memory.predicate.cycle",
              transfer.block->getFirstNonPHI());
          predicatePhi->setMetadata(
              "symcc.ifss_continuation_memory_predicate_cycle",
              proof);
          for (const CyclicPredicateLeaf &leaf :
               transfer.predicateLeaves) {
            Value *leafValue = materializeTransfer(
                leaf.byteLanes, leaf.block, nullptr, false,
                nullptr, {});
            predicatePhi->addIncoming(leafValue, leaf.block);
          }
          backedgeValue = predicatePhi;
        } else {
          backedgeValue = materializeTransfer(
              transfer.byteLanes, transfer.block, transfer.guard,
              transfer.storeWhenTrue,
              transfer.symbolicRegion.get(), {});
        }
        phi->addIncoming(backedgeValue, transfer.block);
      }
    }
    value = phi;
  } else if (state.kind == MemoryStateKind::ByteComposition) {
    value = materializeByteComposition(state, source, proof);
  } else {
    SmallVector<Value *, kMaxNestedPhiIncoming> incomingValues;
    for (std::shared_ptr<ReachingState> &incoming :
         state.incomingStates)
      incomingValues.push_back(materializeProvenance(
          *incoming, source, proof, values));
    auto *phi = PHINode::Create(
        source.getType(), incomingValues.size(),
        "ifss.cont.memory.nested",
        state.memoryPhi->getBlock()->getFirstNonPHI());
    for (unsigned index = 0; index < incomingValues.size(); ++index)
      phi->addIncoming(incomingValues[index], state.incomingBlocks[index]);
    phi->setMetadata(
        "symcc.ifss_continuation_memory_nested", proof);
    value = phi;
  }
  values[&state] = value;
  return value;
}

void lowerCandidate(MemoryCandidate &candidate, unsigned slotOrdinal) {
  LoadInst *load = candidate.load;
  BasicBlock *dispatch = candidate.exitId->getParent();
  Instruction *insertBefore = dispatch->getFirstNonPHI();
  MDNode *proof = memoryProof(
      load->getContext(), candidate, slotOrdinal);
  auto *state = PHINode::Create(
      load->getType(), candidate.captures.size(),
      "ifss.cont.memory", insertBefore);
  Constant *neutral = Constant::getNullValue(load->getType());
  for (const CaptureState &capture : candidate.captures) {
    Value *value = neutral;
    if (capture.destinationOrdinal == candidate.destinationOrdinal) {
      ReachingState &reaching =
          candidate.states[capture.ordinal];
      DenseMap<ReachingState *, Value *> values;
      value = materializeProvenance(
          reaching, *load, proof, values);
    }
    state->addIncoming(value, capture.block);
  }

  state->setMetadata("symcc.ifss_continuation_memory", proof);
  load->setMetadata("symcc.ifss_continuation_memory_source", proof);
  load->replaceAllUsesWith(state);
}

LoadInst *findMemorySourceLoad(Function &function, uint64_t loadSite,
                               const MDNode *proof) {
  LoadInst *result = nullptr;
  for (BasicBlock &block : function)
    for (Instruction &instruction : block) {
      auto *load = dyn_cast<LoadInst>(&instruction);
      if (load == nullptr || stableSiteId(*load) != loadSite ||
          load->getMetadata(
              "symcc.ifss_continuation_memory_source") != proof)
        continue;
      if (result != nullptr)
        return nullptr;
      result = load;
    }
  return result;
}

bool collectScalarSlots(BasicBlock &dispatch,
                        const ContinuationSummary &summary,
                        SmallVectorImpl<ScalarSlotProof> &slots) {
  SmallVector<bool, 8> seen(summary.scalarSlotCount, false);
  for (PHINode &phi : dispatch.phis()) {
    if (phi.getMetadata("symcc.ifss_continuation_liveout") == nullptr)
      continue;
    ScalarSlotProof slot;
    if (!parseScalarSlotProof(phi, summary.controllerSite, slot) ||
        slot.ordinal >= summary.scalarSlotCount ||
        slot.destinationOrdinal >= summary.destinationCount ||
        seen[slot.ordinal])
      return false;
    seen[slot.ordinal] = true;
    slots.push_back(slot);
  }
  if (slots.size() != summary.scalarSlotCount ||
      std::find(seen.begin(), seen.end(), false) != seen.end())
    return false;
  std::sort(
      slots.begin(), slots.end(),
      [](const ScalarSlotProof &left, const ScalarSlotProof &right) {
        return left.ordinal < right.ordinal;
      });
  return true;
}

bool collectMemorySlots(Function &function, BasicBlock &dispatch,
                        const ContinuationSummary &summary,
                        AAResults &aliasAnalysis, MemorySSA &memorySSA,
                        DominatorTree &dominators,
                        SmallVectorImpl<ManifestMemorySlot> &slots) {
  SmallVector<bool, kMaxContinuationMemorySlots> seen(
      kMaxContinuationMemorySlots, false);
  for (PHINode &phi : dispatch.phis()) {
    const MDNode *proof =
        phi.getMetadata("symcc.ifss_continuation_memory");
    if (proof == nullptr)
      continue;
    unsigned ordinal = 0;
    unsigned destinationOrdinal = 0;
    uint64_t loadSite = 0;
    unsigned exitCount = 0;
    unsigned relevantExitCount = 0;
    bool allowsLiveOnEntry = false;
    bool allowsNestedMemoryPhi = false;
    bool allowsByteComposition = false;
    bool allowsGuardedByteComposition = false;
    bool allowsCyclicByteComposition = false;
    bool allowsSymbolicRegionCyclicByteComposition = false;
    bool allowsSymbolicRegionMultiLatchCyclicByteComposition =
        false;
    bool allowsConditionalCyclicByteComposition = false;
    bool allowsMultiLatchCyclicByteComposition = false;
    bool allowsConditionalMultiLatchCyclicByteComposition =
        false;
    bool allowsBoundedMultiLatchCyclicByteComposition =
        false;
    bool allowsNestedPredicateCyclicByteComposition =
        false;
    bool allowsOrderedWriterGraph = false;
    bool allowsSymbolicRegionWriterGraph = false;
    bool allowsPointerPartitionPriority = false;
    bool allowsPointerPartition = false;
    bool allowsGuardedWritePriority = false;
    if (!parseMemorySlotHeader(
            phi, summary.controllerSite, ordinal,
            destinationOrdinal, loadSite, exitCount,
            relevantExitCount, allowsLiveOnEntry,
            allowsNestedMemoryPhi, allowsByteComposition,
            allowsGuardedByteComposition,
            allowsCyclicByteComposition,
            allowsSymbolicRegionCyclicByteComposition,
            allowsSymbolicRegionMultiLatchCyclicByteComposition,
            allowsConditionalCyclicByteComposition,
            allowsMultiLatchCyclicByteComposition,
            allowsConditionalMultiLatchCyclicByteComposition,
            allowsBoundedMultiLatchCyclicByteComposition,
            allowsNestedPredicateCyclicByteComposition,
            allowsOrderedWriterGraph,
            allowsSymbolicRegionWriterGraph,
            allowsPointerPartitionPriority,
            allowsPointerPartition,
            allowsGuardedWritePriority) ||
        destinationOrdinal >= summary.destinationCount ||
        exitCount != summary.exitCount || seen[ordinal])
      return false;
    (void)relevantExitCount;

    LoadInst *load = findMemorySourceLoad(function, loadSite, proof);
    MemoryCandidate candidate;
    if (load == nullptr ||
        !collectCandidate(
            *load, aliasAnalysis, memorySSA, dominators, candidate,
            false) ||
        candidate.exitId->getParent() != &dispatch ||
        candidate.destinationOrdinal != destinationOrdinal ||
        candidate.hasByteLaneComposition() !=
            allowsByteComposition ||
        (!allowsOrderedWriterGraph &&
         candidate.hasGuardedByteLaneComposition() !=
             allowsGuardedByteComposition) ||
        candidate.hasCyclicByteLaneComposition() !=
            allowsCyclicByteComposition ||
        candidate.hasSymbolicRegionCyclicByteLaneComposition() !=
            allowsSymbolicRegionCyclicByteComposition ||
        candidate
                .hasSymbolicRegionMultiLatchCyclicByteLaneComposition() !=
            allowsSymbolicRegionMultiLatchCyclicByteComposition ||
        candidate.hasConditionalCyclicByteLaneComposition() !=
            allowsConditionalCyclicByteComposition ||
        candidate.hasMultiLatchCyclicByteLaneComposition() !=
            allowsMultiLatchCyclicByteComposition ||
        (!allowsNestedPredicateCyclicByteComposition &&
         !allowsBoundedMultiLatchCyclicByteComposition &&
         !allowsSymbolicRegionMultiLatchCyclicByteComposition &&
         candidate
                 .hasConditionalMultiLatchCyclicByteLaneComposition() !=
             allowsConditionalMultiLatchCyclicByteComposition) ||
        (!allowsNestedPredicateCyclicByteComposition &&
         !allowsSymbolicRegionMultiLatchCyclicByteComposition &&
         candidate
                 .hasBoundedMultiLatchCyclicByteLaneComposition() !=
             allowsBoundedMultiLatchCyclicByteComposition) ||
        candidate
                .hasNestedPredicateCyclicByteLaneComposition() !=
            allowsNestedPredicateCyclicByteComposition ||
        candidate.hasOrderedWriterGraphComposition() !=
            allowsOrderedWriterGraph ||
        candidate.hasSymbolicRegionWriterGraphComposition() !=
            allowsSymbolicRegionWriterGraph ||
        (!allowsOrderedWriterGraph &&
         candidate.hasPointerPartitionPriorityComposition() !=
             allowsPointerPartitionPriority) ||
        (!allowsOrderedWriterGraph &&
         candidate.hasPointerPartition() !=
             allowsPointerPartition) ||
        (!allowsOrderedWriterGraph &&
         candidate.hasGuardedWritePriority() !=
             allowsGuardedWritePriority) ||
        (!allowsByteComposition &&
         (candidate.hasLiveOnEntry() ||
          candidate.hasNestedMemoryPhi()) != allowsLiveOnEntry) ||
        candidate.hasNestedMemoryPhi() != allowsNestedMemoryPhi ||
        !memoryProofMatches(phi, candidate, ordinal, loadSite))
      return false;
    seen[ordinal] = true;
    slots.push_back({ordinal, loadSite, std::move(candidate)});
  }
  if (slots.size() > kMaxContinuationMemorySlots)
    return false;
  std::sort(
      slots.begin(), slots.end(),
      [](const ManifestMemorySlot &left,
         const ManifestMemorySlot &right) {
        return left.ordinal < right.ordinal;
      });
  for (unsigned index = 0; index < slots.size(); ++index)
    if (slots[index].ordinal != index)
      return false;
  return true;
}

bool collectResumeDestinations(
    Function &function, BasicBlock &dispatch,
    const ContinuationSummary &summary, ArrayRef<CaptureState> captures,
    SmallVectorImpl<BasicBlock *> &destinations) {
  destinations.assign(summary.destinationCount, nullptr);
  SmallVector<bool, kMaxContinuationExits> seen(
      summary.exitCount, false);
  SmallPtrSet<BasicBlock *, kMaxContinuationExits> captureBlocks;
  for (const CaptureState &capture : captures)
    captureBlocks.insert(capture.block);

  unsigned resumes = 0;
  for (BasicBlock &block : function) {
    auto *branch = dyn_cast<BranchInst>(block.getTerminator());
    uint64_t controllerSite = 0;
    ExitProof proof;
    if (branch == nullptr ||
        !parseExitProof(*branch, controllerSite, proof) ||
        controllerSite != summary.controllerSite)
      continue;
    if (captureBlocks.count(&block) != 0) {
      if (!branch->isUnconditional() ||
          branch->getSuccessor(0) != &dispatch)
        return false;
      continue;
    }
    if (!branch->isUnconditional() || proof.ordinal >= summary.exitCount ||
        proof.destinationOrdinal >= summary.destinationCount ||
        seen[proof.ordinal])
      return false;
    const CaptureState &capture = captures[proof.ordinal];
    if (capture.destinationOrdinal != proof.destinationOrdinal ||
        capture.sourceTerminatorSite != proof.sourceTerminatorSite ||
        capture.successorIndex != proof.successorIndex)
      return false;
    BasicBlock *destination = branch->getSuccessor(0);
    BasicBlock *&known = destinations[proof.destinationOrdinal];
    if (known != nullptr && known != destination)
      return false;
    known = destination;
    seen[proof.ordinal] = true;
    ++resumes;
  }
  return resumes == summary.exitCount &&
         std::find(seen.begin(), seen.end(), false) == seen.end() &&
         std::find(destinations.begin(), destinations.end(), nullptr) ==
             destinations.end();
}

uint64_t mixProvenanceFingerprint(uint64_t fingerprint,
                                  unsigned exitOrdinal,
                                  const ReachingState &root) {
  ProvenanceNodeList nodes;
  flattenProvenance(root, nodes);
  DenseMap<const ReachingState *, unsigned> indices;
  for (unsigned ordinal = 0; ordinal < nodes.size(); ++ordinal)
    indices[nodes[ordinal]] = ordinal;
  fingerprint = mixSiteIdInteger(fingerprint, exitOrdinal);
  fingerprint = mixSiteIdInteger(fingerprint, nodes.size());
  fingerprint = mixSiteIdInteger(fingerprint, 0);
  for (const ReachingState *state : nodes) {
    fingerprint = mixSiteIdInteger(
        fingerprint, static_cast<uint64_t>(state->kind));
    fingerprint = mixSiteIdInteger(
        fingerprint, provenanceSourceSite(*state));
    fingerprint =
        mixSiteIdInteger(fingerprint, state->skipped.size());
    for (Instruction *instruction : state->skipped)
      fingerprint =
          mixSiteIdInteger(fingerprint, stableSiteId(*instruction));
    fingerprint =
        mixSiteIdInteger(fingerprint, state->incomingStates.size());
    for (unsigned incoming = 0;
         incoming < state->incomingStates.size(); ++incoming) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          stableSiteId(
              *state->incomingBlocks[incoming]->getTerminator()));
      fingerprint = mixSiteIdInteger(
          fingerprint,
          indices.lookup(state->incomingStates[incoming].get()));
    }
  }
  return fingerprint;
}

uint64_t mixByteCompositionFingerprint(uint64_t fingerprint,
                                       unsigned exitOrdinal,
                                       const ReachingState &state,
                                       bool guardedSchema,
                                       bool prioritySchema = false) {
  fingerprint = mixSiteIdInteger(fingerprint, exitOrdinal);
  fingerprint = mixSiteIdInteger(fingerprint, state.byteWidth);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.littleEndian ? 0 : 1);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.skipped.size());
  for (Instruction *instruction : state.skipped)
    fingerprint =
        mixSiteIdInteger(fingerprint, stableSiteId(*instruction));
  fingerprint =
      mixSiteIdInteger(fingerprint, state.byteLanes.size());
  for (const ByteLaneSource &lane : state.byteLanes) {
    fingerprint = mixSiteIdInteger(fingerprint, lane.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, static_cast<uint64_t>(lane.kind()));
    fingerprint = mixSiteIdInteger(
        fingerprint,
        lane.store == nullptr ? 0 : stableSiteId(*lane.store));
    fingerprint = mixSiteIdInteger(fingerprint, lane.sourceByte);
    fingerprint = mixSiteIdInteger(fingerprint, lane.sourceWidth);
    if (guardedSchema) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          prioritySchema ? lane.guarded.size()
                         : (lane.hasGuardedSource() ? 1 : 0));
      const unsigned guardedCount =
          prioritySchema ? lane.guarded.size()
                         : (lane.hasGuardedSource() ? 1 : 0);
      for (unsigned guardedIndex = 0;
           guardedIndex < guardedCount; ++guardedIndex) {
        const GuardedByteOverlay &overlay =
            lane.guarded[guardedIndex];
        if (prioritySchema)
          fingerprint = mixSiteIdInteger(
              fingerprint, overlay.priority);
        fingerprint = mixSiteIdInteger(
            fingerprint, stableSiteId(*overlay.guard));
        fingerprint = mixSiteIdInteger(
            fingerprint, overlay.storeWhenTrue ? 1 : 0);
        fingerprint = mixSiteIdInteger(
            fingerprint, stableSiteId(*overlay.store));
        fingerprint = mixSiteIdInteger(
            fingerprint, overlay.sourceByte);
        fingerprint = mixSiteIdInteger(
            fingerprint, overlay.sourceWidth);
      }
    }
  }
  return fingerprint;
}

uint64_t mixSymbolicRegionProofFingerprint(
    uint64_t fingerprint, const OrderedWriterLayer &layer) {
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*layer.store));
  fingerprint =
      mixSiteIdInteger(fingerprint, layer.storeWidth);
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*layer.regionBase));
  fingerprint =
      mixSiteIdInteger(fingerprint, layer.regionExtent);
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*layer.symbolicIndex));
  fingerprint = mixSiteIdInteger(
      fingerprint, layer.symbolicIndexBits);
  fingerprint = mixSiteIdInteger(
      fingerprint,
      static_cast<uint64_t>(layer.symbolicBaseOffset));
  fingerprint = mixSiteIdInteger(
      fingerprint, layer.symbolicLaneCases.size());
  for (const auto &laneCases : layer.symbolicLaneCases) {
    fingerprint =
        mixSiteIdInteger(fingerprint, laneCases.size());
    for (const SymbolicRegionCase &item : laneCases) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(item.indexValue));
      fingerprint =
          mixSiteIdInteger(fingerprint, item.sourceByte);
    }
  }
  return fingerprint;
}

uint64_t mixCyclicByteCompositionFingerprint(
    uint64_t fingerprint, unsigned exitOrdinal,
    const ReachingState &state) {
  fingerprint =
      mixSiteIdInteger(fingerprint, exitOrdinal);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.byteWidth);
  fingerprint = mixSiteIdInteger(
      fingerprint, state.littleEndian ? 0 : 1);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.skipped.size());
  for (Instruction *instruction : state.skipped)
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*instruction));
  fingerprint = mixSiteIdInteger(
      fingerprint,
      stableSiteId(*state.cycleHeader->getTerminator()));
  fingerprint = mixSiteIdInteger(
      fingerprint,
      stableSiteId(*state.cycleEntry->getTerminator()));
  fingerprint = mixSiteIdInteger(
      fingerprint,
      stableSiteId(*state.cycleBackedge->getTerminator()));
  if (state.cycleGuard != nullptr) {
    fingerprint = mixSiteIdInteger(
        fingerprint,
        stableSiteId(*state.cycleBranch->getTerminator()));
    fingerprint = mixSiteIdInteger(
        fingerprint,
        stableSiteId(*state.cycleStoreArm->getTerminator()));
    fingerprint = mixSiteIdInteger(
        fingerprint,
        stableSiteId(*state.cycleCarryArm->getTerminator()));
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*state.cycleGuard));
    fingerprint = mixSiteIdInteger(
        fingerprint, state.cycleStoreWhenTrue ? 1 : 0);
  }
  fingerprint = mixByteCompositionFingerprint(
      fingerprint, exitOrdinal, *state.cycleEntryState, false);
  fingerprint = mixSiteIdInteger(
      fingerprint, state.cycleSkipped.size());
  for (Instruction *instruction : state.cycleSkipped)
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*instruction));
  fingerprint = mixSiteIdInteger(
      fingerprint, state.byteLanes.size());
  for (const ByteLaneSource &lane : state.byteLanes) {
    fingerprint =
        mixSiteIdInteger(fingerprint, lane.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, lane.carry ? 2 : 0);
    fingerprint = mixSiteIdInteger(
        fingerprint,
        lane.carry ? 0 : stableSiteId(*lane.store));
    fingerprint =
        mixSiteIdInteger(fingerprint, lane.sourceByte);
    fingerprint =
        mixSiteIdInteger(fingerprint, lane.sourceWidth);
  }
  if (!state.cycleSymbolicRegions.empty()) {
    fingerprint = mixSiteIdInteger(
        fingerprint, state.cycleSymbolicRegions.size());
    for (const OrderedWriterLayer &writer :
         state.cycleSymbolicRegions)
      fingerprint = mixSymbolicRegionProofFingerprint(
          fingerprint, writer);
  } else if (state.cycleSymbolicRegion != nullptr) {
    fingerprint = mixSymbolicRegionProofFingerprint(
        fingerprint, *state.cycleSymbolicRegion);
  }
  return fingerprint;
}

uint64_t mixMultiLatchCyclicByteCompositionFingerprint(
    uint64_t fingerprint, unsigned exitOrdinal,
    const ReachingState &state, bool taggedSchema,
    bool nestedPredicateSchema, bool symbolicRegionSchema) {
  fingerprint =
      mixSiteIdInteger(fingerprint, exitOrdinal);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.byteWidth);
  fingerprint = mixSiteIdInteger(
      fingerprint, state.littleEndian ? 0 : 1);
  fingerprint =
      mixSiteIdInteger(fingerprint, state.skipped.size());
  for (Instruction *instruction : state.skipped)
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*instruction));
  fingerprint = mixSiteIdInteger(
      fingerprint,
      stableSiteId(*state.cycleHeader->getTerminator()));
  fingerprint = mixSiteIdInteger(
      fingerprint,
      stableSiteId(*state.cycleEntry->getTerminator()));
  fingerprint = mixByteCompositionFingerprint(
      fingerprint, exitOrdinal, *state.cycleEntryState, false);
  fingerprint = mixSiteIdInteger(
      fingerprint, state.cycleTransfers.size());
  for (const CyclicBackedgeTransfer &transfer :
       state.cycleTransfers) {
    fingerprint = mixSiteIdInteger(
        fingerprint,
        stableSiteId(*transfer.block->getTerminator()));
    if (nestedPredicateSchema) {
      const uint64_t kind =
          transfer.hasPredicateTree()
              ? 2
              : (transfer.guard != nullptr ? 1 : 0);
      fingerprint = mixSiteIdInteger(fingerprint, kind);
      if (kind == 2) {
        fingerprint = mixSiteIdInteger(
            fingerprint, transfer.predicateNodes.size());
        fingerprint = mixSiteIdInteger(
            fingerprint, transfer.predicateLeaves.size());
        for (const CyclicPredicateNode &node :
             transfer.predicateNodes) {
          fingerprint = mixSiteIdInteger(
              fingerprint,
              stableSiteId(*node.block->getTerminator()));
          fingerprint = mixSiteIdInteger(
              fingerprint, stableSiteId(*node.guard));
          fingerprint = mixSiteIdInteger(
              fingerprint, node.trueChild.leaf ? 1 : 0);
          fingerprint = mixSiteIdInteger(
              fingerprint, node.trueChild.index);
          fingerprint = mixSiteIdInteger(
              fingerprint, node.falseChild.leaf ? 1 : 0);
          fingerprint = mixSiteIdInteger(
              fingerprint, node.falseChild.index);
        }
        for (unsigned leafIndex = 0;
             leafIndex < transfer.predicateLeaves.size();
             ++leafIndex) {
          const CyclicPredicateLeaf &leaf =
              transfer.predicateLeaves[leafIndex];
          fingerprint =
              mixSiteIdInteger(fingerprint, leafIndex);
          fingerprint = mixSiteIdInteger(
              fingerprint,
              stableSiteId(*leaf.block->getTerminator()));
          fingerprint = mixSiteIdInteger(
              fingerprint, leaf.skipped.size());
          for (Instruction *instruction : leaf.skipped)
            fingerprint = mixSiteIdInteger(
                fingerprint, stableSiteId(*instruction));
          fingerprint = mixSiteIdInteger(
              fingerprint, leaf.byteLanes.size());
          for (const ByteLaneSource &lane : leaf.byteLanes) {
            fingerprint =
                mixSiteIdInteger(fingerprint, lane.ordinal);
            fingerprint = mixSiteIdInteger(
                fingerprint, lane.carry ? 2 : 0);
            fingerprint = mixSiteIdInteger(
                fingerprint,
                lane.carry ? 0 : stableSiteId(*lane.store));
            fingerprint = mixSiteIdInteger(
                fingerprint, lane.sourceByte);
            fingerprint = mixSiteIdInteger(
                fingerprint, lane.sourceWidth);
          }
        }
        continue;
      }
      if (kind == 1) {
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.branch->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.storeArm->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.carryArm->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint, stableSiteId(*transfer.guard));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            transfer.storeWhenTrue ? 1 : 0);
      }
    } else if (taggedSchema) {
      fingerprint = mixSiteIdInteger(
          fingerprint, transfer.guard != nullptr ? 1 : 0);
      if (transfer.guard != nullptr) {
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.branch->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.storeArm->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            stableSiteId(*transfer.carryArm->getTerminator()));
        fingerprint = mixSiteIdInteger(
            fingerprint, stableSiteId(*transfer.guard));
        fingerprint = mixSiteIdInteger(
            fingerprint,
            transfer.storeWhenTrue ? 1 : 0);
      }
    }
    fingerprint = mixSiteIdInteger(
        fingerprint, transfer.skipped.size());
    for (Instruction *instruction : transfer.skipped)
      fingerprint = mixSiteIdInteger(
          fingerprint, stableSiteId(*instruction));
    fingerprint = mixSiteIdInteger(
        fingerprint, transfer.byteLanes.size());
    for (const ByteLaneSource &lane :
         transfer.byteLanes) {
      fingerprint =
          mixSiteIdInteger(fingerprint, lane.ordinal);
      fingerprint = mixSiteIdInteger(
          fingerprint, lane.carry ? 2 : 0);
      fingerprint = mixSiteIdInteger(
          fingerprint,
          lane.carry ? 0 : stableSiteId(*lane.store));
      fingerprint = mixSiteIdInteger(
          fingerprint, lane.sourceByte);
      fingerprint = mixSiteIdInteger(
          fingerprint, lane.sourceWidth);
    }
    if (symbolicRegionSchema) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          transfer.symbolicRegion != nullptr ? 1 : 0);
      if (transfer.symbolicRegion != nullptr)
        fingerprint = mixSymbolicRegionProofFingerprint(
            fingerprint, *transfer.symbolicRegion);
    }
  }
  return fingerprint;
}

uint64_t mixPointerPartitionFingerprint(
    uint64_t fingerprint,
    const PointerPartition &partition) {
  fingerprint = mixSiteIdInteger(
      fingerprint, stableSiteId(*partition.store));
  fingerprint =
      mixSiteIdInteger(fingerprint, partition.storeWidth);
  fingerprint =
      mixSiteIdInteger(fingerprint, partition.nodes.size());
  fingerprint =
      mixSiteIdInteger(fingerprint, partition.leaves.size());
  for (const PointerPartitionNode &node : partition.nodes) {
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*node.select));
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*node.guard));
    fingerprint = mixSiteIdInteger(
        fingerprint, node.trueChild.leaf ? 1 : 0);
    fingerprint = mixSiteIdInteger(
        fingerprint, node.trueChild.index);
    fingerprint = mixSiteIdInteger(
        fingerprint, node.falseChild.leaf ? 1 : 0);
    fingerprint = mixSiteIdInteger(
        fingerprint, node.falseChild.index);
  }
  for (unsigned leafIndex = 0;
       leafIndex < partition.leaves.size(); ++leafIndex) {
    fingerprint =
        mixSiteIdInteger(fingerprint, leafIndex);
    for (int sourceByte :
         partition.leaves[leafIndex].sourceBytes)
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(sourceByte + 1));
  }
  return fingerprint;
}

uint64_t mixOrderedWriterGraphFingerprint(
    uint64_t fingerprint, unsigned exitOrdinal,
    const ReachingState &state) {
  fingerprint = mixByteCompositionFingerprint(
      fingerprint, exitOrdinal, state, false);
  fingerprint = mixSiteIdInteger(
      fingerprint, state.writerLayers.size());
  for (const OrderedWriterLayer &layer : state.writerLayers) {
    fingerprint =
        mixSiteIdInteger(fingerprint, layer.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, static_cast<uint64_t>(layer.kind));
    if (layer.kind == OrderedWriterKind::PointerPartition) {
      fingerprint = mixPointerPartitionFingerprint(
          fingerprint, *layer.pointerPartition);
      continue;
    }
    if (layer.kind == OrderedWriterKind::SymbolicRegion) {
      fingerprint = mixSymbolicRegionProofFingerprint(
          fingerprint, layer);
      continue;
    }
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*layer.store));
    fingerprint =
        mixSiteIdInteger(fingerprint, layer.storeWidth);
    fingerprint = mixSiteIdInteger(
        fingerprint, stableSiteId(*layer.guard));
    fingerprint = mixSiteIdInteger(
        fingerprint, layer.sourceBytes.size());
    for (unsigned lane = 0; lane < layer.sourceBytes.size(); ++lane) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(layer.sourceBytes[lane] + 1));
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(
              layer.storePolarities[lane] + 1));
    }
  }
  return fingerprint;
}

StringRef provenanceKindName(MemoryStateKind kind) {
  switch (kind) {
  case MemoryStateKind::Store:
    return "store";
  case MemoryStateKind::LiveOnEntry:
    return "live-on-entry";
  case MemoryStateKind::MemoryPhi:
    return "memory-phi";
  case MemoryStateKind::ByteComposition:
    return "byte-composition";
  default:
    llvm_unreachable("unknown continuation memory state");
  }
}

json::Array provenanceRecords(const ReachingState &root) {
  ProvenanceNodeList nodes;
  flattenProvenance(root, nodes);
  DenseMap<const ReachingState *, unsigned> indices;
  for (unsigned ordinal = 0; ordinal < nodes.size(); ++ordinal)
    indices[nodes[ordinal]] = ordinal;
  json::Array records;
  for (unsigned ordinal = 0; ordinal < nodes.size(); ++ordinal) {
    const ReachingState &state = *nodes[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["state_kind"] = provenanceKindName(state.kind);
    item["source_site"] =
        std::to_string(provenanceSourceSite(state));
    json::Array skipped;
    for (Instruction *instruction : state.skipped)
      skipped.push_back(
          std::to_string(stableSiteId(*instruction)));
    item["skipped_nomod_sites"] = std::move(skipped);
    json::Array incomingRecords;
    for (unsigned incoming = 0;
         incoming < state.incomingStates.size(); ++incoming) {
      json::Object edge;
      edge["block_site"] = std::to_string(stableSiteId(
          *state.incomingBlocks[incoming]->getTerminator()));
      edge["node"] = static_cast<int64_t>(
          indices.lookup(state.incomingStates[incoming].get()));
      incomingRecords.push_back(std::move(edge));
    }
    item["incoming"] = std::move(incomingRecords);
    records.push_back(std::move(item));
  }
  return records;
}

json::Object pointerPartitionRecord(
    const PointerPartition &partition) {
  json::Object record;
  record["store_site"] =
      std::to_string(stableSiteId(*partition.store));
  record["store_width"] =
      static_cast<int64_t>(partition.storeWidth);
  json::Array nodes;
  for (unsigned nodeIndex = 0;
       nodeIndex < partition.nodes.size(); ++nodeIndex) {
    const PointerPartitionNode &node =
        partition.nodes[nodeIndex];
    json::Object nodeRecord;
    nodeRecord["ordinal"] = static_cast<int64_t>(nodeIndex);
    nodeRecord["select_site"] =
        std::to_string(stableSiteId(*node.select));
    nodeRecord["guard_site"] =
        std::to_string(stableSiteId(*node.guard));
    nodeRecord["true_kind"] =
        node.trueChild.leaf ? "leaf" : "node";
    nodeRecord["true_index"] =
        static_cast<int64_t>(node.trueChild.index);
    nodeRecord["false_kind"] =
        node.falseChild.leaf ? "leaf" : "node";
    nodeRecord["false_index"] =
        static_cast<int64_t>(node.falseChild.index);
    nodes.push_back(std::move(nodeRecord));
  }
  record["nodes"] = std::move(nodes);
  json::Array leaves;
  for (unsigned leafIndex = 0;
       leafIndex < partition.leaves.size(); ++leafIndex) {
    json::Object leafRecord;
    leafRecord["ordinal"] =
        static_cast<int64_t>(leafIndex);
    json::Array sources;
    for (int sourceByte :
         partition.leaves[leafIndex].sourceBytes)
      sources.push_back(static_cast<int64_t>(sourceByte));
    leafRecord["lane_source_bytes"] = std::move(sources);
    leaves.push_back(std::move(leafRecord));
  }
  record["leaves"] = std::move(leaves);
  return record;
}

json::Object symbolicRegionRecord(
    const OrderedWriterLayer &layer) {
  json::Object record;
  record["store_site"] =
      std::to_string(stableSiteId(*layer.store));
  record["store_width"] =
      static_cast<int64_t>(layer.storeWidth);
  record["region_base_site"] =
      std::to_string(stableSiteId(*layer.regionBase));
  record["region_extent"] =
      std::to_string(layer.regionExtent);
  record["index_site"] =
      std::to_string(stableSiteId(*layer.symbolicIndex));
  record["index_bits"] =
      static_cast<int64_t>(layer.symbolicIndexBits);
  record["base_offset"] = layer.symbolicBaseOffset;
  json::Array laneCases;
  for (unsigned lane = 0;
       lane < layer.symbolicLaneCases.size(); ++lane) {
    json::Object laneRecord;
    laneRecord["ordinal"] = static_cast<int64_t>(lane);
    json::Array cases;
    for (unsigned caseOrdinal = 0;
         caseOrdinal < layer.symbolicLaneCases[lane].size();
         ++caseOrdinal) {
      const SymbolicRegionCase &item =
          layer.symbolicLaneCases[lane][caseOrdinal];
      json::Object caseRecord;
      caseRecord["ordinal"] =
          static_cast<int64_t>(caseOrdinal);
      caseRecord["index_value"] = item.indexValue;
      caseRecord["source_byte"] =
          static_cast<int64_t>(item.sourceByte);
      cases.push_back(std::move(caseRecord));
    }
    laneRecord["cases"] = std::move(cases);
    laneCases.push_back(std::move(laneRecord));
  }
  record["lane_cases"] = std::move(laneCases);
  return record;
}

uint64_t continuationManifestFingerprint(
    StringRef moduleIdentity, StringRef functionName,
    const ContinuationSummary &summary, ArrayRef<CaptureState> captures,
    ArrayRef<ScalarSlotProof> scalarSlots,
    ArrayRef<ManifestMemorySlot> memorySlots) {
  uint64_t fingerprint =
      mixSiteIdText(1469598103934665603ULL, kManifestSchema);
  fingerprint = mixSiteIdText(fingerprint, moduleIdentity);
  fingerprint = mixSiteIdText(fingerprint, functionName);
  fingerprint =
      mixSiteIdInteger(fingerprint, summary.controllerSite);
  fingerprint = mixSiteIdInteger(fingerprint, summary.exitCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, summary.destinationCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, summary.scalarSlotCount);
  fingerprint =
      mixSiteIdInteger(fingerprint, memorySlots.size());
  fingerprint = mixSiteIdInteger(fingerprint, summary.blockCount);
  fingerprint = mixSiteIdInteger(fingerprint, summary.pathCount);
  for (const CaptureState &capture : captures) {
    fingerprint = mixSiteIdInteger(fingerprint, capture.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, capture.sourceTerminatorSite);
    fingerprint =
        mixSiteIdInteger(fingerprint, capture.successorIndex);
    fingerprint = mixSiteIdInteger(
        fingerprint, capture.destinationOrdinal);
  }
  for (const ScalarSlotProof &slot : scalarSlots) {
    fingerprint = mixSiteIdInteger(fingerprint, slot.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, slot.destinationOrdinal);
    fingerprint =
        mixSiteIdInteger(fingerprint, slot.originalPhiSite);
  }
  for (const ManifestMemorySlot &slot : memorySlots) {
    fingerprint = mixSiteIdInteger(fingerprint, slot.ordinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, slot.candidate.destinationOrdinal);
    fingerprint = mixSiteIdInteger(fingerprint, slot.loadSite);
    const bool byteComposition =
        slot.candidate.hasByteLaneComposition();
    const bool guardedByteComposition =
        slot.candidate.hasGuardedByteLaneComposition();
    const bool guardedWritePriority =
        slot.candidate.hasGuardedWritePriority();
    const bool cyclicByteComposition =
        slot.candidate.hasCyclicByteLaneComposition();
    const bool symbolicRegionCyclicByteComposition =
        slot.candidate
            .hasSymbolicRegionCyclicByteLaneComposition();
    const bool orderedSymbolicRegionCyclicByteComposition =
        slot.candidate
            .hasOrderedSymbolicRegionCyclicByteLaneComposition();
    const bool symbolicRegionMultiLatchCyclicByteComposition =
        slot.candidate
            .hasSymbolicRegionMultiLatchCyclicByteLaneComposition();
    const bool conditionalCyclicByteComposition =
        slot.candidate.hasConditionalCyclicByteLaneComposition();
    const bool multiLatchCyclicByteComposition =
        slot.candidate.hasMultiLatchCyclicByteLaneComposition();
    const bool conditionalMultiLatchCyclicByteComposition =
        slot.candidate
            .hasConditionalMultiLatchCyclicByteLaneComposition();
    const bool boundedMultiLatchCyclicByteComposition =
        slot.candidate
            .hasBoundedMultiLatchCyclicByteLaneComposition();
    const bool nestedPredicateCyclicByteComposition =
        slot.candidate
            .hasNestedPredicateCyclicByteLaneComposition();
    const bool orderedWriterGraph =
        slot.candidate.hasOrderedWriterGraphComposition();
    const bool symbolicRegionWriterGraph =
        slot.candidate.hasSymbolicRegionWriterGraphComposition();
    const bool pointerPartition =
        slot.candidate.hasPointerPartition();
    const bool pointerPartitionPriority =
        slot.candidate.hasPointerPartitionPriorityComposition();
    const bool nested = slot.candidate.hasNestedMemoryPhi();
    const bool extended = slot.candidate.hasLiveOnEntry();
    if (orderedSymbolicRegionCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint,
          kMemoryOrderedSymbolicRegionCyclicByteLaneSchema);
    else if (symbolicRegionMultiLatchCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint,
          kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema);
    else if (symbolicRegionCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint, kMemorySymbolicRegionCyclicByteLaneSchema);
    else if (symbolicRegionWriterGraph)
      fingerprint = mixSiteIdText(
          fingerprint, kMemorySymbolicRegionWriterGraphSchema);
    else if (orderedWriterGraph)
      fingerprint = mixSiteIdText(
          fingerprint, kMemoryOrderedWriterGraphSchema);
    else if (guardedWritePriority)
      fingerprint =
          mixSiteIdText(fingerprint, kMemoryGuardedPrioritySchema);
    else if (pointerPartitionPriority)
      fingerprint = mixSiteIdText(
          fingerprint, kMemoryPointerPartitionPrioritySchema);
    else if (pointerPartition)
      fingerprint =
          mixSiteIdText(fingerprint, kMemoryPointerPartitionSchema);
    else if (nestedPredicateCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint,
          kMemoryNestedPredicateCyclicByteLaneSchema);
    else if (boundedMultiLatchCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint,
          kMemoryBoundedMultiLatchCyclicByteLaneSchema);
    else if (conditionalMultiLatchCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint,
          kMemoryConditionalMultiLatchCyclicByteLaneSchema);
    else if (multiLatchCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint, kMemoryMultiLatchCyclicByteLaneSchema);
    else if (conditionalCyclicByteComposition)
      fingerprint = mixSiteIdText(
          fingerprint, kMemoryConditionalCyclicByteLaneSchema);
    else if (cyclicByteComposition)
      fingerprint =
          mixSiteIdText(fingerprint, kMemoryCyclicByteLaneSchema);
    else if (guardedByteComposition)
      fingerprint =
          mixSiteIdText(fingerprint, kMemoryGuardedByteLaneSchema);
    else if (byteComposition)
      fingerprint =
          mixSiteIdText(fingerprint, kMemoryByteLaneSchema);
    else if (nested)
      fingerprint = mixSiteIdText(fingerprint, kMemoryNestedSchema);
    else if (extended)
      fingerprint = mixSiteIdText(fingerprint, kMemoryInitialSchema);
    for (const CaptureState &capture : slot.candidate.captures) {
      if (capture.destinationOrdinal !=
          slot.candidate.destinationOrdinal)
        continue;
      const ReachingState &state =
          slot.candidate.states[capture.ordinal];
      if (byteComposition) {
        if (orderedWriterGraph) {
          fingerprint = mixOrderedWriterGraphFingerprint(
              fingerprint, capture.ordinal, state);
          continue;
        }
        if (pointerPartition) {
          fingerprint = mixSiteIdInteger(
              fingerprint,
              state.pointerPartition != nullptr ? 1 : 0);
          fingerprint = mixByteCompositionFingerprint(
              fingerprint, capture.ordinal, state,
              pointerPartitionPriority);
          if (state.pointerPartition != nullptr)
            fingerprint = mixPointerPartitionFingerprint(
                fingerprint, *state.pointerPartition);
          continue;
        }
        if (cyclicByteComposition) {
          fingerprint = mixSiteIdInteger(
              fingerprint,
              state.cycleEntryState != nullptr ? 1 : 0);
          if (state.cycleEntryState != nullptr) {
            fingerprint = multiLatchCyclicByteComposition
                              ? mixMultiLatchCyclicByteCompositionFingerprint(
                                    fingerprint, capture.ordinal, state,
                                    conditionalMultiLatchCyclicByteComposition ||
                                        boundedMultiLatchCyclicByteComposition ||
                                        symbolicRegionMultiLatchCyclicByteComposition,
                                    nestedPredicateCyclicByteComposition,
                                    symbolicRegionMultiLatchCyclicByteComposition)
                              : mixCyclicByteCompositionFingerprint(
                                    fingerprint, capture.ordinal, state);
            continue;
          }
        }
        fingerprint = mixByteCompositionFingerprint(
            fingerprint, capture.ordinal, state,
            guardedByteComposition,
            guardedWritePriority);
        continue;
      }
      if (nested) {
        fingerprint = mixProvenanceFingerprint(
            fingerprint, capture.ordinal, state);
        continue;
      }
      fingerprint = mixSiteIdInteger(fingerprint, capture.ordinal);
      if (extended)
        fingerprint = mixSiteIdInteger(
            fingerprint, static_cast<uint64_t>(state.kind));
      fingerprint = mixSiteIdInteger(
          fingerprint,
          state.kind == MemoryStateKind::Store
              ? stableSiteId(*state.store)
              : 0);
      fingerprint =
          mixSiteIdInteger(fingerprint, state.skipped.size());
      for (Instruction *instruction : state.skipped)
        fingerprint =
            mixSiteIdInteger(fingerprint, stableSiteId(*instruction));
    }
  }
  return fingerprint;
}

std::string buildContinuationManifestLine(
    Function &function, PHINode &exitId,
    const ContinuationSummary &summary, AAResults &aliasAnalysis,
    MemorySSA &memorySSA, DominatorTree &dominators) {
  BasicBlock &dispatch = *exitId.getParent();
  SmallVector<CaptureState, kMaxContinuationExits> captures;
  SmallVector<BasicBlock *, kMaxContinuationExits> destinations;
  SmallVector<ScalarSlotProof, 8> scalarSlots;
  SmallVector<ManifestMemorySlot, kMaxContinuationMemorySlots>
      memorySlots;
  if (!collectCaptures(
          dispatch, summary.controllerSite, summary.exitCount,
          captures) ||
      !collectResumeDestinations(
          function, dispatch, summary, captures, destinations) ||
      !collectScalarSlots(dispatch, summary, scalarSlots) ||
      !collectMemorySlots(
          function, dispatch, summary, aliasAnalysis, memorySSA,
          dominators, memorySlots))
    return {};

  StringRef moduleIdentity = stableModuleIdentity(*function.getParent());
  const uint64_t fingerprint = continuationManifestFingerprint(
      moduleIdentity, function.getName(), summary, captures,
      scalarSlots, memorySlots);

  json::Object record;
  record["schema"] = kManifestSchema;
  record["module"] = moduleIdentity.str();
  record["function"] = function.getName().str();
  record["controller_site"] =
      std::to_string(summary.controllerSite);
  record["exit_count"] = static_cast<int64_t>(summary.exitCount);
  record["destination_count"] =
      static_cast<int64_t>(summary.destinationCount);
  record["scalar_slot_count"] =
      static_cast<int64_t>(summary.scalarSlotCount);
  record["memory_slot_count"] =
      static_cast<int64_t>(memorySlots.size());
  record["block_count"] = static_cast<int64_t>(summary.blockCount);
  record["path_count"] = static_cast<int64_t>(summary.pathCount);
  const bool hasLiveOnEntry = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasLiveOnEntry();
      });
  const bool hasNestedMemoryPhi = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasNestedMemoryPhi();
      });
  const bool hasByteComposition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasByteLaneComposition();
      });
  const bool hasGuardedByteComposition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasGuardedByteLaneComposition();
      });
  const bool hasGuardedWritePriority = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasGuardedWritePriority();
      });
  const bool hasCyclicByteComposition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasCyclicByteLaneComposition();
      });
  const bool hasSymbolicRegionCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasSymbolicRegionCyclicByteLaneComposition();
          });
  const bool hasOrderedSymbolicRegionCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasOrderedSymbolicRegionCyclicByteLaneComposition();
          });
  const bool hasSymbolicRegionMultiLatchCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasSymbolicRegionMultiLatchCyclicByteLaneComposition();
          });
  const bool hasConditionalCyclicByteComposition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate
            .hasConditionalCyclicByteLaneComposition();
      });
  const bool hasMultiLatchCyclicByteComposition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate
            .hasMultiLatchCyclicByteLaneComposition();
      });
  const bool hasConditionalMultiLatchCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasConditionalMultiLatchCyclicByteLaneComposition();
          });
  const bool hasBoundedMultiLatchCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasBoundedMultiLatchCyclicByteLaneComposition();
          });
  const bool hasNestedPredicateCyclicByteComposition =
      std::any_of(
          memorySlots.begin(), memorySlots.end(),
          [](const ManifestMemorySlot &slot) {
            return slot.candidate
                .hasNestedPredicateCyclicByteLaneComposition();
          });
  const bool hasOrderedWriterGraph = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasOrderedWriterGraphComposition();
      });
  const bool hasSymbolicRegionWriterGraph = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate
            .hasSymbolicRegionWriterGraphComposition();
      });
  const bool hasPointerPartition = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate.hasPointerPartition();
      });
  const bool hasPointerPartitionPriority = std::any_of(
      memorySlots.begin(), memorySlots.end(),
      [](const ManifestMemorySlot &slot) {
        return slot.candidate
            .hasPointerPartitionPriorityComposition();
      });
  StringRef analysis = "llvm-memoryssa-aa-revalidated-v1";
  if (memorySlots.empty())
    analysis = "structural-only-v1";
  else if (hasOrderedSymbolicRegionCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-ordered-symbolic-region-cyclic-byte-lane-revalidated-v19";
  else if (hasSymbolicRegionMultiLatchCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-symbolic-region-multi-latch-cyclic-byte-lane-revalidated-v18";
  else if (hasSymbolicRegionCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-symbolic-region-cyclic-byte-lane-revalidated-v17";
  else if (hasSymbolicRegionWriterGraph)
    analysis =
        "llvm-memoryssa-aa-symbolic-region-writer-graph-revalidated-v16";
  else if (hasOrderedWriterGraph)
    analysis =
        "llvm-memoryssa-aa-ordered-writer-graph-revalidated-v15";
  else if (hasGuardedWritePriority)
    analysis =
        "llvm-memoryssa-aa-guarded-write-priority-revalidated-v8";
  else if (hasPointerPartitionPriority)
    analysis =
        "llvm-memoryssa-aa-pointer-union-priority-revalidated-v11";
  else if (hasPointerPartition)
    analysis =
        "llvm-memoryssa-aa-finite-pointer-union-revalidated-v7";
  else if (hasNestedPredicateCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-nested-predicate-cyclic-byte-lane-revalidated-v14";
  else if (hasBoundedMultiLatchCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-bounded-multi-latch-cyclic-byte-lane-revalidated-v13";
  else if (hasConditionalMultiLatchCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-conditional-multi-latch-cyclic-byte-lane-revalidated-v12";
  else if (hasMultiLatchCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-multi-latch-cyclic-byte-lane-revalidated-v10";
  else if (hasConditionalCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-conditional-cyclic-byte-lane-revalidated-v9";
  else if (hasCyclicByteComposition)
    analysis =
        "llvm-memoryssa-aa-cyclic-byte-lane-revalidated-v6";
  else if (hasGuardedByteComposition)
    analysis =
        "llvm-memoryssa-aa-guarded-byte-lane-revalidated-v5";
  else if (hasByteComposition)
    analysis = "llvm-memoryssa-aa-byte-lane-revalidated-v4";
  else if (hasNestedMemoryPhi)
    analysis = "llvm-memoryssa-aa-nested-phi-revalidated-v3";
  else if (hasLiveOnEntry)
    analysis =
        "llvm-memoryssa-aa-live-on-entry-revalidated-v2";
  record["analysis"] = analysis;

  json::Array exits;
  for (const CaptureState &capture : captures) {
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(capture.ordinal);
    item["source_site"] =
        std::to_string(capture.sourceTerminatorSite);
    item["successor_index"] =
        static_cast<int64_t>(capture.successorIndex);
    item["destination"] =
        static_cast<int64_t>(capture.destinationOrdinal);
    exits.push_back(std::move(item));
  }
  record["exits"] = std::move(exits);

  json::Array destinationRecords;
  for (unsigned ordinal = 0; ordinal < destinations.size(); ++ordinal) {
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    json::Array destinationExits;
    for (const CaptureState &capture : captures)
      if (capture.destinationOrdinal == ordinal)
        destinationExits.push_back(
            static_cast<int64_t>(capture.ordinal));
    item["exits"] = std::move(destinationExits);
    destinationRecords.push_back(std::move(item));
  }
  record["destinations"] = std::move(destinationRecords);

  json::Array scalarRecords;
  for (const ScalarSlotProof &slot : scalarSlots) {
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(slot.ordinal);
    item["destination"] =
        static_cast<int64_t>(slot.destinationOrdinal);
    item["original_phi_site"] =
        std::to_string(slot.originalPhiSite);
    scalarRecords.push_back(std::move(item));
  }
  record["scalar_slots"] = std::move(scalarRecords);

  json::Array memoryRecords;
  for (const ManifestMemorySlot &slot : memorySlots) {
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(slot.ordinal);
    item["destination"] = static_cast<int64_t>(
        slot.candidate.destinationOrdinal);
    item["load_site"] = std::to_string(slot.loadSite);
    const bool byteComposition =
        slot.candidate.hasByteLaneComposition();
    const bool guardedByteComposition =
        slot.candidate.hasGuardedByteLaneComposition();
    const bool guardedWritePriority =
        slot.candidate.hasGuardedWritePriority();
    const bool cyclicByteComposition =
        slot.candidate.hasCyclicByteLaneComposition();
    const bool symbolicRegionCyclicByteComposition =
        slot.candidate
            .hasSymbolicRegionCyclicByteLaneComposition();
    const bool orderedSymbolicRegionCyclicByteComposition =
        slot.candidate
            .hasOrderedSymbolicRegionCyclicByteLaneComposition();
    const bool symbolicRegionMultiLatchCyclicByteComposition =
        slot.candidate
            .hasSymbolicRegionMultiLatchCyclicByteLaneComposition();
    const bool conditionalCyclicByteComposition =
        slot.candidate.hasConditionalCyclicByteLaneComposition();
    const bool multiLatchCyclicByteComposition =
        slot.candidate.hasMultiLatchCyclicByteLaneComposition();
    const bool conditionalMultiLatchCyclicByteComposition =
        slot.candidate
            .hasConditionalMultiLatchCyclicByteLaneComposition();
    const bool boundedMultiLatchCyclicByteComposition =
        slot.candidate
            .hasBoundedMultiLatchCyclicByteLaneComposition();
    const bool nestedPredicateCyclicByteComposition =
        slot.candidate
            .hasNestedPredicateCyclicByteLaneComposition();
    const bool orderedWriterGraph =
        slot.candidate.hasOrderedWriterGraphComposition();
    const bool symbolicRegionWriterGraph =
        slot.candidate.hasSymbolicRegionWriterGraphComposition();
    const bool pointerPartition =
        slot.candidate.hasPointerPartition();
    const bool pointerPartitionPriority =
        slot.candidate.hasPointerPartitionPriorityComposition();
    const bool nested = slot.candidate.hasNestedMemoryPhi();
    const bool extended = slot.candidate.hasLiveOnEntry();
    if (orderedSymbolicRegionCyclicByteComposition)
      item["state_schema"] =
          kMemoryOrderedSymbolicRegionCyclicByteLaneSchema;
    else if (symbolicRegionMultiLatchCyclicByteComposition)
      item["state_schema"] =
          kMemorySymbolicRegionMultiLatchCyclicByteLaneSchema;
    else if (symbolicRegionCyclicByteComposition)
      item["state_schema"] =
          kMemorySymbolicRegionCyclicByteLaneSchema;
    else if (symbolicRegionWriterGraph)
      item["state_schema"] =
          kMemorySymbolicRegionWriterGraphSchema;
    else if (orderedWriterGraph)
      item["state_schema"] = kMemoryOrderedWriterGraphSchema;
    else if (guardedWritePriority)
      item["state_schema"] = kMemoryGuardedPrioritySchema;
    else if (pointerPartitionPriority)
      item["state_schema"] =
          kMemoryPointerPartitionPrioritySchema;
    else if (pointerPartition)
      item["state_schema"] = kMemoryPointerPartitionSchema;
    else if (nestedPredicateCyclicByteComposition)
      item["state_schema"] =
          kMemoryNestedPredicateCyclicByteLaneSchema;
    else if (boundedMultiLatchCyclicByteComposition)
      item["state_schema"] =
          kMemoryBoundedMultiLatchCyclicByteLaneSchema;
    else if (conditionalMultiLatchCyclicByteComposition)
      item["state_schema"] =
          kMemoryConditionalMultiLatchCyclicByteLaneSchema;
    else if (multiLatchCyclicByteComposition)
      item["state_schema"] =
          kMemoryMultiLatchCyclicByteLaneSchema;
    else if (conditionalCyclicByteComposition)
      item["state_schema"] =
          kMemoryConditionalCyclicByteLaneSchema;
    else if (cyclicByteComposition)
      item["state_schema"] = kMemoryCyclicByteLaneSchema;
    else if (guardedByteComposition)
      item["state_schema"] = kMemoryGuardedByteLaneSchema;
    else if (byteComposition)
      item["state_schema"] = kMemoryByteLaneSchema;
    else if (nested)
      item["state_schema"] = kMemoryNestedSchema;
    else if (extended)
      item["state_schema"] = kMemoryInitialSchema;
    json::Array exitStates;
    for (const CaptureState &capture : slot.candidate.captures) {
      if (capture.destinationOrdinal !=
          slot.candidate.destinationOrdinal)
        continue;
      const ReachingState &state =
          slot.candidate.states[capture.ordinal];
      json::Object exitState;
      exitState["exit"] = static_cast<int64_t>(capture.ordinal);
      if (byteComposition) {
        if (orderedWriterGraph)
          exitState["state_kind"] =
              state.writerLayers.empty()
                  ? "linear-byte-composition"
                  : (state.requiresSymbolicRegionWriterGraph()
                         ? "symbolic-region-writer-graph"
                         : "ordered-writer-graph");
        else if (pointerPartition)
          exitState["state_kind"] =
              state.pointerPartition != nullptr
                  ? "finite-pointer-union"
                  : "linear-byte-composition";
        if (cyclicByteComposition)
          exitState["state_kind"] =
              state.cycleEntryState != nullptr
                  ? (orderedSymbolicRegionCyclicByteComposition
                         ? "ordered-symbolic-region-cyclic-byte-composition"
                         : (symbolicRegionMultiLatchCyclicByteComposition
                         ? "symbolic-region-multi-latch-cyclic-byte-composition"
                         : (state.cycleSymbolicRegion != nullptr
                         ? "symbolic-region-cyclic-byte-composition"
                         : (state.cycleGuard != nullptr
                         ? "conditional-cyclic-byte-composition"
                         : (!state.cycleTransfers.empty()
                                ? (nestedPredicateCyclicByteComposition
                                       ? "nested-predicate-cyclic-byte-composition"
                                       : (boundedMultiLatchCyclicByteComposition
                                       ? "bounded-multi-latch-cyclic-byte-composition"
                                       : (conditionalMultiLatchCyclicByteComposition
                                              ? "conditional-multi-latch-cyclic-byte-composition"
                                              : "multi-latch-cyclic-byte-composition")))
                                : "cyclic-byte-composition")))))
                  : "linear-byte-composition";
        exitState["byte_width"] =
            static_cast<int64_t>(state.byteWidth);
        exitState["endianness"] =
            state.littleEndian ? "little" : "big";
        json::Array skipped;
        for (Instruction *instruction : state.skipped)
          skipped.push_back(
              std::to_string(stableSiteId(*instruction)));
        exitState["skipped_nomod_sites"] = std::move(skipped);
        if (state.cycleEntryState != nullptr) {
          exitState["cycle_header_site"] = std::to_string(
              stableSiteId(
                  *state.cycleHeader->getTerminator()));
          exitState["cycle_entry_site"] = std::to_string(
              stableSiteId(
                  *state.cycleEntry->getTerminator()));
          if (state.cycleTransfers.empty())
            exitState["cycle_backedge_site"] = std::to_string(
                stableSiteId(
                    *state.cycleBackedge->getTerminator()));
          if (state.cycleGuard != nullptr) {
            exitState["cycle_branch_site"] = std::to_string(
                stableSiteId(
                    *state.cycleBranch->getTerminator()));
            exitState["cycle_store_arm_site"] = std::to_string(
                stableSiteId(
                    *state.cycleStoreArm->getTerminator()));
            exitState["cycle_carry_arm_site"] = std::to_string(
                stableSiteId(
                    *state.cycleCarryArm->getTerminator()));
            exitState["cycle_guard_site"] = std::to_string(
                stableSiteId(*state.cycleGuard));
            exitState["cycle_store_when"] =
                state.cycleStoreWhenTrue ? "true" : "false";
          }
          json::Object entry;
          json::Array entrySkipped;
          for (Instruction *instruction :
               state.cycleEntryState->skipped)
            entrySkipped.push_back(std::to_string(
                stableSiteId(*instruction)));
          entry["skipped_nomod_sites"] =
              std::move(entrySkipped);
          json::Array entryLanes;
          for (const ByteLaneSource &lane :
               state.cycleEntryState->byteLanes) {
            json::Object laneRecord;
            laneRecord["ordinal"] =
                static_cast<int64_t>(lane.ordinal);
            laneRecord["source_kind"] =
                lane.store == nullptr
                    ? "live-on-entry"
                    : "store";
            laneRecord["source_site"] = std::to_string(
                lane.store == nullptr
                    ? 0
                    : stableSiteId(*lane.store));
            laneRecord["source_byte"] =
                static_cast<int64_t>(lane.sourceByte);
            laneRecord["source_width"] =
                static_cast<int64_t>(lane.sourceWidth);
            entryLanes.push_back(
                std::move(laneRecord));
          }
          entry["lanes"] = std::move(entryLanes);
          exitState["entry_state"] = std::move(entry);

          if (!state.cycleTransfers.empty()) {
            json::Array transfers;
            for (unsigned transferIndex = 0;
                 transferIndex < state.cycleTransfers.size();
                 ++transferIndex) {
              const CyclicBackedgeTransfer &transfer =
                  state.cycleTransfers[transferIndex];
              json::Object transferRecord;
              transferRecord["ordinal"] =
                  static_cast<int64_t>(transferIndex);
              transferRecord["cycle_backedge_site"] =
                  std::to_string(stableSiteId(
                      *transfer.block->getTerminator()));
              if (nestedPredicateCyclicByteComposition) {
                transferRecord["transfer_kind"] =
                    transfer.hasPredicateTree()
                        ? "predicate-tree"
                        : (transfer.guard == nullptr
                               ? "unconditional"
                               : "conditional");
                if (transfer.hasPredicateTree()) {
                  json::Array nodes;
                  for (unsigned nodeIndex = 0;
                       nodeIndex < transfer.predicateNodes.size();
                       ++nodeIndex) {
                    const CyclicPredicateNode &node =
                        transfer.predicateNodes[nodeIndex];
                    json::Object nodeRecord;
                    nodeRecord["ordinal"] =
                        static_cast<int64_t>(nodeIndex);
                    nodeRecord["branch_site"] =
                        std::to_string(stableSiteId(
                            *node.block->getTerminator()));
                    nodeRecord["guard_site"] =
                        std::to_string(stableSiteId(*node.guard));
                    auto childRecord =
                        [](const CyclicPredicateChild &child) {
                          json::Object record;
                          record["kind"] =
                              child.leaf ? "leaf" : "node";
                          record["index"] =
                              static_cast<int64_t>(child.index);
                          return record;
                        };
                    nodeRecord["true_child"] =
                        childRecord(node.trueChild);
                    nodeRecord["false_child"] =
                        childRecord(node.falseChild);
                    nodes.push_back(std::move(nodeRecord));
                  }
                  transferRecord["predicate_nodes"] =
                      std::move(nodes);
                  json::Array leaves;
                  for (unsigned leafIndex = 0;
                       leafIndex <
                           transfer.predicateLeaves.size();
                       ++leafIndex) {
                    const CyclicPredicateLeaf &leaf =
                        transfer.predicateLeaves[leafIndex];
                    json::Object leafRecord;
                    leafRecord["ordinal"] =
                        static_cast<int64_t>(leafIndex);
                    leafRecord["leaf_site"] =
                        std::to_string(stableSiteId(
                            *leaf.block->getTerminator()));
                    json::Array skipped;
                    for (Instruction *instruction :
                         leaf.skipped)
                      skipped.push_back(std::to_string(
                          stableSiteId(*instruction)));
                    leafRecord["skipped_nomod_sites"] =
                        std::move(skipped);
                    json::Array lanes;
                    for (const ByteLaneSource &lane :
                         leaf.byteLanes) {
                      json::Object laneRecord;
                      laneRecord["ordinal"] =
                          static_cast<int64_t>(lane.ordinal);
                      laneRecord["source_kind"] =
                          lane.carry ? "carry" : "store";
                      laneRecord["source_site"] =
                          std::to_string(
                              lane.carry
                                  ? 0
                                  : stableSiteId(*lane.store));
                      laneRecord["source_byte"] =
                          static_cast<int64_t>(
                              lane.sourceByte);
                      laneRecord["source_width"] =
                          static_cast<int64_t>(
                              lane.sourceWidth);
                      lanes.push_back(
                          std::move(laneRecord));
                    }
                    leafRecord["lanes"] =
                        std::move(lanes);
                    leaves.push_back(std::move(leafRecord));
                  }
                  transferRecord["predicate_leaves"] =
                      std::move(leaves);
                  transfers.push_back(
                      std::move(transferRecord));
                  continue;
                }
                if (transfer.guard != nullptr) {
                  transferRecord["cycle_branch_site"] =
                      std::to_string(stableSiteId(
                          *transfer.branch->getTerminator()));
                  transferRecord["cycle_store_arm_site"] =
                      std::to_string(stableSiteId(
                          *transfer.storeArm->getTerminator()));
                  transferRecord["cycle_carry_arm_site"] =
                      std::to_string(stableSiteId(
                          *transfer.carryArm->getTerminator()));
                  transferRecord["cycle_guard_site"] =
                      std::to_string(
                          stableSiteId(*transfer.guard));
                  transferRecord["cycle_store_when"] =
                      transfer.storeWhenTrue ? "true" : "false";
                }
              } else if (
                  conditionalMultiLatchCyclicByteComposition ||
                  boundedMultiLatchCyclicByteComposition ||
                  symbolicRegionMultiLatchCyclicByteComposition) {
                transferRecord["transfer_kind"] =
                    transfer.guard == nullptr
                        ? "unconditional"
                        : "conditional";
                if (transfer.guard != nullptr) {
                  transferRecord["cycle_branch_site"] =
                      std::to_string(stableSiteId(
                          *transfer.branch->getTerminator()));
                  transferRecord["cycle_store_arm_site"] =
                      std::to_string(stableSiteId(
                          *transfer.storeArm->getTerminator()));
                  transferRecord["cycle_carry_arm_site"] =
                      std::to_string(stableSiteId(
                          *transfer.carryArm->getTerminator()));
                  transferRecord["cycle_guard_site"] =
                      std::to_string(
                          stableSiteId(*transfer.guard));
                  transferRecord["cycle_store_when"] =
                      transfer.storeWhenTrue ? "true" : "false";
                }
              }
              json::Array transferSkipped;
              for (Instruction *instruction :
                   transfer.skipped)
                transferSkipped.push_back(std::to_string(
                    stableSiteId(*instruction)));
              transferRecord["skipped_nomod_sites"] =
                  std::move(transferSkipped);
              json::Array transferLanes;
              for (const ByteLaneSource &lane :
                   transfer.byteLanes) {
                json::Object laneRecord;
                laneRecord["ordinal"] =
                    static_cast<int64_t>(lane.ordinal);
                laneRecord["source_kind"] =
                    lane.carry
                        ? "carry"
                        : (transfer.guard != nullptr
                               ? "guarded-store"
                               : "store");
                laneRecord["source_site"] = std::to_string(
                    lane.carry
                        ? 0
                        : stableSiteId(*lane.store));
                laneRecord["source_byte"] =
                    static_cast<int64_t>(lane.sourceByte);
                laneRecord["source_width"] =
                    static_cast<int64_t>(lane.sourceWidth);
                transferLanes.push_back(
                    std::move(laneRecord));
              }
              transferRecord["lanes"] =
                  std::move(transferLanes);
              if (symbolicRegionMultiLatchCyclicByteComposition &&
                  transfer.symbolicRegion != nullptr)
                transferRecord["symbolic_region_writer"] =
                    symbolicRegionRecord(
                        *transfer.symbolicRegion);
              transfers.push_back(
                  std::move(transferRecord));
            }
            exitState["backedge_states"] =
                std::move(transfers);
            exitStates.push_back(std::move(exitState));
            continue;
          }

          json::Object backedge;
          json::Array backedgeSkipped;
          for (Instruction *instruction :
               state.cycleSkipped)
            backedgeSkipped.push_back(std::to_string(
                stableSiteId(*instruction)));
          backedge["skipped_nomod_sites"] =
              std::move(backedgeSkipped);
          json::Array backedgeLanes;
          for (const ByteLaneSource &lane :
               state.byteLanes) {
            json::Object laneRecord;
            laneRecord["ordinal"] =
                static_cast<int64_t>(lane.ordinal);
            laneRecord["source_kind"] =
                lane.carry
                    ? "carry"
                    : (state.cycleGuard != nullptr
                           ? "guarded-store"
                           : "store");
            laneRecord["source_site"] = std::to_string(
                lane.carry
                    ? 0
                    : stableSiteId(*lane.store));
            laneRecord["source_byte"] =
                static_cast<int64_t>(lane.sourceByte);
            laneRecord["source_width"] =
                static_cast<int64_t>(lane.sourceWidth);
            backedgeLanes.push_back(
                std::move(laneRecord));
          }
          backedge["lanes"] =
              std::move(backedgeLanes);
          if (state.cycleSymbolicRegion != nullptr)
            backedge["symbolic_region_writer"] =
                symbolicRegionRecord(
                    *state.cycleSymbolicRegion);
          if (!state.cycleSymbolicRegions.empty()) {
            json::Array writers;
            for (unsigned writerIndex = 0;
                 writerIndex < state.cycleSymbolicRegions.size();
                 ++writerIndex) {
              json::Object writer = symbolicRegionRecord(
                  state.cycleSymbolicRegions[writerIndex]);
              writer["ordinal"] =
                  static_cast<int64_t>(writerIndex);
              writers.push_back(std::move(writer));
            }
            backedge["symbolic_region_writers"] =
                std::move(writers);
          }
          exitState["backedge_state"] =
              std::move(backedge);
          exitStates.push_back(std::move(exitState));
          continue;
        }
        json::Array lanes;
        for (const ByteLaneSource &lane : state.byteLanes) {
          json::Object laneRecord;
          laneRecord["ordinal"] =
              static_cast<int64_t>(lane.ordinal);
          laneRecord["source_kind"] =
              lane.store == nullptr ? "live-on-entry" : "store";
          laneRecord["source_site"] = std::to_string(
              lane.store == nullptr ? 0 : stableSiteId(*lane.store));
          laneRecord["source_byte"] =
              static_cast<int64_t>(lane.sourceByte);
          laneRecord["source_width"] =
              static_cast<int64_t>(lane.sourceWidth);
          if (!orderedWriterGraph && guardedByteComposition &&
              lane.hasGuardedSource()) {
            auto guardedRecord =
                [&](const GuardedByteOverlay &overlay) {
                  json::Object guarded;
                  if (guardedWritePriority)
                    guarded["priority"] =
                        static_cast<int64_t>(
                            overlay.priority);
                  guarded["guard_site"] = std::to_string(
                      stableSiteId(*overlay.guard));
                  guarded["store_when"] =
                      overlay.storeWhenTrue ? "true" : "false";
                  guarded["source_site"] = std::to_string(
                      stableSiteId(*overlay.store));
                  guarded["source_byte"] =
                      static_cast<int64_t>(overlay.sourceByte);
                  guarded["source_width"] =
                      static_cast<int64_t>(overlay.sourceWidth);
                  return guarded;
                };
            if (guardedWritePriority) {
              json::Array guardedSources;
              for (const GuardedByteOverlay &overlay :
                   lane.guarded)
                guardedSources.push_back(
                    guardedRecord(overlay));
              laneRecord["guarded_sources"] =
                  std::move(guardedSources);
            } else {
              laneRecord["guarded_source"] =
                  guardedRecord(lane.guarded.front());
            }
          }
          lanes.push_back(std::move(laneRecord));
        }
        exitState["lanes"] = std::move(lanes);
        if (orderedWriterGraph) {
          json::Array layers;
          for (const OrderedWriterLayer &layer :
               state.writerLayers) {
            json::Object layerRecord;
            layerRecord["ordinal"] =
                static_cast<int64_t>(layer.ordinal);
            if (layer.kind ==
                OrderedWriterKind::PointerPartition) {
              layerRecord["kind"] = "pointer-partition";
              layerRecord["pointer_partition"] =
                  pointerPartitionRecord(*layer.pointerPartition);
            } else if (
                layer.kind ==
                OrderedWriterKind::SymbolicRegion) {
              layerRecord["kind"] = "symbolic-region-write";
              layerRecord["store_site"] =
                  std::to_string(stableSiteId(*layer.store));
              layerRecord["store_width"] =
                  static_cast<int64_t>(layer.storeWidth);
              layerRecord["region_base_site"] =
                  std::to_string(
                      stableSiteId(*layer.regionBase));
              layerRecord["region_extent"] =
                  std::to_string(layer.regionExtent);
              layerRecord["index_site"] =
                  std::to_string(
                      stableSiteId(*layer.symbolicIndex));
              layerRecord["index_bits"] =
                  static_cast<int64_t>(
                      layer.symbolicIndexBits);
              layerRecord["base_offset"] =
                  layer.symbolicBaseOffset;
              json::Array laneCases;
              for (unsigned lane = 0;
                   lane < layer.symbolicLaneCases.size();
                   ++lane) {
                json::Object laneRecord;
                laneRecord["ordinal"] =
                    static_cast<int64_t>(lane);
                json::Array cases;
                for (unsigned caseOrdinal = 0;
                     caseOrdinal <
                         layer.symbolicLaneCases[lane].size();
                     ++caseOrdinal) {
                  const SymbolicRegionCase &item =
                      layer.symbolicLaneCases[lane][caseOrdinal];
                  json::Object caseRecord;
                  caseRecord["ordinal"] =
                      static_cast<int64_t>(caseOrdinal);
                  caseRecord["index_value"] =
                      item.indexValue;
                  caseRecord["source_byte"] =
                      static_cast<int64_t>(
                          item.sourceByte);
                  cases.push_back(std::move(caseRecord));
                }
                laneRecord["cases"] = std::move(cases);
                laneCases.push_back(std::move(laneRecord));
              }
              layerRecord["lane_cases"] =
                  std::move(laneCases);
            } else {
              layerRecord["kind"] = "guarded-write";
              layerRecord["store_site"] =
                  std::to_string(stableSiteId(*layer.store));
              layerRecord["store_width"] =
                  static_cast<int64_t>(layer.storeWidth);
              layerRecord["guard_site"] =
                  std::to_string(stableSiteId(*layer.guard));
              json::Array sources;
              json::Array polarities;
              for (unsigned lane = 0;
                   lane < layer.sourceBytes.size(); ++lane) {
                sources.push_back(static_cast<int64_t>(
                    layer.sourceBytes[lane]));
                polarities.push_back(
                    layer.storePolarities[lane] < 0
                        ? "none"
                        : (layer.storePolarities[lane] != 0
                               ? "true"
                               : "false"));
              }
              layerRecord["lane_source_bytes"] =
                  std::move(sources);
              layerRecord["lane_store_when"] =
                  std::move(polarities);
            }
            layers.push_back(std::move(layerRecord));
          }
          exitState["writer_layers"] = std::move(layers);
        } else if (state.pointerPartition != nullptr) {
          exitState["pointer_partition"] =
              pointerPartitionRecord(*state.pointerPartition);
        }
        exitStates.push_back(std::move(exitState));
        continue;
      }
      if (nested) {
        exitState["root_node"] = static_cast<int64_t>(0);
        exitState["provenance_nodes"] =
            provenanceRecords(state);
        exitStates.push_back(std::move(exitState));
        continue;
      }
      if (extended)
        exitState["state_kind"] =
            state.kind == MemoryStateKind::Store
                ? "store"
                : "live-on-entry";
      if (state.kind == MemoryStateKind::Store)
        exitState["store_site"] =
            std::to_string(stableSiteId(*state.store));
      json::Array skipped;
      for (Instruction *instruction : state.skipped)
        skipped.push_back(
            std::to_string(stableSiteId(*instruction)));
      exitState["skipped_nomod_sites"] = std::move(skipped);
      exitStates.push_back(std::move(exitState));
    }
    item["exit_states"] = std::move(exitStates);
    memoryRecords.push_back(std::move(item));
  }
  record["memory_slots"] = std::move(memoryRecords);
  record["proof_fingerprint"] = std::to_string(fingerprint);
  return formatv("{0}", json::Value(std::move(record))).str();
}

void emitContinuationManifests(Function &function,
                               AAResults &aliasAnalysis,
                               MemorySSA &memorySSA) {
  const char *path =
      std::getenv("SYMCC_IFSS_CONTINUATION_MANIFEST_OUT");
  if (path == nullptr || *path == '\0')
    return;

  DominatorTree dominators(function);
  SmallVector<std::string, 2> records;
  for (BasicBlock &block : function)
    for (PHINode &phi : block.phis()) {
      ContinuationSummary summary;
      if (!phi.getType()->isIntegerTy(8) ||
          !parseContinuationSummary(phi, summary))
        continue;
      std::string record = buildContinuationManifestLine(
          function, phi, summary, aliasAnalysis, memorySSA,
          dominators);
      if (!record.empty())
        records.push_back(std::move(record));
    }
  if (records.empty())
    return;

  (void)appendManifestRecords(path, records);
}

bool lowerContinuationMemory(Function &function, AAResults &aliasAnalysis,
                             MemorySSA &memorySSA) {
  if (!enabled(std::getenv("SYMCC_IFSS_CONTINUATION_MEMORY")))
    return false;

  DominatorTree dominators(function);
  std::vector<MemoryCandidate> candidates;
  for (BasicBlock &block : function)
    for (Instruction &instruction : block)
      if (auto *load = dyn_cast<LoadInst>(&instruction)) {
        MemoryCandidate candidate;
        if (collectCandidate(
                *load, aliasAnalysis, memorySSA, dominators, candidate))
          candidates.push_back(std::move(candidate));
      }
  if (candidates.empty())
    return false;

  DenseMap<PHINode *, unsigned> slotCounts;
  SmallPtrSet<PHINode *, 8> rejectedControllers;
  for (const MemoryCandidate &candidate : candidates)
    if (++slotCounts[candidate.exitId] >
        kMaxContinuationMemorySlots)
      rejectedControllers.insert(candidate.exitId);

  DenseMap<PHINode *, unsigned> nextSlot;
  bool changed = false;
  for (MemoryCandidate &candidate : candidates) {
    if (rejectedControllers.count(candidate.exitId) != 0)
      continue;
    lowerCandidate(candidate, nextSlot[candidate.exitId]++);
    changed = true;
  }
  return changed;
}

} // namespace

char IFSSContinuationMemoryLegacyPass::ID = 0;

bool IFSSContinuationMemoryLegacyPass::runOnFunction(Function &function) {
  auto &aliasAnalysis =
      getAnalysis<AAResultsWrapperPass>().getAAResults();
  auto &memorySSA = getAnalysis<MemorySSAWrapperPass>().getMSSA();
  bool changed =
      lowerContinuationMemory(function, aliasAnalysis, memorySSA);
  emitContinuationManifests(function, aliasAnalysis, memorySSA);
  return changed;
}

void IFSSContinuationMemoryLegacyPass::getAnalysisUsage(
    AnalysisUsage &usage) const {
  usage.addRequired<AAResultsWrapperPass>();
  usage.addRequired<MemorySSAWrapperPass>();
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses IFSSContinuationMemoryPass::run(
    Function &function, FunctionAnalysisManager &analyses) {
  auto &aliasAnalysis = analyses.getResult<AAManager>(function);
  auto &memorySSA =
      analyses.getResult<MemorySSAAnalysis>(function).getMSSA();
  bool changed =
      lowerContinuationMemory(function, aliasAnalysis, memorySSA);
  emitContinuationManifests(function, aliasAnalysis, memorySSA);
  return changed ? PreservedAnalyses::none()
                 : PreservedAnalyses::all();
}
#endif

} // namespace symcc
