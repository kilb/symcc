// This file is part of SymCC.
//
// Hydra-style targeted control-flow melding for expensive symbolic branches.
// The aggressive memory mode is failure-preserving rather than semantics-
// preserving; util/hydra_transform.py therefore replays every reported failure
// on the original binary before it can be retained.

#include "HydraTransformation.h"

#include "ManifestWriter.h"
#include "SiteId.h"

#include <llvm/Analysis/PostDominators.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/Config/llvm-config.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/ValueMap.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/FormatVariadic.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/MemoryBuffer.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Transforms/Utils/BasicBlockUtils.h>
#include <llvm/Transforms/Utils/Cloning.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <optional>
#include <set>
#include <string>
#include <system_error>
#include <tuple>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr char kManifestSchema[] = "symcc-hydra-transform-v1";
constexpr char kProfileSchemaV1[] = "symcc-hydra-profile-v1";
constexpr char kProfileSchemaV2[] = "symcc-hydra-profile-v2";
constexpr char kMultiBlockSchema[] =
    "bounded-multiblock-linear-hydra-v1";
constexpr char kUnequalLinearSchema[] =
    "bounded-unequal-linear-hydra-v2";
constexpr char kInternalTreeSchema[] =
    "bounded-internal-tree-hydra-v3";
constexpr char kInternalDagSchema[] =
    "bounded-acyclic-sese-dag-hydra-v4";
constexpr char kSharedPredicateDagSchema[] =
    "bounded-shared-predicate-sese-dag-hydra-v5";
constexpr char kCrossRegionSharedPredicateDagSchema[] =
    "bounded-cross-region-shared-predicate-sese-dag-hydra-v6";
constexpr char kLlvmSemanticsPolicy[] =
    "llvm-poison-undef-freeze-refinement-v1";
constexpr char kInactiveOperandPolicy[] =
    "path-guarded-safe-constants-v1";
constexpr char kFreezePolicy[] =
    "one-dynamic-instance-per-alignment-slot-v1";
constexpr char kExceptionPolicy[] =
    "reject-non-branch-terminators-and-eh-pads-v1";
constexpr unsigned kMaxArmBlocks = 4;
constexpr unsigned kMaxTreeArmBlocks = 7;
constexpr unsigned kMaxInternalBranches = 3;
constexpr unsigned kMaxArmLeaves = 4;
constexpr unsigned kMaxDagArmBlocks = 10;
constexpr unsigned kMaxDagInternalBranches = 4;
constexpr unsigned kMaxDagArmLeaves = 8;
constexpr unsigned kMaxDagLocalMerges = 3;
constexpr unsigned kMaxArmInstructions = 64;
constexpr unsigned kMaxSharedDagArmBlocks = 14;
constexpr unsigned kMaxSharedDagInternalBranches = 5;
constexpr unsigned kMaxSharedDagArmLeaves = 10;
constexpr unsigned kMaxSharedDagLocalMerges = 4;
constexpr unsigned kMaxSharedDagLocalPhis = 12;
constexpr unsigned kMaxSharedDagArmInstructions = 96;
constexpr unsigned kMaxCrossRegionSites = 4;

struct DagBudget {
  unsigned blocks = 0;
  unsigned internalBranches = 0;
  unsigned leaves = 0;
  unsigned localMerges = 0;
  unsigned instructions = 0;
};

constexpr DagBudget kDagV4Budget{
    kMaxDagArmBlocks, kMaxDagInternalBranches, kMaxDagArmLeaves,
    kMaxDagLocalMerges, kMaxArmInstructions};
constexpr DagBudget kSharedDagV5Budget{
    kMaxSharedDagArmBlocks, kMaxSharedDagInternalBranches,
    kMaxSharedDagArmLeaves, kMaxSharedDagLocalMerges,
    kMaxSharedDagArmInstructions};

struct ProfileEntry {
  uint64_t site = 0;
  double score = 0.0;
  uint64_t observations = 0;
  uint64_t interesting = 0;
  uint64_t solverTimeUs = 0;
};

struct ProfileDocument {
  bool supplied = false;
  bool valid = false;
  std::string schema;
  std::string selectionSource = "implicit";
  std::string profileSha256;
  std::string profiledExecutableSha256;
  std::string profiledCommandSha256;
  std::vector<ProfileEntry> entries;
};

struct AlignmentSlot {
  Instruction *left = nullptr;
  Instruction *right = nullptr;
};

struct AlignmentProofSlot {
  uint64_t leftSite = 0;
  uint64_t rightSite = 0;
  unsigned leftOpcode = 0;
  unsigned rightOpcode = 0;
  int leftBlockOrdinal = -1;
  int rightBlockOrdinal = -1;
};

struct ArmIncomingEdge {
  int blockOrdinal = -1;
  int successorIndex = -1;
};

struct ArmBlockInfo {
  BasicBlock *block = nullptr;
  BranchInst *terminator = nullptr;
  int parentOrdinal = -1;
  int incomingEdge = -1;
  SmallVector<ArmIncomingEdge, 4> predecessors;
  SmallVector<int, 2> successors;
};

struct ArmLeaf {
  unsigned blockOrdinal = 0;
  unsigned successorIndex = 0;
};

struct ArmTopologyProof {
  uint64_t blockSite = 0;
  uint64_t terminatorSite = 0;
  int parentOrdinal = -1;
  int incomingEdge = -1;
  SmallVector<ArmIncomingEdge, 4> predecessors;
  SmallVector<int, 2> successors;
};

struct Diamond {
  BranchInst *branch = nullptr;
  BasicBlock *left = nullptr;
  BasicBlock *right = nullptr;
  BasicBlock *leftExit = nullptr;
  BasicBlock *rightExit = nullptr;
  BasicBlock *merge = nullptr;
  SmallVector<BasicBlock *, kMaxTreeArmBlocks> leftBlocks;
  SmallVector<BasicBlock *, kMaxTreeArmBlocks> rightBlocks;
  SmallVector<ArmBlockInfo, kMaxTreeArmBlocks> leftTopology;
  SmallVector<ArmBlockInfo, kMaxTreeArmBlocks> rightTopology;
  SmallVector<ArmLeaf, kMaxArmLeaves> leftLeaves;
  SmallVector<ArmLeaf, kMaxArmLeaves> rightLeaves;
  SmallVector<Instruction *, 16> leftInstructions;
  SmallVector<Instruction *, 16> rightInstructions;
  SmallVector<PHINode *, 8> outputPhis;
  bool internalDag = false;
  bool sharedPredicateDag = false;
};

struct DagGuardCache {
  DenseMap<uint64_t, Value *> edges;
  unsigned reusedEdges = 0;
};

struct PredicateNegationEntry {
  Value *negation = nullptr;
  uint64_t producerSite = 0;
};

struct PredicateGuardCache {
  DenseMap<Value *, PredicateNegationEntry> negations;
  unsigned reusedNegations = 0;
  unsigned crossRegionReusedNegations = 0;
  uint64_t currentRegionSite = 0;
  BranchInst *currentInsertionPoint = nullptr;
  DominatorTree *dominators = nullptr;
  SmallVector<uint64_t, kMaxCrossRegionSites>
      currentCrossRegionSourceSites;
};

struct TransformStats {
  uint64_t site = 0;
  double profileScore = 0.0;
  uint64_t profileObservations = 0;
  uint64_t profileInteresting = 0;
  uint64_t profileSolverTimeUs = 0;
  std::string selectionSource = "implicit";
  std::string profileSchema;
  std::string profileSha256;
  std::string profiledExecutableSha256;
  std::string profiledCommandSha256;
  unsigned aligned = 0;
  unsigned extraAlu = 0;
  unsigned leftFreezes = 0;
  unsigned rightFreezes = 0;
  unsigned alignedFreezePairs = 0;
  unsigned extraFreezes = 0;
  unsigned linearizedLoads = 0;
  unsigned readbackStores = 0;
  unsigned selects = 0;
  unsigned outputs = 0;
  unsigned leftArmBlocks = 0;
  unsigned rightArmBlocks = 0;
  unsigned leftInstructionCount = 0;
  unsigned rightInstructionCount = 0;
  unsigned editDistance = 0;
  bool unequalArms = false;
  bool internalTree = false;
  bool internalDag = false;
  bool sharedPredicateDag = false;
  bool crossRegionSharedPredicateDag = false;
  unsigned leftLocalMerges = 0;
  unsigned rightLocalMerges = 0;
  unsigned canonicalGuardEdges = 0;
  unsigned reusedGuardEdges = 0;
  unsigned reusedPredicateNegations = 0;
  unsigned uniquePredicates = 0;
  unsigned reusedPredicateOccurrences = 0;
  unsigned crossRegionReusedPredicateNegations = 0;
  uint64_t structureFingerprint = 0;
  uint64_t transactionFingerprint = 0;
  unsigned transactionOrdinal = 0;
  unsigned transactionSize = 0;
  SmallVector<uint64_t, kMaxTreeArmBlocks> leftBlockSites;
  SmallVector<uint64_t, kMaxTreeArmBlocks> rightBlockSites;
  SmallVector<ArmTopologyProof, kMaxTreeArmBlocks> leftTopology;
  SmallVector<ArmTopologyProof, kMaxTreeArmBlocks> rightTopology;
  SmallVector<ArmLeaf, kMaxArmLeaves> leftLeaves;
  SmallVector<ArmLeaf, kMaxArmLeaves> rightLeaves;
  SmallVector<uint64_t, 8> leftLocalPhiSites;
  SmallVector<uint64_t, 8> rightLocalPhiSites;
  SmallVector<uint64_t, 8> leftPredicateSites;
  SmallVector<uint64_t, 8> rightPredicateSites;
  SmallVector<uint64_t, kMaxCrossRegionSites> transactionSites;
  SmallVector<uint64_t, 8> transactionSharedPredicateSites;
  SmallVector<uint64_t, kMaxCrossRegionSites> crossRegionSourceSites;
  SmallVector<uint64_t, 8> outputPhiSites;
  SmallVector<AlignmentProofSlot, 32> alignment;
};

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

std::optional<uint64_t> parseUnsigned(StringRef text) {
  uint64_t value = 0;
  if (text.trim().getAsInteger(0, value) || value == 0)
    return std::nullopt;
  return value;
}

bool parseSiteSequence(
    const char *text, SmallVectorImpl<uint64_t> &sites) {
  if (text == nullptr || *text == '\0')
    return false;
  SmallVector<StringRef, kMaxCrossRegionSites + 1> fields;
  StringRef(text).split(fields, ',', -1, false);
  if (fields.size() < 2 ||
      fields.size() > kMaxCrossRegionSites)
    return false;
  std::set<uint64_t> unique;
  for (StringRef field : fields) {
    auto site = parseUnsigned(field);
    if (!site || !unique.insert(*site).second)
      return false;
    sites.push_back(*site);
  }
  return true;
}

std::optional<double> parseFiniteScore(StringRef text) {
  std::string storage = text.trim().str();
  if (storage.empty())
    return std::nullopt;
  char *end = nullptr;
  errno = 0;
  double value = std::strtod(storage.c_str(), &end);
  if (errno != 0 || end == storage.c_str() || *end != '\0' ||
      !std::isfinite(value))
    return std::nullopt;
  return value;
}

double parseScore(StringRef text) {
  return parseFiniteScore(text).value_or(0.0);
}

std::optional<uint64_t> parseNonnegative(StringRef text) {
  uint64_t value = 0;
  if (text.trim().getAsInteger(0, value))
    return std::nullopt;
  return value;
}

bool isSha256(StringRef text) {
  if (text.size() != 64)
    return false;
  return std::all_of(text.begin(), text.end(), [](char character) {
    return (character >= '0' && character <= '9') ||
           (character >= 'a' && character <= 'f');
  });
}

bool startsWith(StringRef text, StringRef prefix) {
#if LLVM_VERSION_MAJOR >= 18
  return text.starts_with(prefix);
#else
  return text.startswith(prefix);
#endif
}

ProfileDocument readProfile(StringRef path) {
  ProfileDocument document;
  if (path.empty()) {
    document.valid = true;
    return document;
  }
  document.supplied = true;
  auto buffer = MemoryBuffer::getFile(path);
  if (!buffer)
    return document;
  SmallVector<StringRef, 64> lines;
  buffer.get()->getBuffer().split(lines, '\n');
  bool malformed = false;
  std::set<uint64_t> sites;
  for (StringRef line : lines) {
    line = line.trim();
    if (line.empty())
      continue;
    if (startsWith(line, "#")) {
      if (line == "# symcc-hydra-profile-v1") {
        if (!document.schema.empty())
          malformed = true;
        document.schema = kProfileSchemaV1;
        document.selectionSource = "profile-v1";
      } else if (line == "# symcc-hydra-profile-v2") {
        if (!document.schema.empty())
          malformed = true;
        document.schema = kProfileSchemaV2;
        document.selectionSource = "profile-v2";
      } else if (startsWith(line, "# profile_sha256 ")) {
        if (!document.profileSha256.empty())
          malformed = true;
        document.profileSha256 =
            line.drop_front(StringRef("# profile_sha256 ").size()).str();
      } else if (startsWith(
                     line, "# profiled_executable_sha256 ")) {
        if (!document.profiledExecutableSha256.empty())
          malformed = true;
        document.profiledExecutableSha256 =
            line.drop_front(
                    StringRef("# profiled_executable_sha256 ").size())
                .str();
      } else if (startsWith(line, "# profiled_command_sha256 ")) {
        if (!document.profiledCommandSha256.empty())
          malformed = true;
        document.profiledCommandSha256 =
            line.drop_front(
                    StringRef("# profiled_command_sha256 ").size())
                .str();
      }
      continue;
    }
    SmallVector<StringRef, 8> fields;
    line.split(fields, ' ', -1, false);
    if (document.schema.empty()) {
      malformed = true;
      continue;
    }
    const bool strict = document.schema == kProfileSchemaV2;
    if (fields.size() < 2 || (strict && fields.size() != 5)) {
      malformed = malformed || strict;
      continue;
    }
    auto site = parseUnsigned(fields[0]);
    auto parsedScore = parseFiniteScore(fields[1]);
    const double score = parsedScore.value_or(0.0);
    if (!site || !parsedScore || score < 0.0 ||
        (strict && !sites.insert(*site).second)) {
      malformed = malformed || strict;
      continue;
    }
    ProfileEntry entry;
    entry.site = *site;
    entry.score = score;
    auto observations =
        fields.size() > 2 ? parseNonnegative(fields[2])
                          : std::optional<uint64_t>(0);
    auto interesting =
        fields.size() > 3 ? parseNonnegative(fields[3])
                          : std::optional<uint64_t>(0);
    auto solverTime =
        fields.size() > 4 ? parseNonnegative(fields[4])
                          : std::optional<uint64_t>(0);
    if (!observations || !interesting || !solverTime ||
        (strict && (*observations == 0 ||
                    *interesting > *observations))) {
      malformed = malformed || strict;
      continue;
    }
    entry.observations = *observations;
    entry.interesting = *interesting;
    entry.solverTimeUs = *solverTime;
    document.entries.push_back(entry);
  }
  if (document.schema == kProfileSchemaV1)
    document.valid = !malformed;
  else if (document.schema == kProfileSchemaV2)
    document.valid =
        !malformed && isSha256(document.profileSha256) &&
        isSha256(document.profiledExecutableSha256) &&
        isSha256(document.profiledCommandSha256);
  return document;
}

std::set<uint64_t> readDenylist(StringRef path) {
  std::set<uint64_t> sites;
  if (path.empty())
    return sites;
  auto buffer = MemoryBuffer::getFile(path);
  if (!buffer)
    return sites;
  SmallVector<StringRef, 64> lines;
  buffer.get()->getBuffer().split(lines, '\n');
  for (StringRef line : lines) {
    line = line.trim();
    if (line.empty() ||
#if LLVM_VERSION_MAJOR >= 18
        line.starts_with("#")
#else
        line.startswith("#")
#endif
    )
      continue;
    if (auto site = parseUnsigned(line.split(' ').first))
      sites.insert(*site);
  }
  return sites;
}

bool isSupportedAlu(const Instruction &instruction) {
  return isa<BinaryOperator>(instruction) || isa<UnaryOperator>(instruction) ||
         isa<CmpInst>(instruction) || isa<CastInst>(instruction) ||
         isa<SelectInst>(instruction) || isa<GetElementPtrInst>(instruction) ||
         isa<FreezeInst>(instruction);
}

bool isSupportedMemory(const Instruction &instruction, bool aggressive) {
  if (!aggressive)
    return false;
  if (const auto *load = dyn_cast<LoadInst>(&instruction))
    return load->isSimple();
  if (const auto *store = dyn_cast<StoreInst>(&instruction))
    return store->isSimple();
  return false;
}

bool isSupportedInstruction(const Instruction &instruction, bool aggressive) {
  return isSupportedAlu(instruction) ||
         isSupportedMemory(instruction, aggressive);
}

bool containsBlock(ArrayRef<BasicBlock *> blocks,
                   const BasicBlock *candidate) {
  return std::find(blocks.begin(), blocks.end(), candidate) !=
         blocks.end();
}

bool collectArmBlock(
    BasicBlock *current, BasicBlock *predecessor, int incomingEdge,
    SmallVectorImpl<BasicBlock *> &blocks,
    SmallVectorImpl<ArmBlockInfo> &topology,
    SmallVectorImpl<ArmLeaf> &leaves, BasicBlock *&merge,
    bool aggressive, SmallVectorImpl<Instruction *> &instructions,
    unsigned &internalBranches, std::string &reason) {
  if (blocks.size() >= kMaxTreeArmBlocks ||
      current->hasAddressTaken() || current->isEHPad() ||
      current->getSinglePredecessor() != predecessor ||
      containsBlock(blocks, current)) {
    reason = "non-tree-arm";
    return false;
  }

  const unsigned ordinal = blocks.size();
  blocks.push_back(current);
  ArmBlockInfo info;
  info.block = current;
  info.parentOrdinal =
      predecessor == nullptr || ordinal == 0
          ? -1
          : static_cast<int>(
                std::find(blocks.begin(), blocks.end() - 1, predecessor) -
                blocks.begin());
  info.incomingEdge = ordinal == 0 ? -1 : incomingEdge;
  if (info.parentOrdinal >= 0)
    info.predecessors.push_back(
        {info.parentOrdinal, info.incomingEdge});
  auto *terminator = dyn_cast<BranchInst>(current->getTerminator());
  if (terminator == nullptr ||
      (terminator->isConditional() &&
       terminator->getSuccessor(0) == terminator->getSuccessor(1))) {
    reason = "non-tree-arm";
    return false;
  }
  info.terminator = terminator;
  if (terminator->isConditional() &&
      ++internalBranches > kMaxInternalBranches) {
    reason = "internal-branch-budget";
    return false;
  }
  topology.push_back(info);

  for (Instruction &instruction : *current) {
    if (instruction.isTerminator() ||
        isa<DbgInfoIntrinsic>(instruction))
      continue;
    if (isa<PHINode>(instruction) ||
        !isSupportedInstruction(instruction, aggressive)) {
      reason = "unsupported-instruction";
      return false;
    }
    if (instructions.size() >= kMaxArmInstructions) {
      reason = "arm-instruction-budget";
      return false;
    }
    instructions.push_back(&instruction);
  }

  const unsigned successorCount = terminator->getNumSuccessors();
  for (unsigned successorIndex = 0;
       successorIndex < successorCount; ++successorIndex) {
    BasicBlock *successor = terminator->getSuccessor(successorIndex);
    if (successor == current || containsBlock(blocks, successor)) {
      reason = "cyclic-tree-arm";
      return false;
    }
    if (pred_size(successor) != 1) {
      if (merge != nullptr && merge != successor) {
        reason = "multi-exit";
        return false;
      }
      if (leaves.size() >= kMaxArmLeaves ||
          std::any_of(leaves.begin(), leaves.end(),
                      [ordinal](const ArmLeaf &leaf) {
            return leaf.blockOrdinal == ordinal;
          })) {
        reason = "arm-leaf-budget";
        return false;
      }
      merge = successor;
      leaves.push_back({ordinal, successorIndex});
      topology[ordinal].successors.push_back(-1);
      continue;
    }

    const int childOrdinal = static_cast<int>(blocks.size());
    topology[ordinal].successors.push_back(childOrdinal);
    if (!collectArmBlock(
            successor, current, static_cast<int>(successorIndex),
            blocks, topology, leaves, merge, aggressive, instructions,
            internalBranches, reason))
      return false;
  }
  return true;
}

bool collectArm(
    BasicBlock *start, BasicBlock *entry,
    SmallVectorImpl<BasicBlock *> &blocks,
    SmallVectorImpl<ArmBlockInfo> &topology,
    SmallVectorImpl<ArmLeaf> &leaves, BasicBlock *&merge,
    bool aggressive, SmallVectorImpl<Instruction *> &instructions,
    std::string &reason) {
  unsigned internalBranches = 0;
  if (!collectArmBlock(
          start, entry, -1, blocks, topology, leaves, merge, aggressive,
          instructions, internalBranches, reason))
    return false;
  if (merge == nullptr || leaves.empty()) {
    reason = "multi-exit";
    return false;
  }
  return true;
}

bool collectDagArm(
    BasicBlock *start, BasicBlock *entry, BasicBlock *merge,
    SmallVectorImpl<BasicBlock *> &blocks,
    SmallVectorImpl<ArmBlockInfo> &topology,
    SmallVectorImpl<ArmLeaf> &leaves, bool aggressive,
    SmallVectorImpl<Instruction *> &instructions,
    unsigned &internalBranches, unsigned &localMerges,
    const DagBudget &budget, std::string &reason) {
  SmallVector<BasicBlock *, kMaxDagArmBlocks> discovered;
  SmallVector<BasicBlock *, kMaxDagArmBlocks> worklist;
  worklist.push_back(start);
  while (!worklist.empty()) {
    BasicBlock *current = worklist.pop_back_val();
    if (current == merge || containsBlock(discovered, current))
      continue;
    if (current == entry || discovered.size() >= budget.blocks ||
        current->hasAddressTaken() || current->isEHPad()) {
      reason = "dag-block-budget-or-entry";
      return false;
    }
    auto *terminator = dyn_cast<BranchInst>(current->getTerminator());
    if (terminator == nullptr ||
        (terminator->isConditional() &&
         terminator->getSuccessor(0) == terminator->getSuccessor(1))) {
      reason = "non-dag-arm";
      return false;
    }
    discovered.push_back(current);
    for (unsigned successorIndex = 0;
         successorIndex < terminator->getNumSuccessors();
         ++successorIndex) {
      BasicBlock *successor =
          terminator->getSuccessor(successorIndex);
      if (successor != merge)
        worklist.push_back(successor);
    }
  }
  if (discovered.empty()) {
    reason = "empty-dag-arm";
    return false;
  }

  DenseMap<BasicBlock *, unsigned> discoveryOrdinal;
  for (unsigned ordinal = 0; ordinal < discovered.size(); ++ordinal)
    discoveryOrdinal[discovered[ordinal]] = ordinal;
  SmallVector<unsigned, kMaxDagArmBlocks> indegree(
      discovered.size(), 0);
  for (BasicBlock *block : discovered) {
    for (BasicBlock *predecessor : predecessors(block)) {
      if (block == start && predecessor == entry)
        continue;
      auto found = discoveryOrdinal.find(predecessor);
      if (found == discoveryOrdinal.end()) {
        reason = "external-dag-predecessor";
        return false;
      }
      ++indegree[discoveryOrdinal[block]];
    }
  }
  if (indegree[discoveryOrdinal[start]] != 0 ||
      std::count(indegree.begin(), indegree.end(), 0U) != 1) {
    reason = "non-single-entry-dag";
    return false;
  }

  SmallVector<BasicBlock *, kMaxDagArmBlocks> ready;
  ready.push_back(start);
  while (!ready.empty()) {
    std::sort(
        ready.begin(), ready.end(),
        [](BasicBlock *left, BasicBlock *right) {
          return stableSiteId(*left) > stableSiteId(*right);
        });
    BasicBlock *block = ready.pop_back_val();
    blocks.push_back(block);
    auto *terminator = cast<BranchInst>(block->getTerminator());
    for (unsigned successorIndex = 0;
         successorIndex < terminator->getNumSuccessors();
         ++successorIndex) {
      BasicBlock *successor =
          terminator->getSuccessor(successorIndex);
      if (successor == merge)
        continue;
      auto found = discoveryOrdinal.find(successor);
      if (found == discoveryOrdinal.end() || indegree[found->second] == 0) {
        reason = "cyclic-or-multi-exit-dag";
        return false;
      }
      if (--indegree[found->second] == 0)
        ready.push_back(successor);
    }
  }
  if (blocks.size() != discovered.size() ||
      blocks.front() != start) {
    reason = "cyclic-dag-arm";
    return false;
  }

  DenseMap<BasicBlock *, int> ordinalByBlock;
  for (unsigned ordinal = 0; ordinal < blocks.size(); ++ordinal)
    ordinalByBlock[blocks[ordinal]] = static_cast<int>(ordinal);
  for (unsigned ordinal = 0; ordinal < blocks.size(); ++ordinal) {
    BasicBlock *block = blocks[ordinal];
    ArmBlockInfo info;
    info.block = block;
    info.terminator = cast<BranchInst>(block->getTerminator());
    for (BasicBlock *predecessor : predecessors(block)) {
      if (ordinal == 0 && predecessor == entry)
        continue;
      auto predecessorOrdinal = ordinalByBlock.find(predecessor);
      if (predecessorOrdinal == ordinalByBlock.end() ||
          predecessorOrdinal->second >= static_cast<int>(ordinal)) {
        reason = "non-topological-dag-predecessor";
        return false;
      }
      auto *predecessorTerminator =
          dyn_cast<BranchInst>(predecessor->getTerminator());
      int incomingEdge = -1;
      if (predecessorTerminator != nullptr) {
        for (unsigned successorIndex = 0;
             successorIndex <
             predecessorTerminator->getNumSuccessors();
             ++successorIndex) {
          if (predecessorTerminator->getSuccessor(successorIndex) ==
              block) {
            if (incomingEdge >= 0) {
              reason = "duplicate-dag-edge";
              return false;
            }
            incomingEdge = static_cast<int>(successorIndex);
          }
        }
      }
      if (incomingEdge < 0) {
        reason = "missing-dag-edge";
        return false;
      }
      info.predecessors.push_back(
          {predecessorOrdinal->second, incomingEdge});
    }
    std::sort(
        info.predecessors.begin(), info.predecessors.end(),
        [](const ArmIncomingEdge &left,
           const ArmIncomingEdge &right) {
          return std::tie(left.blockOrdinal, left.successorIndex) <
                 std::tie(right.blockOrdinal, right.successorIndex);
        });
    if (ordinal == 0) {
      if (!info.predecessors.empty() ||
          block->getSinglePredecessor() != entry) {
        reason = "non-single-entry-dag";
        return false;
      }
    } else if (info.predecessors.empty()) {
      reason = "unreachable-dag-block";
      return false;
    }
    if (info.predecessors.size() == 1) {
      info.parentOrdinal = info.predecessors.front().blockOrdinal;
      info.incomingEdge =
          info.predecessors.front().successorIndex;
    } else if (info.predecessors.size() > 1 &&
               ++localMerges > budget.localMerges) {
      reason = "dag-local-merge-budget";
      return false;
    }

    if (info.terminator->isConditional() &&
        ++internalBranches > budget.internalBranches) {
      reason = "dag-internal-branch-budget";
      return false;
    }
    for (unsigned successorIndex = 0;
         successorIndex < info.terminator->getNumSuccessors();
         ++successorIndex) {
      BasicBlock *successor =
          info.terminator->getSuccessor(successorIndex);
      if (successor == merge) {
        if (leaves.size() >= budget.leaves ||
            std::any_of(
                leaves.begin(), leaves.end(),
                [ordinal](const ArmLeaf &leaf) {
                  return leaf.blockOrdinal == ordinal;
                })) {
          reason = "dag-leaf-budget";
          return false;
        }
        leaves.push_back({ordinal, successorIndex});
        info.successors.push_back(-1);
        continue;
      }
      auto successorOrdinal = ordinalByBlock.find(successor);
      if (successorOrdinal == ordinalByBlock.end() ||
          successorOrdinal->second <= static_cast<int>(ordinal)) {
        reason = "cyclic-or-multi-exit-dag";
        return false;
      }
      info.successors.push_back(successorOrdinal->second);
    }
    topology.push_back(std::move(info));

    for (Instruction &instruction : *block) {
      if (instruction.isTerminator() ||
          isa<DbgInfoIntrinsic>(instruction))
        continue;
      if (auto *phi = dyn_cast<PHINode>(&instruction)) {
        if (ordinal == 0 ||
            phi->getNumIncomingValues() !=
                topology.back().predecessors.size() ||
            !phi->getType()->isFirstClassType() ||
            phi->getType()->isTokenTy()) {
          reason = "unsupported-dag-phi";
          return false;
        }
        for (const ArmIncomingEdge &incoming :
             topology.back().predecessors) {
          if (phi->getBasicBlockIndex(
                  blocks[incoming.blockOrdinal]) < 0) {
            reason = "unsupported-dag-phi";
            return false;
          }
        }
      } else if (!isSupportedInstruction(instruction, aggressive)) {
        reason = "unsupported-instruction";
        return false;
      }
      if (instructions.size() >= budget.instructions) {
        reason = "arm-instruction-budget";
        return false;
      }
      instructions.push_back(&instruction);
    }
  }
  if (leaves.empty()) {
    reason = "dag-has-no-merge-edge";
    return false;
  }
  return true;
}

bool hasOnlyDiamondUse(const Instruction &instruction,
                       const Diamond &diamond) {
  const ArrayRef<BasicBlock *> arm =
      containsBlock(diamond.leftBlocks, instruction.getParent())
          ? ArrayRef<BasicBlock *>(diamond.leftBlocks)
          : ArrayRef<BasicBlock *>(diamond.rightBlocks);
  for (const User *user : instruction.users()) {
    const auto *use = dyn_cast<Instruction>(user);
    if (use == nullptr)
      return false;
    if (containsBlock(arm, use->getParent()))
      continue;
    const auto *phi = dyn_cast<PHINode>(use);
    if (phi == nullptr || phi->getParent() != diamond.merge)
      return false;
  }
  return true;
}

bool collectTreeDiamond(BranchInst &branch, bool aggressive,
                        Diamond &result, std::string &reason) {
  if (!branch.isConditional()) {
    reason = "not-conditional";
    return false;
  }
  BasicBlock *entry = branch.getParent();
  BasicBlock *left = branch.getSuccessor(0);
  BasicBlock *right = branch.getSuccessor(1);
  if (left == right || left == entry || right == entry ||
      left->hasAddressTaken() || right->hasAddressTaken()) {
    reason = "not-single-entry";
    return false;
  }

  BasicBlock *leftMerge = nullptr;
  BasicBlock *rightMerge = nullptr;
  if (!collectArm(
          left, entry, result.leftBlocks, result.leftTopology,
          result.leftLeaves, leftMerge, aggressive,
          result.leftInstructions, reason) ||
      !collectArm(
          right, entry, result.rightBlocks, result.rightTopology,
          result.rightLeaves, rightMerge, aggressive,
          result.rightInstructions, reason))
    return false;
  if (leftMerge != rightMerge) {
    reason = "non-isomorphic-arms";
    return false;
  }
  const bool internalTree =
      std::any_of(
          result.leftTopology.begin(), result.leftTopology.end(),
          [](const ArmBlockInfo &info) {
            return info.terminator->isConditional();
          }) ||
      std::any_of(
          result.rightTopology.begin(), result.rightTopology.end(),
          [](const ArmBlockInfo &info) {
            return info.terminator->isConditional();
          });
  if (!internalTree &&
      (result.leftBlocks.size() > kMaxArmBlocks ||
       result.rightBlocks.size() > kMaxArmBlocks)) {
    reason = "linear-arm-budget";
    return false;
  }
  if (result.leftInstructions.empty() &&
      result.rightInstructions.empty()) {
    reason = "empty-alignment";
    return false;
  }
  BasicBlock *merge = leftMerge;
  if (merge == nullptr || merge == entry ||
      containsBlock(result.leftBlocks, merge) ||
      containsBlock(result.rightBlocks, merge) ||
      pred_size(merge) !=
          result.leftLeaves.size() + result.rightLeaves.size()) {
    reason = "multi-exit";
    return false;
  }

  result.branch = &branch;
  result.left = left;
  result.right = right;
  result.leftExit =
      result.leftBlocks[result.leftLeaves.front().blockOrdinal];
  result.rightExit =
      result.rightBlocks[result.rightLeaves.front().blockOrdinal];
  result.merge = merge;
  SmallVector<BasicBlock *, 2 * kMaxArmLeaves> leafBlocks;
  for (const ArmLeaf &leaf : result.leftLeaves)
    leafBlocks.push_back(result.leftBlocks[leaf.blockOrdinal]);
  for (const ArmLeaf &leaf : result.rightLeaves)
    leafBlocks.push_back(result.rightBlocks[leaf.blockOrdinal]);
  for (BasicBlock *predecessor : predecessors(merge)) {
    if (!containsBlock(leafBlocks, predecessor) ||
        std::count(leafBlocks.begin(), leafBlocks.end(), predecessor) != 1) {
      reason = "external-merge-predecessor";
      return false;
    }
  }
  for (PHINode &phi : merge->phis()) {
    if (result.outputPhis.size() >= 8 ||
        phi.getNumIncomingValues() != leafBlocks.size() ||
        !phi.getType()->isFirstClassType() || phi.getType()->isTokenTy()) {
      reason = "unsupported-phi";
      return false;
    }
    for (BasicBlock *leaf : leafBlocks) {
      if (phi.getBasicBlockIndex(leaf) < 0) {
        reason = "unsupported-phi";
        return false;
      }
    }
    result.outputPhis.push_back(&phi);
  }
  for (Instruction *instruction : result.leftInstructions) {
    if (!hasOnlyDiamondUse(*instruction, result)) {
      reason = "escaping-value";
      return false;
    }
  }
  for (Instruction *instruction : result.rightInstructions) {
    if (!hasOnlyDiamondUse(*instruction, result)) {
      reason = "escaping-value";
      return false;
    }
  }
  return true;
}

bool collectDagDiamond(BranchInst &branch, bool aggressive,
                       const DagBudget &budget, bool sharedPredicateDag,
                       Diamond &result, std::string &reason) {
  if (!branch.isConditional()) {
    reason = "not-conditional";
    return false;
  }
  BasicBlock *entry = branch.getParent();
  BasicBlock *left = branch.getSuccessor(0);
  BasicBlock *right = branch.getSuccessor(1);
  if (left == right || left == entry || right == entry ||
      left->hasAddressTaken() || right->hasAddressTaken()) {
    reason = "not-single-entry";
    return false;
  }

  PostDominatorTree postDominators(*entry->getParent());
  BasicBlock *merge =
      postDominators.findNearestCommonDominator(left, right);
  if (merge == nullptr || merge == entry || merge == left ||
      merge == right) {
    reason = "no-proper-dag-merge";
    return false;
  }
  unsigned leftBranches = 0;
  unsigned rightBranches = 0;
  unsigned leftMerges = 0;
  unsigned rightMerges = 0;
  if (!collectDagArm(
          left, entry, merge, result.leftBlocks,
          result.leftTopology, result.leftLeaves, aggressive,
          result.leftInstructions, leftBranches, leftMerges, budget,
          reason) ||
      !collectDagArm(
          right, entry, merge, result.rightBlocks,
          result.rightTopology, result.rightLeaves, aggressive,
          result.rightInstructions, rightBranches, rightMerges, budget,
          reason))
    return false;
  if (leftMerges + rightMerges == 0) {
    reason = "dag-has-no-local-merge";
    return false;
  }
  if (std::any_of(
          result.leftBlocks.begin(), result.leftBlocks.end(),
          [&result](BasicBlock *block) {
            return containsBlock(result.rightBlocks, block);
          })) {
    reason = "overlapping-dag-arms";
    return false;
  }
  if (result.leftInstructions.empty() &&
      result.rightInstructions.empty()) {
    reason = "empty-alignment";
    return false;
  }
  if (containsBlock(result.leftBlocks, merge) ||
      containsBlock(result.rightBlocks, merge) ||
      pred_size(merge) !=
          result.leftLeaves.size() + result.rightLeaves.size()) {
    reason = "multi-exit";
    return false;
  }

  SmallVector<BasicBlock *, 2 * kMaxDagArmLeaves> leafBlocks;
  for (const ArmLeaf &leaf : result.leftLeaves)
    leafBlocks.push_back(result.leftBlocks[leaf.blockOrdinal]);
  for (const ArmLeaf &leaf : result.rightLeaves)
    leafBlocks.push_back(result.rightBlocks[leaf.blockOrdinal]);
  for (BasicBlock *predecessor : predecessors(merge)) {
    if (!containsBlock(leafBlocks, predecessor) ||
        std::count(leafBlocks.begin(), leafBlocks.end(),
                   predecessor) != 1) {
      reason = "external-merge-predecessor";
      return false;
    }
  }
  for (PHINode &phi : merge->phis()) {
    if (result.outputPhis.size() >= 8 ||
        phi.getNumIncomingValues() != leafBlocks.size() ||
        !phi.getType()->isFirstClassType() ||
        phi.getType()->isTokenTy()) {
      reason = "unsupported-phi";
      return false;
    }
    for (BasicBlock *leaf : leafBlocks) {
      if (phi.getBasicBlockIndex(leaf) < 0) {
        reason = "unsupported-phi";
        return false;
      }
    }
    result.outputPhis.push_back(&phi);
  }

  result.branch = &branch;
  result.left = left;
  result.right = right;
  result.leftExit =
      result.leftBlocks[result.leftLeaves.front().blockOrdinal];
  result.rightExit =
      result.rightBlocks[result.rightLeaves.front().blockOrdinal];
  result.merge = merge;
  result.internalDag = true;
  result.sharedPredicateDag = sharedPredicateDag;
  for (Instruction *instruction : result.leftInstructions) {
    if (!hasOnlyDiamondUse(*instruction, result)) {
      reason = "escaping-value";
      return false;
    }
  }
  for (Instruction *instruction : result.rightInstructions) {
    if (!hasOnlyDiamondUse(*instruction, result)) {
      reason = "escaping-value";
      return false;
    }
  }
  return true;
}

bool collectDiamond(BranchInst &branch, bool aggressive,
                    Diamond &result, std::string &reason) {
  Diamond treeResult;
  std::string treeReason;
  if (collectTreeDiamond(
          branch, aggressive, treeResult, treeReason)) {
    result = std::move(treeResult);
    return true;
  }
  Diamond dagResult;
  std::string dagReason;
  if (collectDagDiamond(
          branch, aggressive, kDagV4Budget, false, dagResult,
          dagReason)) {
    result = std::move(dagResult);
    return true;
  }
  Diamond sharedDagResult;
  std::string sharedDagReason;
  if (collectDagDiamond(
          branch, aggressive, kSharedDagV5Budget, true,
          sharedDagResult, sharedDagReason)) {
    result = std::move(sharedDagResult);
    return true;
  }
  reason = sharedDagReason.empty()
               ? (dagReason.empty() ? treeReason : dagReason)
               : sharedDagReason;
  return false;
}

bool compatible(const Instruction &left, const Instruction &right) {
  if (isa<PHINode>(left) || isa<PHINode>(right))
    return false;
  if (left.getOpcode() != right.getOpcode())
    return false;
  if (const auto *leftLoad = dyn_cast<LoadInst>(&left)) {
    const auto *rightLoad = cast<LoadInst>(&right);
    return leftLoad->getType() == rightLoad->getType() &&
           leftLoad->getPointerAddressSpace() ==
               rightLoad->getPointerAddressSpace();
  }
  if (const auto *leftStore = dyn_cast<StoreInst>(&left)) {
    const auto *rightStore = cast<StoreInst>(&right);
    return leftStore->getValueOperand()->getType() ==
               rightStore->getValueOperand()->getType() &&
           leftStore->getPointerAddressSpace() ==
               rightStore->getPointerAddressSpace() &&
           leftStore->getPointerOperand() == rightStore->getPointerOperand();
  }
  return left.isSameOperationAs(&right);
}

std::vector<AlignmentSlot> align(const Diamond &diamond) {
  const size_t leftSize = diamond.leftInstructions.size();
  const size_t rightSize = diamond.rightInstructions.size();
  std::vector<std::vector<unsigned>> lengths(
      leftSize + 1, std::vector<unsigned>(rightSize + 1, 0));
  for (size_t left = leftSize; left-- > 0;) {
    for (size_t right = rightSize; right-- > 0;) {
      if (compatible(*diamond.leftInstructions[left],
                     *diamond.rightInstructions[right]))
        lengths[left][right] = lengths[left + 1][right + 1] + 1;
      else
        lengths[left][right] =
            std::max(lengths[left + 1][right], lengths[left][right + 1]);
    }
  }

  std::vector<AlignmentSlot> result;
  size_t left = 0;
  size_t right = 0;
  while (left < leftSize || right < rightSize) {
    if (left < leftSize && right < rightSize &&
        compatible(*diamond.leftInstructions[left],
                   *diamond.rightInstructions[right]) &&
        lengths[left][right] == lengths[left + 1][right + 1] + 1) {
      result.push_back(
          {diamond.leftInstructions[left++], diamond.rightInstructions[right++]});
    } else if (right == rightSize ||
               (left < leftSize &&
                lengths[left + 1][right] >= lengths[left][right + 1])) {
      result.push_back({diamond.leftInstructions[left++], nullptr});
    } else {
      result.push_back({nullptr, diamond.rightInstructions[right++]});
    }
  }
  return result;
}

Value *mapped(Value *value, const ValueToValueMapTy &mapping) {
  auto found = mapping.find(value);
  if (found == mapping.end())
    return value;
  Value *result = found->second;
  return result;
}

Constant *safeConstant(Type *type, const Instruction &instruction,
                       unsigned operandIndex) {
  if (!type->isFirstClassType() || type->isTokenTy())
    return nullptr;
  if ((instruction.getOpcode() == Instruction::SDiv ||
       instruction.getOpcode() == Instruction::UDiv ||
       instruction.getOpcode() == Instruction::SRem ||
       instruction.getOpcode() == Instruction::URem) &&
      operandIndex == 1) {
    Type *scalar = type->getScalarType();
    if (!scalar->isIntegerTy())
      return nullptr;
    Constant *one = ConstantInt::get(cast<IntegerType>(scalar), 1);
    if (auto *vector = dyn_cast<VectorType>(type)) {
#if LLVM_VERSION_MAJOR >= 11
      return ConstantVector::getSplat(vector->getElementCount(), one);
#else
      return ConstantVector::getSplat(vector->getNumElements(), one);
#endif
    }
    return one;
  }
  return Constant::getNullValue(type);
}

Value *selected(IRBuilder<> &builder, Value *condition, Value *left,
                Value *right, TransformStats &stats, StringRef name) {
  if (left == right)
    return left;
  ++stats.selects;
  Value *result = builder.CreateSelect(condition, left, right, name);
  if (auto *instruction = dyn_cast<Instruction>(result))
    instruction->setMetadata(
        "symcc.hydra_select",
        MDNode::get(instruction->getContext(), {}));
  return result;
}

Instruction *cloneAlu(Instruction &source, ArrayRef<Value *> operands,
                      IRBuilder<> &builder, StringRef name) {
  Instruction *clone = source.clone();
  for (unsigned index = 0; index < operands.size(); ++index)
    clone->setOperand(index, operands[index]);
  builder.Insert(clone, name);
  return clone;
}

Value *blockGuard(
    BasicBlock *block, ArrayRef<ArmBlockInfo> topology,
    ValueToValueMapTy &mapping, DenseMap<BasicBlock *, Value *> &guards,
    IRBuilder<> &builder) {
  auto foundGuard = guards.find(block);
  if (foundGuard != guards.end())
    return foundGuard->second;
  auto found = std::find_if(
      topology.begin(), topology.end(),
      [block](const ArmBlockInfo &info) {
        return info.block == block;
      });
  if (found == topology.end() || found->parentOrdinal < 0 ||
      static_cast<size_t>(found->parentOrdinal) >= topology.size())
    return nullptr;
  const ArmBlockInfo &parent = topology[found->parentOrdinal];
  Value *parentGuard =
      blockGuard(parent.block, topology, mapping, guards, builder);
  if (parentGuard == nullptr || parent.terminator == nullptr ||
      !parent.terminator->isConditional())
    return nullptr;
  Value *condition =
      mapped(parent.terminator->getCondition(), mapping);
  if (auto *instruction = dyn_cast<Instruction>(
          parent.terminator->getCondition())) {
    if (std::any_of(
            topology.begin(), topology.end(),
            [instruction](const ArmBlockInfo &info) {
              return info.block == instruction->getParent();
            }) &&
        mapping.find(instruction) == mapping.end())
      return nullptr;
  }
  Value *edgeCondition =
      found->incomingEdge == 0
          ? condition
          : builder.CreateNot(condition, "hydra.path.not");
  Value *guard =
      builder.CreateAnd(parentGuard, edgeCondition, "hydra.path");
  guards[block] = guard;
  return guard;
}

Value *leafGuard(
    const ArmLeaf &leaf, ArrayRef<BasicBlock *> blocks,
    ArrayRef<ArmBlockInfo> topology, ValueToValueMapTy &mapping,
    DenseMap<BasicBlock *, Value *> &guards, IRBuilder<> &builder) {
  if (leaf.blockOrdinal >= blocks.size() ||
      leaf.blockOrdinal >= topology.size())
    return nullptr;
  BasicBlock *block = blocks[leaf.blockOrdinal];
  Value *guard =
      blockGuard(block, topology, mapping, guards, builder);
  BranchInst *terminator = topology[leaf.blockOrdinal].terminator;
  if (guard == nullptr || terminator == nullptr)
    return nullptr;
  if (!terminator->isConditional())
    return guard;
  Value *condition =
      mapped(terminator->getCondition(), mapping);
  if (auto *instruction =
          dyn_cast<Instruction>(terminator->getCondition())) {
    if (mapping.find(instruction) == mapping.end())
      return nullptr;
  }
  Value *edgeCondition =
      leaf.successorIndex == 0
          ? condition
          : builder.CreateNot(condition, "hydra.leaf.not");
  return builder.CreateAnd(guard, edgeCondition, "hydra.leaf");
}

Value *dagBlockGuard(
    BasicBlock *block, ArrayRef<ArmBlockInfo> topology,
    ValueToValueMapTy &mapping, DenseMap<BasicBlock *, Value *> &guards,
    DagGuardCache *edgeCache, PredicateGuardCache *predicateCache,
    IRBuilder<> &builder);

Value *negatedPredicate(
    Value *condition, PredicateGuardCache *cache, IRBuilder<> &builder,
    StringRef name) {
  if (cache == nullptr)
    return builder.CreateNot(condition, name);
  auto found = cache->negations.find(condition);
  if (found != cache->negations.end()) {
    PredicateNegationEntry &entry = found->second;
    const bool sameRegion =
        entry.producerSite == cache->currentRegionSite;
    const auto *negation = dyn_cast_or_null<Instruction>(
        entry.negation);
    const bool dominatesCurrentRegion =
        !sameRegion && negation != nullptr &&
        cache->dominators != nullptr &&
        cache->currentInsertionPoint != nullptr &&
        cache->dominators->dominates(
            negation, cache->currentInsertionPoint);
    if (sameRegion || dominatesCurrentRegion) {
      ++cache->reusedNegations;
      if (!sameRegion) {
        ++cache->crossRegionReusedNegations;
        if (std::find(
                cache->currentCrossRegionSourceSites.begin(),
                cache->currentCrossRegionSourceSites.end(),
                entry.producerSite) ==
            cache->currentCrossRegionSourceSites.end())
          cache->currentCrossRegionSourceSites.push_back(
              entry.producerSite);
      }
      return entry.negation;
    }
  }
  Value *negation = builder.CreateNot(condition, name);
  cache->negations[condition] = {
      negation, cache->currentRegionSite};
  return negation;
}

Value *dagEdgeGuard(
    const ArmBlockInfo &predecessor, unsigned successorIndex,
    ArrayRef<ArmBlockInfo> topology, ValueToValueMapTy &mapping,
    DenseMap<BasicBlock *, Value *> &guards, DagGuardCache *edgeCache,
    PredicateGuardCache *predicateCache, IRBuilder<> &builder) {
  if (successorIndex >= predecessor.successors.size())
    return nullptr;
  uint64_t cacheKey = 0;
  if (edgeCache != nullptr) {
    const ArmBlockInfo *begin = topology.data();
    const ArmBlockInfo *current = &predecessor;
    if (current < begin || current >= begin + topology.size())
      return nullptr;
    cacheKey =
        (static_cast<uint64_t>(current - begin) << 1) | successorIndex;
    auto found = edgeCache->edges.find(cacheKey);
    if (found != edgeCache->edges.end()) {
      ++edgeCache->reusedEdges;
      return found->second;
    }
  }
  Value *guard = dagBlockGuard(
      predecessor.block, topology, mapping, guards, edgeCache,
      predicateCache, builder);
  if (guard == nullptr || predecessor.terminator == nullptr)
    return nullptr;
  if (!predecessor.terminator->isConditional()) {
    if (successorIndex != 0)
      return nullptr;
    if (edgeCache != nullptr)
      edgeCache->edges[cacheKey] = guard;
    return guard;
  }
  Value *originalCondition =
      predecessor.terminator->getCondition();
  if (auto *instruction =
          dyn_cast<Instruction>(originalCondition)) {
    if (std::any_of(
            topology.begin(), topology.end(),
            [instruction](const ArmBlockInfo &info) {
              return info.block == instruction->getParent();
            }) &&
        mapping.find(instruction) == mapping.end())
      return nullptr;
  }
  Value *condition = mapped(originalCondition, mapping);
  Value *edgeCondition =
      successorIndex == 0
          ? condition
          : negatedPredicate(
                condition, predicateCache, builder,
                "hydra.dag.edge.not");
  Value *edgeGuard = builder.CreateAnd(
      guard, edgeCondition, "hydra.dag.edge");
  if (edgeCache != nullptr)
    edgeCache->edges[cacheKey] = edgeGuard;
  return edgeGuard;
}

Value *dagBlockGuard(
    BasicBlock *block, ArrayRef<ArmBlockInfo> topology,
    ValueToValueMapTy &mapping, DenseMap<BasicBlock *, Value *> &guards,
    DagGuardCache *edgeCache, PredicateGuardCache *predicateCache,
    IRBuilder<> &builder) {
  auto foundGuard = guards.find(block);
  if (foundGuard != guards.end())
    return foundGuard->second;
  auto found = std::find_if(
      topology.begin(), topology.end(),
      [block](const ArmBlockInfo &info) {
        return info.block == block;
      });
  if (found == topology.end() || found->predecessors.empty())
    return nullptr;
  Value *guard = nullptr;
  for (const ArmIncomingEdge &incoming : found->predecessors) {
    if (incoming.blockOrdinal < 0 ||
        static_cast<size_t>(incoming.blockOrdinal) >=
            topology.size() ||
        incoming.successorIndex < 0)
      return nullptr;
    Value *edgeGuard = dagEdgeGuard(
        topology[incoming.blockOrdinal],
        static_cast<unsigned>(incoming.successorIndex), topology,
        mapping, guards, edgeCache, predicateCache, builder);
    if (edgeGuard == nullptr)
      return nullptr;
    guard = guard == nullptr
                ? edgeGuard
                : builder.CreateOr(
                      guard, edgeGuard, "hydra.dag.path");
  }
  guards[block] = guard;
  return guard;
}

Value *dagLeafGuard(
    const ArmLeaf &leaf, ArrayRef<BasicBlock *> blocks,
    ArrayRef<ArmBlockInfo> topology, ValueToValueMapTy &mapping,
    DenseMap<BasicBlock *, Value *> &guards, DagGuardCache *edgeCache,
    PredicateGuardCache *predicateCache, IRBuilder<> &builder) {
  if (leaf.blockOrdinal >= blocks.size() ||
      leaf.blockOrdinal >= topology.size())
    return nullptr;
  return dagEdgeGuard(
      topology[leaf.blockOrdinal], leaf.successorIndex, topology,
      mapping, guards, edgeCache, predicateCache, builder);
}

Value *materializeDagPhi(
    PHINode &phi, ArrayRef<BasicBlock *> blocks,
    ArrayRef<ArmBlockInfo> topology, ValueToValueMapTy &mapping,
    DenseMap<BasicBlock *, Value *> &guards, IRBuilder<> &builder,
    TransformStats &stats, DagGuardCache *edgeCache,
    PredicateGuardCache *predicateCache) {
  auto found = std::find_if(
      topology.begin(), topology.end(),
      [&phi](const ArmBlockInfo &info) {
        return info.block == phi.getParent();
      });
  if (found == topology.end() ||
      found->predecessors.size() != phi.getNumIncomingValues())
    return nullptr;
  Value *replacement = Constant::getNullValue(phi.getType());
  for (const ArmIncomingEdge &incoming : found->predecessors) {
    if (incoming.blockOrdinal < 0 ||
        static_cast<size_t>(incoming.blockOrdinal) >= blocks.size() ||
        incoming.successorIndex < 0)
      return nullptr;
    BasicBlock *predecessor = blocks[incoming.blockOrdinal];
    int incomingIndex = phi.getBasicBlockIndex(predecessor);
    if (incomingIndex < 0)
      return nullptr;
    Value *incomingValue = phi.getIncomingValue(incomingIndex);
    if (auto *instruction =
            dyn_cast<Instruction>(incomingValue)) {
      if (containsBlock(blocks, instruction->getParent()) &&
          mapping.find(instruction) == mapping.end())
        return nullptr;
    }
    Value *guard = dagEdgeGuard(
        topology[incoming.blockOrdinal],
        static_cast<unsigned>(incoming.successorIndex), topology,
        mapping, guards, edgeCache, predicateCache, builder);
    if (guard == nullptr)
      return nullptr;
    replacement = selected(
        builder, guard, mapped(incomingValue, mapping), replacement,
        stats, "hydra.dag.phi");
  }
  mapping[&phi] = replacement;
  return replacement;
}

Value *guardedPairOperand(
    Instruction &left, unsigned operandIndex, Value *leftOperand,
    Value *rightOperand, Value *leftGuard,
    Value *rightGuard, IRBuilder<> &builder, TransformStats &stats) {
  Constant *fallback =
      safeConstant(leftOperand->getType(), left, operandIndex);
  if (fallback == nullptr)
    fallback = Constant::getNullValue(leftOperand->getType());
  Value *rightValue = selected(
      builder, rightGuard, rightOperand, fallback, stats,
      "hydra.tree.right");
  return selected(
      builder, leftGuard, leftOperand, rightValue, stats,
      "hydra.tree.left");
}

Instruction *mergeTreeAlu(
    Instruction &left, Instruction &right,
    ValueToValueMapTy &leftMap, ValueToValueMapTy &rightMap,
    Value *leftGuard, Value *rightGuard, IRBuilder<> &builder,
    TransformStats &stats) {
  SmallVector<Value *, 8> operands;
  for (unsigned index = 0; index < left.getNumOperands(); ++index) {
    operands.push_back(guardedPairOperand(
        left, index, mapped(left.getOperand(index), leftMap),
        mapped(right.getOperand(index), rightMap), leftGuard, rightGuard,
        builder, stats));
  }
  Instruction *merged =
      cloneAlu(left, operands, builder, "hydra.tree.merged");
  leftMap[&left] = merged;
  rightMap[&right] = merged;
  ++stats.aligned;
  return merged;
}

Instruction *mergeTreeExtraAlu(
    Instruction &source, ValueToValueMapTy &mapping, Value *guard,
    IRBuilder<> &builder, TransformStats &stats) {
  SmallVector<Value *, 8> operands;
  for (unsigned index = 0; index < source.getNumOperands(); ++index) {
    Value *original = mapped(source.getOperand(index), mapping);
    Constant *fallback =
        safeConstant(original->getType(), source, index);
    if (fallback == nullptr)
      fallback = Constant::getNullValue(original->getType());
    operands.push_back(selected(
        builder, guard, original, fallback, stats,
        "hydra.tree.extra.operand"));
  }
  Instruction *merged =
      cloneAlu(source, operands, builder, "hydra.tree.extra");
  mapping[&source] = merged;
  ++stats.extraAlu;
  return merged;
}

Instruction *mergeAlu(Instruction &left, Instruction &right,
                      ValueToValueMapTy &leftMap,
                      ValueToValueMapTy &rightMap, Value *condition,
                      IRBuilder<> &builder, TransformStats &stats) {
  SmallVector<Value *, 8> operands;
  for (unsigned index = 0; index < left.getNumOperands(); ++index) {
    Value *leftOperand = mapped(left.getOperand(index), leftMap);
    Value *rightOperand = mapped(right.getOperand(index), rightMap);
    operands.push_back(selected(builder, condition, leftOperand, rightOperand,
                                stats, "hydra.operand"));
  }
  Instruction *merged =
      cloneAlu(left, operands, builder, "hydra.merged");
  leftMap[&left] = merged;
  rightMap[&right] = merged;
  ++stats.aligned;
  return merged;
}

Instruction *mergeExtraAlu(Instruction &source, bool onLeft,
                           ValueToValueMapTy &mapping, Value *condition,
                           IRBuilder<> &builder, TransformStats &stats) {
  SmallVector<Value *, 8> operands;
  for (unsigned index = 0; index < source.getNumOperands(); ++index) {
    Value *original = mapped(source.getOperand(index), mapping);
    Value *extra = safeConstant(original->getType(), source, index);
    if (extra == nullptr)
      extra = original;
    Value *left = onLeft ? original : extra;
    Value *right = onLeft ? extra : original;
    operands.push_back(selected(builder, condition, left, right, stats,
                                "hydra.extra.operand"));
  }
  Instruction *merged =
      cloneAlu(source, operands, builder, "hydra.extra");
  mapping[&source] = merged;
  ++stats.extraAlu;
  return merged;
}

LoadInst *cloneLoad(LoadInst &source, Value *pointer, IRBuilder<> &builder,
                    StringRef name) {
  LoadInst *load = builder.CreateLoad(source.getType(), pointer, name);
  load->setAlignment(source.getAlign());
  load->copyMetadata(source);
  return load;
}

void mergeLoads(LoadInst &left, LoadInst &right,
                ValueToValueMapTy &leftMap, ValueToValueMapTy &rightMap,
                Value *condition, IRBuilder<> &builder,
                TransformStats &stats) {
  Value *leftPointer = mapped(left.getPointerOperand(), leftMap);
  Value *rightPointer = mapped(right.getPointerOperand(), rightMap);
  if (leftPointer == rightPointer) {
    LoadInst *load = cloneLoad(left, leftPointer, builder, "hydra.load");
    leftMap[&left] = load;
    rightMap[&right] = load;
    ++stats.aligned;
    return;
  }
  LoadInst *leftLoad =
      cloneLoad(left, leftPointer, builder, "hydra.load.left");
  LoadInst *rightLoad =
      cloneLoad(right, rightPointer, builder, "hydra.load.right");
  Value *result = selected(builder, condition, leftLoad, rightLoad, stats,
                           "hydra.load.value");
  leftMap[&left] = result;
  rightMap[&right] = result;
  stats.linearizedLoads += 2;
}

void mergeExtraLoad(LoadInst &source, ValueToValueMapTy &mapping,
                    IRBuilder<> &builder, TransformStats &stats) {
  Value *pointer = mapped(source.getPointerOperand(), mapping);
  LoadInst *load =
      cloneLoad(source, pointer, builder, "hydra.extra.load");
  mapping[&source] = load;
  ++stats.linearizedLoads;
}

void mergeStore(StoreInst &source, bool onLeft,
                ValueToValueMapTy &mapping, Value *condition,
                IRBuilder<> &builder, TransformStats &stats) {
  Value *pointer = mapped(source.getPointerOperand(), mapping);
  Value *original = mapped(source.getValueOperand(), mapping);
  LoadInst *oldValue =
      builder.CreateLoad(original->getType(), pointer, "hydra.store.old");
  oldValue->setAlignment(source.getAlign());
  Value *value = onLeft
                     ? selected(builder, condition, original, oldValue, stats,
                                "hydra.store.value")
                     : selected(builder, condition, oldValue, original, stats,
                                "hydra.store.value");
  StoreInst *store = builder.CreateStore(value, pointer);
  store->setAlignment(source.getAlign());
  store->copyMetadata(source);
  ++stats.readbackStores;
}

void mergeStores(StoreInst &left, StoreInst &right,
                 ValueToValueMapTy &leftMap, ValueToValueMapTy &rightMap,
                 Value *condition, IRBuilder<> &builder,
                 TransformStats &stats) {
  Value *leftPointer = mapped(left.getPointerOperand(), leftMap);
  Value *rightPointer = mapped(right.getPointerOperand(), rightMap);
  if (leftPointer != rightPointer) {
    mergeStore(left, true, leftMap, condition, builder, stats);
    mergeStore(right, false, rightMap, condition, builder, stats);
    return;
  }
  Value *leftValue = mapped(left.getValueOperand(), leftMap);
  Value *rightValue = mapped(right.getValueOperand(), rightMap);
  Value *value = selected(builder, condition, leftValue, rightValue, stats,
                          "hydra.store.value");
  StoreInst *store = builder.CreateStore(value, leftPointer);
  store->setAlignment(std::min(left.getAlign(), right.getAlign()));
  store->copyMetadata(left);
  ++stats.aligned;
}

void mergeTreeLoads(
    LoadInst &left, LoadInst &right, ValueToValueMapTy &leftMap,
    ValueToValueMapTy &rightMap, Value *leftGuard, Value *rightGuard,
    IRBuilder<> &builder, TransformStats &stats) {
  Value *leftPointer = mapped(left.getPointerOperand(), leftMap);
  Value *rightPointer = mapped(right.getPointerOperand(), rightMap);
  if (leftPointer == rightPointer) {
    LoadInst *load =
        cloneLoad(left, leftPointer, builder, "hydra.tree.load");
    leftMap[&left] = load;
    rightMap[&right] = load;
    ++stats.aligned;
    return;
  }
  LoadInst *leftLoad =
      cloneLoad(left, leftPointer, builder, "hydra.tree.load.left");
  LoadInst *rightLoad =
      cloneLoad(right, rightPointer, builder, "hydra.tree.load.right");
  Constant *fallback = Constant::getNullValue(left.getType());
  Value *rightValue = selected(
      builder, rightGuard, rightLoad, fallback, stats,
      "hydra.tree.load.right.value");
  Value *result = selected(
      builder, leftGuard, leftLoad, rightValue, stats,
      "hydra.tree.load.value");
  leftMap[&left] = result;
  rightMap[&right] = result;
  stats.linearizedLoads += 2;
}

void mergeTreeStore(
    StoreInst &source, ValueToValueMapTy &mapping, Value *guard,
    IRBuilder<> &builder, TransformStats &stats) {
  Value *pointer = mapped(source.getPointerOperand(), mapping);
  Value *original = mapped(source.getValueOperand(), mapping);
  LoadInst *oldValue =
      builder.CreateLoad(original->getType(), pointer,
                         "hydra.tree.store.old");
  oldValue->setAlignment(source.getAlign());
  Value *value = selected(
      builder, guard, original, oldValue, stats,
      "hydra.tree.store.value");
  StoreInst *store = builder.CreateStore(value, pointer);
  store->setAlignment(source.getAlign());
  store->copyMetadata(source);
  ++stats.readbackStores;
}

void mergeTreeStores(
    StoreInst &left, StoreInst &right,
    ValueToValueMapTy &leftMap, ValueToValueMapTy &rightMap,
    Value *leftGuard, Value *rightGuard, IRBuilder<> &builder,
    TransformStats &stats) {
  Value *leftPointer = mapped(left.getPointerOperand(), leftMap);
  Value *rightPointer = mapped(right.getPointerOperand(), rightMap);
  if (leftPointer != rightPointer) {
    mergeTreeStore(left, leftMap, leftGuard, builder, stats);
    mergeTreeStore(right, rightMap, rightGuard, builder, stats);
    return;
  }
  Value *leftValue = mapped(left.getValueOperand(), leftMap);
  Value *rightValue = mapped(right.getValueOperand(), rightMap);
  LoadInst *oldValue =
      builder.CreateLoad(leftValue->getType(), leftPointer,
                         "hydra.tree.store.old");
  oldValue->setAlignment(std::min(left.getAlign(), right.getAlign()));
  Value *rightSelected = selected(
      builder, rightGuard, rightValue, oldValue, stats,
      "hydra.tree.store.right");
  Value *value = selected(
      builder, leftGuard, leftValue, rightSelected, stats,
      "hydra.tree.store.value");
  StoreInst *store = builder.CreateStore(value, leftPointer);
  store->setAlignment(std::min(left.getAlign(), right.getAlign()));
  store->copyMetadata(left);
  ++stats.aligned;
  ++stats.readbackStores;
}

bool applyDiamond(
    Diamond &diamond, const ProfileEntry &profile,
    TransformStats &stats,
    PredicateGuardCache *functionPredicateCache = nullptr,
    DominatorTree *dominators = nullptr) {
  Value *condition = diamond.branch->getCondition();
  IRBuilder<> builder(diamond.branch);
  ValueToValueMapTy leftMap;
  ValueToValueMapTy rightMap;
  DenseMap<BasicBlock *, Value *> leftGuards;
  DenseMap<BasicBlock *, Value *> rightGuards;
  DagGuardCache leftEdgeCache;
  DagGuardCache rightEdgeCache;
  PredicateGuardCache localPredicateCache;
  PredicateGuardCache *activePredicateCache =
      functionPredicateCache == nullptr ? &localPredicateCache
                                        : functionPredicateCache;
  const unsigned reusedNegationsBefore =
      activePredicateCache->reusedNegations;
  const unsigned crossRegionReusesBefore =
      activePredicateCache->crossRegionReusedNegations;
  activePredicateCache->currentRegionSite = profile.site;
  activePredicateCache->currentInsertionPoint = diamond.branch;
  activePredicateCache->dominators = dominators;
  activePredicateCache->currentCrossRegionSourceSites.clear();

  stats.leftArmBlocks = diamond.leftBlocks.size();
  stats.rightArmBlocks = diamond.rightBlocks.size();
  stats.leftInstructionCount = diamond.leftInstructions.size();
  stats.rightInstructionCount = diamond.rightInstructions.size();
  stats.unequalArms =
      stats.leftArmBlocks != stats.rightArmBlocks;
  auto conditionalCount = [](ArrayRef<ArmBlockInfo> topology) {
    return static_cast<unsigned>(std::count_if(
        topology.begin(), topology.end(),
        [](const ArmBlockInfo &info) {
          return info.terminator != nullptr &&
                 info.terminator->isConditional();
        }));
  };
  const unsigned leftInternalBranches =
      conditionalCount(diamond.leftTopology);
  const unsigned rightInternalBranches =
      conditionalCount(diamond.rightTopology);
  stats.internalDag = diamond.internalDag;
  stats.sharedPredicateDag = diamond.sharedPredicateDag;
  stats.internalTree =
      !stats.internalDag &&
      (leftInternalBranches != 0 || rightInternalBranches != 0);
  auto localMergeCount = [](ArrayRef<ArmBlockInfo> topology) {
    return static_cast<unsigned>(std::count_if(
        topology.begin(), topology.end(),
        [](const ArmBlockInfo &info) {
          return info.predecessors.size() > 1;
        }));
  };
  stats.leftLocalMerges =
      localMergeCount(diamond.leftTopology);
  stats.rightLocalMerges =
      localMergeCount(diamond.rightTopology);
  if (stats.sharedPredicateDag) {
    auto recordPredicates = [](ArrayRef<ArmBlockInfo> topology,
                               SmallVectorImpl<uint64_t> &sites,
                               unsigned &edges) {
      for (const ArmBlockInfo &info : topology) {
        edges += info.successors.size();
        if (info.terminator != nullptr &&
            info.terminator->isConditional())
          sites.push_back(
              stableSiteId(*info.terminator->getCondition()));
      }
    };
    recordPredicates(
        diamond.leftTopology, stats.leftPredicateSites,
        stats.canonicalGuardEdges);
    recordPredicates(
        diamond.rightTopology, stats.rightPredicateSites,
        stats.canonicalGuardEdges);
    std::set<uint64_t> uniquePredicates(
        stats.leftPredicateSites.begin(),
        stats.leftPredicateSites.end());
    uniquePredicates.insert(
        stats.rightPredicateSites.begin(),
        stats.rightPredicateSites.end());
    stats.uniquePredicates = uniquePredicates.size();
    stats.reusedPredicateOccurrences =
        stats.leftPredicateSites.size() +
        stats.rightPredicateSites.size() -
        stats.uniquePredicates;
  }
  for (BasicBlock *block : diamond.leftBlocks)
    stats.leftBlockSites.push_back(stableSiteId(*block));
  for (BasicBlock *block : diamond.rightBlocks)
    stats.rightBlockSites.push_back(stableSiteId(*block));
  auto copyTopology = [](ArrayRef<ArmBlockInfo> source,
                         SmallVectorImpl<ArmTopologyProof> &destination) {
    for (const ArmBlockInfo &info : source) {
      ArmTopologyProof proof;
      proof.blockSite = stableSiteId(*info.block);
      proof.terminatorSite = stableSiteId(*info.terminator);
      proof.parentOrdinal = info.parentOrdinal;
      proof.incomingEdge = info.incomingEdge;
      proof.predecessors.append(
          info.predecessors.begin(), info.predecessors.end());
      proof.successors.append(
          info.successors.begin(), info.successors.end());
      destination.push_back(std::move(proof));
    }
  };
  copyTopology(diamond.leftTopology, stats.leftTopology);
  copyTopology(diamond.rightTopology, stats.rightTopology);
  stats.leftLeaves.append(
      diamond.leftLeaves.begin(), diamond.leftLeaves.end());
  stats.rightLeaves.append(
      diamond.rightLeaves.begin(), diamond.rightLeaves.end());
  if (stats.internalDag) {
    const unsigned localPhiLimit =
        stats.sharedPredicateDag ? kMaxSharedDagLocalPhis : 8;
    for (Instruction *instruction : diamond.leftInstructions)
      if (isa<PHINode>(instruction)) {
        if (stats.leftLocalPhiSites.size() >= localPhiLimit)
          return false;
        stats.leftLocalPhiSites.push_back(
            stableSiteId(*instruction));
      }
    for (Instruction *instruction : diamond.rightInstructions)
      if (isa<PHINode>(instruction)) {
        if (stats.rightLocalPhiSites.size() >= localPhiLimit)
          return false;
        stats.rightLocalPhiSites.push_back(
            stableSiteId(*instruction));
      }
  }
  for (PHINode *phi : diamond.outputPhis)
    stats.outputPhiSites.push_back(stableSiteId(*phi));
  if (stats.internalTree || stats.internalDag) {
    std::set<uint64_t> proofIdentities = {profile.site};
    auto insertIdentity = [&proofIdentities](uint64_t identity) {
      return proofIdentities.insert(identity).second;
    };
    for (uint64_t site : stats.leftBlockSites)
      if (!insertIdentity(site))
        return false;
    for (uint64_t site : stats.rightBlockSites)
      if (!insertIdentity(site))
        return false;
    for (const ArmTopologyProof &block : stats.leftTopology)
      if (!insertIdentity(block.terminatorSite))
        return false;
    for (const ArmTopologyProof &block : stats.rightTopology)
      if (!insertIdentity(block.terminatorSite))
        return false;
    for (Instruction *instruction : diamond.leftInstructions)
      if (!insertIdentity(stableSiteId(*instruction)))
        return false;
    for (Instruction *instruction : diamond.rightInstructions)
      if (!insertIdentity(stableSiteId(*instruction)))
        return false;
    for (uint64_t site : stats.outputPhiSites)
      if (!insertIdentity(site))
        return false;
  }

  std::vector<AlignmentSlot> alignment = align(diamond);
  auto blockOrdinal = [](Instruction *instruction,
                         ArrayRef<BasicBlock *> blocks) {
    if (instruction == nullptr)
      return -1;
    auto found = std::find(
        blocks.begin(), blocks.end(), instruction->getParent());
    return found == blocks.end()
               ? -1
               : static_cast<int>(
                     std::distance(blocks.begin(), found));
  };
  for (const AlignmentSlot &slot : alignment) {
    const bool leftFreeze =
        slot.left != nullptr && isa<FreezeInst>(slot.left);
    const bool rightFreeze =
        slot.right != nullptr && isa<FreezeInst>(slot.right);
    stats.leftFreezes += leftFreeze ? 1 : 0;
    stats.rightFreezes += rightFreeze ? 1 : 0;
    stats.alignedFreezePairs +=
        leftFreeze && rightFreeze ? 1 : 0;
    stats.extraFreezes +=
        leftFreeze != rightFreeze ? 1 : 0;
    stats.editDistance +=
        (slot.left == nullptr) != (slot.right == nullptr) ? 1 : 0;
    stats.alignment.push_back({
        slot.left == nullptr ? 0 : stableSiteId(*slot.left),
        slot.right == nullptr ? 0 : stableSiteId(*slot.right),
        slot.left == nullptr ? 0 : slot.left->getOpcode(),
        slot.right == nullptr ? 0 : slot.right->getOpcode(),
        blockOrdinal(slot.left, diamond.leftBlocks),
        blockOrdinal(slot.right, diamond.rightBlocks),
    });
  }
  StringRef regionSchema =
      stats.crossRegionSharedPredicateDag
          ? kCrossRegionSharedPredicateDagSchema
          : stats.sharedPredicateDag
                ? kSharedPredicateDagSchema
          : (stats.internalDag
                 ? kInternalDagSchema
                 : (stats.internalTree
                        ? kInternalTreeSchema
                        : (stats.unequalArms ? kUnequalLinearSchema
                                             : kMultiBlockSchema)));
  uint64_t fingerprint =
      mixSiteIdText(1469598103934665603ULL, regionSchema);
  fingerprint = mixSiteIdInteger(fingerprint, profile.site);
  if (stats.crossRegionSharedPredicateDag) {
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.transactionFingerprint);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.transactionOrdinal);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.transactionSize);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.transactionSites.size());
    for (uint64_t site : stats.transactionSites)
      fingerprint = mixSiteIdInteger(fingerprint, site);
    fingerprint = mixSiteIdInteger(
        fingerprint,
        stats.transactionSharedPredicateSites.size());
    for (uint64_t site :
         stats.transactionSharedPredicateSites)
      fingerprint = mixSiteIdInteger(fingerprint, site);
  }
  if (stats.internalTree || stats.internalDag) {
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.leftArmBlocks);
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.rightArmBlocks);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.leftInstructionCount);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.rightInstructionCount);
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.editDistance);
    fingerprint =
        mixSiteIdInteger(fingerprint, leftInternalBranches);
    fingerprint =
        mixSiteIdInteger(fingerprint, rightInternalBranches);
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.leftLeaves.size());
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.rightLeaves.size());
    if (stats.internalDag) {
      fingerprint = mixSiteIdInteger(
          fingerprint, stats.leftLocalMerges);
      fingerprint = mixSiteIdInteger(
          fingerprint, stats.rightLocalMerges);
      fingerprint = mixSiteIdInteger(
          fingerprint, stats.leftLocalPhiSites.size());
      fingerprint = mixSiteIdInteger(
          fingerprint, stats.rightLocalPhiSites.size());
      if (stats.sharedPredicateDag) {
        fingerprint = mixSiteIdInteger(
            fingerprint, stats.canonicalGuardEdges);
        fingerprint = mixSiteIdInteger(
            fingerprint, stats.leftPredicateSites.size());
        fingerprint = mixSiteIdInteger(
            fingerprint, stats.rightPredicateSites.size());
        fingerprint = mixSiteIdInteger(
            fingerprint, stats.uniquePredicates);
        fingerprint = mixSiteIdInteger(
            fingerprint, stats.reusedPredicateOccurrences);
      }
    }
  } else if (stats.unequalArms) {
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.leftArmBlocks);
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.rightArmBlocks);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.leftInstructionCount);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.rightInstructionCount);
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.editDistance);
  } else {
    fingerprint =
        mixSiteIdInteger(fingerprint, stats.leftArmBlocks);
  }
  for (uint64_t site : stats.leftBlockSites)
    fingerprint = mixSiteIdInteger(fingerprint, site);
  for (uint64_t site : stats.rightBlockSites)
    fingerprint = mixSiteIdInteger(fingerprint, site);
  if (stats.internalTree || stats.internalDag) {
    auto mixTopology = [&fingerprint](
                           ArrayRef<ArmTopologyProof> topology,
                           bool dag) {
      for (const ArmTopologyProof &block : topology) {
        fingerprint =
            mixSiteIdInteger(fingerprint, block.blockSite);
        fingerprint =
            mixSiteIdInteger(fingerprint, block.terminatorSite);
        if (dag) {
          fingerprint = mixSiteIdInteger(
              fingerprint, block.predecessors.size());
          for (const ArmIncomingEdge &predecessor :
               block.predecessors) {
            fingerprint = mixSiteIdInteger(
                fingerprint,
                static_cast<uint64_t>(
                    predecessor.blockOrdinal + 1));
            fingerprint = mixSiteIdInteger(
                fingerprint,
                static_cast<uint64_t>(
                    predecessor.successorIndex));
          }
        } else {
          fingerprint = mixSiteIdInteger(
              fingerprint,
              static_cast<uint64_t>(block.parentOrdinal + 1));
          fingerprint = mixSiteIdInteger(
              fingerprint,
              static_cast<uint64_t>(block.incomingEdge + 1));
        }
        fingerprint =
            mixSiteIdInteger(fingerprint, block.successors.size());
        for (int successor : block.successors)
          fingerprint = mixSiteIdInteger(
              fingerprint,
              static_cast<uint64_t>(successor + 1));
      }
    };
    mixTopology(stats.leftTopology, stats.internalDag);
    mixTopology(stats.rightTopology, stats.internalDag);
    auto mixLeaves = [&fingerprint](ArrayRef<ArmLeaf> leaves) {
      for (const ArmLeaf &leaf : leaves) {
        fingerprint =
            mixSiteIdInteger(fingerprint, leaf.blockOrdinal);
        fingerprint =
            mixSiteIdInteger(fingerprint, leaf.successorIndex);
      }
    };
    mixLeaves(stats.leftLeaves);
    mixLeaves(stats.rightLeaves);
    if (stats.internalDag) {
      for (uint64_t site : stats.leftLocalPhiSites)
        fingerprint = mixSiteIdInteger(fingerprint, site);
      for (uint64_t site : stats.rightLocalPhiSites)
        fingerprint = mixSiteIdInteger(fingerprint, site);
      if (stats.sharedPredicateDag) {
        for (uint64_t site : stats.leftPredicateSites)
          fingerprint = mixSiteIdInteger(fingerprint, site);
        for (uint64_t site : stats.rightPredicateSites)
          fingerprint = mixSiteIdInteger(fingerprint, site);
      }
    }
  }
  fingerprint =
      mixSiteIdInteger(fingerprint, stats.alignment.size());
  for (const AlignmentProofSlot &slot : stats.alignment) {
    fingerprint = mixSiteIdInteger(fingerprint, slot.leftSite);
    fingerprint = mixSiteIdInteger(fingerprint, slot.rightSite);
    fingerprint = mixSiteIdInteger(fingerprint, slot.leftOpcode);
    fingerprint = mixSiteIdInteger(fingerprint, slot.rightOpcode);
    if (stats.unequalArms || stats.internalTree ||
        stats.internalDag) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(slot.leftBlockOrdinal + 1));
      fingerprint = mixSiteIdInteger(
          fingerprint,
          static_cast<uint64_t>(slot.rightBlockOrdinal + 1));
    }
  }
  fingerprint =
      mixSiteIdInteger(fingerprint, stats.outputPhiSites.size());
  for (uint64_t site : stats.outputPhiSites)
    fingerprint = mixSiteIdInteger(fingerprint, site);

  DagGuardCache *leftEdgeCachePointer =
      stats.sharedPredicateDag ? &leftEdgeCache : nullptr;
  DagGuardCache *rightEdgeCachePointer =
      stats.sharedPredicateDag ? &rightEdgeCache : nullptr;
  PredicateGuardCache *predicateCachePointer =
      stats.sharedPredicateDag ? activePredicateCache : nullptr;
  if (stats.internalTree || stats.internalDag) {
    leftGuards[diamond.left] = condition;
    rightGuards[diamond.right] =
        negatedPredicate(
            condition, predicateCachePointer, builder,
            "hydra.outer.not");
  }
  for (const AlignmentSlot &slot : alignment) {
    Value *leftGuardValue = nullptr;
    Value *rightGuardValue = nullptr;
    if (stats.internalTree || stats.internalDag) {
      if (slot.left != nullptr)
        leftGuardValue =
            stats.internalDag
                ? dagBlockGuard(
                      slot.left->getParent(),
                      diamond.leftTopology, leftMap, leftGuards,
                      leftEdgeCachePointer, predicateCachePointer,
                      builder)
                : blockGuard(
                      slot.left->getParent(),
                      diamond.leftTopology, leftMap, leftGuards,
                      builder);
      if (slot.right != nullptr)
        rightGuardValue =
            stats.internalDag
                ? dagBlockGuard(
                      slot.right->getParent(),
                      diamond.rightTopology, rightMap, rightGuards,
                      rightEdgeCachePointer, predicateCachePointer,
                      builder)
                : blockGuard(
                      slot.right->getParent(),
                      diamond.rightTopology, rightMap, rightGuards,
                      builder);
      if ((slot.left != nullptr && leftGuardValue == nullptr) ||
          (slot.right != nullptr && rightGuardValue == nullptr))
        return false;
    }
    if (stats.internalDag &&
        (isa_and_nonnull<PHINode>(slot.left) ||
         isa_and_nonnull<PHINode>(slot.right))) {
      if (auto *leftPhi = dyn_cast_or_null<PHINode>(slot.left)) {
        if (materializeDagPhi(
                *leftPhi, diamond.leftBlocks,
                diamond.leftTopology, leftMap, leftGuards, builder,
                stats, leftEdgeCachePointer,
                predicateCachePointer) == nullptr)
          return false;
      }
      if (auto *rightPhi = dyn_cast_or_null<PHINode>(slot.right)) {
        if (materializeDagPhi(
                *rightPhi, diamond.rightBlocks,
                diamond.rightTopology, rightMap, rightGuards,
                builder, stats, rightEdgeCachePointer,
                predicateCachePointer) == nullptr)
          return false;
      }
      continue;
    }
    if (slot.left != nullptr && slot.right != nullptr) {
      if (stats.internalTree || stats.internalDag) {
        if (auto *leftLoad = dyn_cast<LoadInst>(slot.left)) {
          mergeTreeLoads(
              *leftLoad, *cast<LoadInst>(slot.right), leftMap, rightMap,
              leftGuardValue, rightGuardValue, builder, stats);
        } else if (auto *leftStore =
                       dyn_cast<StoreInst>(slot.left)) {
          mergeTreeStores(
              *leftStore, *cast<StoreInst>(slot.right), leftMap, rightMap,
              leftGuardValue, rightGuardValue, builder, stats);
        } else {
          mergeTreeAlu(
              *slot.left, *slot.right, leftMap, rightMap,
              leftGuardValue, rightGuardValue, builder, stats);
        }
      } else if (auto *leftLoad = dyn_cast<LoadInst>(slot.left)) {
        mergeLoads(
            *leftLoad, *cast<LoadInst>(slot.right), leftMap, rightMap,
            condition, builder, stats);
      } else if (auto *leftStore = dyn_cast<StoreInst>(slot.left)) {
        mergeStores(
            *leftStore, *cast<StoreInst>(slot.right), leftMap, rightMap,
            condition, builder, stats);
      } else {
        mergeAlu(
            *slot.left, *slot.right, leftMap, rightMap, condition,
            builder, stats);
      }
      continue;
    }

    Instruction *source = slot.left != nullptr ? slot.left : slot.right;
    bool onLeft = slot.left != nullptr;
    ValueToValueMapTy &mapping = onLeft ? leftMap : rightMap;
    Value *guard = onLeft ? leftGuardValue : rightGuardValue;
    if (stats.internalTree || stats.internalDag) {
      if (auto *load = dyn_cast<LoadInst>(source))
        mergeExtraLoad(*load, mapping, builder, stats);
      else if (auto *store = dyn_cast<StoreInst>(source))
        mergeTreeStore(*store, mapping, guard, builder, stats);
      else
        mergeTreeExtraAlu(*source, mapping, guard, builder, stats);
    } else if (auto *load = dyn_cast<LoadInst>(source))
      mergeExtraLoad(*load, mapping, builder, stats);
    else if (auto *store = dyn_cast<StoreInst>(source))
      mergeStore(*store, onLeft, mapping, condition, builder, stats);
    else
      mergeExtraAlu(*source, onLeft, mapping, condition, builder, stats);
  }

  for (PHINode *phi : diamond.outputPhis) {
    Value *replacement = nullptr;
    if (stats.internalTree || stats.internalDag) {
      replacement = Constant::getNullValue(phi->getType());
      auto addLeaves = [&](ArrayRef<ArmLeaf> leaves,
                           ArrayRef<BasicBlock *> blocks,
                           ArrayRef<ArmBlockInfo> topology,
                           ValueToValueMapTy &mapping,
                           DenseMap<BasicBlock *, Value *> &guards,
                           DagGuardCache *edgeCache) {
        for (const ArmLeaf &leaf : leaves) {
          BasicBlock *block = blocks[leaf.blockOrdinal];
          Value *guard =
              stats.internalDag
                  ? dagLeafGuard(
                        leaf, blocks, topology, mapping, guards,
                        edgeCache, predicateCachePointer, builder)
                  : leafGuard(
                        leaf, blocks, topology, mapping, guards,
                        builder);
          if (guard == nullptr)
            return false;
          Value *incoming = mapped(
              phi->getIncomingValueForBlock(block), mapping);
          replacement = selected(
              builder, guard, incoming, replacement, stats,
              "hydra.tree.output");
        }
        return true;
      };
      if (!addLeaves(
              diamond.leftLeaves, diamond.leftBlocks,
              diamond.leftTopology, leftMap, leftGuards,
              leftEdgeCachePointer) ||
          !addLeaves(
              diamond.rightLeaves, diamond.rightBlocks,
              diamond.rightTopology, rightMap, rightGuards,
              rightEdgeCachePointer))
        return false;
    } else {
      Value *left = mapped(
          phi->getIncomingValueForBlock(diamond.leftExit), leftMap);
      Value *right = mapped(
          phi->getIncomingValueForBlock(diamond.rightExit), rightMap);
      replacement = selected(
          builder, condition, left, right, stats, "hydra.output");
    }
    phi->replaceAllUsesWith(replacement);
    ++stats.outputs;
  }
  for (PHINode *phi : diamond.outputPhis)
    phi->eraseFromParent();

  if (stats.sharedPredicateDag) {
    stats.reusedGuardEdges =
        leftEdgeCache.reusedEdges + rightEdgeCache.reusedEdges;
    stats.reusedPredicateNegations =
        activePredicateCache->reusedNegations -
        reusedNegationsBefore;
    stats.crossRegionReusedPredicateNegations =
        activePredicateCache->crossRegionReusedNegations -
        crossRegionReusesBefore;
    stats.crossRegionSourceSites.append(
        activePredicateCache->currentCrossRegionSourceSites.begin(),
        activePredicateCache->currentCrossRegionSourceSites.end());
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.reusedGuardEdges);
    fingerprint = mixSiteIdInteger(
        fingerprint, stats.reusedPredicateNegations);
    if (stats.crossRegionSharedPredicateDag) {
      fingerprint = mixSiteIdInteger(
          fingerprint,
          stats.crossRegionReusedPredicateNegations);
      fingerprint = mixSiteIdInteger(
          fingerprint, stats.crossRegionSourceSites.size());
      for (uint64_t site : stats.crossRegionSourceSites)
        fingerprint = mixSiteIdInteger(fingerprint, site);
    }
  }
  stats.structureFingerprint = fingerprint;

  auto *mergedBranch = BranchInst::Create(diamond.merge);
  LLVMContext &context = mergedBranch->getContext();
  SmallVector<Metadata *, 32> regionProof;
  regionProof.push_back(MDString::get(context, regionSchema));
  regionProof.push_back(ConstantAsMetadata::get(
      ConstantInt::get(Type::getInt64Ty(context), profile.site)));
  regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
      Type::getInt32Ty(context), diamond.leftBlocks.size())));
  if (stats.unequalArms || stats.internalTree ||
      stats.internalDag)
    regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
        Type::getInt32Ty(context), diamond.rightBlocks.size())));
  regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
      Type::getInt32Ty(context), diamond.leftInstructions.size())));
  regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
      Type::getInt32Ty(context), diamond.rightInstructions.size())));
  if (stats.internalTree || stats.internalDag) {
    regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
        Type::getInt32Ty(context), leftInternalBranches)));
    regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
        Type::getInt32Ty(context), rightInternalBranches)));
    regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
        Type::getInt32Ty(context), diamond.leftLeaves.size())));
    regionProof.push_back(ConstantAsMetadata::get(ConstantInt::get(
        Type::getInt32Ty(context), diamond.rightLeaves.size())));
    if (stats.internalDag) {
      regionProof.push_back(ConstantAsMetadata::get(
          ConstantInt::get(
              Type::getInt32Ty(context),
              stats.leftLocalMerges)));
      regionProof.push_back(ConstantAsMetadata::get(
          ConstantInt::get(
              Type::getInt32Ty(context),
              stats.rightLocalMerges)));
      regionProof.push_back(ConstantAsMetadata::get(
          ConstantInt::get(
              Type::getInt32Ty(context),
              stats.leftLocalPhiSites.size())));
      regionProof.push_back(ConstantAsMetadata::get(
          ConstantInt::get(
              Type::getInt32Ty(context),
              stats.rightLocalPhiSites.size())));
      if (stats.sharedPredicateDag) {
        regionProof.push_back(ConstantAsMetadata::get(
            ConstantInt::get(
                Type::getInt32Ty(context),
                stats.canonicalGuardEdges)));
        regionProof.push_back(ConstantAsMetadata::get(
            ConstantInt::get(
                Type::getInt32Ty(context),
                stats.leftPredicateSites.size())));
        regionProof.push_back(ConstantAsMetadata::get(
            ConstantInt::get(
                Type::getInt32Ty(context),
                stats.rightPredicateSites.size())));
        regionProof.push_back(ConstantAsMetadata::get(
            ConstantInt::get(
                Type::getInt32Ty(context),
                stats.uniquePredicates)));
        regionProof.push_back(ConstantAsMetadata::get(
            ConstantInt::get(
                Type::getInt32Ty(context),
                stats.reusedPredicateOccurrences)));
        if (stats.crossRegionSharedPredicateDag) {
          regionProof.push_back(ConstantAsMetadata::get(
              ConstantInt::get(
                  Type::getInt64Ty(context),
                  stats.transactionFingerprint)));
          regionProof.push_back(ConstantAsMetadata::get(
              ConstantInt::get(
                  Type::getInt32Ty(context),
                  stats.transactionOrdinal)));
          regionProof.push_back(ConstantAsMetadata::get(
              ConstantInt::get(
                  Type::getInt32Ty(context),
                  stats.transactionSize)));
          regionProof.push_back(ConstantAsMetadata::get(
              ConstantInt::get(
                  Type::getInt32Ty(context),
                  stats.crossRegionReusedPredicateNegations)));
          regionProof.push_back(ConstantAsMetadata::get(
              ConstantInt::get(
                  Type::getInt32Ty(context),
                  stats.crossRegionSourceSites.size())));
          for (uint64_t site : stats.crossRegionSourceSites)
            regionProof.push_back(ConstantAsMetadata::get(
                ConstantInt::get(
                    Type::getInt64Ty(context), site)));
        }
      }
    }
  }
  mergedBranch->setMetadata(
      "symcc.hydra_region", MDNode::get(context, regionProof));
  ReplaceInstWithInst(diamond.branch, mergedBranch);
  SmallVector<BasicBlock *, 2 * kMaxTreeArmBlocks> deadBlocks;
  deadBlocks.append(
      diamond.leftBlocks.begin(), diamond.leftBlocks.end());
  deadBlocks.append(
      diamond.rightBlocks.begin(), diamond.rightBlocks.end());
  DeleteDeadBlocks(deadBlocks);

  stats.site = profile.site;
  stats.profileScore = profile.score;
  stats.profileObservations = profile.observations;
  stats.profileInteresting = profile.interesting;
  stats.profileSolverTimeUs = profile.solverTimeUs;
  return true;
}

std::string blockName(const BasicBlock &block) {
  if (block.hasName())
    return block.getName().str();
  unsigned index = 0;
  for (const BasicBlock &candidate : *block.getParent()) {
    if (&candidate == &block)
      break;
    ++index;
  }
  return ("bb" + std::to_string(index));
}

void writeManifest(const Module &module, const Function &function,
                   StringRef entryBlock, bool aggressive,
                   const TransformStats &stats, StringRef path) {
  if (path.empty())
    return;
  json::Object record;
  record["schema"] = kManifestSchema;
  record["module"] = module.getModuleIdentifier();
  record["source_file"] = module.getSourceFileName();
  record["function"] = function.getName().str();
  record["entry_block"] = entryBlock.str();
  record["region_schema"] =
      stats.crossRegionSharedPredicateDag
          ? kCrossRegionSharedPredicateDagSchema
          : stats.sharedPredicateDag
                ? kSharedPredicateDagSchema
          : (stats.internalDag
                 ? kInternalDagSchema
                 : (stats.internalTree
                        ? kInternalTreeSchema
                        : (stats.unequalArms ? kUnequalLinearSchema
                                             : kMultiBlockSchema)));
  record["site"] = stats.site;
  record["mode"] = aggressive ? "aggressive-memory" : "safe-alu";
  record["llvm_ir_semantics"] = kLlvmSemanticsPolicy;
  record["llvm_version"] = LLVM_VERSION_STRING;
  record["llvm_major"] = LLVM_VERSION_MAJOR;
  record["inactive_operand_policy"] = kInactiveOperandPolicy;
  record["freeze_policy"] = kFreezePolicy;
  record["exception_policy"] = kExceptionPolicy;
  record["left_freeze_instructions"] =
      static_cast<int64_t>(stats.leftFreezes);
  record["right_freeze_instructions"] =
      static_cast<int64_t>(stats.rightFreezes);
  record["aligned_freeze_pairs"] =
      static_cast<int64_t>(stats.alignedFreezePairs);
  record["extra_freeze_instructions"] =
      static_cast<int64_t>(stats.extraFreezes);
  record["profile_score"] = stats.profileScore;
  record["profile_observations"] = stats.profileObservations;
  record["profile_interesting"] = stats.profileInteresting;
  record["profile_solver_time_us"] = stats.profileSolverTimeUs;
  record["selection_source"] = stats.selectionSource;
  if (!stats.profileSchema.empty())
    record["profile_schema"] = stats.profileSchema;
  if (!stats.profileSha256.empty())
    record["profile_sha256"] = stats.profileSha256;
  if (!stats.profiledExecutableSha256.empty())
    record["profiled_executable_sha256"] =
        stats.profiledExecutableSha256;
  if (!stats.profiledCommandSha256.empty())
    record["profiled_command_sha256"] =
        stats.profiledCommandSha256;
  record["aligned_pairs"] = stats.aligned;
  record["extra_alu"] = stats.extraAlu;
  record["linearized_loads"] = stats.linearizedLoads;
  record["readback_stores"] = stats.readbackStores;
  record["selects"] = stats.selects;
  record["output_phis"] = stats.outputs;
  if (stats.internalTree || stats.internalDag) {
    record["left_arm_blocks"] =
        static_cast<int64_t>(stats.leftArmBlocks);
    record["right_arm_blocks"] =
        static_cast<int64_t>(stats.rightArmBlocks);
    record["left_instruction_count"] =
        static_cast<int64_t>(stats.leftInstructionCount);
    record["right_instruction_count"] =
        static_cast<int64_t>(stats.rightInstructionCount);
    record["edit_distance"] =
        static_cast<int64_t>(stats.editDistance);
    record["alignment_algorithm"] =
        stats.crossRegionSharedPredicateDag
            ? "compatible-lcs-cross-region-shared-dag-topological-left-tie-v6"
            : stats.sharedPredicateDag
                  ? "compatible-lcs-shared-dag-topological-left-tie-v5"
            : (stats.internalDag
                   ? "compatible-lcs-dag-topological-left-tie-v4"
                   : "compatible-lcs-tree-preorder-left-tie-v3");
    record["unequal_arm_blocks"] = stats.unequalArms;
    record["internal_tree"] = stats.internalTree;
    if (stats.internalDag)
      record["internal_dag"] = true;
    if (stats.sharedPredicateDag)
      record["shared_predicate_dag"] = true;
    auto countBranches = [](ArrayRef<ArmTopologyProof> topology) {
      return static_cast<int64_t>(std::count_if(
          topology.begin(), topology.end(),
          [](const ArmTopologyProof &block) {
            return block.successors.size() == 2;
          }));
    };
    record["left_internal_branches"] =
        countBranches(stats.leftTopology);
    record["right_internal_branches"] =
        countBranches(stats.rightTopology);
    record["left_leaf_edges"] =
        static_cast<int64_t>(stats.leftLeaves.size());
    record["right_leaf_edges"] =
        static_cast<int64_t>(stats.rightLeaves.size());
    if (stats.internalDag) {
      record["left_local_merges"] =
          static_cast<int64_t>(stats.leftLocalMerges);
      record["right_local_merges"] =
          static_cast<int64_t>(stats.rightLocalMerges);
      record["left_local_phis"] =
          static_cast<int64_t>(stats.leftLocalPhiSites.size());
      record["right_local_phis"] =
          static_cast<int64_t>(stats.rightLocalPhiSites.size());
      if (stats.sharedPredicateDag) {
        record["guard_reuse_policy"] =
            stats.crossRegionSharedPredicateDag
                ? "per-arm-edge-and-function-dominating-negation-hash-cons-v2"
                : "per-arm-edge-and-global-negation-hash-cons-v1";
        record["canonical_guard_edges"] =
            static_cast<int64_t>(stats.canonicalGuardEdges);
        record["reused_guard_edges"] =
            static_cast<int64_t>(stats.reusedGuardEdges);
        record["unique_predicates"] =
            static_cast<int64_t>(stats.uniquePredicates);
        record["reused_predicate_occurrences"] =
            static_cast<int64_t>(
                stats.reusedPredicateOccurrences);
        record["reused_predicate_negations"] =
            static_cast<int64_t>(
                stats.reusedPredicateNegations);
        if (stats.crossRegionSharedPredicateDag) {
          record["multi_site_transaction"] = true;
          record["transaction_fingerprint"] =
              std::to_string(stats.transactionFingerprint);
          record["transaction_ordinal"] =
              static_cast<int64_t>(stats.transactionOrdinal);
          record["transaction_size"] =
              static_cast<int64_t>(stats.transactionSize);
          json::Array transactionSites;
          for (uint64_t site : stats.transactionSites)
            transactionSites.push_back(std::to_string(site));
          record["transaction_sites"] =
              std::move(transactionSites);
          json::Array sharedPredicates;
          for (uint64_t site :
               stats.transactionSharedPredicateSites)
            sharedPredicates.push_back(std::to_string(site));
          record["transaction_shared_predicate_sites"] =
              std::move(sharedPredicates);
          record["cross_region_reused_predicate_negations"] =
              static_cast<int64_t>(
                  stats.crossRegionReusedPredicateNegations);
          json::Array sourceSites;
          for (uint64_t site : stats.crossRegionSourceSites)
            sourceSites.push_back(std::to_string(site));
          record["cross_region_source_sites"] =
              std::move(sourceSites);
        }
      }
    }
  } else if (stats.unequalArms) {
    record["left_arm_blocks"] =
        static_cast<int64_t>(stats.leftArmBlocks);
    record["right_arm_blocks"] =
        static_cast<int64_t>(stats.rightArmBlocks);
    record["left_instruction_count"] =
        static_cast<int64_t>(stats.leftInstructionCount);
    record["right_instruction_count"] =
        static_cast<int64_t>(stats.rightInstructionCount);
    record["edit_distance"] =
        static_cast<int64_t>(stats.editDistance);
    record["alignment_algorithm"] =
        "compatible-lcs-left-tie-v2";
    record["unequal_arm_blocks"] = true;
  } else {
    record["arm_blocks"] =
        static_cast<int64_t>(stats.leftArmBlocks);
  }
  record["multi_block"] =
      std::max(stats.leftArmBlocks, stats.rightArmBlocks) > 1;
  json::Array leftBlocks;
  for (uint64_t site : stats.leftBlockSites)
    leftBlocks.push_back(std::to_string(site));
  record["left_block_sites"] = std::move(leftBlocks);
  json::Array rightBlocks;
  for (uint64_t site : stats.rightBlockSites)
    rightBlocks.push_back(std::to_string(site));
  record["right_block_sites"] = std::move(rightBlocks);
  if (stats.internalTree || stats.internalDag) {
    auto topologyArray = [dag = stats.internalDag](
                             ArrayRef<ArmTopologyProof> topology) {
      json::Array result;
      for (unsigned ordinal = 0; ordinal < topology.size(); ++ordinal) {
        const ArmTopologyProof &block = topology[ordinal];
        json::Object item;
        item["ordinal"] = static_cast<int64_t>(ordinal);
        item["block_site"] = std::to_string(block.blockSite);
        item["terminator_site"] =
            std::to_string(block.terminatorSite);
        if (dag) {
          json::Array predecessors;
          for (const ArmIncomingEdge &predecessor :
               block.predecessors) {
            json::Object edge;
            edge["block_ordinal"] =
                static_cast<int64_t>(
                    predecessor.blockOrdinal);
            edge["successor_index"] =
                static_cast<int64_t>(
                    predecessor.successorIndex);
            predecessors.push_back(std::move(edge));
          }
          item["predecessors"] = std::move(predecessors);
        } else {
          item["parent_ordinal"] =
              static_cast<int64_t>(block.parentOrdinal);
          item["incoming_edge"] =
              static_cast<int64_t>(block.incomingEdge);
        }
        item["terminator_kind"] =
            block.successors.size() == 2 ? "conditional"
                                         : "unconditional";
        json::Array successors;
        for (int successor : block.successors) {
          json::Object edge;
          if (successor < 0) {
            edge["kind"] = "merge";
          } else {
            edge["kind"] = "block";
            edge["ordinal"] = static_cast<int64_t>(successor);
          }
          successors.push_back(std::move(edge));
        }
        item["successors"] = std::move(successors);
        result.push_back(std::move(item));
      }
      return result;
    };
    record["left_topology"] =
        topologyArray(stats.leftTopology);
    record["right_topology"] =
        topologyArray(stats.rightTopology);
    auto leafArray = [](ArrayRef<ArmLeaf> leaves) {
      json::Array result;
      for (const ArmLeaf &leaf : leaves) {
        json::Object item;
        item["block_ordinal"] =
            static_cast<int64_t>(leaf.blockOrdinal);
        item["successor_index"] =
            static_cast<int64_t>(leaf.successorIndex);
        result.push_back(std::move(item));
      }
      return result;
    };
    record["left_leaves"] = leafArray(stats.leftLeaves);
    record["right_leaves"] = leafArray(stats.rightLeaves);
    if (stats.internalDag) {
      json::Array leftPhis;
      for (uint64_t site : stats.leftLocalPhiSites)
        leftPhis.push_back(std::to_string(site));
      record["left_local_phi_sites"] = std::move(leftPhis);
      json::Array rightPhis;
      for (uint64_t site : stats.rightLocalPhiSites)
        rightPhis.push_back(std::to_string(site));
      record["right_local_phi_sites"] = std::move(rightPhis);
      if (stats.sharedPredicateDag) {
        json::Array leftPredicates;
        for (uint64_t site : stats.leftPredicateSites)
          leftPredicates.push_back(std::to_string(site));
        record["left_predicate_sites"] =
            std::move(leftPredicates);
        json::Array rightPredicates;
        for (uint64_t site : stats.rightPredicateSites)
          rightPredicates.push_back(std::to_string(site));
        record["right_predicate_sites"] =
            std::move(rightPredicates);
      }
    }
  }
  json::Array alignment;
  for (unsigned ordinal = 0; ordinal < stats.alignment.size(); ++ordinal) {
    const AlignmentProofSlot &slot = stats.alignment[ordinal];
    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["left_site"] =
        slot.leftSite == 0 ? "" : std::to_string(slot.leftSite);
    item["right_site"] =
        slot.rightSite == 0 ? "" : std::to_string(slot.rightSite);
    item["left_opcode"] = static_cast<int64_t>(slot.leftOpcode);
    item["right_opcode"] = static_cast<int64_t>(slot.rightOpcode);
    if (stats.unequalArms || stats.internalTree ||
        stats.internalDag) {
      item["left_block_ordinal"] =
          static_cast<int64_t>(slot.leftBlockOrdinal);
      item["right_block_ordinal"] =
          static_cast<int64_t>(slot.rightBlockOrdinal);
    }
    alignment.push_back(std::move(item));
  }
  record["alignment"] = std::move(alignment);
  json::Array outputPhis;
  for (uint64_t site : stats.outputPhiSites)
    outputPhis.push_back(std::to_string(site));
  record["output_phi_sites"] = std::move(outputPhis);
  record["structure_fingerprint"] =
      std::to_string(stats.structureFingerprint);
  record["single_site_build"] =
      !stats.crossRegionSharedPredicateDag;
  record["requires_original_replay"] = aggressive;
  record["requires_original_coverage_replay"] = true;

  (void)appendManifestRecord(
      path, formatv("{0}", json::Value(std::move(record))).str());
}

} // namespace

bool transformHydra(Module &module) {
  if (!enabled(std::getenv("SYMCC_HYDRA")))
    return false;

  const char *profilePath = std::getenv("SYMCC_HYDRA_PROFILE");
  const char *denylistPath = std::getenv("SYMCC_HYDRA_DENYLIST");
  const char *manifestPath = std::getenv("SYMCC_HYDRA_MANIFEST_OUT");
  bool aggressive = true;
  if (const char *mode = std::getenv("SYMCC_HYDRA_MODE"))
    aggressive = !StringRef(mode).equals_insensitive("safe");

  ProfileDocument profile;
  bool explicitSite = false;
  SmallVector<uint64_t, kMaxCrossRegionSites> explicitMultiSites;
  const char *multiSiteText = std::getenv("SYMCC_HYDRA_SITES");
  const bool explicitMultiSite = multiSiteText != nullptr;
  if (explicitMultiSite) {
    if (!parseSiteSequence(multiSiteText, explicitMultiSites))
      return false;
    profile.valid = true;
    profile.selectionSource = "explicit-sites";
    for (unsigned ordinal = 0;
         ordinal < explicitMultiSites.size(); ++ordinal)
      profile.entries.push_back(ProfileEntry{
          explicitMultiSites[ordinal],
          static_cast<double>(
              explicitMultiSites.size() - ordinal),
          0, 0, 0});
  } else if (const char *siteText =
                 std::getenv("SYMCC_HYDRA_SITE")) {
    if (auto site = parseUnsigned(siteText)) {
      explicitSite = true;
      profile.valid = true;
      profile.selectionSource = "explicit-site";
      profile.entries.push_back(
          ProfileEntry{*site, 1.0, 0, 0, 0});
    }
  }
  if (!explicitSite && !explicitMultiSite)
    profile = readProfile(profilePath == nullptr ? "" : profilePath);
  if (!profile.valid)
    return false;
  const bool profileRequired =
      explicitSite || explicitMultiSite || profile.supplied;
  const std::set<uint64_t> denied =
      readDenylist(denylistPath == nullptr ? "" : denylistPath);
  double minimumScore = 0.0;
  if (const char *score = std::getenv("SYMCC_HYDRA_MIN_SCORE"))
    minimumScore = parseScore(score);

  DenseMap<uint64_t, ProfileEntry> bySite;
  for (const ProfileEntry &entry : profile.entries) {
    auto found = bySite.find(entry.site);
    if (found == bySite.end() || entry.score > found->second.score)
      bySite[entry.site] = entry;
  }

  initializeStableSiteIds(module);
  struct Candidate {
    BranchInst *branch = nullptr;
    ProfileEntry profile;
  };
  std::vector<Candidate> candidates;
  for (Function &function : module) {
    if (function.isDeclaration())
      continue;
    for (BasicBlock &block : function) {
      auto *branch = dyn_cast<BranchInst>(block.getTerminator());
      if (branch == nullptr || !branch->isConditional())
        continue;
      uint64_t site = stableSiteId(*branch);
      if (denied.count(site) != 0)
        continue;
      auto found = bySite.find(site);
      if (profileRequired && found == bySite.end())
        continue;
      ProfileEntry entry =
          found == bySite.end() ? ProfileEntry{site, 1.0, 0, 0, 0}
                                : found->second;
      if (entry.score < minimumScore)
        continue;
      Diamond diamond;
      std::string reason;
      if (collectDiamond(*branch, aggressive, diamond, reason))
        candidates.push_back({branch, entry});
    }
  }
  if (candidates.empty())
    return false;

  if (explicitMultiSite) {
    std::vector<Candidate> ordered;
    ordered.reserve(explicitMultiSites.size());
    for (uint64_t site : explicitMultiSites) {
      auto found = std::find_if(
          candidates.begin(), candidates.end(),
          [site](const Candidate &candidate) {
            return candidate.profile.site == site;
          });
      if (found == candidates.end())
        return false;
      ordered.push_back(*found);
    }

    Function *function = ordered.front().branch->getFunction();
    std::vector<Diamond> diamonds;
    diamonds.reserve(ordered.size());
    for (const Candidate &candidate : ordered) {
      if (candidate.branch->getFunction() != function)
        return false;
      Diamond diamond;
      std::string reason;
      if (!collectDiamond(
              *candidate.branch, aggressive, diamond, reason) ||
          !diamond.sharedPredicateDag)
        return false;
      diamonds.push_back(std::move(diamond));
    }

    auto ownedBy = [](const Diamond &diamond,
                      const BasicBlock *block) {
      return containsBlock(diamond.leftBlocks, block) ||
             containsBlock(diamond.rightBlocks, block);
    };
    for (size_t left = 0; left < diamonds.size(); ++left) {
      for (size_t right = left + 1;
           right < diamonds.size(); ++right) {
        for (BasicBlock *block : diamonds[left].leftBlocks)
          if (ownedBy(diamonds[right], block))
            return false;
        for (BasicBlock *block : diamonds[left].rightBlocks)
          if (ownedBy(diamonds[right], block))
            return false;
        if (ownedBy(
                diamonds[left],
                diamonds[right].branch->getParent()) ||
            ownedBy(
                diamonds[right],
                diamonds[left].branch->getParent()) ||
            ownedBy(
                diamonds[left], diamonds[right].merge) ||
            ownedBy(
                diamonds[right], diamonds[left].merge))
          return false;
      }
    }

    DominatorTree initialDominators(*function);
    for (size_t ordinal = 1; ordinal < diamonds.size();
         ++ordinal) {
      if (!initialDominators.dominates(
              diamonds[ordinal - 1].branch->getParent(),
              diamonds[ordinal].branch->getParent()))
        return false;
    }

    auto predicateSet = [](const Diamond &diamond) {
      std::set<Value *> predicates;
      auto collect = [&predicates](
                         ArrayRef<ArmBlockInfo> topology) {
        for (const ArmBlockInfo &info : topology)
          if (info.terminator != nullptr &&
              info.terminator->isConditional())
            predicates.insert(
                info.terminator->getCondition());
      };
      collect(diamond.leftTopology);
      collect(diamond.rightTopology);
      return predicates;
    };
    std::set<Value *> sharedPredicates =
        predicateSet(diamonds.front());
    for (size_t ordinal = 1; ordinal < diamonds.size();
         ++ordinal) {
      std::set<Value *> current =
          predicateSet(diamonds[ordinal]);
      for (auto iterator = sharedPredicates.begin();
           iterator != sharedPredicates.end();) {
        if (current.count(*iterator) == 0)
          iterator = sharedPredicates.erase(iterator);
        else
          ++iterator;
      }
    }
    if (sharedPredicates.empty())
      return false;
    SmallVector<uint64_t, 8> sharedPredicateSites;
    for (Value *predicate : sharedPredicates)
      sharedPredicateSites.push_back(
          stableSiteId(*predicate));
    std::sort(
        sharedPredicateSites.begin(),
        sharedPredicateSites.end());
    sharedPredicateSites.erase(
        std::unique(
            sharedPredicateSites.begin(),
            sharedPredicateSites.end()),
        sharedPredicateSites.end());
    if (sharedPredicateSites.size() !=
        sharedPredicates.size())
      return false;

    uint64_t transactionFingerprint =
        mixSiteIdText(
            1469598103934665603ULL,
            kCrossRegionSharedPredicateDagSchema);
    transactionFingerprint = mixSiteIdInteger(
        transactionFingerprint, explicitMultiSites.size());
    for (uint64_t site : explicitMultiSites)
      transactionFingerprint =
          mixSiteIdInteger(transactionFingerprint, site);
    transactionFingerprint = mixSiteIdInteger(
        transactionFingerprint, sharedPredicateSites.size());
    for (uint64_t site : sharedPredicateSites)
      transactionFingerprint =
          mixSiteIdInteger(transactionFingerprint, site);

    PredicateGuardCache functionPredicateCache;
    for (unsigned ordinal = 0; ordinal < diamonds.size();
         ++ordinal) {
      DominatorTree dominators(*function);
      TransformStats stats;
      stats.crossRegionSharedPredicateDag = true;
      stats.transactionFingerprint = transactionFingerprint;
      stats.transactionOrdinal = ordinal;
      stats.transactionSize = diamonds.size();
      stats.transactionSites.append(
          explicitMultiSites.begin(), explicitMultiSites.end());
      stats.transactionSharedPredicateSites.append(
          sharedPredicateSites.begin(),
          sharedPredicateSites.end());
      std::string entryBlock =
          blockName(*diamonds[ordinal].branch->getParent());
      if (!applyDiamond(
              diamonds[ordinal], ordered[ordinal].profile,
              stats, &functionPredicateCache, &dominators))
        return false;
      if (ordinal != 0 &&
          stats.crossRegionReusedPredicateNegations == 0)
        return false;
      stats.selectionSource = profile.selectionSource;
      writeManifest(
          module, *function, entryBlock, aggressive, stats,
          manifestPath == nullptr ? "" : manifestPath);
    }
    return true;
  }

  std::sort(candidates.begin(), candidates.end(),
            [](const Candidate &left, const Candidate &right) {
              if (left.profile.score != right.profile.score)
                return left.profile.score > right.profile.score;
              return left.profile.site < right.profile.site;
            });
  Candidate selectedCandidate = candidates.front();
  BranchInst *selected = selectedCandidate.branch;
  Function *function = selected->getFunction();
  std::string entryBlock = blockName(*selected->getParent());
  Diamond diamond;
  std::string reason;
  if (!collectDiamond(*selected, aggressive, diamond, reason))
    return false;

  TransformStats stats;
  if (!applyDiamond(diamond, selectedCandidate.profile, stats))
    return false;
  stats.selectionSource = profile.selectionSource;
  stats.profileSchema = profile.schema;
  stats.profileSha256 = profile.profileSha256;
  stats.profiledExecutableSha256 =
      profile.profiledExecutableSha256;
  stats.profiledCommandSha256 = profile.profiledCommandSha256;
  writeManifest(module, *function, entryBlock, aggressive, stats,
                manifestPath == nullptr ? "" : manifestPath);
  return true;
}

char HydraTransformationLegacyPass::ID = 0;

bool HydraTransformationLegacyPass::runOnModule(Module &module) {
  return transformHydra(module);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses HydraTransformationPass::run(
    Module &module, ModuleAnalysisManager &) {
  return transformHydra(module) ? PreservedAnalyses::none()
                                : PreservedAnalyses::all();
}
#endif

} // namespace symcc
