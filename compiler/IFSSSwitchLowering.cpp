// This file is part of SymCC.
//
// Lower a strict bounded switch fanout into a deterministic branch graph.
// Data and MemorySSA state then reuse the ordinary IFSS partition proof.

#include "IFSSSwitchLowering.h"

#include "ManifestWriter.h"
#include "SiteId.h"

#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/SmallString.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/IR/CFG.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Metadata.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/ProfDataUtils.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/FormatVariadic.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/MemoryBuffer.h>
#include <llvm/Support/raw_ostream.h>

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

using namespace llvm;

namespace symcc {
namespace {

constexpr unsigned kMaxSwitchArms = 8;
constexpr char kLinearSwitchSchema[] = "bounded-switch-chain-v1";
constexpr char kBalancedSwitchSchema[] = "bounded-switch-tree-v1";
constexpr char kProfileSwitchSchema[] = "bounded-switch-profile-tree-v1";
constexpr char kProfileProofSchema[] = "profile-weighted-switch-v1";
constexpr char kProfileFallbackSchema[] = "profile-switch-fallback-v1";
constexpr char kSwitchManifestSchema[] = "symcc-ifss-switch-tree-v1";
constexpr char kSharedDestinationSchema[] =
    "shared-switch-edge-split-v1";
constexpr char kSharedPHISchema[] =
    "shared-switch-phi-multiplicity-v1";

enum class SwitchLoweringMode {
  Linear,
  Balanced,
  Profile,
};

struct SwitchCase {
  ConstantInt *value = nullptr;
  BasicBlock *destination = nullptr;
  unsigned ordinal = 0;
};

struct SwitchRegion {
  SwitchInst *instruction = nullptr;
  SmallVector<SwitchCase, kMaxSwitchArms> cases;
  SmallVector<BasicBlock *, kMaxSwitchArms> destinations;
  DenseMap<BasicBlock *, unsigned> edgeMultiplicity;
  BasicBlock *defaultDestination = nullptr;
  BasicBlock *merge = nullptr;
  unsigned edgeCount = 0;
  bool returnExits = false;
};

struct RawSwitchProfile {
  bool invalid = false;
  std::map<std::string, uint64_t> weights;
};

struct SwitchProfileDatabase {
  bool pathProvided = false;
  bool readable = false;
  bool malformed = false;
  std::map<uint64_t, RawSwitchProfile> bySite;
};

struct SelectedSwitchProfile {
  bool valid = false;
  std::string source;
  std::string fallbackReason;
  SmallVector<uint64_t, kMaxSwitchArms> sortedCaseWeights;
  uint64_t defaultWeight = 0;
  uint64_t fingerprint = 0;
  uint64_t objectiveCost = 0;
  unsigned split[kMaxSwitchArms][kMaxSwitchArms + 1] = {};
};

bool enabled(const char *value) {
  if (value == nullptr || *value == '\0')
    return false;
  StringRef text(value);
  return !text.equals_insensitive("0") && !text.equals_insensitive("false") &&
         !text.equals_insensitive("off") && !text.equals_insensitive("no");
}

SwitchLoweringMode switchLoweringMode() {
  const char *value = std::getenv("SYMCC_IFSS_SWITCH_MODE");
  if (value != nullptr) {
    if (StringRef(value).equals_insensitive("balanced"))
      return SwitchLoweringMode::Balanced;
    if (StringRef(value).equals_insensitive("profile"))
      return SwitchLoweringMode::Profile;
  }
  return SwitchLoweringMode::Linear;
}

std::optional<std::string> canonicalUnsignedText(StringRef text) {
  text = text.trim();
  if (text.empty())
    return std::nullopt;
  for (char character : text)
    if (character < '0' || character > '9')
      return std::nullopt;
  size_t first = 0;
  while (first + 1 < text.size() && text[first] == '0')
    ++first;
  return text.substr(first).str();
}

std::string caseValueText(const ConstantInt &value) {
  SmallString<64> storage;
  value.getValue().toString(storage, 10, false);
  return storage.str().str();
}

SwitchProfileDatabase readSwitchProfiles() {
  SwitchProfileDatabase database;
  const char *path = std::getenv("SYMCC_IFSS_SWITCH_PROFILE");
  if (path == nullptr || *path == '\0')
    return database;
  database.pathProvided = true;
  auto buffer = MemoryBuffer::getFile(path);
  if (!buffer)
    return database;
  database.readable = true;

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
    SmallVector<StringRef, 4> fields;
    line.split(fields, ' ', -1, false);
    if (fields.size() != 3) {
      database.malformed = true;
      continue;
    }
    uint64_t site = 0;
    uint64_t weight = 0;
    if (fields[0].getAsInteger(0, site) ||
        fields[2].getAsInteger(10, weight) || weight == 0) {
      database.malformed = true;
      continue;
    }
    std::string key;
    if (fields[1].equals_insensitive("default")) {
      key = "default";
    } else {
      auto canonical = canonicalUnsignedText(fields[1]);
      if (!canonical) {
        database.malformed = true;
        continue;
      }
      key = *canonical;
    }
    RawSwitchProfile &profile = database.bySite[site];
    if (!profile.weights.emplace(key, weight).second)
      profile.invalid = true;
  }
  return database;
}

uint64_t saturatingAdd(uint64_t left, uint64_t right) {
  if (left > std::numeric_limits<uint64_t>::max() - right)
    return std::numeric_limits<uint64_t>::max();
  return left + right;
}

uint64_t saturatingAdd(uint64_t first, uint64_t second, uint64_t third) {
  return saturatingAdd(saturatingAdd(first, second), third);
}

Metadata *integerMetadata(LLVMContext &context, unsigned bits, uint64_t value) {
  return ConstantAsMetadata::get(
      ConstantInt::get(IntegerType::get(context, bits), value));
}

bool collectSwitchRegion(SwitchInst &instruction, SwitchRegion &result) {
  const unsigned arms = instruction.getNumSuccessors();
  if (arms < 2 || arms > kMaxSwitchArms ||
      instruction.getNumCases() + 1 != arms)
    return false;

  BasicBlock *controller = instruction.getParent();
  SmallPtrSet<BasicBlock *, kMaxSwitchArms> uniqueDestinations;
  result.instruction = &instruction;
  result.edgeCount = arms;
  result.defaultDestination = instruction.getDefaultDest();
  result.destinations.push_back(result.defaultDestination);
  uniqueDestinations.insert(result.defaultDestination);
  ++result.edgeMultiplicity[result.defaultDestination];
  unsigned ordinal = 0;
  for (auto caseHandle : instruction.cases()) {
    BasicBlock *destination = caseHandle.getCaseSuccessor();
    if (uniqueDestinations.insert(destination).second)
      result.destinations.push_back(destination);
    ++result.edgeMultiplicity[destination];
    result.cases.push_back(
        {caseHandle.getCaseValue(), destination, ordinal++});
  }
  if (result.destinations.size() < 2)
    return false;

  for (BasicBlock *destination : result.destinations)
    if (destination == controller || destination->hasAddressTaken() ||
        destination->isEHPad() ||
        destination->getUniquePredecessor() != controller ||
        pred_size(destination) != result.edgeMultiplicity.lookup(destination))
      return false;
  for (BasicBlock *destination : result.destinations) {
    const unsigned expectedEdges =
        result.edgeMultiplicity.lookup(destination);
    for (PHINode &phi : destination->phis()) {
      Value *edgeValue = nullptr;
      unsigned incomingEdges = 0;
      for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
        if (phi.getIncomingBlock(index) != controller)
          continue;
        Value *incoming = phi.getIncomingValue(index);
        if (edgeValue != nullptr && incoming != edgeValue)
          return false;
        edgeValue = incoming;
        ++incomingEdges;
      }
      if (incomingEdges != expectedEdges)
        return false;
    }
  }

  Instruction *firstExit = result.destinations.front()->getTerminator();
  result.returnExits = isa<ReturnInst>(firstExit);
  if (result.returnExits) {
    for (BasicBlock *destination : result.destinations)
      if (!isa<ReturnInst>(destination->getTerminator()))
        return false;
    return true;
  }

  auto *firstBranch = dyn_cast<BranchInst>(firstExit);
  if (firstBranch == nullptr || !firstBranch->isUnconditional())
    return false;
  result.merge = firstBranch->getSuccessor(0);
  if (result.merge == controller ||
      uniqueDestinations.count(result.merge) != 0 ||
      pred_size(result.merge) != result.destinations.size())
    return false;
  for (BasicBlock *destination : result.destinations) {
    auto *branch = dyn_cast<BranchInst>(destination->getTerminator());
    if (branch == nullptr || !branch->isUnconditional() ||
        branch->getSuccessor(0) != result.merge)
      return false;
  }
  for (BasicBlock *predecessor : predecessors(result.merge))
    if (uniqueDestinations.count(predecessor) == 0)
      return false;
  return true;
}

SmallVector<SwitchCase, kMaxSwitchArms>
sortedSwitchCases(const SwitchRegion &region) {
  SmallVector<SwitchCase, kMaxSwitchArms> result = region.cases;
  std::sort(
      result.begin(), result.end(),
      [](const SwitchCase &left, const SwitchCase &right) {
        return left.value->getValue().ult(right.value->getValue());
      });
  return result;
}

SelectedSwitchProfile buildSelectedSwitchProfile(
    const SwitchRegion &region, ArrayRef<uint64_t> sortedCaseWeights,
    uint64_t defaultWeight, StringRef source) {
  SelectedSwitchProfile selected;
  const uint64_t site = stableSiteId(*region.instruction);
  SmallVector<SwitchCase, kMaxSwitchArms> cases =
      sortedSwitchCases(region);
  assert(sortedCaseWeights.size() == cases.size() &&
         "selected switch profile has wrong case count");
  selected.source = source.str();
  selected.sortedCaseWeights.assign(
      sortedCaseWeights.begin(), sortedCaseWeights.end());
  selected.defaultWeight = defaultWeight;

  uint64_t profileFingerprint =
      mixSiteIdInteger(1469598103934665603ULL, site);
  profileFingerprint =
      mixSiteIdText(profileFingerprint, selected.source);
  for (unsigned index = 0; index < cases.size(); ++index) {
    profileFingerprint =
        mixSiteIdText(profileFingerprint, caseValueText(*cases[index].value));
    profileFingerprint = mixSiteIdInteger(
        profileFingerprint, selected.sortedCaseWeights[index]);
  }
  profileFingerprint = mixSiteIdText(profileFingerprint, "default");
  profileFingerprint =
      mixSiteIdInteger(profileFingerprint, selected.defaultWeight);
  selected.fingerprint = profileFingerprint;

  uint64_t cost[kMaxSwitchArms][kMaxSwitchArms + 1] = {};
  for (unsigned length = 2; length <= cases.size(); ++length) {
    for (unsigned begin = 0; begin + length <= cases.size(); ++begin) {
      const unsigned end = begin + length;
      uint64_t intervalWeight = 0;
      for (unsigned index = begin; index < end; ++index)
        intervalWeight = saturatingAdd(
            intervalWeight, selected.sortedCaseWeights[index]);
      uint64_t best = std::numeric_limits<uint64_t>::max();
      unsigned bestSplit = begin + 1;
      for (unsigned middle = begin + 1; middle < end; ++middle) {
        uint64_t candidate = saturatingAdd(
            intervalWeight, cost[begin][middle], cost[middle][end]);
        if (candidate < best) {
          best = candidate;
          bestSplit = middle;
        }
      }
      cost[begin][end] = best;
      selected.split[begin][end] = bestSplit;
    }
  }
  uint64_t totalCaseWeight = 0;
  for (uint64_t weight : selected.sortedCaseWeights)
    totalCaseWeight = saturatingAdd(totalCaseWeight, weight);
  selected.objectiveCost =
      saturatingAdd(cost[0][cases.size()], totalCaseWeight);
  selected.valid = true;
  return selected;
}

SelectedSwitchProfile selectLLVMBranchWeightProfile(
    const SwitchRegion &region, StringRef missingReason) {
  SelectedSwitchProfile selected;
  SmallVector<uint32_t, kMaxSwitchArms> edgeWeights;
  if (!extractBranchWeights(*region.instruction, edgeWeights)) {
    selected.fallbackReason =
        region.instruction->getMetadata(LLVMContext::MD_prof) == nullptr
            ? missingReason.str()
            : "invalid-branch-weights";
    return selected;
  }
  if (edgeWeights.size() != region.edgeCount) {
    selected.fallbackReason = "invalid-branch-weight-count";
    return selected;
  }
  for (uint32_t weight : edgeWeights)
    if (weight == 0) {
      selected.fallbackReason = "zero-branch-weight";
      return selected;
    }

  std::map<std::string, uint64_t> caseWeights;
  for (unsigned index = 0; index < region.cases.size(); ++index)
    caseWeights.emplace(
        caseValueText(*region.cases[index].value), edgeWeights[index + 1]);
  SmallVector<uint64_t, kMaxSwitchArms> sortedWeights;
  for (const SwitchCase &switchCase : sortedSwitchCases(region))
    sortedWeights.push_back(
        caseWeights.at(caseValueText(*switchCase.value)));
  return buildSelectedSwitchProfile(
      region, sortedWeights, edgeWeights.front(),
      "llvm-branch-weights");
}

SelectedSwitchProfile selectSwitchProfile(
    const SwitchRegion &region,
    const SwitchProfileDatabase &database) {
  if (!database.pathProvided)
    return selectLLVMBranchWeightProfile(
        region, "missing-profile-and-branch-weights");

  SelectedSwitchProfile selected;
  if (!database.readable) {
    selected.fallbackReason = "unreadable-profile";
    return selected;
  }
  if (database.malformed) {
    selected.fallbackReason = "malformed-profile";
    return selected;
  }

  const uint64_t site = stableSiteId(*region.instruction);
  auto profileIt = database.bySite.find(site);
  if (profileIt == database.bySite.end())
    return selectLLVMBranchWeightProfile(
        region, "missing-site-and-branch-weights");
  const RawSwitchProfile &raw = profileIt->second;
  if (raw.invalid) {
    selected.fallbackReason = "invalid-site-profile";
    return selected;
  }

  SmallVector<SwitchCase, kMaxSwitchArms> cases =
      sortedSwitchCases(region);
  if (raw.weights.size() != cases.size() + 1 ||
      raw.weights.find("default") == raw.weights.end()) {
    selected.fallbackReason = "incomplete-profile";
    return selected;
  }
  SmallVector<uint64_t, kMaxSwitchArms> sortedWeights;
  for (const SwitchCase &switchCase : cases) {
    auto weight = raw.weights.find(caseValueText(*switchCase.value));
    if (weight == raw.weights.end()) {
      selected.fallbackReason = "case-set-mismatch";
      return selected;
    }
    sortedWeights.push_back(weight->second);
  }
  return buildSelectedSwitchProfile(
      region, sortedWeights, raw.weights.at("default"),
      "external-stable-site");
}

StringRef switchModeName(SwitchLoweringMode mode) {
  switch (mode) {
  case SwitchLoweringMode::Linear:
    return "linear";
  case SwitchLoweringMode::Balanced:
    return "balanced";
  case SwitchLoweringMode::Profile:
    return "profile";
  default:
    llvm_unreachable("unknown switch lowering mode");
  }
}

unsigned destinationOrdinal(
    const SwitchRegion &region, const BasicBlock *destination) {
  auto found = std::find(
      region.destinations.begin(), region.destinations.end(), destination);
  assert(found != region.destinations.end() &&
         "switch case references unknown destination");
  return static_cast<unsigned>(found - region.destinations.begin());
}

std::string buildSwitchManifestLine(
    const Module &module, const SwitchRegion &region,
    SwitchLoweringMode requestedMode,
    const SelectedSwitchProfile *profile) {
  const bool profileValid = profile != nullptr && profile->valid;
  const bool rangeTree = requestedMode != SwitchLoweringMode::Linear;
  const StringRef effectiveMode =
      requestedMode == SwitchLoweringMode::Profile && !profileValid
          ? StringRef("balanced")
          : switchModeName(requestedMode);
  const uint64_t site = stableSiteId(*region.instruction);

  json::Object record;
  record["schema"] = kSwitchManifestSchema;
  record["module"] = module.getModuleIdentifier();
  record["source_file"] = module.getSourceFileName();
  record["function"] =
      region.instruction->getFunction()->getName().str();
  record["site"] = std::to_string(site);
  record["requested_mode"] = switchModeName(requestedMode).str();
  record["effective_mode"] = effectiveMode.str();
  record["node_schema"] =
      requestedMode == SwitchLoweringMode::Linear
          ? kLinearSwitchSchema
          : (profileValid ? kProfileSwitchSchema
                          : kBalancedSwitchSchema);
  record["logical_edges"] =
      static_cast<int64_t>(region.edgeCount);
  record["unique_destinations"] =
      static_cast<int64_t>(region.destinations.size());
  record["shared_destinations"] =
      region.edgeCount != region.destinations.size();
  record["profile_valid"] = profileValid;
  record["profile_source"] =
      profileValid ? profile->source : "none";
  record["fallback_reason"] =
      profile != nullptr && !profile->valid
          ? profile->fallbackReason
          : "";
  record["profile_fingerprint"] =
      profileValid ? std::to_string(profile->fingerprint) : "";
  record["default_weight"] =
      profileValid ? std::to_string(profile->defaultWeight) : "";
  record["objective_cost"] =
      profileValid ? std::to_string(profile->objectiveCost) : "";

  SmallVector<SwitchCase, kMaxSwitchArms> sortedCases =
      sortedSwitchCases(region);
  std::map<std::string, uint64_t> profileWeights;
  if (profileValid)
    for (unsigned index = 0; index < sortedCases.size(); ++index)
      profileWeights.emplace(
          caseValueText(*sortedCases[index].value),
          profile->sortedCaseWeights[index]);

  json::Array cases;
  for (const SwitchCase &switchCase : region.cases) {
    json::Object item;
    const std::string value = caseValueText(*switchCase.value);
    item["ordinal"] = static_cast<int64_t>(switchCase.ordinal);
    item["value"] = value;
    item["destination"] = static_cast<int64_t>(
        destinationOrdinal(region, switchCase.destination));
    item["weight"] =
        profileValid ? std::to_string(profileWeights.at(value)) : "";
    cases.push_back(std::move(item));
  }
  record["cases"] = std::move(cases);

  unsigned loweredEdgeTotal = 0;
  json::Array destinations;
  for (unsigned ordinal = 0; ordinal < region.destinations.size();
       ++ordinal) {
    BasicBlock *destination = region.destinations[ordinal];
    unsigned loweredMultiplicity = 0;
    for (const SwitchCase &switchCase : region.cases)
      if (switchCase.destination == destination)
        ++loweredMultiplicity;
    if (rangeTree) {
      if (region.defaultDestination == destination)
        loweredMultiplicity += region.cases.size();
    } else if (region.defaultDestination == destination) {
      ++loweredMultiplicity;
    }
    loweredEdgeTotal += loweredMultiplicity;

    json::Object item;
    item["ordinal"] = static_cast<int64_t>(ordinal);
    item["original_multiplicity"] = static_cast<int64_t>(
        region.edgeMultiplicity.lookup(destination));
    item["lowered_multiplicity"] =
        static_cast<int64_t>(loweredMultiplicity);
    item["is_default"] = destination == region.defaultDestination;
    destinations.push_back(std::move(item));
  }
  record["lowered_edges"] = static_cast<int64_t>(loweredEdgeTotal);
  record["destinations"] = std::move(destinations);

  uint64_t treeFingerprint =
      mixSiteIdInteger(1469598103934665603ULL, site);
  treeFingerprint =
      mixSiteIdText(treeFingerprint, effectiveMode);
  if (profileValid)
    treeFingerprint =
        mixSiteIdInteger(treeFingerprint, profile->fingerprint);
  treeFingerprint =
      mixSiteIdInteger(treeFingerprint, region.edgeCount);
  treeFingerprint =
      mixSiteIdInteger(treeFingerprint, loweredEdgeTotal);
  treeFingerprint = mixSiteIdInteger(
      treeFingerprint, region.destinations.size());
  treeFingerprint = mixSiteIdInteger(
      treeFingerprint,
      destinationOrdinal(region, region.defaultDestination));
  for (BasicBlock *destination : region.destinations) {
    unsigned loweredMultiplicity = 0;
    for (const SwitchCase &switchCase : region.cases)
      if (switchCase.destination == destination)
        ++loweredMultiplicity;
    if (rangeTree) {
      if (region.defaultDestination == destination)
        loweredMultiplicity += region.cases.size();
    } else if (region.defaultDestination == destination) {
      ++loweredMultiplicity;
    }
    treeFingerprint = mixSiteIdInteger(
        treeFingerprint, region.edgeMultiplicity.lookup(destination));
    treeFingerprint =
        mixSiteIdInteger(treeFingerprint, loweredMultiplicity);
  }

  json::Array nodes;
  unsigned nextNode = 0;
  if (!rangeTree) {
    for (const SwitchCase &switchCase : region.cases) {
      json::Object node;
      node["id"] = static_cast<int64_t>(nextNode++);
      node["kind"] = "equal";
      node["case_ordinal"] =
          static_cast<int64_t>(switchCase.ordinal);
      node["value"] = caseValueText(*switchCase.value);
      node["destination"] = static_cast<int64_t>(
          destinationOrdinal(region, switchCase.destination));
      treeFingerprint = mixSiteIdText(
          treeFingerprint, caseValueText(*switchCase.value));
      treeFingerprint =
          mixSiteIdInteger(treeFingerprint, switchCase.ordinal);
      treeFingerprint = mixSiteIdInteger(
          treeFingerprint,
          destinationOrdinal(region, switchCase.destination));
      nodes.push_back(std::move(node));
    }
  } else {
    std::function<void(unsigned, unsigned, unsigned)> emit =
        [&](unsigned begin, unsigned end, unsigned depth) {
          const unsigned nodeId = nextNode++;
          json::Object node;
          node["id"] = static_cast<int64_t>(nodeId);
          node["begin"] = static_cast<int64_t>(begin);
          node["end"] = static_cast<int64_t>(end);
          node["depth"] = static_cast<int64_t>(depth);
          if (end - begin == 1) {
            const SwitchCase &switchCase = sortedCases[begin];
            node["kind"] = "equal";
            node["case_ordinal"] =
                static_cast<int64_t>(switchCase.ordinal);
            node["value"] = caseValueText(*switchCase.value);
            node["destination"] = static_cast<int64_t>(
                destinationOrdinal(region, switchCase.destination));
            treeFingerprint = mixSiteIdText(
                treeFingerprint, caseValueText(*switchCase.value));
            treeFingerprint =
                mixSiteIdInteger(treeFingerprint, switchCase.ordinal);
            treeFingerprint = mixSiteIdInteger(
                treeFingerprint,
                destinationOrdinal(region, switchCase.destination));
            nodes.push_back(std::move(node));
            return;
          }
          const unsigned middle =
              profileValid
                  ? profile->split[begin][end]
                  : begin + (end - begin) / 2;
          node["kind"] = "unsigned-upper";
          node["split"] = static_cast<int64_t>(middle);
          node["value"] =
              caseValueText(*sortedCases[middle - 1].value);
          treeFingerprint =
              mixSiteIdInteger(treeFingerprint, begin);
          treeFingerprint =
              mixSiteIdInteger(treeFingerprint, end);
          treeFingerprint =
              mixSiteIdInteger(treeFingerprint, middle);
          nodes.push_back(std::move(node));
          emit(begin, middle, depth + 1);
          emit(middle, end, depth + 1);
        };
    emit(0, sortedCases.size(), 0);
  }
  record["tree_fingerprint"] = std::to_string(treeFingerprint);
  record["nodes"] = std::move(nodes);
  return formatv("{0}", json::Value(std::move(record))).str();
}

void writeSwitchManifest(
    StringRef path, ArrayRef<std::string> records) {
  (void)appendManifestRecords(path, records);
}

void attachSwitchMetadata(Instruction &instruction, StringRef schema,
                          uint64_t switchSite, unsigned ordinal,
                          StringRef kind, ConstantInt *boundary,
                          unsigned arms) {
  LLVMContext &context = instruction.getContext();
  SmallVector<Metadata *, 6> operands;
  operands.push_back(MDString::get(context, schema));
  operands.push_back(integerMetadata(context, 64, switchSite));
  operands.push_back(integerMetadata(context, 32, ordinal));
  operands.push_back(integerMetadata(context, 32, arms));
  operands.push_back(MDString::get(context, kind));
  if (boundary != nullptr)
    operands.push_back(ConstantAsMetadata::get(boundary));
  instruction.setMetadata("symcc.ifss_switch",
                          MDNode::get(context, operands));
}

void attachDefaultMetadata(Instruction &instruction, StringRef schema,
                           uint64_t switchSite, unsigned ordinal,
                           unsigned arms) {
  LLVMContext &context = instruction.getContext();
  Metadata *operands[] = {
      MDString::get(context, schema),
      integerMetadata(context, 64, switchSite),
      integerMetadata(context, 32, ordinal),
      integerMetadata(context, 32, arms),
      MDString::get(context, "default"),
  };
  instruction.setMetadata("symcc.ifss_switch_default",
                          MDNode::get(context, operands));
}

using DestinationPredecessors =
    DenseMap<BasicBlock *, SmallVector<BasicBlock *, kMaxSwitchArms>>;

struct RangeLoweringResult {
  Instruction *rootCondition = nullptr;
  BranchInst *rootBranch = nullptr;
  uint64_t switchSite = 0;
};

void rewriteDestinationPHIs(
    SwitchRegion &region,
    const DestinationPredecessors &newPredecessors) {
  BasicBlock *oldPredecessor = region.instruction->getParent();
  for (BasicBlock *destination : region.destinations) {
    auto found = newPredecessors.find(destination);
    assert(found != newPredecessors.end() && !found->second.empty() &&
           "lowered switch destination has no predecessor");
    for (PHINode &phi : destination->phis()) {
      Value *incomingValue = nullptr;
      unsigned incomingEdges = 0;
      for (unsigned index = 0; index < phi.getNumIncomingValues(); ++index) {
        if (phi.getIncomingBlock(index) != oldPredecessor)
          continue;
        Value *value = phi.getIncomingValue(index);
        assert((incomingValue == nullptr || incomingValue == value) &&
               "validated duplicate switch edges changed PHI value");
        incomingValue = value;
        ++incomingEdges;
      }
      assert(incomingValue != nullptr &&
             "validated switch destination lost predecessor");
      assert(incomingEdges ==
                 region.edgeMultiplicity.lookup(destination) &&
             "validated switch edge multiplicity changed");
      (void)incomingEdges;
      for (unsigned index = phi.getNumIncomingValues(); index != 0;
           --index)
        if (phi.getIncomingBlock(index - 1) == oldPredecessor)
          phi.removeIncomingValue(index - 1, false);
      for (BasicBlock *predecessor : found->second)
        phi.addIncoming(incomingValue, predecessor);
    }
  }
}

void attachSharedDestinationProof(
    SwitchRegion &region,
    const DestinationPredecessors &newPredecessors,
    Instruction &rootCondition, BranchInst &rootBranch,
    uint64_t switchSite, unsigned expectedLoweredEdges) {
  if (region.destinations.size() == region.edgeCount)
    return;

  LLVMContext &context = rootCondition.getContext();
  unsigned loweredEdges = 0;
  for (BasicBlock *destination : region.destinations) {
    auto found = newPredecessors.find(destination);
    assert(found != newPredecessors.end() &&
           "shared switch destination lost lowered edges");
    loweredEdges += found->second.size();
  }
  assert(loweredEdges == expectedLoweredEdges &&
         "shared switch lowering produced unexpected edge multiplicity");
  (void)expectedLoweredEdges;

  SmallVector<Metadata *, 24> summaryOperands;
  summaryOperands.push_back(
      MDString::get(context, kSharedDestinationSchema));
  summaryOperands.push_back(integerMetadata(context, 64, switchSite));
  summaryOperands.push_back(
      integerMetadata(context, 32, region.edgeCount));
  summaryOperands.push_back(integerMetadata(context, 32, loweredEdges));
  summaryOperands.push_back(integerMetadata(
      context, 32, region.destinations.size()));
  for (unsigned ordinal = 0; ordinal < region.destinations.size();
       ++ordinal) {
    BasicBlock *destination = region.destinations[ordinal];
    summaryOperands.push_back(integerMetadata(context, 32, ordinal));
    summaryOperands.push_back(integerMetadata(
        context, 32, region.edgeMultiplicity.lookup(destination)));
    summaryOperands.push_back(integerMetadata(
        context, 32, newPredecessors.find(destination)->second.size()));
  }
  MDNode *summary = MDNode::get(context, summaryOperands);
  rootCondition.setMetadata("symcc.ifss_switch_shared", summary);
  rootBranch.setMetadata("symcc.ifss_switch_shared", summary);

  for (unsigned ordinal = 0; ordinal < region.destinations.size();
       ++ordinal) {
    BasicBlock *destination = region.destinations[ordinal];
    auto found = newPredecessors.find(destination);
    assert(found != newPredecessors.end() &&
           "shared switch destination lost lowered edges");
    const unsigned originalEdges =
        region.edgeMultiplicity.lookup(destination);
    Metadata *operands[] = {
        MDString::get(context, kSharedPHISchema),
        integerMetadata(context, 64, switchSite),
        integerMetadata(context, 32, ordinal),
        integerMetadata(context, 32, originalEdges),
        integerMetadata(context, 32, found->second.size()),
    };
    MDNode *proof = MDNode::get(context, operands);
    for (PHINode &phi : destination->phis())
      phi.setMetadata("symcc.ifss_switch_phi", proof);
  }
}

RangeLoweringResult lowerLinearSwitchRegion(SwitchRegion &region) {
  SwitchInst *switchInstruction = region.instruction;
  Function *function = switchInstruction->getFunction();
  LLVMContext &context = switchInstruction->getContext();
  Value *condition = switchInstruction->getCondition();
  const uint64_t switchSite = stableSiteId(*switchInstruction);
  const unsigned arms = region.edgeCount;
  const StringRef schema = kLinearSwitchSchema;
  RangeLoweringResult result;
  result.switchSite = switchSite;

  SmallVector<BasicBlock *, kMaxSwitchArms> testBlocks;
  testBlocks.push_back(switchInstruction->getParent());
  for (unsigned index = 1; index < region.cases.size(); ++index)
    testBlocks.push_back(BasicBlock::Create(
        context, "ifss.switch.test", function,
        region.destinations.front()));

  DestinationPredecessors newPredecessors;
  for (unsigned index = 0; index < region.cases.size(); ++index) {
    BasicBlock *testBlock = testBlocks[index];
    IRBuilder<> builder(context);
    if (index == 0)
      builder.SetInsertPoint(switchInstruction);
    else
      builder.SetInsertPoint(testBlock);
    auto *matches = cast<ICmpInst>(builder.CreateICmpEQ(
        condition, region.cases[index].value, "ifss.switch.case"));
    BasicBlock *falseDestination =
        index + 1 < region.cases.size()
            ? testBlocks[index + 1]
            : region.defaultDestination;
    auto *branch = builder.CreateCondBr(
        matches, region.cases[index].destination, falseDestination);
    branch->setDebugLoc(switchInstruction->getDebugLoc());
    matches->setDebugLoc(switchInstruction->getDebugLoc());
    attachSwitchMetadata(
        *matches, schema, switchSite, region.cases[index].ordinal,
        "case", region.cases[index].value, arms);
    attachSwitchMetadata(
        *branch, schema, switchSite, region.cases[index].ordinal,
        "case", region.cases[index].value, arms);
    if (index == 0) {
      result.rootCondition = matches;
      result.rootBranch = branch;
    }
    newPredecessors[region.cases[index].destination].push_back(testBlock);
  }
  newPredecessors[region.defaultDestination].push_back(testBlocks.back());

  rewriteDestinationPHIs(region, newPredecessors);
  assert(result.rootCondition != nullptr && result.rootBranch != nullptr &&
         "switch linear lowering did not record its root");
  attachSharedDestinationProof(
      region, newPredecessors, *result.rootCondition,
      *result.rootBranch, switchSite, region.edgeCount);
  switchInstruction->eraseFromParent();

  BasicBlock *defaultPredecessor = testBlocks.back();
  Instruction *defaultTerminator = defaultPredecessor->getTerminator();
  assert(defaultTerminator != nullptr &&
         "lowered switch default edge has no terminator");
  attachDefaultMetadata(
      *defaultTerminator, schema, switchSite, arms - 1, arms);
  return result;
}

RangeLoweringResult lowerRangeSwitchRegion(
    SwitchRegion &region, StringRef schema,
    const std::function<unsigned(unsigned, unsigned)> &chooseSplit) {
  SwitchInst *switchInstruction = region.instruction;
  Function *function = switchInstruction->getFunction();
  LLVMContext &context = switchInstruction->getContext();
  Value *condition = switchInstruction->getCondition();
  const uint64_t switchSite = stableSiteId(*switchInstruction);
  const unsigned arms = region.edgeCount;
  SmallVector<SwitchCase, kMaxSwitchArms> sortedCases =
      sortedSwitchCases(region);

  DestinationPredecessors newPredecessors;
  unsigned rangeOrdinal = 0;
  RangeLoweringResult result;
  result.switchSite = switchSite;
  std::function<void(unsigned, unsigned, BasicBlock *)> emit =
      [&](unsigned begin, unsigned end, BasicBlock *block) {
        IRBuilder<> builder(context);
        if (block == switchInstruction->getParent())
          builder.SetInsertPoint(switchInstruction);
        else
          builder.SetInsertPoint(block);

        const unsigned count = end - begin;
        if (count == 1) {
          const SwitchCase &switchCase = sortedCases[begin];
          auto *matches = cast<ICmpInst>(builder.CreateICmpEQ(
              condition, switchCase.value, "ifss.switch.case"));
          auto *branch = builder.CreateCondBr(
              matches, switchCase.destination,
              region.defaultDestination);
          matches->setDebugLoc(switchInstruction->getDebugLoc());
          branch->setDebugLoc(switchInstruction->getDebugLoc());
          attachSwitchMetadata(
              *matches, schema, switchSite, switchCase.ordinal,
              "case", switchCase.value, arms);
          attachSwitchMetadata(
              *branch, schema, switchSite, switchCase.ordinal,
              "case", switchCase.value, arms);
          attachDefaultMetadata(
              *branch, schema, switchSite, arms - 1, arms);
          if (block == switchInstruction->getParent()) {
            result.rootCondition = matches;
            result.rootBranch = branch;
          }
          newPredecessors[switchCase.destination].push_back(block);
          newPredecessors[region.defaultDestination].push_back(block);
          return;
        }

        const unsigned middle = chooseSplit(begin, end);
        assert(middle > begin && middle < end &&
               "switch range split must make progress");
        ConstantInt *upperBound = sortedCases[middle - 1].value;
        BasicBlock *lower = BasicBlock::Create(
            context, "ifss.switch.lower", function,
            region.defaultDestination);
        BasicBlock *upper = BasicBlock::Create(
            context, "ifss.switch.upper", function,
            region.defaultDestination);
        auto *inLowerRange = cast<ICmpInst>(builder.CreateICmpULE(
            condition, upperBound, "ifss.switch.range"));
        auto *branch = builder.CreateCondBr(inLowerRange, lower, upper);
        inLowerRange->setDebugLoc(switchInstruction->getDebugLoc());
        branch->setDebugLoc(switchInstruction->getDebugLoc());
        const unsigned ordinal = rangeOrdinal++;
        attachSwitchMetadata(
            *inLowerRange, schema, switchSite, ordinal,
            "unsigned-upper", upperBound, arms);
        attachSwitchMetadata(
            *branch, schema, switchSite, ordinal,
            "unsigned-upper", upperBound, arms);
        if (block == switchInstruction->getParent()) {
          result.rootCondition = inLowerRange;
          result.rootBranch = branch;
        }
        emit(begin, middle, lower);
        emit(middle, end, upper);
      };

  emit(0, sortedCases.size(), switchInstruction->getParent());
  rewriteDestinationPHIs(region, newPredecessors);
  assert(result.rootCondition != nullptr && result.rootBranch != nullptr &&
         "switch range lowering did not record its root");
  attachSharedDestinationProof(
      region, newPredecessors, *result.rootCondition,
      *result.rootBranch, switchSite,
      static_cast<unsigned>(2 * region.cases.size()));
  switchInstruction->eraseFromParent();
  return result;
}

void attachProfileProof(
    const RangeLoweringResult &root, const SwitchRegion &region,
    const SelectedSwitchProfile &profile) {
  LLVMContext &context = root.rootCondition->getContext();
  SmallVector<SwitchCase, kMaxSwitchArms> cases =
      sortedSwitchCases(region);
  SmallVector<Metadata *, 32> operands;
  operands.push_back(MDString::get(context, kProfileProofSchema));
  operands.push_back(integerMetadata(context, 64, root.switchSite));
  operands.push_back(MDString::get(context, profile.source));
  operands.push_back(integerMetadata(context, 64, profile.fingerprint));
  operands.push_back(integerMetadata(context, 64, profile.defaultWeight));
  operands.push_back(integerMetadata(context, 64, profile.objectiveCost));
  operands.push_back(integerMetadata(context, 32, cases.size()));
  for (unsigned index = 0; index < cases.size(); ++index) {
    operands.push_back(ConstantAsMetadata::get(cases[index].value));
    operands.push_back(
        integerMetadata(context, 64, profile.sortedCaseWeights[index]));
  }
  MDNode *proof = MDNode::get(context, operands);
  root.rootCondition->setMetadata("symcc.ifss_switch_profile", proof);
  root.rootBranch->setMetadata("symcc.ifss_switch_profile", proof);
}

void attachProfileFallback(
    const RangeLoweringResult &root, StringRef reason) {
  LLVMContext &context = root.rootCondition->getContext();
  Metadata *operands[] = {
      MDString::get(context, kProfileFallbackSchema),
      integerMetadata(context, 64, root.switchSite),
      MDString::get(context, reason),
  };
  MDNode *proof = MDNode::get(context, operands);
  root.rootCondition->setMetadata(
      "symcc.ifss_switch_profile_fallback", proof);
  root.rootBranch->setMetadata(
      "symcc.ifss_switch_profile_fallback", proof);
}

void lowerBalancedSwitchRegion(SwitchRegion &region) {
  lowerRangeSwitchRegion(
      region, kBalancedSwitchSchema,
      [](unsigned begin, unsigned end) {
        return begin + (end - begin) / 2;
      });
}

void lowerProfileSwitchRegion(
    SwitchRegion &region, const SelectedSwitchProfile &profile) {
  if (!profile.valid) {
    RangeLoweringResult root = lowerRangeSwitchRegion(
        region, kBalancedSwitchSchema,
        [](unsigned begin, unsigned end) {
          return begin + (end - begin) / 2;
        });
    attachProfileFallback(root, profile.fallbackReason);
    return;
  }

  RangeLoweringResult root = lowerRangeSwitchRegion(
      region, kProfileSwitchSchema,
      [&](unsigned begin, unsigned end) {
        return profile.split[begin][end];
      });
  attachProfileProof(root, region, profile);
}

} // namespace

bool lowerIFSSSwitches(Module &module) {
  if (!enabled(std::getenv("SYMCC_IFSS_SWITCH_STATE")))
    return false;

  std::vector<SwitchRegion> regions;
  for (Function &function : module)
    if (!function.isDeclaration())
      for (Instruction &instruction : instructions(function))
        if (auto *switchInstruction = dyn_cast<SwitchInst>(&instruction)) {
          SwitchRegion region;
          if (collectSwitchRegion(*switchInstruction, region))
            regions.push_back(std::move(region));
        }
  if (regions.empty())
    return false;

  initializeStableSiteIds(module);
  const SwitchLoweringMode mode = switchLoweringMode();
  std::vector<SelectedSwitchProfile> selectedProfiles;
  if (mode == SwitchLoweringMode::Profile) {
    const SwitchProfileDatabase profiles = readSwitchProfiles();
    selectedProfiles.reserve(regions.size());
    for (const SwitchRegion &region : regions)
      selectedProfiles.push_back(selectSwitchProfile(region, profiles));
  }

  SmallVector<std::string, 8> manifestRecords;
  const char *manifestPath =
      std::getenv("SYMCC_IFSS_SWITCH_MANIFEST_OUT");
  if (manifestPath != nullptr && *manifestPath != '\0') {
    manifestRecords.reserve(regions.size());
    for (size_t index = 0; index < regions.size(); ++index)
      manifestRecords.push_back(buildSwitchManifestLine(
          module, regions[index], mode,
          mode == SwitchLoweringMode::Profile
              ? &selectedProfiles[index]
              : nullptr));
  }

  for (size_t index = 0; index < regions.size(); ++index) {
    SwitchRegion &region = regions[index];
    if (mode == SwitchLoweringMode::Balanced)
      lowerBalancedSwitchRegion(region);
    else if (mode == SwitchLoweringMode::Profile)
      lowerProfileSwitchRegion(region, selectedProfiles[index]);
    else
      lowerLinearSwitchRegion(region);
  }
  writeSwitchManifest(
      manifestPath == nullptr ? StringRef() : StringRef(manifestPath),
      manifestRecords);
  return true;
}

char IFSSSwitchLoweringLegacyPass::ID = 0;

bool IFSSSwitchLoweringLegacyPass::runOnModule(Module &module) {
  return lowerIFSSSwitches(module);
}

#if LLVM_VERSION_MAJOR >= 13
PreservedAnalyses IFSSSwitchLoweringPass::run(
    Module &module, ModuleAnalysisManager &) {
  return lowerIFSSSwitches(module) ? PreservedAnalyses::none()
                                  : PreservedAnalyses::all();
}
#endif

} // namespace symcc
