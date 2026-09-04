#!/usr/bin/env python3
"""Verify bounded linear-arm Hydra alignment manifests."""

import argparse
import json
import math
import re
import sys
from pathlib import Path


MANIFEST_SCHEMA = "symcc-hydra-transform-v1"
REGION_SCHEMA = "bounded-multiblock-linear-hydra-v1"
UNEQUAL_REGION_SCHEMA = "bounded-unequal-linear-hydra-v2"
TREE_REGION_SCHEMA = "bounded-internal-tree-hydra-v3"
DAG_REGION_SCHEMA = "bounded-acyclic-sese-dag-hydra-v4"
SHARED_DAG_REGION_SCHEMA = "bounded-shared-predicate-sese-dag-hydra-v5"
CROSS_REGION_SHARED_DAG_SCHEMA = (
    "bounded-cross-region-shared-predicate-sese-dag-hydra-v6"
)
FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
UINT64_MASK = (1 << 64) - 1
LLVM_PHI_OPCODE = 55
LLVM_FREEZE_OPCODE = 67
PROFILE_SCHEMA_V1 = "symcc-hydra-profile-v1"
PROFILE_SCHEMA_V2 = "symcc-hydra-profile-v2"
LLVM_SEMANTICS_POLICY = "llvm-poison-undef-freeze-refinement-v1"
INACTIVE_OPERAND_POLICY = "path-guarded-safe-constants-v1"
FREEZE_POLICY = "one-dynamic-instance-per-alignment-slot-v1"
EXCEPTION_POLICY = "reject-non-branch-terminators-and-eh-pads-v1"
SEMANTICS_FIELDS = {
    "llvm_ir_semantics",
    "llvm_version",
    "llvm_major",
    "inactive_operand_policy",
    "freeze_policy",
    "exception_policy",
    "left_freeze_instructions",
    "right_freeze_instructions",
    "aligned_freeze_pairs",
    "extra_freeze_instructions",
}


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def integer(value, minimum, maximum, field):
    require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{field} must be an integer",
    )
    require(minimum <= value <= maximum, f"{field} is out of range")
    return value


def canonical_uint64(value, field, allow_empty=False):
    if allow_empty and value == "":
        return 0
    require(isinstance(value, str), f"{field} must be a string")
    require(
        value == "0" or (value and value[0] != "0" and value.isdigit()),
        f"{field} is not canonical unsigned decimal",
    )
    number = int(value)
    require(0 < number <= UINT64_MASK, f"{field} is out of range")
    return number


def sha256(value, field):
    require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{field} is not a canonical SHA-256 digest",
    )
    return value


def mix_byte(value, byte):
    return ((value ^ byte) * FNV_PRIME) & UINT64_MASK


def mix_text(value, text):
    for byte in text.encode("utf-8"):
        value = mix_byte(value, byte)
    return mix_byte(value, 0xFF)


def mix_integer(value, number):
    for shift in range(0, 64, 8):
        value = mix_byte(value, (number >> shift) & 0xFF)
    return value


def site_array(record, field, count):
    values = record.get(field)
    require(
        isinstance(values, list) and len(values) == count,
        f"{field} length mismatch",
    )
    parsed = [
        canonical_uint64(value, f"{field}[{index}]")
        for index, value in enumerate(values)
    ]
    require(len(set(parsed)) == len(parsed), f"{field} has duplicates")
    return parsed


def site_sequence(record, field, count):
    values = record.get(field)
    require(
        isinstance(values, list) and len(values) == count,
        f"{field} length mismatch",
    )
    return [
        canonical_uint64(value, f"{field}[{index}]")
        for index, value in enumerate(values)
    ]


def tree_topology(
    record,
    side,
    block_count,
    block_sites,
    claimed_branches,
    claimed_leaves,
):
    field = f"{side}_topology"
    topology = record.get(field)
    require(
        isinstance(topology, list) and len(topology) == block_count,
        f"{field} length mismatch",
    )
    parsed = []
    inbound = [0] * block_count
    parent_claims = [None] * block_count
    merge_edges = []
    terminator_sites = set()
    branch_count = 0
    for ordinal, item in enumerate(topology):
        require(isinstance(item, dict), f"{field} block must be an object")
        require(item.get("ordinal") == ordinal, f"{field} ordinal mismatch")
        require(
            canonical_uint64(item.get("block_site"), "block_site")
            == block_sites[ordinal],
            f"{field} block site mismatch",
        )
        terminator_site = canonical_uint64(
            item.get("terminator_site"), "terminator_site"
        )
        require(
            terminator_site not in terminator_sites,
            f"{field} has duplicate terminator sites",
        )
        terminator_sites.add(terminator_site)
        parent = integer(
            item.get("parent_ordinal"), -1, block_count - 1, "parent_ordinal"
        )
        incoming = integer(item.get("incoming_edge"), -1, 1, "incoming_edge")
        if ordinal == 0:
            require(
                parent == -1 and incoming == -1,
                f"{field} root parent mismatch",
            )
        else:
            require(
                0 <= parent < ordinal and incoming in (0, 1),
                f"{field} is not preorder",
            )
        parent_claims[ordinal] = (parent, incoming)
        successors = item.get("successors")
        require(
            isinstance(successors, list) and len(successors) in (1, 2),
            f"{field} successor count mismatch",
        )
        kind = item.get("terminator_kind")
        expected_kind = "conditional" if len(successors) == 2 else "unconditional"
        require(kind == expected_kind, f"{field} terminator kind mismatch")
        if len(successors) == 2:
            branch_count += 1
        parsed_successors = []
        for successor_index, edge in enumerate(successors):
            require(isinstance(edge, dict), f"{field} edge must be an object")
            edge_kind = edge.get("kind")
            if edge_kind == "merge":
                require(
                    set(edge) == {"kind"},
                    f"{field} merge edge has extra fields",
                )
                parsed_successors.append(-1)
                merge_edges.append((ordinal, successor_index))
            else:
                require(edge_kind == "block", f"{field} edge kind mismatch")
                require(
                    set(edge) == {"kind", "ordinal"},
                    f"{field} block edge fields mismatch",
                )
                child = integer(
                    edge.get("ordinal"), ordinal + 1, block_count - 1,
                    "successor ordinal",
                )
                parsed_successors.append(child)
                inbound[child] += 1
                require(
                    parent_claims[child] is None
                    or parent_claims[child] == (ordinal, successor_index),
                    f"{field} child parent mismatch",
                )
        parsed.append(
            (
                block_sites[ordinal],
                terminator_site,
                parent + 1,
                incoming + 1,
                tuple(value + 1 for value in parsed_successors),
            )
        )
    require(inbound[0] == 0, f"{field} root has an incoming edge")
    for ordinal in range(1, block_count):
        require(inbound[ordinal] == 1, f"{field} block is not uniquely reached")
        parent, incoming = parent_claims[ordinal]
        require(
            parsed[parent][4][incoming] == ordinal + 1,
            f"{field} parent edge does not name child",
        )
    require(branch_count == claimed_branches, f"{field} branch count mismatch")
    leaves = record.get(f"{side}_leaves")
    require(
        isinstance(leaves, list) and len(leaves) == claimed_leaves,
        f"{side}_leaves length mismatch",
    )
    parsed_leaves = []
    for item in leaves:
        require(isinstance(item, dict), f"{side} leaf must be an object")
        block = integer(
            item.get("block_ordinal"), 0, block_count - 1, "block_ordinal"
        )
        successor = integer(
            item.get("successor_index"), 0, 1, "successor_index"
        )
        require(
            successor < len(topology[block]["successors"]),
            f"{side} leaf successor is out of range",
        )
        parsed_leaves.append((block, successor))
    require(
        len({block for block, _ in parsed_leaves}) == len(parsed_leaves),
        f"{side} has duplicate leaf blocks",
    )
    require(parsed_leaves == merge_edges, f"{side} leaf edge set mismatch")
    return parsed, parsed_leaves


def dag_topology(
    record,
    side,
    block_count,
    block_sites,
    claimed_branches,
    claimed_leaves,
    claimed_local_merges,
):
    field = f"{side}_topology"
    topology = record.get(field)
    require(
        isinstance(topology, list) and len(topology) == block_count,
        f"{field} length mismatch",
    )
    inbound = [[] for _ in range(block_count)]
    successor_rows = []
    merge_edges = []
    terminator_sites = set()
    branch_count = 0
    for ordinal, item in enumerate(topology):
        require(isinstance(item, dict), f"{field} block must be an object")
        require(item.get("ordinal") == ordinal, f"{field} ordinal mismatch")
        require(
            canonical_uint64(item.get("block_site"), "block_site")
            == block_sites[ordinal],
            f"{field} block site mismatch",
        )
        terminator_site = canonical_uint64(
            item.get("terminator_site"), "terminator_site"
        )
        require(
            terminator_site not in terminator_sites,
            f"{field} has duplicate terminator sites",
        )
        terminator_sites.add(terminator_site)
        successors = item.get("successors")
        require(
            isinstance(successors, list) and len(successors) in (1, 2),
            f"{field} successor count mismatch",
        )
        expected_kind = "conditional" if len(successors) == 2 else "unconditional"
        require(
            item.get("terminator_kind") == expected_kind,
            f"{field} terminator kind mismatch",
        )
        if len(successors) == 2:
            branch_count += 1
        parsed_successors = []
        for successor_index, edge in enumerate(successors):
            require(isinstance(edge, dict), f"{field} edge must be an object")
            if edge.get("kind") == "merge":
                require(
                    set(edge) == {"kind"},
                    f"{field} merge edge has extra fields",
                )
                parsed_successors.append(-1)
                merge_edges.append((ordinal, successor_index))
                continue
            require(
                edge.get("kind") == "block",
                f"{field} edge kind mismatch",
            )
            require(
                set(edge) == {"kind", "ordinal"},
                f"{field} block edge fields mismatch",
            )
            child = integer(
                edge.get("ordinal"),
                ordinal + 1,
                block_count - 1,
                "successor ordinal",
            )
            parsed_successors.append(child)
            inbound[child].append((ordinal, successor_index))
        successor_rows.append((terminator_site, tuple(parsed_successors)))
    require(branch_count == claimed_branches, f"{field} branch count mismatch")

    parsed = []
    local_merges = 0
    for ordinal, item in enumerate(topology):
        predecessors = item.get("predecessors")
        require(
            isinstance(predecessors, list),
            f"{field} predecessors must be a list",
        )
        parsed_predecessors = []
        for edge in predecessors:
            require(
                isinstance(edge, dict)
                and set(edge) == {"block_ordinal", "successor_index"},
                f"{field} predecessor fields mismatch",
            )
            parent = integer(
                edge.get("block_ordinal"),
                0,
                max(0, ordinal - 1),
                "predecessor block ordinal",
            )
            successor_index = integer(
                edge.get("successor_index"),
                0,
                1,
                "predecessor successor index",
            )
            parsed_predecessors.append((parent, successor_index))
        require(
            parsed_predecessors == sorted(set(parsed_predecessors)),
            f"{field} predecessors are not canonical",
        )
        require(
            parsed_predecessors == sorted(inbound[ordinal]),
            f"{field} predecessor set mismatch",
        )
        if ordinal == 0:
            require(not parsed_predecessors, f"{field} root has predecessors")
        else:
            require(parsed_predecessors, f"{field} block is unreachable")
        if len(parsed_predecessors) > 1:
            local_merges += 1
        terminator_site, successors = successor_rows[ordinal]
        parsed.append(
            (
                block_sites[ordinal],
                terminator_site,
                tuple(
                    (parent + 1, successor_index)
                    for parent, successor_index in parsed_predecessors
                ),
                tuple(successor + 1 for successor in successors),
            )
        )
    require(
        local_merges == claimed_local_merges,
        f"{field} local merge count mismatch",
    )
    leaves = record.get(f"{side}_leaves")
    require(
        isinstance(leaves, list) and len(leaves) == claimed_leaves,
        f"{side}_leaves length mismatch",
    )
    parsed_leaves = []
    for item in leaves:
        require(isinstance(item, dict), f"{side} leaf must be an object")
        block = integer(
            item.get("block_ordinal"), 0, block_count - 1, "block_ordinal"
        )
        successor = integer(
            item.get("successor_index"), 0, 1, "successor_index"
        )
        require(
            successor < len(topology[block]["successors"]),
            f"{side} leaf successor is out of range",
        )
        parsed_leaves.append((block, successor))
    require(
        len({block for block, _ in parsed_leaves}) == len(parsed_leaves),
        f"{side} has duplicate leaf blocks",
    )
    require(parsed_leaves == merge_edges, f"{side} leaf edge set mismatch")
    return parsed, parsed_leaves


def verify_record(record):
    require(isinstance(record, dict), "record must be an object")
    require(record.get("schema") == MANIFEST_SCHEMA, "unknown schema")
    region_schema = record.get("region_schema")
    require(
        region_schema
        in (
            REGION_SCHEMA,
            UNEQUAL_REGION_SCHEMA,
            TREE_REGION_SCHEMA,
            DAG_REGION_SCHEMA,
            SHARED_DAG_REGION_SCHEMA,
            CROSS_REGION_SHARED_DAG_SCHEMA,
        ),
        "unknown region",
    )
    unequal = region_schema == UNEQUAL_REGION_SCHEMA
    tree = region_schema == TREE_REGION_SCHEMA
    cross_region = region_schema == CROSS_REGION_SHARED_DAG_SCHEMA
    shared_dag = region_schema in (
        SHARED_DAG_REGION_SCHEMA,
        CROSS_REGION_SHARED_DAG_SCHEMA,
    )
    dag = region_schema in (
        DAG_REGION_SCHEMA,
        SHARED_DAG_REGION_SCHEMA,
        CROSS_REGION_SHARED_DAG_SCHEMA,
    )
    site = integer(record.get("site"), 1, UINT64_MASK, "site")
    if tree or dag:
        max_blocks = 14 if shared_dag else (10 if dag else 7)
        max_branches = 5 if shared_dag else (4 if dag else 3)
        max_leaves = 10 if shared_dag else (8 if dag else 4)
        left_arm_blocks = integer(
            record.get("left_arm_blocks"), 1, max_blocks, "left_arm_blocks"
        )
        right_arm_blocks = integer(
            record.get("right_arm_blocks"), 1, max_blocks, "right_arm_blocks"
        )
        require(
            record.get("internal_tree") is tree,
            "tree classification mismatch",
        )
        if dag:
            require(
                record.get("internal_dag") is True,
                "DAG classification mismatch",
            )
            require(
                record.get("shared_predicate_dag") is True
                if shared_dag
                else record.get("shared_predicate_dag") is not True,
                "shared-DAG classification mismatch",
            )
        require(
            record.get("unequal_arm_blocks")
            is (left_arm_blocks != right_arm_blocks),
            "unequal-arm classification mismatch",
        )
        require(
            record.get("alignment_algorithm")
            == (
                "compatible-lcs-cross-region-shared-dag-topological-left-tie-v6"
                if cross_region
                else "compatible-lcs-shared-dag-topological-left-tie-v5"
                if shared_dag
                else "compatible-lcs-dag-topological-left-tie-v4"
                if dag
                else "compatible-lcs-tree-preorder-left-tie-v3"
            ),
            "unknown alignment algorithm",
        )
        left_internal_branches = integer(
            record.get("left_internal_branches"),
            0,
            max_branches,
            "left_internal_branches",
        )
        right_internal_branches = integer(
            record.get("right_internal_branches"),
            0,
            max_branches,
            "right_internal_branches",
        )
        require(
            left_internal_branches + right_internal_branches > 0,
            "region has no internal branch",
        )
        left_leaf_edges = integer(
            record.get("left_leaf_edges"), 1, max_leaves, "left_leaf_edges"
        )
        right_leaf_edges = integer(
            record.get("right_leaf_edges"), 1, max_leaves, "right_leaf_edges"
        )
        if dag:
            left_local_merges = integer(
                record.get("left_local_merges"),
                0,
                4 if shared_dag else 3,
                "left_local_merges",
            )
            right_local_merges = integer(
                record.get("right_local_merges"),
                0,
                4 if shared_dag else 3,
                "right_local_merges",
            )
            require(
                left_local_merges + right_local_merges > 0,
                "DAG has no local merge",
            )
            left_local_phi_count = integer(
                record.get("left_local_phis"),
                0,
                12 if shared_dag else 8,
                "left_local_phis",
            )
            right_local_phi_count = integer(
                record.get("right_local_phis"),
                0,
                12 if shared_dag else 8,
                "right_local_phis",
            )
    elif unequal:
        left_arm_blocks = integer(
            record.get("left_arm_blocks"), 1, 4, "left_arm_blocks"
        )
        right_arm_blocks = integer(
            record.get("right_arm_blocks"), 1, 4, "right_arm_blocks"
        )
        require(
            left_arm_blocks != right_arm_blocks,
            "unequal schema has equal arm lengths",
        )
        require(
            record.get("unequal_arm_blocks") is True,
            "unequal-arm classification mismatch",
        )
        require(
            record.get("alignment_algorithm")
            == "compatible-lcs-left-tie-v2",
            "unknown alignment algorithm",
        )
    else:
        left_arm_blocks = right_arm_blocks = integer(
            record.get("arm_blocks"), 1, 4, "arm_blocks"
        )
    require(
        record.get("multi_block")
        is (max(left_arm_blocks, right_arm_blocks) > 1),
        "multi_block classification mismatch",
    )
    require(
        bool(record.get("single_site_build")) is (not cross_region),
        "single/multi-site build classification mismatch",
    )
    require(
        bool(record.get("requires_original_coverage_replay")),
        "original coverage replay is not required",
    )
    mode = record.get("mode")
    require(mode in ("safe-alu", "aggressive-memory"), "unknown mode")
    require(
        record.get("requires_original_replay")
        is (mode == "aggressive-memory"),
        "failure replay classification mismatch",
    )
    present_semantics = SEMANTICS_FIELDS.intersection(record)
    require(
        not present_semantics or present_semantics == SEMANTICS_FIELDS,
        "incomplete LLVM semantics contract",
    )
    semantic_contract = bool(present_semantics)
    if semantic_contract:
        require(
            record.get("llvm_ir_semantics") == LLVM_SEMANTICS_POLICY,
            "unknown LLVM semantics policy",
        )
        require(
            record.get("inactive_operand_policy")
            == INACTIVE_OPERAND_POLICY,
            "unknown inactive-operand policy",
        )
        require(
            record.get("freeze_policy") == FREEZE_POLICY,
            "unknown freeze policy",
        )
        require(
            record.get("exception_policy") == EXCEPTION_POLICY,
            "unknown exception policy",
        )
        version = record.get("llvm_version")
        require(
            isinstance(version, str)
            and 0 < len(version) <= 128
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[-+._A-Za-z0-9]*)?", version),
            "LLVM version is not canonical",
        )
        llvm_major = integer(
            record.get("llvm_major"), 8, 18, "llvm_major"
        )
        require(
            int(version.split(".", 1)[0]) == llvm_major,
            "LLVM major/version mismatch",
        )
    score = record.get("profile_score")
    require(
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(float(score)),
        "invalid profile score",
    )
    selection_source = record.get("selection_source")
    if selection_source is not None:
        require(
            selection_source
            in (
                "implicit",
                "explicit-site",
                "explicit-sites",
                "profile-v1",
                "profile-v2",
            ),
            "unknown selection source",
        )
        if selection_source == "profile-v2":
            require(
                record.get("profile_schema") == PROFILE_SCHEMA_V2,
                "v2 profile schema mismatch",
            )
            sha256(record.get("profile_sha256"), "profile_sha256")
            sha256(
                record.get("profiled_executable_sha256"),
                "profiled_executable_sha256",
            )
            sha256(
                record.get("profiled_command_sha256"),
                "profiled_command_sha256",
            )
        elif selection_source == "profile-v1":
            require(
                record.get("profile_schema") == PROFILE_SCHEMA_V1,
                "v1 profile schema mismatch",
            )
            require(
                all(
                    field not in record
                    for field in (
                        "profile_sha256",
                        "profiled_executable_sha256",
                        "profiled_command_sha256",
                    )
                ),
                "v1 profile unexpectedly claims v2 provenance",
            )
        else:
            require(
                all(
                    field not in record
                    for field in (
                        "profile_schema",
                        "profile_sha256",
                        "profiled_executable_sha256",
                        "profiled_command_sha256",
                    )
                ),
                "non-profile selection claims profile provenance",
            )

    left_blocks = site_array(
        record, "left_block_sites", left_arm_blocks
    )
    right_blocks = site_array(
        record, "right_block_sites", right_arm_blocks
    )
    require(
        set(left_blocks).isdisjoint(right_blocks),
        "arm block sites overlap",
    )
    alignment = record.get("alignment")
    require(isinstance(alignment, list) and alignment, "empty alignment")
    parsed_alignment = []
    left_instruction_sites = set()
    right_instruction_sites = set()
    left_phi_instruction_sites = []
    right_phi_instruction_sites = []
    aligned_pairs = 0
    edit_distance = 0
    left_freezes = 0
    right_freezes = 0
    aligned_freeze_pairs = 0
    extra_freezes = 0
    previous_left_block = -1
    previous_right_block = -1
    for ordinal, item in enumerate(alignment):
        require(isinstance(item, dict), "alignment slot must be an object")
        require(item.get("ordinal") == ordinal, "alignment ordinal mismatch")
        left_site = canonical_uint64(
            item.get("left_site"), "left_site", allow_empty=True
        )
        right_site = canonical_uint64(
            item.get("right_site"), "right_site", allow_empty=True
        )
        left_opcode = integer(
            item.get("left_opcode"), 0, 1024, "left_opcode"
        )
        right_opcode = integer(
            item.get("right_opcode"), 0, 1024, "right_opcode"
        )
        require(left_site or right_site, "alignment slot is empty")
        require(
            bool(left_site) == bool(left_opcode),
            "left site/opcode presence mismatch",
        )
        require(
            bool(right_site) == bool(right_opcode),
            "right site/opcode presence mismatch",
        )
        if left_site:
            require(
                left_site not in left_instruction_sites,
                "left instruction appears twice",
            )
            left_instruction_sites.add(left_site)
            if left_opcode == LLVM_PHI_OPCODE:
                left_phi_instruction_sites.append(left_site)
            if left_opcode == LLVM_FREEZE_OPCODE:
                left_freezes += 1
        if right_site:
            require(
                right_site not in right_instruction_sites,
                "right instruction appears twice",
            )
            right_instruction_sites.add(right_site)
            if right_opcode == LLVM_PHI_OPCODE:
                right_phi_instruction_sites.append(right_site)
            if right_opcode == LLVM_FREEZE_OPCODE:
                right_freezes += 1
        if left_site and right_site:
            aligned_pairs += 1
            require(
                left_opcode == right_opcode,
                "aligned opcodes differ",
            )
            if left_opcode == LLVM_FREEZE_OPCODE:
                aligned_freeze_pairs += 1
        else:
            edit_distance += 1
            if left_opcode == LLVM_FREEZE_OPCODE or right_opcode == LLVM_FREEZE_OPCODE:
                extra_freezes += 1
        if unequal or tree or dag:
            left_block = integer(
                item.get("left_block_ordinal"),
                -1,
                left_arm_blocks - 1,
                "left_block_ordinal",
            )
            right_block = integer(
                item.get("right_block_ordinal"),
                -1,
                right_arm_blocks - 1,
                "right_block_ordinal",
            )
            require(
                bool(left_site) is (left_block >= 0),
                "left site/block presence mismatch",
            )
            require(
                bool(right_site) is (right_block >= 0),
                "right site/block presence mismatch",
            )
            if left_block >= 0:
                require(
                    left_block >= previous_left_block,
                    "left block order regresses",
                )
                previous_left_block = left_block
            if right_block >= 0:
                require(
                    right_block >= previous_right_block,
                    "right block order regresses",
                )
                previous_right_block = right_block
            parsed_alignment.append(
                (
                    left_site,
                    right_site,
                    left_opcode,
                    right_opcode,
                    left_block + 1,
                    right_block + 1,
                )
            )
        else:
            parsed_alignment.append(
                (left_site, right_site, left_opcode, right_opcode)
            )
    require(
        record.get("aligned_pairs") == aligned_pairs,
        "aligned pair count mismatch",
    )
    if semantic_contract:
        require(
            integer(
                record.get("left_freeze_instructions"),
                0,
                96 if shared_dag else 64,
                "left_freeze_instructions",
            )
            == left_freezes,
            "left freeze count mismatch",
        )
        require(
            integer(
                record.get("right_freeze_instructions"),
                0,
                96 if shared_dag else 64,
                "right_freeze_instructions",
            )
            == right_freezes,
            "right freeze count mismatch",
        )
        require(
            integer(
                record.get("aligned_freeze_pairs"),
                0,
                96 if shared_dag else 64,
                "aligned_freeze_pairs",
            )
            == aligned_freeze_pairs,
            "aligned freeze count mismatch",
        )
        require(
            integer(
                record.get("extra_freeze_instructions"),
                0,
                96 if shared_dag else 64,
                "extra_freeze_instructions",
            )
            == extra_freezes,
            "extra freeze count mismatch",
        )
    require(
        left_instruction_sites.isdisjoint(right_instruction_sites),
        "arm instruction sites overlap",
    )
    if unequal or tree or dag:
        require(
            integer(
                record.get("left_instruction_count"),
                0,
                96 if shared_dag else 64,
                "left_instruction_count",
            )
            == len(left_instruction_sites),
            "left instruction count mismatch",
        )
        require(
            integer(
                record.get("right_instruction_count"),
                0,
                96 if shared_dag else 64,
                "right_instruction_count",
            )
            == len(right_instruction_sites),
            "right instruction count mismatch",
        )
        require(
            integer(
                record.get("edit_distance"),
                0,
                192 if shared_dag else 128,
                "edit_distance",
            )
            == edit_distance,
            "edit distance mismatch",
        )

    output_count = integer(
        record.get("output_phis"), 0, 8, "output_phis"
    )
    output_sites = site_array(record, "output_phi_sites", output_count)
    transaction_fingerprint = 0
    transaction_ordinal = 0
    transaction_size = 0
    transaction_sites = []
    transaction_shared_predicate_sites = []
    cross_region_reused_predicate_negations = 0
    cross_region_source_sites = []
    transaction_function = None
    if cross_region:
        transaction_function = record.get("function")
        require(
            isinstance(transaction_function, str)
            and 0 < len(transaction_function) <= 1024
            and all(
                ord(character) >= 0x20
                for character in transaction_function
            ),
            "transaction function is not canonical",
        )
        require(
            record.get("multi_site_transaction") is True,
            "v6 record is not a multi-site transaction",
        )
        transaction_size = integer(
            record.get("transaction_size"),
            2,
            4,
            "transaction_size",
        )
        transaction_ordinal = integer(
            record.get("transaction_ordinal"),
            0,
            transaction_size - 1,
            "transaction_ordinal",
        )
        transaction_sites = site_sequence(
            record, "transaction_sites", transaction_size
        )
        require(
            len(set(transaction_sites)) == transaction_size,
            "transaction sites are not unique",
        )
        require(
            transaction_sites[transaction_ordinal] == site,
            "transaction ordinal/site mismatch",
        )
        raw_shared = record.get(
            "transaction_shared_predicate_sites"
        )
        require(
            isinstance(raw_shared, list)
            and 1 <= len(raw_shared) <= 10,
            "transaction shared predicate count is out of range",
        )
        transaction_shared_predicate_sites = site_sequence(
            record,
            "transaction_shared_predicate_sites",
            len(raw_shared),
        )
        require(
            transaction_shared_predicate_sites
            == sorted(set(transaction_shared_predicate_sites)),
            "transaction shared predicates are not canonical",
        )
        cross_region_reused_predicate_negations = integer(
            record.get(
                "cross_region_reused_predicate_negations"
            ),
            0,
            4096,
            "cross_region_reused_predicate_negations",
        )
        raw_sources = record.get("cross_region_source_sites")
        require(
            isinstance(raw_sources, list)
            and len(raw_sources) <= transaction_ordinal,
            "cross-region source count is out of range",
        )
        cross_region_source_sites = site_sequence(
            record,
            "cross_region_source_sites",
            len(raw_sources),
        )
        require(
            len(set(cross_region_source_sites))
            == len(cross_region_source_sites),
            "cross-region sources are not unique",
        )
        preceding = set(transaction_sites[:transaction_ordinal])
        require(
            set(cross_region_source_sites) <= preceding,
            "cross-region source is not a preceding transaction site",
        )
        if transaction_ordinal == 0:
            require(
                cross_region_reused_predicate_negations == 0
                and not cross_region_source_sites,
                "first transaction record claims cross-region reuse",
            )
        else:
            require(
                cross_region_reused_predicate_negations > 0
                and cross_region_source_sites
                and cross_region_reused_predicate_negations
                >= len(cross_region_source_sites),
                "later transaction record has no proven reuse",
            )
        transaction_fingerprint = FNV_OFFSET
        transaction_fingerprint = mix_text(
            transaction_fingerprint,
            CROSS_REGION_SHARED_DAG_SCHEMA,
        )
        transaction_fingerprint = mix_integer(
            transaction_fingerprint, transaction_size
        )
        for transaction_site in transaction_sites:
            transaction_fingerprint = mix_integer(
                transaction_fingerprint, transaction_site
            )
        transaction_fingerprint = mix_integer(
            transaction_fingerprint,
            len(transaction_shared_predicate_sites),
        )
        for predicate_site in transaction_shared_predicate_sites:
            transaction_fingerprint = mix_integer(
                transaction_fingerprint, predicate_site
            )
        require(
            canonical_uint64(
                record.get("transaction_fingerprint"),
                "transaction_fingerprint",
            )
            == transaction_fingerprint,
            "transaction fingerprint mismatch",
        )
    else:
        require(
            all(
                field not in record
                for field in (
                    "multi_site_transaction",
                    "transaction_fingerprint",
                    "transaction_ordinal",
                    "transaction_size",
                    "transaction_sites",
                    "transaction_shared_predicate_sites",
                    "cross_region_reused_predicate_negations",
                    "cross_region_source_sites",
                )
            ),
            "legacy record contains v6 transaction fields",
        )
    fingerprint = FNV_OFFSET
    fingerprint = mix_text(fingerprint, region_schema)
    fingerprint = mix_integer(fingerprint, site)
    if cross_region:
        fingerprint = mix_integer(
            fingerprint, transaction_fingerprint
        )
        fingerprint = mix_integer(
            fingerprint, transaction_ordinal
        )
        fingerprint = mix_integer(fingerprint, transaction_size)
        fingerprint = mix_integer(
            fingerprint, len(transaction_sites)
        )
        for transaction_site in transaction_sites:
            fingerprint = mix_integer(
                fingerprint, transaction_site
            )
        fingerprint = mix_integer(
            fingerprint,
            len(transaction_shared_predicate_sites),
        )
        for predicate_site in transaction_shared_predicate_sites:
            fingerprint = mix_integer(
                fingerprint, predicate_site
            )
    topology_proof = None
    left_local_phi_sites = []
    right_local_phi_sites = []
    left_predicate_sites = []
    right_predicate_sites = []
    canonical_guard_edges = 0
    unique_predicates = 0
    reused_predicate_occurrences = 0
    reused_guard_edges = 0
    reused_predicate_negations = 0
    if tree or dag:
        topology_parser = dag_topology if dag else tree_topology
        left_arguments = (
            (
                record,
                "left",
                left_arm_blocks,
                left_blocks,
                left_internal_branches,
                left_leaf_edges,
                left_local_merges,
            )
            if dag
            else (
                record,
                "left",
                left_arm_blocks,
                left_blocks,
                left_internal_branches,
                left_leaf_edges,
            )
        )
        right_arguments = (
            (
                record,
                "right",
                right_arm_blocks,
                right_blocks,
                right_internal_branches,
                right_leaf_edges,
                right_local_merges,
            )
            if dag
            else (
                record,
                "right",
                right_arm_blocks,
                right_blocks,
                right_internal_branches,
                right_leaf_edges,
            )
        )
        left_topology, left_leaves = topology_parser(*left_arguments)
        right_topology, right_leaves = topology_parser(*right_arguments)
        topology_proof = (
            left_topology,
            right_topology,
            left_leaves,
            right_leaves,
        )
        if dag:
            left_local_phi_sites = site_array(
                record, "left_local_phi_sites", left_local_phi_count
            )
            right_local_phi_sites = site_array(
                record, "right_local_phi_sites", right_local_phi_count
            )
            require(
                left_local_phi_sites == left_phi_instruction_sites,
                "left local PHI sites do not match PHI instructions",
            )
            require(
                right_local_phi_sites == right_phi_instruction_sites,
                "right local PHI sites do not match PHI instructions",
            )
            if shared_dag:
                require(
                    record.get("guard_reuse_policy")
                    == (
                        "per-arm-edge-and-function-dominating-negation-hash-cons-v2"
                        if cross_region
                        else "per-arm-edge-and-global-negation-hash-cons-v1"
                    ),
                    "unknown shared-DAG guard reuse policy",
                )
                canonical_guard_edges = sum(
                    len(block[3])
                    for topology in (left_topology, right_topology)
                    for block in topology
                )
                require(
                    integer(
                        record.get("canonical_guard_edges"),
                        1,
                        48,
                        "canonical_guard_edges",
                    )
                    == canonical_guard_edges,
                    "canonical guard edge count mismatch",
                )
                reused_guard_edges = integer(
                    record.get("reused_guard_edges"),
                    0,
                    4096,
                    "reused_guard_edges",
                )
                left_predicate_sites = site_sequence(
                    record,
                    "left_predicate_sites",
                    left_internal_branches,
                )
                right_predicate_sites = site_sequence(
                    record,
                    "right_predicate_sites",
                    right_internal_branches,
                )
                all_predicates = (
                    left_predicate_sites + right_predicate_sites
                )
                unique_predicates = len(set(all_predicates))
                reused_predicate_occurrences = (
                    len(all_predicates) - unique_predicates
                )
                require(
                    integer(
                        record.get("unique_predicates"),
                        1,
                        left_internal_branches + right_internal_branches,
                        "unique_predicates",
                    )
                    == unique_predicates,
                    "unique predicate count mismatch",
                )
                require(
                    integer(
                        record.get("reused_predicate_occurrences"),
                        0,
                        left_internal_branches + right_internal_branches,
                        "reused_predicate_occurrences",
                    )
                    == reused_predicate_occurrences,
                    "reused predicate occurrence count mismatch",
                )
                reused_predicate_negations = integer(
                    record.get("reused_predicate_negations"),
                    0,
                    4096,
                    "reused_predicate_negations",
                )
                if cross_region:
                    require(
                        set(transaction_shared_predicate_sites)
                        <= set(all_predicates),
                        "transaction predicate is absent from region",
                    )
                    require(
                        cross_region_reused_predicate_negations
                        <= reused_predicate_negations,
                        "cross-region reuse exceeds total reuse",
                    )
                require(
                    any((
                        left_arm_blocks > 10,
                        right_arm_blocks > 10,
                        left_internal_branches > 4,
                        right_internal_branches > 4,
                        left_leaf_edges > 8,
                        right_leaf_edges > 8,
                        left_local_merges > 3,
                        right_local_merges > 3,
                        left_local_phi_count > 8,
                        right_local_phi_count > 8,
                        len(left_instruction_sites) > 64,
                        len(right_instruction_sites) > 64,
                    )),
                    "shared-DAG schema does not exceed the v4 proof domain",
                )
        proof_identities = [site, *left_blocks, *right_blocks]
        proof_identities.extend(block[1] for block in left_topology)
        proof_identities.extend(block[1] for block in right_topology)
        proof_identities.extend(left_instruction_sites)
        proof_identities.extend(right_instruction_sites)
        proof_identities.extend(output_sites)
        require(
            len(set(proof_identities)) == len(proof_identities),
            "region proof identities overlap",
        )
    if unequal or tree or dag:
        fingerprint = mix_integer(fingerprint, left_arm_blocks)
        fingerprint = mix_integer(fingerprint, right_arm_blocks)
        fingerprint = mix_integer(
            fingerprint, len(left_instruction_sites)
        )
        fingerprint = mix_integer(
            fingerprint, len(right_instruction_sites)
        )
        fingerprint = mix_integer(fingerprint, edit_distance)
        if tree or dag:
            fingerprint = mix_integer(fingerprint, left_internal_branches)
            fingerprint = mix_integer(fingerprint, right_internal_branches)
            fingerprint = mix_integer(fingerprint, left_leaf_edges)
            fingerprint = mix_integer(fingerprint, right_leaf_edges)
            if dag:
                fingerprint = mix_integer(fingerprint, left_local_merges)
                fingerprint = mix_integer(fingerprint, right_local_merges)
                fingerprint = mix_integer(
                    fingerprint, len(left_local_phi_sites)
                )
                fingerprint = mix_integer(
                    fingerprint, len(right_local_phi_sites)
                )
                if shared_dag:
                    fingerprint = mix_integer(
                        fingerprint, canonical_guard_edges
                    )
                    fingerprint = mix_integer(
                        fingerprint, len(left_predicate_sites)
                    )
                    fingerprint = mix_integer(
                        fingerprint, len(right_predicate_sites)
                    )
                    fingerprint = mix_integer(
                        fingerprint, unique_predicates
                    )
                    fingerprint = mix_integer(
                        fingerprint, reused_predicate_occurrences
                    )
    else:
        fingerprint = mix_integer(fingerprint, left_arm_blocks)
    for block_site in left_blocks:
        fingerprint = mix_integer(fingerprint, block_site)
    for block_site in right_blocks:
        fingerprint = mix_integer(fingerprint, block_site)
    if tree or dag:
        for topology in topology_proof[:2]:
            for block in topology:
                fingerprint = mix_integer(fingerprint, block[0])
                fingerprint = mix_integer(fingerprint, block[1])
                if dag:
                    predecessors = block[2]
                    fingerprint = mix_integer(
                        fingerprint, len(predecessors)
                    )
                    for parent, successor_index in predecessors:
                        fingerprint = mix_integer(fingerprint, parent)
                        fingerprint = mix_integer(
                            fingerprint, successor_index
                        )
                    successors = block[3]
                else:
                    fingerprint = mix_integer(fingerprint, block[2])
                    fingerprint = mix_integer(fingerprint, block[3])
                    successors = block[4]
                fingerprint = mix_integer(fingerprint, len(successors))
                for successor in successors:
                    fingerprint = mix_integer(fingerprint, successor)
        for leaves in topology_proof[2:]:
            for block, successor in leaves:
                fingerprint = mix_integer(fingerprint, block)
                fingerprint = mix_integer(fingerprint, successor)
        if dag:
            for phi_site in left_local_phi_sites:
                fingerprint = mix_integer(fingerprint, phi_site)
            for phi_site in right_local_phi_sites:
                fingerprint = mix_integer(fingerprint, phi_site)
            if shared_dag:
                for predicate_site in left_predicate_sites:
                    fingerprint = mix_integer(
                        fingerprint, predicate_site
                    )
                for predicate_site in right_predicate_sites:
                    fingerprint = mix_integer(
                        fingerprint, predicate_site
                    )
    fingerprint = mix_integer(fingerprint, len(parsed_alignment))
    for slot in parsed_alignment:
        for value in slot:
            fingerprint = mix_integer(fingerprint, value)
    fingerprint = mix_integer(fingerprint, len(output_sites))
    for output_site in output_sites:
        fingerprint = mix_integer(fingerprint, output_site)
    if shared_dag:
        fingerprint = mix_integer(fingerprint, reused_guard_edges)
        fingerprint = mix_integer(
            fingerprint, reused_predicate_negations
        )
        if cross_region:
            fingerprint = mix_integer(
                fingerprint,
                cross_region_reused_predicate_negations,
            )
            fingerprint = mix_integer(
                fingerprint, len(cross_region_source_sites)
            )
            for source_site in cross_region_source_sites:
                fingerprint = mix_integer(
                    fingerprint, source_site
                )
    claimed = canonical_uint64(
        record.get("structure_fingerprint"), "structure_fingerprint"
    )
    require(claimed == fingerprint, "structure fingerprint mismatch")
    if cross_region:
        return {
            "transaction_fingerprint": transaction_fingerprint,
            "transaction_ordinal": transaction_ordinal,
            "transaction_size": transaction_size,
            "transaction_sites": transaction_sites,
            "transaction_shared_predicate_sites":
                transaction_shared_predicate_sites,
            "cross_region_reused_predicate_negations":
                cross_region_reused_predicate_negations,
            "function": transaction_function,
        }
    return None


def verify_path(path):
    count = 0
    transactions = {}
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                proof = verify_record(json.loads(line))
            except (json.JSONDecodeError, VerificationError) as error:
                raise VerificationError(
                    f"{path}:{line_number}: {error}"
                ) from error
            if proof is not None:
                transactions.setdefault(
                    proof["transaction_fingerprint"], []
                ).append(proof)
            count += 1
    require(count > 0, "manifest contains no records")
    for fingerprint, proofs in transactions.items():
        expected_size = proofs[0]["transaction_size"]
        expected_sites = proofs[0]["transaction_sites"]
        expected_predicates = proofs[0][
            "transaction_shared_predicate_sites"
        ]
        expected_function = proofs[0]["function"]
        require(
            len(proofs) == expected_size,
            f"transaction {fingerprint} record count mismatch",
        )
        require(
            [
                proof["transaction_ordinal"]
                for proof in proofs
            ]
            == list(range(expected_size)),
            f"transaction {fingerprint} ordinals are incomplete",
        )
        require(
            all(
                proof["transaction_size"] == expected_size
                and proof["transaction_sites"] == expected_sites
                and proof["transaction_shared_predicate_sites"]
                == expected_predicates
                and proof["function"] == expected_function
                for proof in proofs
            ),
            f"transaction {fingerprint} proof fields disagree",
        )
        require(
            sum(
                proof[
                    "cross_region_reused_predicate_negations"
                ]
                for proof in proofs
            )
            > 0,
            f"transaction {fingerprint} has no cross-region reuse",
        )
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    arguments = parser.parse_args(argv)
    try:
        count = verify_path(arguments.manifest)
    except (OSError, UnicodeError, VerificationError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {count} Hydra transform record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
