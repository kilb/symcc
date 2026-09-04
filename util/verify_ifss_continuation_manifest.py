#!/usr/bin/env python3
"""Verify proof-carrying IFSS continuation JSONL manifests."""

import argparse
import json
import sys
from pathlib import Path


SCHEMA = "symcc-ifss-continuation-manifest-v1"
MEMORY_INITIAL_SCHEMA = (
    "must-alias-or-live-on-entry-continuation-memory-tuple-v2"
)
MEMORY_NESTED_SCHEMA = (
    "bounded-acyclic-memoryphi-continuation-memory-tuple-v3"
)
MEMORY_BYTE_LANE_SCHEMA = "byte-lane-continuation-memory-tuple-v4"
MEMORY_GUARDED_BYTE_LANE_SCHEMA = (
    "guarded-byte-lane-continuation-memory-tuple-v5"
)
MEMORY_CYCLIC_BYTE_LANE_SCHEMA = (
    "cyclic-byte-lane-continuation-memory-tuple-v6"
)
MEMORY_POINTER_PARTITION_SCHEMA = (
    "finite-pointer-union-continuation-memory-tuple-v7"
)
MEMORY_GUARDED_PRIORITY_SCHEMA = (
    "guarded-write-priority-continuation-memory-tuple-v8"
)
MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA = (
    "conditional-cyclic-byte-lane-continuation-memory-tuple-v9"
)
MEMORY_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA = (
    "multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10"
)
MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA = (
    "pointer-union-priority-continuation-memory-tuple-v11"
)
MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA = (
    "conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12"
)
MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA = (
    "bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13"
)
MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA = (
    "nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14"
)
MEMORY_ORDERED_WRITER_GRAPH_SCHEMA = (
    "ordered-writer-graph-continuation-memory-tuple-v15"
)
MEMORY_SYMBOLIC_REGION_WRITER_GRAPH_SCHEMA = (
    "symbolic-region-writer-graph-continuation-memory-tuple-v16"
)
MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA = (
    "symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17"
)
MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA = (
    "symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18"
)
MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA = (
    "ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19"
)
MASK64 = (1 << 64) - 1
FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def integer(value, name, minimum=0, maximum=None):
    require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{name} is not an integer",
    )
    require(value >= minimum, f"{name} is below its bound")
    if maximum is not None:
        require(value <= maximum, f"{name} is above its bound")
    return value


def unsigned_text(value, name):
    require(isinstance(value, str) and value, f"{name} is not text")
    require(value.isascii() and value.isdigit(), f"{name} is not decimal")
    require(value == str(int(value)), f"{name} is not canonical")
    parsed = int(value)
    require(parsed <= MASK64, f"{name} is outside uint64")
    return parsed


def mix_byte(hash_value, byte):
    return ((hash_value ^ byte) * FNV_PRIME) & MASK64


def mix_text(hash_value, value):
    for byte in value.encode("utf-8"):
        hash_value = mix_byte(hash_value, byte)
    return mix_byte(hash_value, 0xFF)


def mix_integer(hash_value, value):
    require(0 <= value <= MASK64, "fingerprint integer is outside uint64")
    for shift in range(0, 64, 8):
        hash_value = mix_byte(hash_value, (value >> shift) & 0xFF)
    return hash_value


def canonical_records(value, name, expected_count):
    require(isinstance(value, list), f"{name} is not a list")
    require(len(value) == expected_count, f"{name} count mismatch")
    for ordinal, item in enumerate(value):
        require(isinstance(item, dict), f"{name} item is not an object")
        require(
            integer(item.get("ordinal"), f"{name} ordinal") == ordinal,
            f"{name} ordinal is not canonical",
        )
    return value


def parse_nested_state(state, expected_exit, exit_count):
    exit_ordinal = integer(
        state.get("exit"), "memory exit", 0, exit_count - 1
    )
    require(exit_ordinal == expected_exit, "memory state exit order mismatch")
    root_node = integer(state.get("root_node"), "root node", 0, 15)
    require(root_node == 0, "provenance root is not canonical")
    raw_nodes = state.get("provenance_nodes")
    require(
        isinstance(raw_nodes, list) and 1 <= len(raw_nodes) <= 16,
        "provenance node count is invalid",
    )
    nodes = canonical_records(
        raw_nodes, "provenance nodes", len(raw_nodes)
    )
    referenced = [0] * len(nodes)
    parsed_nodes = []
    phi_count = 0
    has_store = False
    has_live = False
    kind_tags = {"store": 0, "live-on-entry": 1, "memory-phi": 2}
    for node in nodes:
        ordinal = node["ordinal"]
        state_kind = node.get("state_kind")
        require(state_kind in kind_tags, "unknown provenance state kind")
        kind_tag = kind_tags[state_kind]
        source_site = unsigned_text(
            node.get("source_site"), "provenance source site"
        )
        skipped = node.get("skipped_nomod_sites")
        require(
            isinstance(skipped, list) and len(skipped) <= 8,
            "provenance NoMod chain is invalid",
        )
        parsed_skipped = [
            unsigned_text(site, "provenance NoMod site") for site in skipped
        ]
        require(
            len(set(parsed_skipped)) == len(parsed_skipped),
            "provenance NoMod chain repeats a site",
        )
        incoming = node.get("incoming")
        require(isinstance(incoming, list), "provenance incoming is invalid")
        parsed_incoming = []
        seen_blocks = set()
        if state_kind == "memory-phi":
            phi_count += 1
            require(source_site > 0, "MemoryPhi source site is zero")
            require(
                2 <= len(incoming) <= 4,
                "MemoryPhi incoming count is invalid",
            )
        else:
            require(not incoming, "leaf provenance node has incoming edges")
            if state_kind == "store":
                require(source_site > 0, "store source site is zero")
                has_store = True
            else:
                require(source_site == 0, "LiveOnEntry source is not zero")
                has_live = True
        for edge in incoming:
            require(isinstance(edge, dict), "provenance edge is not an object")
            block_site = unsigned_text(
                edge.get("block_site"), "provenance block site"
            )
            child = integer(
                edge.get("node"),
                "provenance child",
                ordinal + 1,
                len(nodes) - 1,
            )
            require(
                block_site not in seen_blocks,
                "MemoryPhi repeats an incoming block",
            )
            seen_blocks.add(block_site)
            referenced[child] += 1
            parsed_incoming.append((block_site, child))
        parsed_nodes.append(
            (
                kind_tag,
                source_site,
                parsed_skipped,
                parsed_incoming,
            )
        )
    require(phi_count <= 4, "nested provenance has too many MemoryPhi nodes")
    require(
        referenced[0] == 0 and all(count == 1 for count in referenced[1:]),
        "provenance is not a canonical tree",
    )
    return (
        (exit_ordinal, root_node, parsed_nodes),
        has_live,
        has_store,
        phi_count,
    )


def parse_byte_lane_state(
    state,
    expected_exit,
    exit_count,
    guarded_schema=False,
    priority_schema=False,
    require_store=True,
):
    exit_ordinal = integer(
        state.get("exit"), "memory exit", 0, exit_count - 1
    )
    require(exit_ordinal == expected_exit, "memory state exit order mismatch")
    byte_width = integer(state.get("byte_width"), "byte width", 2, 8)
    endianness = state.get("endianness")
    require(endianness in ("little", "big"), "unknown byte endianness")
    endian_tag = 0 if endianness == "little" else 1
    skipped = state.get("skipped_nomod_sites")
    require(
        isinstance(skipped, list) and len(skipped) <= 8,
        "byte-lane NoMod chain is invalid",
    )
    parsed_skipped = [
        unsigned_text(site, "byte-lane NoMod site") for site in skipped
    ]
    require(
        len(set(parsed_skipped)) == len(parsed_skipped),
        "byte-lane NoMod chain repeats a site",
    )

    lanes = canonical_records(state.get("lanes"), "byte lanes", byte_width)
    parsed_lanes = []
    source_widths = {}
    source_bytes = set()
    guarded_source_bytes = set()
    guarded_identity = None
    priority_identities = {}
    has_live = False
    has_store = False
    has_guarded = False
    for lane in lanes:
        ordinal = lane["ordinal"]
        source_kind = lane.get("source_kind")
        require(
            source_kind in ("store", "live-on-entry"),
            "unknown byte-lane source kind",
        )
        source_site = unsigned_text(
            lane.get("source_site"), "byte-lane source site"
        )
        source_width = integer(
            lane.get("source_width"), "byte-lane source width", 1, 8
        )
        source_byte = integer(
            lane.get("source_byte"),
            "byte-lane source byte",
            0,
            source_width - 1,
        )
        if source_kind == "store":
            require(source_site > 0, "byte-lane store site is zero")
            previous_width = source_widths.setdefault(
                source_site, source_width
            )
            require(
                previous_width == source_width,
                "byte-lane store width is inconsistent",
            )
            require(
                (source_site, source_byte) not in source_bytes,
                "byte-lane store byte is reused",
            )
            source_bytes.add((source_site, source_byte))
            kind_tag = 0
            has_store = True
        else:
            require(source_site == 0, "LiveOnEntry byte source is not zero")
            require(
                source_width == byte_width and source_byte == ordinal,
                "LiveOnEntry byte source is not canonical",
            )
            kind_tag = 1
            has_live = True
        guarded_source = lane.get("guarded_source")
        guarded_sources = lane.get("guarded_sources")
        require(
            priority_schema or guarded_sources is None,
            "non-priority byte lane has guarded_sources",
        )
        require(
            not priority_schema or guarded_source is None,
            "priority byte lane has guarded_source",
        )
        require(
            guarded_schema
            or (guarded_source is None and guarded_sources is None),
            "v4 byte lane has a guarded source",
        )
        parsed_guarded = None
        if priority_schema:
            require(
                guarded_sources is None
                or (
                    isinstance(guarded_sources, list)
                    and len(guarded_sources) <= 2
                ),
                "guarded priority sources are invalid",
            )
            parsed_guarded = []
            previous_priority = -1
            for priority_source in guarded_sources or []:
                require(
                    isinstance(priority_source, dict),
                    "guarded priority source is not an object",
                )
                priority = integer(
                    priority_source.get("priority"),
                    "guarded source priority",
                    0,
                    1,
                )
                require(
                    priority > previous_priority,
                    "guarded source priorities are not ordered",
                )
                previous_priority = priority
                guard_site = unsigned_text(
                    priority_source.get("guard_site"),
                    "guarded byte source guard site",
                )
                require(guard_site > 0, "guarded byte source guard is zero")
                store_when = priority_source.get("store_when")
                require(
                    store_when in ("true", "false"),
                    "guarded byte source polarity is invalid",
                )
                store_when_tag = 1 if store_when == "true" else 0
                guarded_site = unsigned_text(
                    priority_source.get("source_site"),
                    "guarded byte source site",
                )
                require(
                    guarded_site > 0, "guarded byte source store is zero"
                )
                guarded_width = integer(
                    priority_source.get("source_width"),
                    "guarded byte source width",
                    1,
                    8,
                )
                guarded_byte = integer(
                    priority_source.get("source_byte"),
                    "guarded byte source byte",
                    0,
                    guarded_width - 1,
                )
                identity = (guard_site, guarded_site, guarded_width)
                known_identity = priority_identities.setdefault(
                    priority, identity
                )
                require(
                    known_identity == identity,
                    "guarded priority identity is inconsistent",
                )
                require(
                    (guarded_site, guarded_byte)
                    not in guarded_source_bytes,
                    "guarded store byte is reused",
                )
                guarded_source_bytes.add((guarded_site, guarded_byte))
                parsed_guarded.append(
                    (
                        priority,
                        guard_site,
                        store_when_tag,
                        guarded_site,
                        guarded_byte,
                        guarded_width,
                    )
                )
                has_guarded = True
                has_store = True
        elif guarded_source is not None:
            require(
                isinstance(guarded_source, dict),
                "guarded byte source is not an object",
            )
            guard_site = unsigned_text(
                guarded_source.get("guard_site"),
                "guarded byte source guard site",
            )
            require(guard_site > 0, "guarded byte source guard is zero")
            store_when = guarded_source.get("store_when")
            require(
                store_when in ("true", "false"),
                "guarded byte source polarity is invalid",
            )
            store_when_tag = 1 if store_when == "true" else 0
            guarded_site = unsigned_text(
                guarded_source.get("source_site"),
                "guarded byte source site",
            )
            require(
                guarded_site > 0, "guarded byte source store is zero"
            )
            guarded_width = integer(
                guarded_source.get("source_width"),
                "guarded byte source width",
                1,
                8,
            )
            guarded_byte = integer(
                guarded_source.get("source_byte"),
                "guarded byte source byte",
                0,
                guarded_width - 1,
            )
            identity = (guard_site, guarded_site, guarded_width)
            if guarded_identity is None:
                guarded_identity = identity
            require(
                guarded_identity == identity,
                "guarded byte state uses multiple alias stores",
            )
            require(
                (guarded_site, guarded_byte)
                not in guarded_source_bytes,
                "guarded store byte is reused",
            )
            guarded_source_bytes.add((guarded_site, guarded_byte))
            parsed_guarded = (
                guard_site,
                store_when_tag,
                guarded_site,
                guarded_byte,
                guarded_width,
            )
            has_guarded = True
            has_store = True
        parsed_lanes.append(
            (
                ordinal,
                kind_tag,
                source_site,
                source_byte,
                source_width,
                parsed_guarded,
            )
        )
    if priority_schema:
        has_guarded = set(priority_identities) == {0, 1}
    require(
        has_store or not require_store,
        "byte-lane state has no store source",
    )
    return (
        (
            exit_ordinal,
            byte_width,
            endian_tag,
            parsed_skipped,
            parsed_lanes,
        ),
        has_live,
        has_store,
        has_guarded,
    )


def parse_symbolic_region_writer_record(record, byte_width, name):
    require(isinstance(record, dict), f"{name} is not an object")
    require(
        all(
            field not in record
            for field in (
                "pointer_partition",
                "guard_site",
                "lane_source_bytes",
                "lane_store_when",
            )
        ),
        f"{name} has incompatible writer fields",
    )
    store_site = unsigned_text(
        record.get("store_site"), f"{name} store site"
    )
    store_width = integer(
        record.get("store_width"), f"{name} store width", 1, 8
    )
    region_base_site = unsigned_text(
        record.get("region_base_site"), f"{name} base site"
    )
    region_extent = unsigned_text(
        record.get("region_extent"), f"{name} extent"
    )
    index_site = unsigned_text(
        record.get("index_site"), f"{name} index site"
    )
    index_bits = integer(
        record.get("index_bits"), f"{name} index bits", 1, 64
    )
    base_offset = integer(
        record.get("base_offset"),
        f"{name} base offset",
        -(1 << 63),
        (1 << 63) - 1,
    )
    require(
        store_site > 0
        and region_base_site > 0
        and index_site > 0
        and 1 <= region_extent <= (1 << 32)
        and region_extent >= byte_width
        and 0 <= base_offset <= region_extent,
        f"{name} identity is invalid",
    )
    raw_lane_cases = record.get("lane_cases")
    require(
        isinstance(raw_lane_cases, list)
        and len(raw_lane_cases) == byte_width,
        f"{name} lane count is invalid",
    )
    lane_cases = canonical_records(
        raw_lane_cases, f"{name} lanes", byte_width
    )
    parsed_lane_cases = []
    signed_min = -(1 << (index_bits - 1))
    signed_max = (1 << (index_bits - 1)) - 1
    has_effect = False
    for lane_record in lane_cases:
        raw_cases = lane_record.get("cases")
        require(
            isinstance(raw_cases, list)
            and len(raw_cases) <= store_width,
            f"{name} case count is invalid",
        )
        cases = canonical_records(
            raw_cases, f"{name} cases", len(raw_cases)
        )
        parsed_cases = []
        seen_indices = set()
        previous_index = None
        previous_source = None
        for case in cases:
            index_value = integer(
                case.get("index_value"),
                f"{name} index value",
                signed_min,
                signed_max,
            )
            source_byte = integer(
                case.get("source_byte"),
                f"{name} source byte",
                0,
                store_width - 1,
            )
            require(
                index_value not in seen_indices,
                f"{name} index case is repeated",
            )
            if previous_index is not None:
                require(
                    index_value == previous_index - 1
                    and source_byte == previous_source + 1,
                    f"{name} cases are not canonical",
                )
            seen_indices.add(index_value)
            previous_index = index_value
            previous_source = source_byte
            parsed_cases.append((index_value, source_byte))
            has_effect = True
        parsed_lane_cases.append(parsed_cases)
    require(has_effect, f"{name} has no overlap cases")
    return (
        store_site,
        store_width,
        region_base_site,
        region_extent,
        index_site,
        index_bits,
        base_offset,
        parsed_lane_cases,
    )


def parse_cyclic_byte_lane_state(
    state,
    expected_exit,
    exit_count,
    conditional_schema=False,
    symbolic_region_schema=False,
    ordered_symbolic_region_schema=False,
):
    require(
        sum(
            bool(value)
            for value in (
                conditional_schema,
                symbolic_region_schema,
                ordered_symbolic_region_schema,
            )
        )
        <= 1,
        "cycle schema classification overlaps",
    )
    state_kind = state.get("state_kind")
    cyclic_kind = (
        "ordered-symbolic-region-cyclic-byte-composition"
        if ordered_symbolic_region_schema
        else (
            "symbolic-region-cyclic-byte-composition"
            if symbolic_region_schema
            else (
                "conditional-cyclic-byte-composition"
                if conditional_schema
                else "cyclic-byte-composition"
            )
        )
    )
    require(
        state_kind
        in ("linear-byte-composition", cyclic_kind),
        "cyclic schema state kind is invalid",
    )
    if state_kind == "linear-byte-composition":
        parsed, has_live, has_store, has_guarded = parse_byte_lane_state(
            state, expected_exit, exit_count, guarded_schema=False
        )
        require(not has_guarded, "cyclic schema has a guarded linear state")
        return (0, parsed), has_live, has_store

    exit_ordinal = integer(
        state.get("exit"), "memory exit", 0, exit_count - 1
    )
    require(exit_ordinal == expected_exit, "memory state exit order mismatch")
    byte_width = integer(state.get("byte_width"), "byte width", 2, 8)
    endianness = state.get("endianness")
    require(endianness in ("little", "big"), "unknown byte endianness")
    endian_tag = 0 if endianness == "little" else 1
    skipped = state.get("skipped_nomod_sites")
    require(
        isinstance(skipped, list) and len(skipped) <= 8,
        "cyclic prefix NoMod chain is invalid",
    )
    parsed_skipped = [
        unsigned_text(site, "cyclic prefix NoMod site") for site in skipped
    ]
    require(
        len(set(parsed_skipped)) == len(parsed_skipped),
        "cyclic prefix NoMod chain repeats a site",
    )
    header_site = unsigned_text(
        state.get("cycle_header_site"), "cycle header site"
    )
    entry_site = unsigned_text(
        state.get("cycle_entry_site"), "cycle entry site"
    )
    backedge_site = unsigned_text(
        state.get("cycle_backedge_site"), "cycle backedge site"
    )
    require(
        header_site > 0
        and entry_site > 0
        and backedge_site > 0
        and len({header_site, entry_site, backedge_site}) == 3,
        "cycle topology sites are invalid",
    )
    conditional_topology = ()
    if conditional_schema:
        branch_site = unsigned_text(
            state.get("cycle_branch_site"), "cycle branch site"
        )
        store_arm_site = unsigned_text(
            state.get("cycle_store_arm_site"), "cycle store-arm site"
        )
        carry_arm_site = unsigned_text(
            state.get("cycle_carry_arm_site"), "cycle carry-arm site"
        )
        guard_site = unsigned_text(
            state.get("cycle_guard_site"), "cycle guard site"
        )
        store_when = state.get("cycle_store_when")
        require(
            store_when in ("true", "false"),
            "cycle store polarity is invalid",
        )
        require(
            len(
                {
                    header_site,
                    entry_site,
                    backedge_site,
                    branch_site,
                    store_arm_site,
                    carry_arm_site,
                    guard_site,
                }
            )
            == 7,
            "conditional cycle topology sites are not unique",
        )
        conditional_topology = (
            branch_site,
            store_arm_site,
            carry_arm_site,
            guard_site,
            1 if store_when == "true" else 0,
        )
    else:
        require(
            all(
                field not in state
                for field in (
                    "cycle_branch_site",
                    "cycle_store_arm_site",
                    "cycle_carry_arm_site",
                    "cycle_guard_site",
                    "cycle_store_when",
                )
            ),
            "unconditional cycle has conditional topology",
        )

    entry = state.get("entry_state")
    require(isinstance(entry, dict), "cycle entry state is missing")
    entry_record = dict(entry)
    entry_record["exit"] = exit_ordinal
    entry_record["byte_width"] = byte_width
    entry_record["endianness"] = endianness
    (
        parsed_entry,
        entry_has_live,
        entry_has_store,
        entry_has_guarded,
    ) = parse_byte_lane_state(
        entry_record, expected_exit, exit_count, guarded_schema=False
    )
    require(not entry_has_guarded, "cycle entry state is guarded")

    backedge = state.get("backedge_state")
    require(isinstance(backedge, dict), "cycle backedge state is missing")
    backedge_skipped = backedge.get("skipped_nomod_sites")
    require(
        isinstance(backedge_skipped, list)
        and len(backedge_skipped) <= 8,
        "cycle backedge NoMod chain is invalid",
    )
    parsed_backedge_skipped = [
        unsigned_text(site, "cycle backedge NoMod site")
        for site in backedge_skipped
    ]
    require(
        len(set(parsed_backedge_skipped))
        == len(parsed_backedge_skipped),
        "cycle backedge NoMod chain repeats a site",
    )
    lanes = canonical_records(
        backedge.get("lanes"), "cycle backedge lanes", byte_width
    )
    parsed_lanes = []
    source_widths = {}
    source_bytes = set()
    has_store = False
    has_carry = False
    for lane in lanes:
        ordinal = lane["ordinal"]
        source_kind = lane.get("source_kind")
        store_kind = "guarded-store" if conditional_schema else "store"
        require(
            source_kind in (store_kind, "carry"),
            "unknown cycle backedge source kind",
        )
        source_site = unsigned_text(
            lane.get("source_site"), "cycle backedge source site"
        )
        source_width = integer(
            lane.get("source_width"),
            "cycle backedge source width",
            1,
            8,
        )
        source_byte = integer(
            lane.get("source_byte"),
            "cycle backedge source byte",
            0,
            source_width - 1,
        )
        if source_kind == "carry":
            require(
                source_site == 0
                and source_width == byte_width
                and source_byte == ordinal,
                "cycle carry source is not canonical",
            )
            kind_tag = 2
            has_carry = True
        else:
            require(source_site > 0, "cycle store site is zero")
            previous_width = source_widths.setdefault(
                source_site, source_width
            )
            require(
                previous_width == source_width,
                "cycle store width is inconsistent",
            )
            require(
                (source_site, source_byte) not in source_bytes,
                "cycle store byte is reused",
            )
            source_bytes.add((source_site, source_byte))
            kind_tag = 0
            has_store = True
        parsed_lanes.append(
            (
                ordinal,
                kind_tag,
                source_site,
                source_byte,
                source_width,
            )
        )
    symbolic_writer = None
    symbolic_writers = None
    if ordered_symbolic_region_schema:
        require(
            has_carry and not has_store,
            "ordered symbolic cycle base is not an all-carry state",
        )
        raw_writers = backedge.get("symbolic_region_writers")
        require(
            isinstance(raw_writers, list)
            and 2 <= len(raw_writers) <= 4,
            "ordered symbolic cycle writer count is invalid",
        )
        writers = canonical_records(
            raw_writers,
            "ordered symbolic cycle writers",
            len(raw_writers),
        )
        symbolic_writers = [
            parse_symbolic_region_writer_record(
                writer,
                byte_width,
                f"ordered symbolic cycle writer {ordinal}",
            )
            for ordinal, writer in enumerate(writers)
        ]
        newest = symbolic_writers[0]
        require(
            len({writer[0] for writer in symbolic_writers})
            == len(symbolic_writers),
            "ordered symbolic cycle repeats a store site",
        )
        require(
            all(
                writer[2] == newest[2]
                and writer[3] == newest[3]
                for writer in symbolic_writers
            ),
            "ordered symbolic cycle writers do not share a fixed region",
        )
        require(
            "symbolic_region_writer" not in backedge,
            "ordered symbolic cycle has a legacy writer",
        )
    elif symbolic_region_schema:
        require(
            has_carry and not has_store,
            "symbolic cycle base is not an all-carry state",
        )
        require(
            "symbolic_region_writers" not in backedge,
            "symbolic cycle has ordered writer fields",
        )
        symbolic_writer = parse_symbolic_region_writer_record(
            backedge.get("symbolic_region_writer"),
            byte_width,
            "symbolic cycle writer",
        )
    else:
        require(
            "symbolic_region_writer" not in backedge
            and "symbolic_region_writers" not in backedge,
            "legacy cycle has a symbolic writer",
        )
        require(
            has_store and has_carry,
            "cycle transfer is not a partial store/carry fixed point",
        )
    parsed = (
        exit_ordinal,
        byte_width,
        endian_tag,
        parsed_skipped,
        header_site,
        entry_site,
        backedge_site,
        conditional_topology,
        parsed_entry,
        parsed_backedge_skipped,
        parsed_lanes,
    )
    if ordered_symbolic_region_schema:
        parsed += (symbolic_writers,)
    elif symbolic_region_schema:
        parsed += (symbolic_writer,)
    return (
        (1, parsed),
        entry_has_live,
        entry_has_store
        or has_store
        or symbolic_writer is not None
        or symbolic_writers is not None,
    )


def parse_multi_latch_cyclic_byte_lane_state(
    state,
    expected_exit,
    exit_count,
    conditional_schema=False,
    bounded_schema=False,
    nested_predicate_schema=False,
    symbolic_region_schema=False,
):
    state_kind = state.get("state_kind")
    require(
        sum(
            (
                bool(conditional_schema),
                bool(bounded_schema),
                bool(nested_predicate_schema),
                bool(symbolic_region_schema),
            )
        )
        <= 1,
        "multi-latch schema classification overlaps",
    )
    cyclic_kind = (
        "symbolic-region-multi-latch-cyclic-byte-composition"
        if symbolic_region_schema
        else (
            "nested-predicate-cyclic-byte-composition"
            if nested_predicate_schema
            else (
                "bounded-multi-latch-cyclic-byte-composition"
                if bounded_schema
                else (
                    "conditional-multi-latch-cyclic-byte-composition"
                    if conditional_schema
                    else "multi-latch-cyclic-byte-composition"
                )
            )
        )
    )
    tagged_schema = (
        conditional_schema
        or bounded_schema
        or nested_predicate_schema
        or symbolic_region_schema
    )
    require(
        state_kind
        in (
            "linear-byte-composition",
            cyclic_kind,
        ),
        "multi-latch schema state kind is invalid",
    )
    if state_kind == "linear-byte-composition":
        parsed, has_live, has_store, has_guarded = parse_byte_lane_state(
            state, expected_exit, exit_count, guarded_schema=False
        )
        require(not has_guarded, "multi-latch linear state is guarded")
        return (0, parsed), has_live, has_store

    exit_ordinal = integer(
        state.get("exit"), "memory exit", 0, exit_count - 1
    )
    require(exit_ordinal == expected_exit, "memory state exit order mismatch")
    byte_width = integer(state.get("byte_width"), "byte width", 2, 8)
    endianness = state.get("endianness")
    require(endianness in ("little", "big"), "unknown byte endianness")
    endian_tag = 0 if endianness == "little" else 1
    skipped = state.get("skipped_nomod_sites")
    require(
        isinstance(skipped, list) and len(skipped) <= 8,
        "multi-latch prefix NoMod chain is invalid",
    )
    parsed_skipped = [
        unsigned_text(site, "multi-latch prefix NoMod site")
        for site in skipped
    ]
    require(
        len(set(parsed_skipped)) == len(parsed_skipped),
        "multi-latch prefix NoMod chain repeats a site",
    )
    header_site = unsigned_text(
        state.get("cycle_header_site"), "cycle header site"
    )
    entry_site = unsigned_text(
        state.get("cycle_entry_site"), "cycle entry site"
    )
    require(
        header_site > 0
        and entry_site > 0
        and header_site != entry_site,
        "multi-latch header/entry topology is invalid",
    )
    require(
        "cycle_backedge_site" not in state
        and "backedge_state" not in state,
        "multi-latch state has a single-backedge record",
    )

    entry = state.get("entry_state")
    require(isinstance(entry, dict), "multi-latch entry state is missing")
    entry_record = dict(entry)
    entry_record["exit"] = exit_ordinal
    entry_record["byte_width"] = byte_width
    entry_record["endianness"] = endianness
    (
        parsed_entry,
        entry_has_live,
        entry_has_store,
        entry_has_guarded,
    ) = parse_byte_lane_state(
        entry_record, expected_exit, exit_count, guarded_schema=False
    )
    require(not entry_has_guarded, "multi-latch entry state is guarded")

    raw_transfers = state.get("backedge_states")
    require(
        isinstance(raw_transfers, list)
        and (
            (
                1 <= len(raw_transfers) <= 4
                if nested_predicate_schema
                else 2 <= len(raw_transfers) <= 4
            )
            if (
                bounded_schema
                or nested_predicate_schema
                or symbolic_region_schema
            )
            else len(raw_transfers) == 2
        ),
        "multi-latch backedge count is invalid",
    )
    transfers = canonical_records(
        raw_transfers, "multi-latch backedges", len(raw_transfers)
    )
    parsed_transfers = []
    topology_sites = {header_site, entry_site}
    has_store = False
    conditional_count = 0
    predicate_count = 0
    symbolic_region_count = 0
    for transfer in transfers:
        backedge_site = unsigned_text(
            transfer.get("cycle_backedge_site"),
            "multi-latch backedge site",
        )
        require(
            backedge_site > 0 and backedge_site not in topology_sites,
            "multi-latch topology site is repeated",
        )
        topology_sites.add(backedge_site)
        conditional_topology = ()
        conditional_transfer = False
        conditional_fields = (
            "cycle_branch_site",
            "cycle_store_arm_site",
            "cycle_carry_arm_site",
            "cycle_guard_site",
            "cycle_store_when",
        )
        if tagged_schema:
            transfer_kind = transfer.get("transfer_kind")
            allowed_kinds = (
                ("unconditional", "conditional", "predicate-tree")
                if nested_predicate_schema
                else ("unconditional", "conditional")
            )
            require(
                transfer_kind in allowed_kinds,
                "multi-latch transfer kind is invalid",
            )
            if transfer_kind == "predicate-tree":
                require(
                    nested_predicate_schema
                    and all(
                        field not in transfer
                        for field in (
                            *conditional_fields,
                            "skipped_nomod_sites",
                            "lanes",
                            "symbolic_region_writer",
                        )
                    ),
                    "predicate-tree transfer has linear fields",
                )
                raw_nodes = transfer.get("predicate_nodes")
                raw_leaves = transfer.get("predicate_leaves")
                require(
                    isinstance(raw_nodes, list)
                    and isinstance(raw_leaves, list),
                    "cycle predicate tree records are missing",
                )
                nodes = canonical_records(
                    raw_nodes,
                    "cycle predicate nodes",
                    len(raw_nodes),
                )
                leaves = canonical_records(
                    raw_leaves,
                    "cycle predicate leaves",
                    len(raw_leaves),
                )
                require(
                    2 <= len(nodes) <= 3
                    and 3 <= len(leaves) <= 4
                    and len(nodes) + 1 == len(leaves),
                    "cycle predicate tree budget is invalid",
                )
                node_indegree = [0] * len(nodes)
                leaf_indegree = [0] * len(leaves)
                parsed_nodes = []
                for node in nodes:
                    node_ordinal = node["ordinal"]
                    branch_site = unsigned_text(
                        node.get("branch_site"),
                        "cycle predicate branch site",
                    )
                    guard_site = unsigned_text(
                        node.get("guard_site"),
                        "cycle predicate guard site",
                    )
                    require(
                        branch_site > 0
                        and guard_site > 0
                        and branch_site not in topology_sites
                        and guard_site != branch_site,
                        "cycle predicate node topology is invalid",
                    )
                    topology_sites.add(branch_site)
                    parsed_children = []
                    for field in ("true_child", "false_child"):
                        child = node.get(field)
                        require(
                            isinstance(child, dict),
                            "cycle predicate child is missing",
                        )
                        child_kind = child.get("kind")
                        require(
                            child_kind in ("node", "leaf"),
                            "cycle predicate child kind is invalid",
                        )
                        upper = (
                            len(leaves) - 1
                            if child_kind == "leaf"
                            else len(nodes) - 1
                        )
                        child_index = integer(
                            child.get("index"),
                            "cycle predicate child index",
                            0,
                            upper,
                        )
                        if child_kind == "leaf":
                            leaf_indegree[child_index] += 1
                            child_kind_tag = 1
                        else:
                            require(
                                child_index > node_ordinal,
                                "cycle predicate node is cyclic or non-canonical",
                            )
                            node_indegree[child_index] += 1
                            child_kind_tag = 0
                        parsed_children.extend(
                            (child_kind_tag, child_index)
                        )
                    parsed_nodes.append(
                        (
                            branch_site,
                            guard_site,
                            *parsed_children,
                        )
                    )
                require(
                    node_indegree[0] == 0
                    and all(value == 1 for value in node_indegree[1:])
                    and all(value == 1 for value in leaf_indegree),
                    "cycle predicate tree is disconnected",
                )

                parsed_leaves = []
                tree_has_store = False
                for leaf in leaves:
                    leaf_ordinal = leaf["ordinal"]
                    leaf_site = unsigned_text(
                        leaf.get("leaf_site"),
                        "cycle predicate leaf site",
                    )
                    require(
                        leaf_site > 0
                        and leaf_site not in topology_sites,
                        "cycle predicate leaf topology is repeated",
                    )
                    topology_sites.add(leaf_site)
                    leaf_skipped = leaf.get("skipped_nomod_sites")
                    require(
                        isinstance(leaf_skipped, list)
                        and len(leaf_skipped) <= 8,
                        "cycle predicate leaf NoMod chain is invalid",
                    )
                    parsed_leaf_skipped = [
                        unsigned_text(
                            site, "cycle predicate leaf NoMod site"
                        )
                        for site in leaf_skipped
                    ]
                    require(
                        len(set(parsed_leaf_skipped))
                        == len(parsed_leaf_skipped),
                        "cycle predicate leaf NoMod chain repeats a site",
                    )
                    leaf_lanes = canonical_records(
                        leaf.get("lanes"),
                        "cycle predicate leaf lanes",
                        byte_width,
                    )
                    parsed_leaf_lanes = []
                    leaf_has_carry = False
                    source_widths = {}
                    source_bytes = set()
                    for lane in leaf_lanes:
                        lane_ordinal = lane["ordinal"]
                        source_kind = lane.get("source_kind")
                        require(
                            source_kind in ("store", "carry"),
                            "cycle predicate leaf source kind is invalid",
                        )
                        source_site = unsigned_text(
                            lane.get("source_site"),
                            "cycle predicate leaf source site",
                        )
                        source_width = integer(
                            lane.get("source_width"),
                            "cycle predicate leaf source width",
                            1,
                            8,
                        )
                        source_byte = integer(
                            lane.get("source_byte"),
                            "cycle predicate leaf source byte",
                            0,
                            source_width - 1,
                        )
                        if source_kind == "carry":
                            require(
                                source_site == 0
                                and source_width == byte_width
                                and source_byte == lane_ordinal,
                                "cycle predicate carry source is not canonical",
                            )
                            kind_tag = 2
                            leaf_has_carry = True
                        else:
                            require(
                                source_site > 0,
                                "cycle predicate store site is zero",
                            )
                            previous_width = source_widths.setdefault(
                                source_site, source_width
                            )
                            require(
                                previous_width == source_width
                                and (source_site, source_byte)
                                not in source_bytes,
                                "cycle predicate store source is inconsistent",
                            )
                            source_bytes.add((source_site, source_byte))
                            kind_tag = 0
                            tree_has_store = True
                        parsed_leaf_lanes.append(
                            (
                                lane_ordinal,
                                kind_tag,
                                source_site,
                                source_byte,
                                source_width,
                            )
                        )
                    require(
                        leaf_has_carry,
                        "cycle predicate leaf is not carry or partial store/carry",
                    )
                    parsed_leaves.append(
                        (
                            leaf_ordinal,
                            leaf_site,
                            parsed_leaf_skipped,
                            parsed_leaf_lanes,
                        )
                    )
                require(
                    tree_has_store,
                    "cycle predicate tree has no store leaf",
                )
                has_store = True
                predicate_count += 1
                parsed_transfers.append(
                    (
                        backedge_site,
                        2,
                        parsed_nodes,
                        parsed_leaves,
                    )
                )
                continue
            conditional_transfer = transfer_kind == "conditional"
            if conditional_transfer:
                branch_site = unsigned_text(
                    transfer.get("cycle_branch_site"),
                    "multi-latch branch site",
                )
                store_arm_site = unsigned_text(
                    transfer.get("cycle_store_arm_site"),
                    "multi-latch store-arm site",
                )
                carry_arm_site = unsigned_text(
                    transfer.get("cycle_carry_arm_site"),
                    "multi-latch carry-arm site",
                )
                guard_site = unsigned_text(
                    transfer.get("cycle_guard_site"),
                    "multi-latch guard site",
                )
                store_when = transfer.get("cycle_store_when")
                require(
                    store_when in ("true", "false"),
                    "multi-latch store polarity is invalid",
                )
                conditional_block_sites = {
                    branch_site,
                    store_arm_site,
                    carry_arm_site,
                }
                require(
                    guard_site > 0
                    and guard_site not in conditional_block_sites
                    and guard_site not in topology_sites
                    and 0 not in conditional_block_sites
                    and len(conditional_block_sites) == 3
                    and not conditional_block_sites.intersection(
                        topology_sites
                    ),
                    "conditional multi-latch topology is not unique",
                )
                # A dominating comparison may intentionally guard more than
                # one latch. Only CFG block sites must be disjoint.
                topology_sites.update(conditional_block_sites)
                conditional_topology = (
                    branch_site,
                    store_arm_site,
                    carry_arm_site,
                    guard_site,
                    1 if store_when == "true" else 0,
                )
                conditional_count += 1
            else:
                require(
                    all(field not in transfer for field in conditional_fields),
                    "unconditional multi-latch transfer has conditional topology",
                )
        else:
            require(
                "transfer_kind" not in transfer
                and all(field not in transfer for field in conditional_fields),
                "v10 multi-latch transfer has conditional topology",
            )
        transfer_skipped = transfer.get("skipped_nomod_sites")
        require(
            isinstance(transfer_skipped, list)
            and len(transfer_skipped) <= 8,
            "multi-latch backedge NoMod chain is invalid",
        )
        parsed_transfer_skipped = [
            unsigned_text(site, "multi-latch backedge NoMod site")
            for site in transfer_skipped
        ]
        require(
            len(set(parsed_transfer_skipped))
            == len(parsed_transfer_skipped),
            "multi-latch backedge NoMod chain repeats a site",
        )
        lanes = canonical_records(
            transfer.get("lanes"),
            "multi-latch backedge lanes",
            byte_width,
        )
        parsed_lanes = []
        source_widths = {}
        source_bytes = set()
        transfer_has_store = False
        transfer_has_carry = False
        for lane in lanes:
            ordinal = lane["ordinal"]
            source_kind = lane.get("source_kind")
            store_kind = (
                "guarded-store" if conditional_transfer else "store"
            )
            require(
                source_kind in (store_kind, "carry"),
                "unknown multi-latch source kind",
            )
            source_site = unsigned_text(
                lane.get("source_site"), "multi-latch source site"
            )
            source_width = integer(
                lane.get("source_width"),
                "multi-latch source width",
                1,
                8,
            )
            source_byte = integer(
                lane.get("source_byte"),
                "multi-latch source byte",
                0,
                source_width - 1,
            )
            if source_kind == "carry":
                require(
                    source_site == 0
                    and source_width == byte_width
                    and source_byte == ordinal,
                    "multi-latch carry source is not canonical",
                )
                kind_tag = 2
                transfer_has_carry = True
            else:
                require(source_site > 0, "multi-latch store site is zero")
                previous_width = source_widths.setdefault(
                    source_site, source_width
                )
                require(
                    previous_width == source_width,
                    "multi-latch store width is inconsistent",
                )
                require(
                    (source_site, source_byte) not in source_bytes,
                    "multi-latch store byte is reused",
                )
                source_bytes.add((source_site, source_byte))
                kind_tag = 0
                transfer_has_store = True
                has_store = True
            parsed_lanes.append(
                (
                    ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                )
            )
        symbolic_writer = None
        if symbolic_region_schema:
            raw_symbolic_writer = transfer.get("symbolic_region_writer")
            if raw_symbolic_writer is not None:
                symbolic_writer = parse_symbolic_region_writer_record(
                    raw_symbolic_writer,
                    byte_width,
                    "multi-latch symbolic writer",
                )
                require(
                    transfer_has_carry and not transfer_has_store,
                    "symbolic multi-latch base is not all-carry",
                )
                symbolic_region_count += 1
                has_store = True
            else:
                require(
                    transfer_has_store and transfer_has_carry,
                    "non-symbolic multi-latch transfer is not partial store/carry",
                )
        else:
            require(
                "symbolic_region_writer" not in transfer,
                "legacy multi-latch transfer has a symbolic writer",
            )
            require(
                transfer_has_store and transfer_has_carry,
                "multi-latch transfer is not partial store/carry",
            )
        parsed_transfer = (
            backedge_site,
            parsed_transfer_skipped,
            parsed_lanes,
        )
        if nested_predicate_schema:
            parsed_transfer = (
                backedge_site,
                1 if conditional_transfer else 0,
                conditional_topology,
                parsed_transfer_skipped,
                parsed_lanes,
            )
        elif tagged_schema:
            parsed_transfer = (
                backedge_site,
                conditional_topology,
                parsed_transfer_skipped,
                parsed_lanes,
            )
        if symbolic_region_schema:
            parsed_transfer += (symbolic_writer,)
        parsed_transfers.append(parsed_transfer)
    require(
        not conditional_schema or conditional_count > 0,
        "conditional multi-latch schema has no conditional transfer",
    )
    require(
        not nested_predicate_schema or predicate_count > 0,
        "nested-predicate schema has no predicate tree",
    )
    require(
        not symbolic_region_schema or symbolic_region_count > 0,
        "symbolic-region multi-latch schema has no symbolic writer",
    )
    parsed = (
        exit_ordinal,
        byte_width,
        endian_tag,
        parsed_skipped,
        header_site,
        entry_site,
        parsed_entry,
        parsed_transfers,
    )
    return (1, parsed), entry_has_live, entry_has_store or has_store


def parse_pointer_partition_state(
    state,
    expected_exit,
    exit_count,
    guarded_schema=False,
    require_base_store=True,
):
    state_kind = state.get("state_kind")
    require(
        state_kind in ("linear-byte-composition", "finite-pointer-union"),
        "pointer-union state kind is invalid",
    )
    parsed_base, has_live, has_store, has_guarded = parse_byte_lane_state(
        state,
        expected_exit,
        exit_count,
        guarded_schema=guarded_schema,
        require_store=require_base_store,
    )
    require(
        guarded_schema or not has_guarded,
        "pointer-union base state is guarded",
    )
    if state_kind == "linear-byte-composition":
        require(
            "pointer_partition" not in state,
            "linear pointer-union state has a partition",
        )
        return (0, parsed_base), has_live, has_store, has_guarded

    partition = state.get("pointer_partition")
    require(isinstance(partition, dict), "pointer partition is missing")
    store_site = unsigned_text(
        partition.get("store_site"), "pointer partition store site"
    )
    require(store_site > 0, "pointer partition store site is zero")
    store_width = integer(
        partition.get("store_width"), "pointer partition store width", 1, 8
    )
    raw_nodes = partition.get("nodes")
    require(
        isinstance(raw_nodes, list) and 2 <= len(raw_nodes) <= 3,
        "pointer partition node count is invalid",
    )
    nodes = canonical_records(raw_nodes, "pointer partition nodes", len(raw_nodes))
    raw_leaves = partition.get("leaves")
    require(
        isinstance(raw_leaves, list)
        and len(raw_leaves) == len(nodes) + 1
        and len(raw_leaves) <= 4,
        "pointer partition leaf count is invalid",
    )
    leaves = canonical_records(
        raw_leaves, "pointer partition leaves", len(raw_leaves)
    )

    parsed_nodes = []
    referenced_nodes = [0] * len(nodes)
    referenced_leaves = [0] * len(leaves)
    select_sites = set()
    for node in nodes:
        ordinal = node["ordinal"]
        select_site = unsigned_text(
            node.get("select_site"), "pointer partition select site"
        )
        guard_site = unsigned_text(
            node.get("guard_site"), "pointer partition guard site"
        )
        require(
            select_site > 0
            and guard_site > 0
            and select_site not in select_sites,
            "pointer partition node identity is invalid",
        )
        select_sites.add(select_site)
        children = []
        for arm in ("true", "false"):
            kind = node.get(f"{arm}_kind")
            require(
                kind in ("node", "leaf"),
                "pointer partition child kind is invalid",
            )
            if kind == "node":
                index = integer(
                    node.get(f"{arm}_index"),
                    "pointer partition child node",
                    ordinal + 1,
                    len(nodes) - 1,
                )
                referenced_nodes[index] += 1
                kind_tag = 0
            else:
                index = integer(
                    node.get(f"{arm}_index"),
                    "pointer partition child leaf",
                    0,
                    len(leaves) - 1,
                )
                referenced_leaves[index] += 1
                kind_tag = 1
            children.append((kind_tag, index))
        parsed_nodes.append(
            (
                select_site,
                guard_site,
                children[0][0],
                children[0][1],
                children[1][0],
                children[1][1],
            )
        )
    require(
        referenced_nodes[0] == 0
        and all(count == 1 for count in referenced_nodes[1:])
        and all(count == 1 for count in referenced_leaves),
        "pointer partition is not a canonical tree",
    )

    def depth(child_kind, child_index):
        if child_kind == 1:
            return 0
        node = parsed_nodes[child_index]
        return 1 + max(
            depth(node[2], node[3]),
            depth(node[4], node[5]),
        )

    require(depth(0, 0) <= 2, "pointer partition exceeds depth two")
    byte_width = parsed_base[1]
    parsed_leaves = []
    lane_has_fallback = [False] * byte_width
    has_overlap = False
    for leaf in leaves:
        raw_sources = leaf.get("lane_source_bytes")
        require(
            isinstance(raw_sources, list)
            and len(raw_sources) == byte_width,
            "pointer partition leaf width mismatch",
        )
        sources = [
            integer(
                source,
                "pointer partition source byte",
                -1,
                store_width - 1,
            )
            for source in raw_sources
        ]
        for lane, source in enumerate(sources):
            lane_has_fallback[lane] |= source < 0
            has_overlap |= source >= 0
        parsed_leaves.append(sources)
    require(
        has_overlap and all(lane_has_fallback),
        "pointer partition lacks overlap or lane fallback",
    )
    parsed_partition = (
        store_site,
        store_width,
        parsed_nodes,
        parsed_leaves,
    )
    return (1, (parsed_base, parsed_partition)), has_live, True, has_guarded


def parse_ordered_writer_graph_state(
    state, expected_exit, exit_count, extended_schema=False
):
    state_kind = state.get("state_kind")
    allowed_state_kinds = {
        "linear-byte-composition",
        "ordered-writer-graph",
    }
    if extended_schema:
        allowed_state_kinds.add("symbolic-region-writer-graph")
    require(
        state_kind in allowed_state_kinds,
        "ordered writer graph state kind is invalid",
    )
    (
        parsed_base,
        has_live,
        base_has_store,
        has_guarded,
    ) = parse_byte_lane_state(
        state,
        expected_exit,
        exit_count,
        guarded_schema=False,
        require_store=False,
    )
    require(
        not has_guarded
        and "pointer_partition" not in state,
        "ordered writer graph has legacy writer fields",
    )
    raw_layers = state.get("writer_layers")
    layer_limit = 8 if extended_schema else 4
    require(
        isinstance(raw_layers, list)
        and len(raw_layers) <= layer_limit,
        "ordered writer layer count is invalid",
    )
    layers = canonical_records(
        raw_layers, "ordered writer layers", len(raw_layers)
    )
    if not layers:
        require(
            state_kind == "linear-byte-composition",
            "empty ordered writer state is not linear",
        )
    else:
        require(
            state_kind != "linear-byte-composition",
            "non-empty ordered writer state is linear",
        )

    byte_width = parsed_base[1]
    parsed_layers = []
    partition_count = 0
    guarded_count = 0
    symbolic_count = 0
    writer_sites = set()
    for layer in layers:
        ordinal = layer["ordinal"]
        kind = layer.get("kind")
        allowed_kinds = {"guarded-write", "pointer-partition"}
        if extended_schema:
            allowed_kinds.add("symbolic-region-write")
        require(
            kind in allowed_kinds,
            "ordered writer kind is invalid",
        )
        if kind == "pointer-partition":
            partition_count += 1
            require(
                all(
                    field not in layer
                    for field in (
                        "store_site",
                        "store_width",
                        "guard_site",
                        "lane_source_bytes",
                        "lane_store_when",
                        "region_base_site",
                        "region_extent",
                        "index_site",
                        "index_bits",
                        "base_offset",
                        "lane_cases",
                    )
                ),
                "pointer writer has guarded fields",
            )
            synthetic = dict(state)
            synthetic.pop("writer_layers", None)
            synthetic["state_kind"] = "finite-pointer-union"
            synthetic["pointer_partition"] = layer.get(
                "pointer_partition"
            )
            parsed_partition_state, _, _, _ = (
                parse_pointer_partition_state(
                    synthetic,
                    expected_exit,
                    exit_count,
                    guarded_schema=False,
                    require_base_store=False,
                )
            )
            require(
                parsed_partition_state[0] == 1,
                "ordered pointer writer is not partitioned",
            )
            _, parsed_partition = parsed_partition_state[1]
            store_site = parsed_partition[0]
            require(
                store_site not in writer_sites,
                "ordered writer store site is repeated",
            )
            writer_sites.add(store_site)
            parsed_layers.append(
                (ordinal, 1, parsed_partition)
            )
            continue

        if kind == "symbolic-region-write":
            symbolic_count += 1
            require(
                all(
                    field not in layer
                    for field in (
                        "pointer_partition",
                        "guard_site",
                        "lane_source_bytes",
                        "lane_store_when",
                    )
                ),
                "symbolic region writer has incompatible fields",
            )
            store_site = unsigned_text(
                layer.get("store_site"),
                "symbolic region store site",
            )
            require(
                store_site > 0 and store_site not in writer_sites,
                "symbolic region store site is invalid",
            )
            writer_sites.add(store_site)
            store_width = integer(
                layer.get("store_width"),
                "symbolic region store width",
                1,
                8,
            )
            region_base_site = unsigned_text(
                layer.get("region_base_site"),
                "symbolic region base site",
            )
            region_extent = unsigned_text(
                layer.get("region_extent"),
                "symbolic region extent",
            )
            index_site = unsigned_text(
                layer.get("index_site"),
                "symbolic region index site",
            )
            index_bits = integer(
                layer.get("index_bits"),
                "symbolic region index bits",
                1,
                64,
            )
            base_offset = integer(
                layer.get("base_offset"),
                "symbolic region base offset",
                -(1 << 63),
                (1 << 63) - 1,
            )
            require(
                region_base_site > 0
                and index_site > 0
                and 1 <= region_extent <= (1 << 32)
                and region_extent >= byte_width
                and 0 <= base_offset <= region_extent,
                "symbolic region identity is invalid",
            )
            raw_lane_cases = layer.get("lane_cases")
            require(
                isinstance(raw_lane_cases, list)
                and len(raw_lane_cases) == byte_width,
                "symbolic region lane count is invalid",
            )
            lane_cases = canonical_records(
                raw_lane_cases,
                "symbolic region lanes",
                byte_width,
            )
            parsed_lane_cases = []
            has_effect = False
            signed_min = -(1 << (index_bits - 1))
            signed_max = (1 << (index_bits - 1)) - 1
            for lane_record in lane_cases:
                raw_cases = lane_record.get("cases")
                require(
                    isinstance(raw_cases, list)
                    and len(raw_cases) <= store_width,
                    "symbolic region case count is invalid",
                )
                cases = canonical_records(
                    raw_cases,
                    "symbolic region cases",
                    len(raw_cases),
                )
                parsed_cases = []
                seen_indices = set()
                previous_index = None
                previous_source = None
                for case in cases:
                    index_value = integer(
                        case.get("index_value"),
                        "symbolic region index value",
                        signed_min,
                        signed_max,
                    )
                    source_byte = integer(
                        case.get("source_byte"),
                        "symbolic region source byte",
                        0,
                        store_width - 1,
                    )
                    require(
                        index_value not in seen_indices,
                        "symbolic region index case is repeated",
                    )
                    if previous_index is not None:
                        require(
                            index_value == previous_index - 1
                            and source_byte == previous_source + 1,
                            "symbolic region cases are not canonical",
                        )
                    seen_indices.add(index_value)
                    previous_index = index_value
                    previous_source = source_byte
                    parsed_cases.append(
                        (index_value, source_byte)
                    )
                    has_effect = True
                parsed_lane_cases.append(parsed_cases)
            require(
                has_effect,
                "symbolic region writer has no overlap cases",
            )
            parsed_layers.append(
                (
                    ordinal,
                    2,
                    store_site,
                    store_width,
                    region_base_site,
                    region_extent,
                    index_site,
                    index_bits,
                    base_offset,
                    parsed_lane_cases,
                )
            )
            continue

        guarded_count += 1
        require(
            all(
                field not in layer
                for field in (
                    "pointer_partition",
                    "region_base_site",
                    "region_extent",
                    "index_site",
                    "index_bits",
                    "base_offset",
                    "lane_cases",
                )
            ),
            "guarded writer has a pointer partition",
        )
        store_site = unsigned_text(
            layer.get("store_site"), "ordered writer store site"
        )
        require(
            store_site > 0 and store_site not in writer_sites,
            "ordered writer store site is invalid",
        )
        writer_sites.add(store_site)
        store_width = integer(
            layer.get("store_width"),
            "ordered writer store width",
            1,
            8,
        )
        guard_site = unsigned_text(
            layer.get("guard_site"), "ordered writer guard site"
        )
        require(guard_site > 0, "ordered writer guard site is zero")
        raw_sources = layer.get("lane_source_bytes")
        raw_polarities = layer.get("lane_store_when")
        require(
            isinstance(raw_sources, list)
            and isinstance(raw_polarities, list)
            and len(raw_sources) == byte_width
            and len(raw_polarities) == byte_width,
            "ordered guarded writer lane shape is invalid",
        )
        lane_records = []
        has_effect = False
        for source, polarity in zip(raw_sources, raw_polarities):
            source_byte = integer(
                source,
                "ordered writer source byte",
                -1,
                store_width - 1,
            )
            require(
                polarity in ("none", "false", "true"),
                "ordered writer polarity is invalid",
            )
            require(
                (source_byte < 0) == (polarity == "none"),
                "ordered writer fallback polarity is inconsistent",
            )
            polarity_tag = (
                -1
                if polarity == "none"
                else (1 if polarity == "true" else 0)
            )
            has_effect |= source_byte >= 0
            lane_records.append((source_byte, polarity_tag))
        require(has_effect, "ordered guarded writer has no effect")
        parsed_layers.append(
            (
                ordinal,
                0,
                store_site,
                store_width,
                guard_site,
                lane_records,
            )
        )

    kind_limit = 8 if extended_schema else 2
    require(
        partition_count <= kind_limit
        and guarded_count <= kind_limit
        and symbolic_count <= kind_limit,
        "ordered writer kind budget is exceeded",
    )
    legacy_pointer_priority = (
        len(parsed_layers) == 2
        and [layer[1] for layer in parsed_layers] == [1, 0]
    )
    requires_legacy_graph = (
        partition_count > 0
        and not (len(parsed_layers) == 1 and partition_count == 1)
        and not legacy_pointer_priority
    )
    requires_extended_graph = (
        symbolic_count > 0
        or len(parsed_layers) > 4
        or partition_count > 2
        or guarded_count > 2
    )
    require(
        (
            state_kind == "symbolic-region-writer-graph"
        )
        == requires_extended_graph,
        "symbolic region writer state kind does not match its graph",
    )
    requires_graph = (
        requires_extended_graph
        if extended_schema
        else requires_legacy_graph
    )
    require(
        base_has_store or bool(parsed_layers),
        "ordered writer state has no store source",
    )
    return (
        (parsed_base, parsed_layers),
        has_live,
        base_has_store or bool(parsed_layers),
        requires_graph,
    )


def mix_plain_byte_state(fingerprint, state):
    exit_ordinal, byte_width, endian_tag, skipped, lanes = state
    fingerprint = mix_integer(fingerprint, exit_ordinal)
    fingerprint = mix_integer(fingerprint, byte_width)
    fingerprint = mix_integer(fingerprint, endian_tag)
    fingerprint = mix_integer(fingerprint, len(skipped))
    for site in skipped:
        fingerprint = mix_integer(fingerprint, site)
    fingerprint = mix_integer(fingerprint, len(lanes))
    for (
        lane_ordinal,
        kind_tag,
        source_site,
        source_byte,
        source_width,
        guarded_source,
    ) in lanes:
        require(
            guarded_source is None,
            "plain byte state has a guarded source",
        )
        fingerprint = mix_integer(fingerprint, lane_ordinal)
        fingerprint = mix_integer(fingerprint, kind_tag)
        fingerprint = mix_integer(fingerprint, source_site)
        fingerprint = mix_integer(fingerprint, source_byte)
        fingerprint = mix_integer(fingerprint, source_width)
    return fingerprint


def mix_guarded_byte_state(fingerprint, state):
    exit_ordinal, byte_width, endian_tag, skipped, lanes = state
    fingerprint = mix_integer(fingerprint, exit_ordinal)
    fingerprint = mix_integer(fingerprint, byte_width)
    fingerprint = mix_integer(fingerprint, endian_tag)
    fingerprint = mix_integer(fingerprint, len(skipped))
    for site in skipped:
        fingerprint = mix_integer(fingerprint, site)
    fingerprint = mix_integer(fingerprint, len(lanes))
    for (
        lane_ordinal,
        kind_tag,
        source_site,
        source_byte,
        source_width,
        guarded_source,
    ) in lanes:
        fingerprint = mix_integer(fingerprint, lane_ordinal)
        fingerprint = mix_integer(fingerprint, kind_tag)
        fingerprint = mix_integer(fingerprint, source_site)
        fingerprint = mix_integer(fingerprint, source_byte)
        fingerprint = mix_integer(fingerprint, source_width)
        fingerprint = mix_integer(
            fingerprint, 1 if guarded_source is not None else 0
        )
        if guarded_source is not None:
            (
                guard_site,
                store_when_tag,
                guarded_site,
                guarded_byte,
                guarded_width,
            ) = guarded_source
            fingerprint = mix_integer(fingerprint, guard_site)
            fingerprint = mix_integer(fingerprint, store_when_tag)
            fingerprint = mix_integer(fingerprint, guarded_site)
            fingerprint = mix_integer(fingerprint, guarded_byte)
            fingerprint = mix_integer(fingerprint, guarded_width)
    return fingerprint


def mix_pointer_partition_record(fingerprint, partition):
    store_site, store_width, nodes, leaves = partition
    fingerprint = mix_integer(fingerprint, store_site)
    fingerprint = mix_integer(fingerprint, store_width)
    fingerprint = mix_integer(fingerprint, len(nodes))
    fingerprint = mix_integer(fingerprint, len(leaves))
    for (
        select_site,
        guard_site,
        true_kind,
        true_index,
        false_kind,
        false_index,
    ) in nodes:
        fingerprint = mix_integer(fingerprint, select_site)
        fingerprint = mix_integer(fingerprint, guard_site)
        fingerprint = mix_integer(fingerprint, true_kind)
        fingerprint = mix_integer(fingerprint, true_index)
        fingerprint = mix_integer(fingerprint, false_kind)
        fingerprint = mix_integer(fingerprint, false_index)
    for leaf_ordinal, sources in enumerate(leaves):
        fingerprint = mix_integer(fingerprint, leaf_ordinal)
        for source_byte in sources:
            fingerprint = mix_integer(fingerprint, source_byte + 1)
    return fingerprint


def mix_symbolic_region_writer_record(fingerprint, writer):
    (
        store_site,
        store_width,
        region_base_site,
        region_extent,
        index_site,
        index_bits,
        base_offset,
        lane_cases,
    ) = writer
    fingerprint = mix_integer(fingerprint, store_site)
    fingerprint = mix_integer(fingerprint, store_width)
    fingerprint = mix_integer(fingerprint, region_base_site)
    fingerprint = mix_integer(fingerprint, region_extent)
    fingerprint = mix_integer(fingerprint, index_site)
    fingerprint = mix_integer(fingerprint, index_bits)
    fingerprint = mix_integer(fingerprint, base_offset & MASK64)
    fingerprint = mix_integer(fingerprint, len(lane_cases))
    for cases in lane_cases:
        fingerprint = mix_integer(fingerprint, len(cases))
        for index_value, source_byte in cases:
            fingerprint = mix_integer(
                fingerprint, index_value & MASK64
            )
            fingerprint = mix_integer(fingerprint, source_byte)
    return fingerprint


def verify_record(record):
    require(isinstance(record, dict), "record is not an object")
    require(record.get("schema") == SCHEMA, "unknown schema")
    module = record.get("module")
    function = record.get("function")
    require(isinstance(module, str) and module, "module is missing")
    require(isinstance(function, str) and function, "function is missing")
    controller_site = unsigned_text(
        record.get("controller_site"), "controller site"
    )
    exit_count = integer(record.get("exit_count"), "exit count", 2, 8)
    destination_count = integer(
        record.get("destination_count"), "destination count", 2, exit_count
    )
    scalar_slot_count = integer(
        record.get("scalar_slot_count"), "scalar slot count", 0, 8
    )
    memory_slot_count = integer(
        record.get("memory_slot_count"), "memory slot count", 0, 4
    )
    block_count = integer(record.get("block_count"), "block count", 0, 32)
    path_count = integer(record.get("path_count"), "path count", 1, 64)
    exits = canonical_records(record.get("exits"), "exits", exit_count)
    destination_exits = [[] for _ in range(destination_count)]
    seen_edges = set()
    parsed_exits = []
    for item in exits:
        ordinal = item["ordinal"]
        source_site = unsigned_text(item.get("source_site"), "source site")
        successor_index = integer(
            item.get("successor_index"), "successor index", 0, 1
        )
        destination = integer(
            item.get("destination"),
            "exit destination",
            0,
            destination_count - 1,
        )
        edge = (source_site, successor_index)
        require(edge not in seen_edges, "duplicate original exit edge")
        seen_edges.add(edge)
        destination_exits[destination].append(ordinal)
        parsed_exits.append(
            (ordinal, source_site, successor_index, destination)
        )
    require(
        all(destination_exits),
        "a continuation destination has no logical exit",
    )

    destinations = canonical_records(
        record.get("destinations"), "destinations", destination_count
    )
    for item in destinations:
        ordinal = item["ordinal"]
        listed = item.get("exits")
        require(
            isinstance(listed, list)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in listed
            ),
            "destination exits are not integers",
        )
        require(
            listed == destination_exits[ordinal],
            "destination exit partition mismatch",
        )

    scalar_slots = canonical_records(
        record.get("scalar_slots"),
        "scalar slots",
        scalar_slot_count,
    )
    parsed_scalar_slots = []
    seen_phi_sites = set()
    for item in scalar_slots:
        destination = integer(
            item.get("destination"),
            "scalar destination",
            0,
            destination_count - 1,
        )
        phi_site = unsigned_text(
            item.get("original_phi_site"), "original PHI site"
        )
        require(phi_site not in seen_phi_sites, "duplicate scalar PHI site")
        seen_phi_sites.add(phi_site)
        parsed_scalar_slots.append((item["ordinal"], destination, phi_site))

    memory_slots = canonical_records(
        record.get("memory_slots"),
        "memory slots",
        memory_slot_count,
    )
    parsed_memory_slots = []
    seen_load_sites = set()
    has_live_on_entry = False
    has_nested_memory_phi = False
    has_byte_lane_composition = False
    has_guarded_byte_lane_composition = False
    has_cyclic_byte_lane_composition = False
    has_pointer_partition = False
    has_guarded_write_priority = False
    has_conditional_cyclic_byte_lane_composition = False
    has_multi_latch_cyclic_byte_lane_composition = False
    has_conditional_multi_latch_cyclic_byte_lane_composition = False
    has_bounded_multi_latch_cyclic_byte_lane_composition = False
    has_nested_predicate_cyclic_byte_lane_composition = False
    has_pointer_partition_priority = False
    has_ordered_writer_graph = False
    has_symbolic_region_writer_graph = False
    has_symbolic_region_cyclic_byte_lane_composition = False
    has_ordered_symbolic_region_cyclic_byte_lane_composition = False
    has_symbolic_region_multi_latch_cyclic_byte_lane_composition = False
    for item in memory_slots:
        destination = integer(
            item.get("destination"),
            "memory destination",
            0,
            destination_count - 1,
        )
        load_site = unsigned_text(item.get("load_site"), "load site")
        require(load_site not in seen_load_sites, "duplicate memory load site")
        seen_load_sites.add(load_site)
        state_schema = item.get("state_schema")
        extended = state_schema is not None
        if extended:
            require(
                state_schema
                in (
                    MEMORY_INITIAL_SCHEMA,
                    MEMORY_NESTED_SCHEMA,
                    MEMORY_BYTE_LANE_SCHEMA,
                    MEMORY_GUARDED_BYTE_LANE_SCHEMA,
                    MEMORY_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_POINTER_PARTITION_SCHEMA,
                    MEMORY_GUARDED_PRIORITY_SCHEMA,
                    MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA,
                    MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_ORDERED_WRITER_GRAPH_SCHEMA,
                    MEMORY_SYMBOLIC_REGION_WRITER_GRAPH_SCHEMA,
                    MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
                ),
                "unknown memory state schema",
            )
        nested = state_schema == MEMORY_NESTED_SCHEMA
        byte_lane = state_schema == MEMORY_BYTE_LANE_SCHEMA
        guarded_byte_lane = (
            state_schema == MEMORY_GUARDED_BYTE_LANE_SCHEMA
        )
        cyclic_byte_lane = (
            state_schema
            in (
                MEMORY_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
            )
        )
        conditional_cyclic_byte_lane = (
            state_schema
            == MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA
        )
        symbolic_region_cyclic_byte_lane = (
            state_schema
            in (
                MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
            )
        )
        ordered_symbolic_region_cyclic_byte_lane = (
            state_schema
            == MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA
        )
        symbolic_region_multi_latch_cyclic_byte_lane = (
            state_schema
            == MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA
        )
        multi_latch_cyclic_byte_lane = (
            state_schema
            in (
                MEMORY_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA,
                MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
            )
        )
        conditional_multi_latch_cyclic_byte_lane = (
            state_schema
            == MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA
        )
        bounded_multi_latch_cyclic_byte_lane = (
            state_schema
            == MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA
        )
        nested_predicate_cyclic_byte_lane = (
            state_schema
            == MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA
        )
        pointer_partition = (
            state_schema
            in (
                MEMORY_POINTER_PARTITION_SCHEMA,
                MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA,
            )
        )
        pointer_partition_priority = (
            state_schema
            == MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA
        )
        guarded_priority = (
            state_schema == MEMORY_GUARDED_PRIORITY_SCHEMA
        )
        ordered_writer_graph = (
            state_schema
            in (
                MEMORY_ORDERED_WRITER_GRAPH_SCHEMA,
                MEMORY_SYMBOLIC_REGION_WRITER_GRAPH_SCHEMA,
            )
        )
        symbolic_region_writer_graph = (
            state_schema
            == MEMORY_SYMBOLIC_REGION_WRITER_GRAPH_SCHEMA
        )
        byte_lane = (
            byte_lane
            or guarded_byte_lane
            or cyclic_byte_lane
            or pointer_partition
            or guarded_priority
            or ordered_writer_graph
        )
        states = item.get("exit_states")
        require(isinstance(states, list) and states, "memory states are missing")
        expected_exits = destination_exits[destination]
        require(
            len(states) == len(expected_exits),
            "memory relevant-exit count mismatch",
        )
        parsed_states = []
        slot_has_live_on_entry = False
        slot_has_store = False
        slot_phi_count = 0
        slot_has_guarded_byte_lane = False
        slot_has_cyclic_byte_lane = False
        slot_has_conditional_cyclic_byte_lane = False
        slot_has_symbolic_region_cyclic_byte_lane = False
        slot_has_ordered_symbolic_region_cyclic_byte_lane = False
        slot_has_symbolic_region_multi_latch_cyclic_byte_lane = False
        slot_has_multi_latch_cyclic_byte_lane = False
        slot_has_conditional_multi_latch_cyclic_byte_lane = False
        slot_has_bounded_multi_latch_cyclic_byte_lane = False
        slot_has_nested_predicate_cyclic_byte_lane = False
        slot_has_pointer_partition = False
        slot_has_pointer_partition_priority = False
        slot_has_guarded_priority = False
        slot_has_ordered_writer_graph = False
        for expected_exit, state in zip(expected_exits, states):
            require(isinstance(state, dict), "memory state is not an object")
            if ordered_writer_graph:
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                    state_requires_graph,
                ) = parse_ordered_writer_graph_state(
                    state,
                    expected_exit,
                    exit_count,
                    extended_schema=symbolic_region_writer_graph,
                )
                parsed_states.append(parsed_state)
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                slot_has_ordered_writer_graph |= state_requires_graph
                continue
            if guarded_priority:
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                    state_has_guarded,
                ) = parse_byte_lane_state(
                    state,
                    expected_exit,
                    exit_count,
                    guarded_schema=True,
                    priority_schema=True,
                )
                parsed_states.append(parsed_state)
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                slot_has_guarded_priority |= state_has_guarded
                continue
            if pointer_partition:
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                    state_has_guarded,
                ) = parse_pointer_partition_state(
                    state,
                    expected_exit,
                    exit_count,
                    guarded_schema=pointer_partition_priority,
                )
                parsed_states.append(parsed_state)
                slot_has_pointer_partition |= parsed_state[0] == 1
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                slot_has_pointer_partition_priority |= (
                    parsed_state[0] == 1 and state_has_guarded
                )
                continue
            if cyclic_byte_lane:
                if multi_latch_cyclic_byte_lane:
                    (
                        parsed_state,
                        state_has_live,
                        state_has_store,
                    ) = parse_multi_latch_cyclic_byte_lane_state(
                        state,
                        expected_exit,
                        exit_count,
                        conditional_schema=(
                            conditional_multi_latch_cyclic_byte_lane
                        ),
                        bounded_schema=(
                            bounded_multi_latch_cyclic_byte_lane
                        ),
                        nested_predicate_schema=(
                            nested_predicate_cyclic_byte_lane
                        ),
                        symbolic_region_schema=(
                            symbolic_region_multi_latch_cyclic_byte_lane
                        ),
                    )
                    parsed_states.append(parsed_state)
                    slot_has_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                    )
                    slot_has_multi_latch_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                    )
                    slot_has_symbolic_region_multi_latch_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                        and symbolic_region_multi_latch_cyclic_byte_lane
                    )
                    slot_has_conditional_multi_latch_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                        and conditional_multi_latch_cyclic_byte_lane
                    )
                    slot_has_bounded_multi_latch_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                        and bounded_multi_latch_cyclic_byte_lane
                        and len(parsed_state[1][-1]) > 2
                    )
                    slot_has_nested_predicate_cyclic_byte_lane |= (
                        parsed_state[0] == 1
                        and nested_predicate_cyclic_byte_lane
                        and any(
                            len(transfer) == 4
                            and transfer[1] == 2
                            for transfer in parsed_state[1][-1]
                        )
                    )
                    slot_has_live_on_entry |= state_has_live
                    has_live_on_entry |= state_has_live
                    slot_has_store |= state_has_store
                    continue
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                ) = parse_cyclic_byte_lane_state(
                    state,
                    expected_exit,
                    exit_count,
                    conditional_schema=conditional_cyclic_byte_lane,
                    symbolic_region_schema=(
                        symbolic_region_cyclic_byte_lane
                        and not ordered_symbolic_region_cyclic_byte_lane
                    ),
                    ordered_symbolic_region_schema=(
                        ordered_symbolic_region_cyclic_byte_lane
                    ),
                )
                parsed_states.append(parsed_state)
                slot_has_cyclic_byte_lane |= parsed_state[0] == 1
                slot_has_conditional_cyclic_byte_lane |= (
                    parsed_state[0] == 1
                    and conditional_cyclic_byte_lane
                )
                slot_has_symbolic_region_cyclic_byte_lane |= (
                    parsed_state[0] == 1
                    and symbolic_region_cyclic_byte_lane
                )
                slot_has_ordered_symbolic_region_cyclic_byte_lane |= (
                    parsed_state[0] == 1
                    and ordered_symbolic_region_cyclic_byte_lane
                )
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                continue
            if byte_lane:
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                    state_has_guarded,
                ) = parse_byte_lane_state(
                    state,
                    expected_exit,
                    exit_count,
                    guarded_schema=guarded_byte_lane,
                )
                parsed_states.append(parsed_state)
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                slot_has_guarded_byte_lane |= state_has_guarded
                continue
            if nested:
                (
                    parsed_state,
                    state_has_live,
                    state_has_store,
                    state_phi_count,
                ) = parse_nested_state(state, expected_exit, exit_count)
                parsed_states.append(parsed_state)
                slot_has_live_on_entry |= state_has_live
                has_live_on_entry |= state_has_live
                slot_has_store |= state_has_store
                slot_phi_count += state_phi_count
                continue
            exit_ordinal = integer(
                state.get("exit"), "memory exit", 0, exit_count - 1
            )
            require(
                exit_ordinal == expected_exit,
                "memory state exit order mismatch",
            )
            if extended:
                state_kind = state.get("state_kind")
                require(
                    state_kind in ("store", "live-on-entry"),
                    "unknown memory state kind",
                )
            else:
                state_kind = "store"
                require(
                    "state_kind" not in state,
                    "legacy memory state has a kind",
                )
            if state_kind == "store":
                source_site = unsigned_text(
                    state.get("store_site"), "store site"
                )
                kind_tag = 0
                slot_has_store = True
            else:
                require(
                    "store_site" not in state,
                    "live-on-entry state has a store",
                )
                source_site = 0
                kind_tag = 1
                has_live_on_entry = True
                slot_has_live_on_entry = True
            skipped = state.get("skipped_nomod_sites")
            require(
                isinstance(skipped, list) and len(skipped) <= 8,
                "NoMod chain is invalid",
            )
            parsed_skipped = [
                unsigned_text(site, "NoMod site") for site in skipped
            ]
            require(
                len(set(parsed_skipped)) == len(parsed_skipped),
                "NoMod chain repeats a site",
            )
            parsed_states.append(
                (exit_ordinal, kind_tag, source_site, parsed_skipped)
            )
        require(
            nested
            or byte_lane
            or extended == slot_has_live_on_entry,
            "memory state schema classification mismatch",
        )
        require(slot_has_store, "memory slot has no store state")
        if guarded_byte_lane:
            require(
                slot_has_guarded_byte_lane,
                "guarded byte schema has no guarded source",
            )
        if cyclic_byte_lane:
            require(
                slot_has_cyclic_byte_lane,
                "cyclic byte schema has no cyclic state",
            )
        if conditional_cyclic_byte_lane:
            require(
                slot_has_conditional_cyclic_byte_lane,
                "conditional cyclic schema has no conditional state",
            )
        if symbolic_region_cyclic_byte_lane:
            require(
                slot_has_symbolic_region_cyclic_byte_lane,
                "symbolic-region cyclic schema has no symbolic cycle",
            )
        if ordered_symbolic_region_cyclic_byte_lane:
            require(
                slot_has_ordered_symbolic_region_cyclic_byte_lane,
                "ordered symbolic-region schema has no ordered cycle",
            )
        if symbolic_region_multi_latch_cyclic_byte_lane:
            require(
                slot_has_symbolic_region_multi_latch_cyclic_byte_lane,
                "symbolic-region multi-latch schema has no symbolic cycle",
            )
        if multi_latch_cyclic_byte_lane:
            require(
                slot_has_multi_latch_cyclic_byte_lane,
                "multi-latch schema has no multi-latch state",
            )
        if conditional_multi_latch_cyclic_byte_lane:
            require(
                slot_has_conditional_multi_latch_cyclic_byte_lane,
                "conditional multi-latch schema has no cycle state",
            )
        if bounded_multi_latch_cyclic_byte_lane:
            require(
                slot_has_bounded_multi_latch_cyclic_byte_lane,
                "bounded multi-latch schema has no bounded cycle state",
            )
        if nested_predicate_cyclic_byte_lane:
            require(
                slot_has_nested_predicate_cyclic_byte_lane,
                "nested-predicate schema has no predicate tree",
            )
        if pointer_partition:
            require(
                slot_has_pointer_partition,
                "pointer-union schema has no partitioned state",
            )
        if pointer_partition_priority:
            require(
                slot_has_pointer_partition_priority,
                "pointer-union priority schema has no guarded base",
            )
        if guarded_priority:
            require(
                slot_has_guarded_priority,
                "guarded priority schema has no priority state",
            )
        if ordered_writer_graph:
            require(
                slot_has_ordered_writer_graph,
                "ordered writer schema has no non-legacy graph",
            )
        if nested:
            require(slot_phi_count > 0, "nested schema has no MemoryPhi")
        has_nested_memory_phi |= nested
        has_byte_lane_composition |= byte_lane
        has_guarded_byte_lane_composition |= guarded_byte_lane
        has_cyclic_byte_lane_composition |= cyclic_byte_lane
        has_conditional_cyclic_byte_lane_composition |= (
            conditional_cyclic_byte_lane
        )
        has_symbolic_region_cyclic_byte_lane_composition |= (
            symbolic_region_cyclic_byte_lane
        )
        has_ordered_symbolic_region_cyclic_byte_lane_composition |= (
            ordered_symbolic_region_cyclic_byte_lane
        )
        has_symbolic_region_multi_latch_cyclic_byte_lane_composition |= (
            symbolic_region_multi_latch_cyclic_byte_lane
        )
        has_multi_latch_cyclic_byte_lane_composition |= (
            multi_latch_cyclic_byte_lane
        )
        has_conditional_multi_latch_cyclic_byte_lane_composition |= (
            conditional_multi_latch_cyclic_byte_lane
        )
        has_bounded_multi_latch_cyclic_byte_lane_composition |= (
            bounded_multi_latch_cyclic_byte_lane
        )
        has_nested_predicate_cyclic_byte_lane_composition |= (
            nested_predicate_cyclic_byte_lane
        )
        has_pointer_partition |= pointer_partition
        has_pointer_partition_priority |= pointer_partition_priority
        has_guarded_write_priority |= guarded_priority
        has_ordered_writer_graph |= ordered_writer_graph
        has_symbolic_region_writer_graph |= (
            symbolic_region_writer_graph
        )
        parsed_memory_slots.append(
            (
                item["ordinal"],
                destination,
                load_site,
                state_schema,
                parsed_states,
            )
        )
    expected_analysis = (
        "structural-only-v1"
        if memory_slot_count == 0
        else (
            "llvm-memoryssa-aa-ordered-symbolic-region-cyclic-byte-lane-revalidated-v19"
            if has_ordered_symbolic_region_cyclic_byte_lane_composition
            else (
                "llvm-memoryssa-aa-symbolic-region-multi-latch-cyclic-byte-lane-revalidated-v18"
                if has_symbolic_region_multi_latch_cyclic_byte_lane_composition
                else (
                    "llvm-memoryssa-aa-symbolic-region-cyclic-byte-lane-revalidated-v17"
                    if has_symbolic_region_cyclic_byte_lane_composition
                    else (
                        "llvm-memoryssa-aa-symbolic-region-writer-graph-revalidated-v16"
                        if has_symbolic_region_writer_graph
                        else (
                            "llvm-memoryssa-aa-ordered-writer-graph-revalidated-v15"
                            if has_ordered_writer_graph
                            else (
                                "llvm-memoryssa-aa-guarded-write-priority-revalidated-v8"
                                if has_guarded_write_priority
                                else (
                                    "llvm-memoryssa-aa-pointer-union-priority-revalidated-v11"
                                    if has_pointer_partition_priority
                                    else (
                                        "llvm-memoryssa-aa-finite-pointer-union-revalidated-v7"
                                        if has_pointer_partition
                                        else (
                                            "llvm-memoryssa-aa-nested-predicate-cyclic-byte-lane-revalidated-v14"
                                            if has_nested_predicate_cyclic_byte_lane_composition
                                            else (
                                                "llvm-memoryssa-aa-bounded-multi-latch-cyclic-byte-lane-revalidated-v13"
                                                if has_bounded_multi_latch_cyclic_byte_lane_composition
                                                else (
                                                    "llvm-memoryssa-aa-conditional-multi-latch-cyclic-byte-lane-revalidated-v12"
                                                    if has_conditional_multi_latch_cyclic_byte_lane_composition
                                                    else (
                                                        "llvm-memoryssa-aa-multi-latch-cyclic-byte-lane-revalidated-v10"
                                                        if has_multi_latch_cyclic_byte_lane_composition
                                                        else (
                                                            "llvm-memoryssa-aa-conditional-cyclic-byte-lane-revalidated-v9"
                                                            if has_conditional_cyclic_byte_lane_composition
                                                            else (
                                                                "llvm-memoryssa-aa-cyclic-byte-lane-revalidated-v6"
                                                                if has_cyclic_byte_lane_composition
                                                                else (
                                                                    "llvm-memoryssa-aa-guarded-byte-lane-revalidated-v5"
                                                                    if has_guarded_byte_lane_composition
                                                                    else (
                                                                        "llvm-memoryssa-aa-byte-lane-revalidated-v4"
                                                                        if has_byte_lane_composition
                                                                        else (
                                                                            "llvm-memoryssa-aa-nested-phi-revalidated-v3"
                                                                            if has_nested_memory_phi
                                                                            else (
                                                                                "llvm-memoryssa-aa-live-on-entry-revalidated-v2"
                                                                                if has_live_on_entry
                                                                                else "llvm-memoryssa-aa-revalidated-v1"
                                                                            )
                                                                        )
                                                                    )
                                                                )
                                                            )
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
    )
    require(
        record.get("analysis") == expected_analysis,
        "analysis classification mismatch",
    )

    fingerprint = mix_text(FNV_OFFSET, SCHEMA)
    fingerprint = mix_text(fingerprint, module)
    fingerprint = mix_text(fingerprint, function)
    fingerprint = mix_integer(fingerprint, controller_site)
    fingerprint = mix_integer(fingerprint, exit_count)
    fingerprint = mix_integer(fingerprint, destination_count)
    fingerprint = mix_integer(fingerprint, scalar_slot_count)
    fingerprint = mix_integer(fingerprint, memory_slot_count)
    fingerprint = mix_integer(fingerprint, block_count)
    fingerprint = mix_integer(fingerprint, path_count)
    for ordinal, source_site, successor_index, destination in parsed_exits:
        fingerprint = mix_integer(fingerprint, ordinal)
        fingerprint = mix_integer(fingerprint, source_site)
        fingerprint = mix_integer(fingerprint, successor_index)
        fingerprint = mix_integer(fingerprint, destination)
    for ordinal, destination, phi_site in parsed_scalar_slots:
        fingerprint = mix_integer(fingerprint, ordinal)
        fingerprint = mix_integer(fingerprint, destination)
        fingerprint = mix_integer(fingerprint, phi_site)
    for (
        ordinal,
        destination,
        load_site,
        state_schema,
        states,
    ) in parsed_memory_slots:
        fingerprint = mix_integer(fingerprint, ordinal)
        fingerprint = mix_integer(fingerprint, destination)
        fingerprint = mix_integer(fingerprint, load_site)
        if state_schema is not None:
            fingerprint = mix_text(fingerprint, state_schema)
        if state_schema == MEMORY_NESTED_SCHEMA:
            for exit_ordinal, root_node, nodes in states:
                fingerprint = mix_integer(fingerprint, exit_ordinal)
                fingerprint = mix_integer(fingerprint, len(nodes))
                fingerprint = mix_integer(fingerprint, root_node)
                for kind_tag, source_site, skipped, incoming in nodes:
                    fingerprint = mix_integer(fingerprint, kind_tag)
                    fingerprint = mix_integer(fingerprint, source_site)
                    fingerprint = mix_integer(fingerprint, len(skipped))
                    for site in skipped:
                        fingerprint = mix_integer(fingerprint, site)
                    fingerprint = mix_integer(fingerprint, len(incoming))
                    for block_site, child in incoming:
                        fingerprint = mix_integer(fingerprint, block_site)
                        fingerprint = mix_integer(fingerprint, child)
            continue
        if state_schema in (
            MEMORY_ORDERED_WRITER_GRAPH_SCHEMA,
            MEMORY_SYMBOLIC_REGION_WRITER_GRAPH_SCHEMA,
        ):
            for base_state, layers in states:
                fingerprint = mix_plain_byte_state(
                    fingerprint, base_state
                )
                fingerprint = mix_integer(fingerprint, len(layers))
                for layer in layers:
                    ordinal = layer[0]
                    kind_tag = layer[1]
                    fingerprint = mix_integer(fingerprint, ordinal)
                    fingerprint = mix_integer(fingerprint, kind_tag)
                    if kind_tag == 1:
                        fingerprint = mix_pointer_partition_record(
                            fingerprint, layer[2]
                        )
                        continue
                    if kind_tag == 2:
                        (
                            _,
                            _,
                            store_site,
                            store_width,
                            region_base_site,
                            region_extent,
                            index_site,
                            index_bits,
                            base_offset,
                            lane_cases,
                        ) = layer
                        fingerprint = mix_integer(
                            fingerprint, store_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, store_width
                        )
                        fingerprint = mix_integer(
                            fingerprint, region_base_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, region_extent
                        )
                        fingerprint = mix_integer(
                            fingerprint, index_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, index_bits
                        )
                        fingerprint = mix_integer(
                            fingerprint, base_offset & MASK64
                        )
                        fingerprint = mix_integer(
                            fingerprint, len(lane_cases)
                        )
                        for cases in lane_cases:
                            fingerprint = mix_integer(
                                fingerprint, len(cases)
                            )
                            for index_value, source_byte in cases:
                                fingerprint = mix_integer(
                                    fingerprint,
                                    index_value & MASK64,
                                )
                                fingerprint = mix_integer(
                                    fingerprint, source_byte
                                )
                        continue
                    (
                        _,
                        _,
                        store_site,
                        store_width,
                        guard_site,
                        lane_records,
                    ) = layer
                    fingerprint = mix_integer(
                        fingerprint, store_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, store_width
                    )
                    fingerprint = mix_integer(
                        fingerprint, guard_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, len(lane_records)
                    )
                    for source_byte, polarity in lane_records:
                        fingerprint = mix_integer(
                            fingerprint, source_byte + 1
                        )
                        fingerprint = mix_integer(
                            fingerprint, polarity + 1
                        )
            continue
        if state_schema == MEMORY_GUARDED_PRIORITY_SCHEMA:
            for (
                exit_ordinal,
                byte_width,
                endian_tag,
                skipped,
                lanes,
            ) in states:
                fingerprint = mix_integer(fingerprint, exit_ordinal)
                fingerprint = mix_integer(fingerprint, byte_width)
                fingerprint = mix_integer(fingerprint, endian_tag)
                fingerprint = mix_integer(fingerprint, len(skipped))
                for site in skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(fingerprint, len(lanes))
                for (
                    lane_ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                    guarded_sources,
                ) in lanes:
                    fingerprint = mix_integer(
                        fingerprint, lane_ordinal
                    )
                    fingerprint = mix_integer(
                        fingerprint, kind_tag
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_byte
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_width
                    )
                    fingerprint = mix_integer(
                        fingerprint, len(guarded_sources)
                    )
                    for (
                        priority,
                        guard_site,
                        store_when_tag,
                        guarded_site,
                        guarded_byte,
                        guarded_width,
                    ) in guarded_sources:
                        fingerprint = mix_integer(
                            fingerprint, priority
                        )
                        fingerprint = mix_integer(
                            fingerprint, guard_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, store_when_tag
                        )
                        fingerprint = mix_integer(
                            fingerprint, guarded_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, guarded_byte
                        )
                        fingerprint = mix_integer(
                            fingerprint, guarded_width
                        )
            continue
        if state_schema in (
            MEMORY_POINTER_PARTITION_SCHEMA,
            MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA,
        ):
            guarded_partition = (
                state_schema
                == MEMORY_POINTER_PARTITION_PRIORITY_SCHEMA
            )
            for state_kind, state_payload in states:
                fingerprint = mix_integer(fingerprint, state_kind)
                if state_kind == 0:
                    fingerprint = (
                        mix_guarded_byte_state(
                            fingerprint, state_payload
                        )
                        if guarded_partition
                        else mix_plain_byte_state(
                            fingerprint, state_payload
                        )
                    )
                    continue
                base_state, partition = state_payload
                fingerprint = (
                    mix_guarded_byte_state(
                        fingerprint, base_state
                    )
                    if guarded_partition
                    else mix_plain_byte_state(
                        fingerprint, base_state
                    )
                )
                (
                    store_site,
                    store_width,
                    nodes,
                    leaves,
                ) = partition
                fingerprint = mix_integer(fingerprint, store_site)
                fingerprint = mix_integer(fingerprint, store_width)
                fingerprint = mix_integer(fingerprint, len(nodes))
                fingerprint = mix_integer(fingerprint, len(leaves))
                for (
                    select_site,
                    guard_site,
                    true_kind,
                    true_index,
                    false_kind,
                    false_index,
                ) in nodes:
                    fingerprint = mix_integer(
                        fingerprint, select_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, guard_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, true_kind
                    )
                    fingerprint = mix_integer(
                        fingerprint, true_index
                    )
                    fingerprint = mix_integer(
                        fingerprint, false_kind
                    )
                    fingerprint = mix_integer(
                        fingerprint, false_index
                    )
                for leaf_ordinal, sources in enumerate(leaves):
                    fingerprint = mix_integer(
                        fingerprint, leaf_ordinal
                    )
                    for source_byte in sources:
                        fingerprint = mix_integer(
                            fingerprint, source_byte + 1
                        )
            continue
        if state_schema in (
            MEMORY_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
        ):
            nested_predicate_schema = (
                state_schema
                == MEMORY_NESTED_PREDICATE_CYCLIC_BYTE_LANE_SCHEMA
            )
            symbolic_region_multi_latch_schema = (
                state_schema
                == MEMORY_SYMBOLIC_REGION_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA
            )
            conditional_multi_latch_schema = (
                state_schema
                in (
                    MEMORY_CONDITIONAL_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                    MEMORY_BOUNDED_MULTI_LATCH_CYCLIC_BYTE_LANE_SCHEMA,
                )
                or nested_predicate_schema
                or symbolic_region_multi_latch_schema
            )
            for state_kind, state_payload in states:
                fingerprint = mix_integer(fingerprint, state_kind)
                if state_kind == 0:
                    fingerprint = mix_plain_byte_state(
                        fingerprint, state_payload
                    )
                    continue
                (
                    exit_ordinal,
                    byte_width,
                    endian_tag,
                    skipped,
                    header_site,
                    entry_site,
                    entry_state,
                    transfers,
                ) = state_payload
                fingerprint = mix_integer(fingerprint, exit_ordinal)
                fingerprint = mix_integer(fingerprint, byte_width)
                fingerprint = mix_integer(fingerprint, endian_tag)
                fingerprint = mix_integer(fingerprint, len(skipped))
                for site in skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(fingerprint, header_site)
                fingerprint = mix_integer(fingerprint, entry_site)
                (
                    entry_exit,
                    entry_width,
                    entry_endian,
                    entry_skipped,
                    entry_lanes,
                ) = entry_state
                fingerprint = mix_integer(fingerprint, entry_exit)
                fingerprint = mix_integer(fingerprint, entry_width)
                fingerprint = mix_integer(fingerprint, entry_endian)
                fingerprint = mix_integer(
                    fingerprint, len(entry_skipped)
                )
                for site in entry_skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(
                    fingerprint, len(entry_lanes)
                )
                for (
                    lane_ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                    guarded_source,
                ) in entry_lanes:
                    require(
                        guarded_source is None,
                        "multi-latch entry state is guarded",
                    )
                    fingerprint = mix_integer(
                        fingerprint, lane_ordinal
                    )
                    fingerprint = mix_integer(
                        fingerprint, kind_tag
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_byte
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_width
                    )
                fingerprint = mix_integer(
                    fingerprint, len(transfers)
                )
                for transfer in transfers:
                    if (
                        nested_predicate_schema
                        and len(transfer) == 4
                        and transfer[1] == 2
                    ):
                        (
                            backedge_site,
                            transfer_kind,
                            nodes,
                            leaves,
                        ) = transfer
                        fingerprint = mix_integer(
                            fingerprint, backedge_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, transfer_kind
                        )
                        fingerprint = mix_integer(
                            fingerprint, len(nodes)
                        )
                        fingerprint = mix_integer(
                            fingerprint, len(leaves)
                        )
                        for (
                            branch_site,
                            guard_site,
                            true_kind,
                            true_index,
                            false_kind,
                            false_index,
                        ) in nodes:
                            fingerprint = mix_integer(
                                fingerprint, branch_site
                            )
                            fingerprint = mix_integer(
                                fingerprint, guard_site
                            )
                            fingerprint = mix_integer(
                                fingerprint, true_kind
                            )
                            fingerprint = mix_integer(
                                fingerprint, true_index
                            )
                            fingerprint = mix_integer(
                                fingerprint, false_kind
                            )
                            fingerprint = mix_integer(
                                fingerprint, false_index
                            )
                        for (
                            leaf_ordinal,
                            leaf_site,
                            leaf_skipped,
                            leaf_lanes,
                        ) in leaves:
                            fingerprint = mix_integer(
                                fingerprint, leaf_ordinal
                            )
                            fingerprint = mix_integer(
                                fingerprint, leaf_site
                            )
                            fingerprint = mix_integer(
                                fingerprint, len(leaf_skipped)
                            )
                            for site in leaf_skipped:
                                fingerprint = mix_integer(
                                    fingerprint, site
                                )
                            fingerprint = mix_integer(
                                fingerprint, len(leaf_lanes)
                            )
                            for (
                                lane_ordinal,
                                kind_tag,
                                source_site,
                                source_byte,
                                source_width,
                            ) in leaf_lanes:
                                fingerprint = mix_integer(
                                    fingerprint, lane_ordinal
                                )
                                fingerprint = mix_integer(
                                    fingerprint, kind_tag
                                )
                                fingerprint = mix_integer(
                                    fingerprint, source_site
                                )
                                fingerprint = mix_integer(
                                    fingerprint, source_byte
                                )
                                fingerprint = mix_integer(
                                    fingerprint, source_width
                                )
                        continue
                    if nested_predicate_schema:
                        (
                            backedge_site,
                            transfer_kind,
                            conditional_topology,
                            transfer_skipped,
                            lanes,
                        ) = transfer
                    elif symbolic_region_multi_latch_schema:
                        (
                            backedge_site,
                            conditional_topology,
                            transfer_skipped,
                            lanes,
                            symbolic_writer,
                        ) = transfer
                    elif conditional_multi_latch_schema:
                        (
                            backedge_site,
                            conditional_topology,
                            transfer_skipped,
                            lanes,
                        ) = transfer
                    else:
                        (
                            backedge_site,
                            transfer_skipped,
                            lanes,
                        ) = transfer
                        conditional_topology = None
                    fingerprint = mix_integer(
                        fingerprint, backedge_site
                    )
                    if conditional_multi_latch_schema:
                        fingerprint = mix_integer(
                            fingerprint,
                            (
                                transfer_kind
                                if nested_predicate_schema
                                else 1 if conditional_topology else 0
                            ),
                        )
                        if conditional_topology:
                            for site_or_polarity in conditional_topology:
                                fingerprint = mix_integer(
                                    fingerprint,
                                    site_or_polarity,
                                )
                    fingerprint = mix_integer(
                        fingerprint, len(transfer_skipped)
                    )
                    for site in transfer_skipped:
                        fingerprint = mix_integer(
                            fingerprint, site
                        )
                    fingerprint = mix_integer(
                        fingerprint, len(lanes)
                    )
                    for (
                        lane_ordinal,
                        kind_tag,
                        source_site,
                        source_byte,
                        source_width,
                    ) in lanes:
                        fingerprint = mix_integer(
                            fingerprint, lane_ordinal
                        )
                        fingerprint = mix_integer(
                            fingerprint, kind_tag
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_byte
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_width
                        )
                    if symbolic_region_multi_latch_schema:
                        fingerprint = mix_integer(
                            fingerprint,
                            1 if symbolic_writer is not None else 0,
                        )
                        if symbolic_writer is not None:
                            fingerprint = (
                                mix_symbolic_region_writer_record(
                                    fingerprint, symbolic_writer
                                )
                            )
            continue
        if state_schema in (
            MEMORY_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
            MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA,
        ):
            conditional_schema = (
                state_schema
                == MEMORY_CONDITIONAL_CYCLIC_BYTE_LANE_SCHEMA
            )
            symbolic_region_schema = (
                state_schema
                == MEMORY_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA
            )
            ordered_symbolic_region_schema = (
                state_schema
                == MEMORY_ORDERED_SYMBOLIC_REGION_CYCLIC_BYTE_LANE_SCHEMA
            )
            for state_kind, state_payload in states:
                fingerprint = mix_integer(fingerprint, state_kind)
                if state_kind == 0:
                    (
                        exit_ordinal,
                        byte_width,
                        endian_tag,
                        skipped,
                        lanes,
                    ) = state_payload
                    fingerprint = mix_integer(
                        fingerprint, exit_ordinal
                    )
                    fingerprint = mix_integer(fingerprint, byte_width)
                    fingerprint = mix_integer(fingerprint, endian_tag)
                    fingerprint = mix_integer(
                        fingerprint, len(skipped)
                    )
                    for site in skipped:
                        fingerprint = mix_integer(fingerprint, site)
                    fingerprint = mix_integer(
                        fingerprint, len(lanes)
                    )
                    for (
                        lane_ordinal,
                        kind_tag,
                        source_site,
                        source_byte,
                        source_width,
                        guarded_source,
                    ) in lanes:
                        require(
                            guarded_source is None,
                            "cyclic linear state is guarded",
                        )
                        fingerprint = mix_integer(
                            fingerprint, lane_ordinal
                        )
                        fingerprint = mix_integer(
                            fingerprint, kind_tag
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_site
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_byte
                        )
                        fingerprint = mix_integer(
                            fingerprint, source_width
                        )
                    continue
                symbolic_writer = None
                symbolic_writers = None
                if ordered_symbolic_region_schema:
                    (
                        exit_ordinal,
                        byte_width,
                        endian_tag,
                        skipped,
                        header_site,
                        entry_site,
                        backedge_site,
                        conditional_topology,
                        entry_state,
                        backedge_skipped,
                        backedge_lanes,
                        symbolic_writers,
                    ) = state_payload
                elif symbolic_region_schema:
                    (
                        exit_ordinal,
                        byte_width,
                        endian_tag,
                        skipped,
                        header_site,
                        entry_site,
                        backedge_site,
                        conditional_topology,
                        entry_state,
                        backedge_skipped,
                        backedge_lanes,
                        symbolic_writer,
                    ) = state_payload
                else:
                    (
                        exit_ordinal,
                        byte_width,
                        endian_tag,
                        skipped,
                        header_site,
                        entry_site,
                        backedge_site,
                        conditional_topology,
                        entry_state,
                        backedge_skipped,
                        backedge_lanes,
                    ) = state_payload
                fingerprint = mix_integer(fingerprint, exit_ordinal)
                fingerprint = mix_integer(fingerprint, byte_width)
                fingerprint = mix_integer(fingerprint, endian_tag)
                fingerprint = mix_integer(fingerprint, len(skipped))
                for site in skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(fingerprint, header_site)
                fingerprint = mix_integer(fingerprint, entry_site)
                fingerprint = mix_integer(fingerprint, backedge_site)
                require(
                    bool(conditional_topology) == conditional_schema,
                    "conditional cycle topology classification mismatch",
                )
                for topology_value in conditional_topology:
                    fingerprint = mix_integer(
                        fingerprint, topology_value
                    )
                (
                    entry_exit,
                    entry_width,
                    entry_endian,
                    entry_skipped,
                    entry_lanes,
                ) = entry_state
                fingerprint = mix_integer(fingerprint, entry_exit)
                fingerprint = mix_integer(fingerprint, entry_width)
                fingerprint = mix_integer(fingerprint, entry_endian)
                fingerprint = mix_integer(
                    fingerprint, len(entry_skipped)
                )
                for site in entry_skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(
                    fingerprint, len(entry_lanes)
                )
                for (
                    lane_ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                    guarded_source,
                ) in entry_lanes:
                    require(
                        guarded_source is None,
                        "cycle entry state is guarded",
                    )
                    fingerprint = mix_integer(
                        fingerprint, lane_ordinal
                    )
                    fingerprint = mix_integer(
                        fingerprint, kind_tag
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_byte
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_width
                    )
                fingerprint = mix_integer(
                    fingerprint, len(backedge_skipped)
                )
                for site in backedge_skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(
                    fingerprint, len(backedge_lanes)
                )
                for (
                    lane_ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                ) in backedge_lanes:
                    fingerprint = mix_integer(
                        fingerprint, lane_ordinal
                    )
                    fingerprint = mix_integer(
                        fingerprint, kind_tag
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_site
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_byte
                    )
                    fingerprint = mix_integer(
                        fingerprint, source_width
                    )
                if symbolic_writers is not None:
                    fingerprint = mix_integer(
                        fingerprint, len(symbolic_writers)
                    )
                    for writer in symbolic_writers:
                        fingerprint = mix_symbolic_region_writer_record(
                            fingerprint, writer
                        )
                elif symbolic_writer is not None:
                    fingerprint = mix_symbolic_region_writer_record(
                        fingerprint, symbolic_writer
                    )
            continue
        if state_schema in (
            MEMORY_BYTE_LANE_SCHEMA,
            MEMORY_GUARDED_BYTE_LANE_SCHEMA,
        ):
            guarded_schema = (
                state_schema == MEMORY_GUARDED_BYTE_LANE_SCHEMA
            )
            for (
                exit_ordinal,
                byte_width,
                endian_tag,
                skipped,
                lanes,
            ) in states:
                fingerprint = mix_integer(fingerprint, exit_ordinal)
                fingerprint = mix_integer(fingerprint, byte_width)
                fingerprint = mix_integer(fingerprint, endian_tag)
                fingerprint = mix_integer(fingerprint, len(skipped))
                for site in skipped:
                    fingerprint = mix_integer(fingerprint, site)
                fingerprint = mix_integer(fingerprint, len(lanes))
                for (
                    lane_ordinal,
                    kind_tag,
                    source_site,
                    source_byte,
                    source_width,
                    guarded_source,
                ) in lanes:
                    fingerprint = mix_integer(fingerprint, lane_ordinal)
                    fingerprint = mix_integer(fingerprint, kind_tag)
                    fingerprint = mix_integer(fingerprint, source_site)
                    fingerprint = mix_integer(fingerprint, source_byte)
                    fingerprint = mix_integer(fingerprint, source_width)
                    if guarded_schema:
                        fingerprint = mix_integer(
                            fingerprint,
                            1 if guarded_source is not None else 0,
                        )
                        if guarded_source is not None:
                            (
                                guard_site,
                                store_when_tag,
                                guarded_site,
                                guarded_byte,
                                guarded_width,
                            ) = guarded_source
                            fingerprint = mix_integer(
                                fingerprint, guard_site
                            )
                            fingerprint = mix_integer(
                                fingerprint, store_when_tag
                            )
                            fingerprint = mix_integer(
                                fingerprint, guarded_site
                            )
                            fingerprint = mix_integer(
                                fingerprint, guarded_byte
                            )
                            fingerprint = mix_integer(
                                fingerprint, guarded_width
                            )
            continue
        for exit_ordinal, kind_tag, source_site, skipped in states:
            fingerprint = mix_integer(fingerprint, exit_ordinal)
            if state_schema is not None:
                fingerprint = mix_integer(fingerprint, kind_tag)
            fingerprint = mix_integer(fingerprint, source_site)
            fingerprint = mix_integer(fingerprint, len(skipped))
            for site in skipped:
                fingerprint = mix_integer(fingerprint, site)
    require(
        unsigned_text(record.get("proof_fingerprint"), "proof fingerprint")
        == fingerprint,
        "proof fingerprint mismatch",
    )
    return True


def verify_path(path):
    count = 0
    with Path(path).open("r", encoding="utf-8") as manifest:
        for line_number, line in enumerate(manifest, 1):
            if not line.strip():
                continue
            try:
                verify_record(json.loads(line))
            except (json.JSONDecodeError, VerificationError) as error:
                raise VerificationError(
                    f"{path}:{line_number}: {error}"
                ) from error
            count += 1
    require(count > 0, f"{path}: manifest is empty")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="+")
    arguments = parser.parse_args(argv)
    verified = 0
    try:
        for path in arguments.manifest:
            verified += verify_path(path)
    except (OSError, VerificationError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {verified} IFSS continuation manifest record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
