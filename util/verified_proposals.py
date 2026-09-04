"""Concrete-validated semantic proposals for hybrid concolic execution.

Planners may propose bounded data transformations, but never executable code.
The MPI worker runs the resulting candidate through the real target and the
coordinator validates the requested branch with engine telemetry before the
candidate is eligible for ordinary AFL coverage triage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from typing import Any, Sequence


_KINDS = {
    "inverse",
    "surrogate",
    "heap_partition",
    "solve_complete",
    "query_hole_completion",
    "history_acquisition",
    "targeted_transform",
    "semantic",
}


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _canonical_id(raw: dict[str, Any]) -> str:
    payload = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class VerifiedProposal:
    proposal_id: str
    kind: str
    source_path: str
    candidate_path: str
    candidate_sha256: str
    target_branch: int
    generator: str = ""
    grammar_rule_id: str = ""
    grammar_context_id: str = ""
    grammar_source_context_id: str = ""
    grammar_candidate_context_id: str = ""
    grammar_ect_shape_id: str = ""
    grammar_ect_instance_id: str = ""
    grammar_sync_transaction_id: str = ""
    grammar_span_present: bool = False
    grammar_span_lo: int = 0
    grammar_span_hi: int = 0
    parser_context_id: str = ""
    parser_symbol: str = ""
    parser_state: str = ""
    parser_trace_sha256: str = ""
    parser_trace_nodes: int = 0
    parser_production_id: str = ""
    parser_production_lhs: str = ""
    parser_production_state: str = ""
    parser_production_arity: int = 0
    parser_recursive_depth: int = 0
    parser_recursion_prefix_hex: str = ""
    parser_recursion_suffix_hex: str = ""
    parser_cfg_fragment_sha256: str = ""
    parser_cfg_fragment_json: str = ""
    parser_cfg_productions: int = 0
    parser_cfg_cycles: int = 0
    parser_cfg_recursive_slots: int = 0
    parser_ect_shape_id: str = ""
    parser_ect_instance_id: str = ""
    parser_ect_instances: int = 0
    parser_cfg_alternatives: int = 0
    parser_epsilon_productions: int = 0
    parser_packed_nodes: int = 0
    parser_packed_edges: int = 0
    parser_nullable_rules: int = 0
    parser_nullable_proofs: int = 0
    parser_nullable_sccs: int = 0
    parser_cache_manifest_sha256: str = ""
    parser_cache_base_sha256: str = ""
    parser_cache_reused_nodes: int = 0
    parser_cache_invalidated_nodes: int = 0
    parser_cache_hit: bool = False
    parser_wall_time_us: int = 0
    parser_trace_bytes: int = 0
    parser_cache_requests: int = 0
    parser_cache_incremental_offers: int = 0
    parser_incremental_receipts: int = 0
    parser_incremental_zero_reuse: int = 0
    parser_node_id_proofs: int = 0
    parser_reported_parse_time_us: int = 0
    parser_cache_reused_nodes_total: int = 0
    parser_cache_invalidated_nodes_total: int = 0
    parser_forest_traces: int = 0
    parser_forest_complete_traces: int = 0
    parser_forest_proofs: int = 0
    parser_forest_raw_nodes: int = 0
    parser_forest_encoded_nodes: int = 0
    parser_forest_primary_clones: int = 0
    parser_forest_packed_alternatives: int = 0
    parser_forest_edges: int = 0
    parser_forest_nullable_rules: int = 0
    parser_forest_parse_time_us: int = 0
    parser_forest_grammar_sha256: str = ""
    parser_cross_traces: int = 0
    parser_cross_agreements: int = 0
    parser_cross_both_accept: int = 0
    parser_cross_primary_only: int = 0
    parser_cross_secondary_only: int = 0
    parser_cross_both_reject: int = 0
    parser_cross_primary_time_us: int = 0
    parser_cross_secondary_time_us: int = 0
    parser_cross_primary_command_sha256: str = ""
    parser_cross_secondary_command_sha256: str = ""
    parser_cross_structural_pairs: int = 0
    parser_cross_primary_selected_spans: int = 0
    parser_cross_primary_forest_spans: int = 0
    parser_cross_secondary_spans: int = 0
    parser_cross_selected_shared_spans: int = 0
    parser_cross_forest_shared_spans: int = 0
    parser_cross_selected_union_spans: int = 0
    parser_cross_forest_union_spans: int = 0
    parser_cross_primary_boundaries: int = 0
    parser_cross_secondary_boundaries: int = 0
    parser_cross_shared_boundaries: int = 0
    parser_cross_union_boundaries: int = 0
    parser_cross_primary_symbols: int = 0
    parser_cross_secondary_symbols: int = 0
    parser_cross_symbol_correspondences: int = 0
    parser_cross_ambiguous_symbol_spans: int = 0
    parser_cross_production_correspondences: int = 0
    parser_cross_correspondence_sha256: str = ""
    parser_cross_correspondence_json: str = ""
    query_id: str = ""
    query_ir_verified: bool = False
    query_hole_lo: int = 0
    query_hole_hi: int = 0
    query_hole_manifest_sha256: str = ""
    history_seed_id: str = ""
    parser_validations: int = 0
    parser_accepted: int = 0
    status: str = "pending"
    attempts: int = 0
    validations: int = 0
    retained: int = 0
    coverage_features: int = 0
    created: float = 0.0
    updated: float = 0.0
    last_reason: str = ""


class VerifiedProposalManager:
    """Ingest, materialize, validate, and persist semantic candidates."""

    SCHEMA = 15
    RESEARCH_ARTIFACT_SCHEMA = "symcc-parser-research-artifact-v1"

    def __init__(
        self,
        proposal_path: str,
        root: str,
        *,
        state_path: str = "",
        max_candidate_bytes: int = 16 * 1024 * 1024,
        max_patch_bytes: int = 64 * 1024,
        max_attempts: int = 2,
        retry_delay: float = 30.0,
        parser_command: Sequence[str] = (),
        parser_timeout: float = 1.0,
        parser_cache_enabled: bool = True,
    ) -> None:
        self.proposal_path = proposal_path
        self.root = root
        self.state_path = state_path or os.path.join(
            root, "proposal_state.json")
        self.max_candidate_bytes = max(1, int(max_candidate_bytes))
        self.max_patch_bytes = max(1, int(max_patch_bytes))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.parser_command = tuple(str(item) for item in parser_command)
        self.parser_timeout = max(0.01, min(60.0, float(parser_timeout)))
        self.parser_cache_enabled = bool(parser_cache_enabled)
        self.records: dict[str, VerifiedProposal] = {}
        self.path_records: dict[str, str] = {}
        self.rejected_inputs = 0
        self.duplicate_inputs = 0
        self.scan_offset = 0
        self.scan_identity = ""
        self.parser_cache_entries: dict[str, dict[str, Any]] = {}
        self.parser_forest_grammar_sha256s: set[str] = set()
        self.parser_cross_command_pairs: set[tuple[str, str]] = set()
        self.parser_cache_limit = 256
        os.makedirs(os.path.join(root, "candidates"), exist_ok=True)
        os.makedirs(os.path.join(root, "parser_cache"), exist_ok=True)
        self._load()

    @staticmethod
    def _valid_digest(value: str) -> bool:
        return (
            len(value) == 64 and
            all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _grammar_lexical_context_id(
        candidate: bytes,
        span_lo: int,
        span_hi: int,
        target_branch: int,
    ) -> str:
        left = candidate[max(0, span_lo - 16):span_lo]
        right = candidate[span_hi:min(len(candidate), span_hi + 16)]
        payload = b"\0".join((
            str(max(0, int(target_branch))).encode("ascii"),
            str(min(
                63, int(math.log2(max(1, len(candidate)))))).encode("ascii"),
            left,
            right,
        ))
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _parser_label(
        value: Any,
        *,
        allow_empty: bool = False,
    ) -> str | None:
        minimum = 0 if allow_empty else 1
        if not isinstance(value, str) or not minimum <= len(value) <= 128:
            return None
        if not value.isascii() or any(
                ord(character) < 32 or ord(character) == 127
                for character in value):
            return None
        return value

    @staticmethod
    def _selected_trace_nodes(trace: dict[str, Any]) -> set[int]:
        nodes = trace["nodes"]
        if trace.get("schema") in {
            "symcc-parser-structural-trace-v3",
            "symcc-parser-structural-trace-v4",
        }:
            return set(trace.get("primary_nodes", ()))
        children: dict[int, list[int]] = {}
        roots = []
        for index, node in enumerate(nodes):
            parent = int(node["parent"])
            if parent < 0:
                roots.append(index)
            else:
                children.setdefault(parent, []).append(index)
        selected: set[int] = set()
        pending = list(reversed(roots))
        while pending:
            index = pending.pop()
            if index in selected:
                continue
            selected.add(index)
            alternatives = nodes[index].get("alternatives", ())
            next_nodes = (
                list(alternatives[0])
                if alternatives else children.get(index, [])
            )
            pending.extend(reversed(next_nodes))
        return selected

    @staticmethod
    def _trace_span_features(
        trace: dict[str, Any],
        indexes: set[int],
        candidate_size: int,
    ) -> tuple[set[tuple[int, int]], set[int]]:
        nodes = trace["nodes"]
        spans = {
            (int(nodes[index]["start"]), int(nodes[index]["end"]))
            for index in indexes
            if not bool(nodes[index]["epsilon"])
        }
        non_root_spans = {
            span for span in spans
            if span != (0, candidate_size)
        }
        if non_root_spans:
            spans = non_root_spans
        boundaries = {
            boundary
            for index in indexes
            for boundary in (
                int(nodes[index]["start"]),
                int(nodes[index]["end"]),
            )
            if 0 < boundary < candidate_size
        }
        return spans, boundaries

    @staticmethod
    def _trace_semantic_nodes(
        trace: dict[str, Any],
        indexes: set[int],
    ) -> tuple[list[int], dict[int, tuple[int, ...]]]:
        """Return selected grammar nodes and transparent-wrapper children."""
        nodes = trace["nodes"]
        direct: dict[int, list[int]] = {}
        if trace.get("schema") in {
            "symcc-parser-structural-trace-v3",
            "symcc-parser-structural-trace-v4",
        }:
            for index in indexes:
                alternatives = nodes[index].get("alternatives", ())
                direct[index] = [
                    child for child in (
                        alternatives[0] if alternatives else ())
                    if child in indexes
                ]
        else:
            for index in indexes:
                direct[index] = []
            for child in indexes:
                parent = int(nodes[child]["parent"])
                if parent in indexes:
                    direct[parent].append(child)
            for children in direct.values():
                children.sort(key=lambda child: (
                    int(nodes[child]["start"]),
                    int(nodes[child]["end"]),
                    child,
                ))

        semantic = [
            index for index in sorted(indexes)
            if not str(nodes[index]["symbol"]).startswith("@")
        ]
        semantic_set = set(semantic)

        def flatten(index: int, parent: int) -> list[int]:
            result: list[int] = []
            pending = [index]
            expanded: set[int] = {parent}
            while pending:
                current = pending.pop()
                if current in semantic_set:
                    result.append(current)
                    continue
                if current in expanded:
                    raise ValueError(
                        "selected parser wrappers contain a cycle")
                expanded.add(current)
                pending.extend(reversed(direct.get(current, ())))
            return result

        children = {
            index: tuple(
                child
                for direct_child in direct.get(index, ())
                for child in flatten(direct_child, index)
            )
            for index in semantic
        }
        return semantic, children

    @staticmethod
    def _cross_correspondence(
        primary: dict[str, Any],
        primary_indexes: set[int],
        secondary: dict[str, Any],
        secondary_indexes: set[int],
    ) -> dict[str, Any]:
        """Build span-unique symbol and partition-equal production evidence."""
        primary_nodes = primary["nodes"]
        secondary_nodes = secondary["nodes"]
        primary_semantic, primary_children = (
            VerifiedProposalManager._trace_semantic_nodes(
                primary, primary_indexes))
        secondary_semantic, secondary_children = (
            VerifiedProposalManager._trace_semantic_nodes(
                secondary, secondary_indexes))

        def structural_shapes(
            nodes: list[dict[str, Any]],
            children: dict[int, tuple[int, ...]],
            indexes: Sequence[int],
        ) -> dict[int, str]:
            memo: dict[int, str] = {}
            colors: dict[int, int] = {}
            for root in indexes:
                if root in memo:
                    continue
                pending: list[tuple[int, bool]] = [(root, False)]
                while pending:
                    index, expanded = pending.pop()
                    if index in memo:
                        continue
                    if expanded:
                        child_shapes = []
                        for child in children[index]:
                            if child not in memo:
                                raise ValueError(
                                    "parser shape order is incomplete")
                            child_shapes.append(memo[child])
                        node = nodes[index]
                        start = int(node["start"])
                        payload = {
                            "epsilon": bool(node["epsilon"]),
                            "length": int(node["end"]) - start,
                            "children": [
                                [
                                    int(nodes[child]["start"]) - start,
                                    int(nodes[child]["end"]) - start,
                                    child_shape,
                                ]
                                for child, child_shape in zip(
                                    children[index], child_shapes)
                            ],
                        }
                        memo[index] = hashlib.sha256(json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii")).hexdigest()
                        colors[index] = 2
                        continue
                    if colors.get(index) == 1:
                        raise ValueError(
                            "selected parser projection contains a cycle")
                    colors[index] = 1
                    pending.append((index, True))
                    for child in reversed(children[index]):
                        if colors.get(child) == 1:
                            raise ValueError(
                                "selected parser projection contains a cycle")
                        if child not in memo:
                            pending.append((child, False))
            return memo

        primary_shapes = structural_shapes(
            primary_nodes, primary_children, primary_semantic)
        secondary_shapes = structural_shapes(
            secondary_nodes, secondary_children, secondary_semantic)

        def by_span_shape(
            nodes: list[dict[str, Any]],
            indexes: Sequence[int],
            shapes: dict[int, str],
        ) -> dict[tuple[int, int, bool, str], list[int]]:
            groups: dict[tuple[int, int, bool, str], list[int]] = {}
            for index in indexes:
                node = nodes[index]
                key = (
                    int(node["start"]),
                    int(node["end"]),
                    bool(node["epsilon"]),
                    shapes[index],
                )
                groups.setdefault(key, []).append(index)
            return groups

        primary_groups = by_span_shape(
            primary_nodes, primary_semantic, primary_shapes)
        secondary_groups = by_span_shape(
            secondary_nodes, secondary_semantic, secondary_shapes)
        shared_spans = sorted(set(primary_groups) & set(secondary_groups))
        ambiguous = sum(
            len(primary_groups[key]) != 1 or
            len(secondary_groups[key]) != 1
            for key in shared_spans
        )
        symbol_counts: dict[tuple[str, str], int] = {}
        production_counts: dict[
            tuple[str, str, str, str, str], int] = {}
        symbol_observations = 0
        production_observations = 0

        def partition(
            nodes: list[dict[str, Any]],
            children: dict[int, tuple[int, ...]],
            index: int,
        ) -> tuple[tuple[int, int, bool], ...]:
            parent = nodes[index]
            start = int(parent["start"])
            return tuple(
                (
                    int(nodes[child]["start"]) - start,
                    int(nodes[child]["end"]) - start,
                    bool(nodes[child]["epsilon"]),
                )
                for child in children[index]
            )

        for key in shared_spans:
            primary_group = primary_groups[key]
            secondary_group = secondary_groups[key]
            if len(primary_group) != 1 or len(secondary_group) != 1:
                continue
            primary_index = primary_group[0]
            secondary_index = secondary_group[0]
            primary_node = primary_nodes[primary_index]
            secondary_node = secondary_nodes[secondary_index]
            symbol_key = (
                str(primary_node["symbol"]),
                str(secondary_node["symbol"]),
            )
            symbol_counts[symbol_key] = (
                symbol_counts.get(symbol_key, 0) + 1)
            symbol_observations += 1

            primary_partition = partition(
                primary_nodes, primary_children, primary_index)
            secondary_partition = partition(
                secondary_nodes, secondary_children, secondary_index)
            if (
                not primary_partition or
                primary_partition != secondary_partition
            ):
                continue
            shape_sha256 = hashlib.sha256(json.dumps(
                {
                    "epsilon": key[2],
                    "length": key[1] - key[0],
                    "partition": primary_partition,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")).hexdigest()
            production_key = (
                str(primary_node["symbol"]),
                str(primary_node["state"]),
                str(secondary_node["symbol"]),
                str(secondary_node["state"]),
                shape_sha256,
            )
            production_counts[production_key] = (
                production_counts.get(production_key, 0) + 1)
            production_observations += 1

        core = {
            "schema": "symcc-parser-correspondence-v1",
            "primary_parser": str(primary["parser"]),
            "secondary_parser": str(secondary["parser"]),
            "primary_symbols": len(primary_semantic),
            "secondary_symbols": len(secondary_semantic),
            "symbol_observations": symbol_observations,
            "ambiguous_symbol_spans": ambiguous,
            "production_observations": production_observations,
            "symbols": [
                [primary_symbol, secondary_symbol, observations]
                for (
                    primary_symbol,
                    secondary_symbol,
                ), observations in sorted(symbol_counts.items())
            ],
            "productions": [
                [
                    primary_symbol,
                    primary_state,
                    secondary_symbol,
                    secondary_state,
                    shape_sha256,
                    observations,
                ]
                for (
                    primary_symbol,
                    primary_state,
                    secondary_symbol,
                    secondary_state,
                    shape_sha256,
                ), observations in sorted(production_counts.items())
            ],
        }
        encoded = json.dumps(
            core, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("ascii")) > 2 * 1024 * 1024:
            raise ValueError("cross-parser correspondence exceeds bound")
        return {
            **core,
            "json": encoded,
            "sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
        }

    @staticmethod
    def _decode_cross_correspondence(
        encoded: str,
        digest: str,
    ) -> dict[str, Any] | None:
        if not encoded and not digest:
            return None
        try:
            raw = json.loads(encoded)
            canonical = json.dumps(
                raw, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True)
        except (TypeError, ValueError, UnicodeError):
            return None
        if (
            len(canonical.encode("ascii")) > 2 * 1024 * 1024 or
            canonical != encoded or
            hashlib.sha256(canonical.encode("ascii")).hexdigest() !=
            digest or
            not isinstance(raw, dict) or
            raw.get("schema") != "symcc-parser-correspondence-v1"
        ):
            return None
        integer_fields = (
            "primary_symbols",
            "secondary_symbols",
            "symbol_observations",
            "ambiguous_symbol_spans",
            "production_observations",
        )
        symbols = raw.get("symbols")
        productions = raw.get("productions")
        if (
            set(raw) != {
                "schema",
                "primary_parser",
                "secondary_parser",
                *integer_fields,
                "symbols",
                "productions",
            } or
            VerifiedProposalManager._parser_label(
                raw.get("primary_parser")) is None or
            VerifiedProposalManager._parser_label(
                raw.get("secondary_parser")) is None or
            any(
                isinstance(raw.get(key), bool) or
                not isinstance(raw.get(key), int) or
                int(raw[key]) < 0
                for key in integer_fields
            ) or
            not isinstance(symbols, list) or
            not isinstance(productions, list)
        ):
            return None
        normalized_symbols: list[list[Any]] = []
        for item in symbols:
            if (
                not isinstance(item, list) or len(item) != 3 or
                VerifiedProposalManager._parser_label(item[0]) is None or
                VerifiedProposalManager._parser_label(item[1]) is None or
                isinstance(item[2], bool) or
                not isinstance(item[2], int) or item[2] <= 0
            ):
                return None
            normalized_symbols.append(
                [str(item[0]), str(item[1]), int(item[2])])
        normalized_productions: list[list[Any]] = []
        for item in productions:
            if (
                not isinstance(item, list) or len(item) != 6 or
                VerifiedProposalManager._parser_label(item[0]) is None or
                VerifiedProposalManager._parser_label(
                    item[1], allow_empty=True) is None or
                VerifiedProposalManager._parser_label(item[2]) is None or
                VerifiedProposalManager._parser_label(
                    item[3], allow_empty=True) is None or
                not isinstance(item[4], str) or
                not VerifiedProposalManager._valid_digest(item[4]) or
                isinstance(item[5], bool) or
                not isinstance(item[5], int) or item[5] <= 0
            ):
                return None
            normalized_productions.append([
                str(item[0]),
                str(item[1]),
                str(item[2]),
                str(item[3]),
                str(item[4]),
                int(item[5]),
            ])
        if (
            normalized_symbols != sorted(normalized_symbols) or
            len({
                (item[0], item[1]) for item in normalized_symbols
            }) != len(normalized_symbols) or
            normalized_productions != sorted(normalized_productions) or
            len({
                tuple(item[:5]) for item in normalized_productions
            }) != len(normalized_productions) or
            sum(item[2] for item in normalized_symbols) !=
            raw["symbol_observations"] or
            sum(item[5] for item in normalized_productions) !=
            raw["production_observations"] or
            raw["symbol_observations"] > raw["primary_symbols"] or
            raw["symbol_observations"] > raw["secondary_symbols"] or
            raw["production_observations"] >
            raw["symbol_observations"]
        ):
            return None
        return raw

    @staticmethod
    def _merge_cross_correspondence(
        previous_encoded: str,
        previous_digest: str,
        current_encoded: str,
        current_digest: str,
    ) -> tuple[str, str] | None:
        current = VerifiedProposalManager._decode_cross_correspondence(
            current_encoded, current_digest)
        if current is None:
            return None
        if not previous_encoded and not previous_digest:
            return current_encoded, current_digest
        previous = VerifiedProposalManager._decode_cross_correspondence(
            previous_encoded, previous_digest)
        if (
            previous is None or
            previous["primary_parser"] != current["primary_parser"] or
            previous["secondary_parser"] != current["secondary_parser"]
        ):
            return None
        symbol_counts: dict[tuple[str, str], int] = {}
        for raw in (previous, current):
            for primary_symbol, secondary_symbol, observations in raw[
                    "symbols"]:
                key = (primary_symbol, secondary_symbol)
                symbol_counts[key] = (
                    symbol_counts.get(key, 0) + observations)
        production_counts: dict[
            tuple[str, str, str, str, str], int] = {}
        for raw in (previous, current):
            for item in raw["productions"]:
                key = tuple(item[:5])
                production_counts[key] = (
                    production_counts.get(key, 0) + int(item[5]))
        core = {
            "schema": "symcc-parser-correspondence-v1",
            "primary_parser": previous["primary_parser"],
            "secondary_parser": previous["secondary_parser"],
            "primary_symbols": (
                previous["primary_symbols"] + current["primary_symbols"]),
            "secondary_symbols": (
                previous["secondary_symbols"] + current["secondary_symbols"]),
            "symbol_observations": (
                previous["symbol_observations"] +
                current["symbol_observations"]),
            "ambiguous_symbol_spans": (
                previous["ambiguous_symbol_spans"] +
                current["ambiguous_symbol_spans"]),
            "production_observations": (
                previous["production_observations"] +
                current["production_observations"]),
            "symbols": [
                [primary_symbol, secondary_symbol, observations]
                for (
                    primary_symbol,
                    secondary_symbol,
                ), observations in sorted(symbol_counts.items())
            ],
            "productions": [
                [*key, observations]
                for key, observations in sorted(production_counts.items())
            ],
        }
        encoded = json.dumps(
            core, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("ascii")) > 2 * 1024 * 1024:
            return None
        return encoded, hashlib.sha256(encoded.encode("ascii")).hexdigest()

    @staticmethod
    def _nullable_certificate(
        parser: str,
        rules: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        normalized_rules: dict[str, dict[str, Any]] = {}
        graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for item in rules:
            if not isinstance(item, dict):
                return None
            lhs_raw = item.get("lhs")
            rhs_raw = item.get("rhs")
            if (
                not isinstance(lhs_raw, list) or len(lhs_raw) != 2 or
                VerifiedProposalManager._parser_label(lhs_raw[0]) is None or
                VerifiedProposalManager._parser_label(
                    lhs_raw[1], allow_empty=True) is None or
                not isinstance(rhs_raw, list) or len(rhs_raw) > 8
            ):
                return None
            lhs = (str(lhs_raw[0]), str(lhs_raw[1]))
            rhs: list[tuple[str, str]] = []
            for child in rhs_raw:
                if (
                    not isinstance(child, list) or len(child) != 2 or
                    VerifiedProposalManager._parser_label(child[0]) is None or
                    VerifiedProposalManager._parser_label(
                        child[1], allow_empty=True) is None
                ):
                    return None
                rhs.append((str(child[0]), str(child[1])))
            core = {
                "schema": "symcc-parser-nullable-rule-v1",
                "parser": parser,
                "lhs": list(lhs),
                "rhs": [list(child) for child in rhs],
            }
            rule_id = hashlib.sha256(json.dumps(
                core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if rule_id in normalized_rules:
                return None
            normalized_rules[rule_id] = {
                "rule_id": rule_id,
                "lhs": list(lhs),
                "rhs": [list(child) for child in rhs],
            }
            graph.setdefault(lhs, set()).update(rhs)
            for child in rhs:
                graph.setdefault(child, set())
        if not normalized_rules:
            return None

        reachable: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for origin in graph:
            visited: set[tuple[str, str]] = set()
            stack = [origin]
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                stack.extend(graph.get(current, ()))
            reachable[origin] = visited
        remaining = set(graph)
        scc_by_node: dict[tuple[str, str], tuple[str, bool]] = {}
        cyclic_sccs: list[dict[str, Any]] = []
        while remaining:
            seed = min(remaining)
            component = {
                node for node in remaining
                if (
                    node in reachable[seed] and
                    seed in reachable[node]
                )
            }
            remaining.difference_update(component)
            cyclic = (
                len(component) > 1 or
                seed in graph.get(seed, set())
            )
            scc_core = {
                "schema": "symcc-parser-nullable-scc-v1",
                "parser": parser,
                "members": [
                    list(member) for member in sorted(component)],
            }
            scc_id = hashlib.sha256(json.dumps(
                scc_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            for member in component:
                scc_by_node[member] = (scc_id, cyclic)
            if cyclic:
                cyclic_sccs.append({
                    "scc_id": scc_id,
                    "members": scc_core["members"],
                })

        proofs: dict[tuple[str, str], tuple[int, str]] = {}
        ordered_rules = sorted(
            normalized_rules.values(),
            key=lambda item: item["rule_id"],
        )
        changed = True
        while changed:
            changed = False
            for rule in ordered_rules:
                lhs = tuple(rule["lhs"])
                rhs = [tuple(child) for child in rule["rhs"]]
                if any(child not in proofs for child in rhs):
                    continue
                depth = (
                    0 if not rhs else
                    1 + max(proofs[child][0] for child in rhs)
                )
                candidate = (depth, rule["rule_id"])
                if lhs not in proofs or candidate < proofs[lhs]:
                    proofs[lhs] = candidate
                    changed = True
        normalized_proofs = []
        for symbol_state, (depth, rule_id) in sorted(proofs.items()):
            scc_id, cyclic = scc_by_node[symbol_state]
            normalized_proofs.append({
                "symbol": symbol_state[0],
                "state": symbol_state[1],
                "rule_id": rule_id,
                "depth": depth,
                "scc_id": scc_id if cyclic else "",
            })
        return {
            "nullable_rules": sorted(
                normalized_rules.values(),
                key=lambda item: item["rule_id"],
            ),
            "nullable_proofs": normalized_proofs,
            "nullable_sccs": sorted(
                cyclic_sccs, key=lambda item: item["scc_id"]),
        }

    def _parser_command_digest(self) -> str:
        payload = json.dumps(
            list(self.parser_command),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    def _drop_parser_cache_entry(self, input_sha256: str) -> None:
        entry = self.parser_cache_entries.pop(input_sha256, None)
        if entry is None:
            return
        path = str(entry.get("trace_path", ""))
        cache_root = os.path.realpath(
            os.path.join(self.root, "parser_cache"))
        if (
            path and
            os.path.dirname(os.path.realpath(path)) == cache_root
        ):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _build_parser_cache_manifest(
        self,
        record: VerifiedProposal,
        candidate: bytes,
    ) -> tuple[str, dict[str, Any]]:
        command_sha256 = self._parser_command_digest()
        manifest: dict[str, Any] = {
            "schema": "symcc-parser-incremental-cache-v1",
            "mode": "cold",
            "parser_command_sha256": command_sha256,
            "candidate_input_sha256": record.candidate_sha256,
            "reusable_nodes": [],
            "invalidated_nodes": [],
        }
        source = b""
        if record.source_path:
            try:
                with open(record.source_path, "rb") as stream:
                    source = stream.read(self.max_candidate_bytes + 1)
            except OSError:
                source = b""
        source_sha256 = (
            hashlib.sha256(source).hexdigest()
            if len(source) <= self.max_candidate_bytes else "")
        entry = self.parser_cache_entries.get(source_sha256)
        if (
            entry is not None and
            entry.get("parser_command_sha256") == command_sha256
        ):
            trace_path = str(entry.get("trace_path", ""))
            try:
                with open(trace_path, "rb") as stream:
                    trace_bytes = stream.read(1024 * 1024 + 1)
                with open(str(entry.get("input_path", "")), "rb") as stream:
                    base = stream.read(self.max_candidate_bytes + 1)
                raw_trace = json.loads(trace_bytes.decode("utf-8"))
            except (OSError, UnicodeError, ValueError, TypeError):
                self._drop_parser_cache_entry(source_sha256)
                entry = None
            if entry is not None and (
                len(trace_bytes) > 1024 * 1024 or
                hashlib.sha256(trace_bytes).hexdigest() !=
                entry.get("trace_sha256") or
                len(base) > self.max_candidate_bytes or
                hashlib.sha256(base).hexdigest() != source_sha256 or
                not isinstance(raw_trace, dict) or
                not isinstance(raw_trace.get("nodes"), list)
            ):
                self._drop_parser_cache_entry(source_sha256)
                entry = None
        if entry is not None:
            prefix = 0
            common_limit = min(len(base), len(candidate))
            while prefix < common_limit and base[prefix] == candidate[prefix]:
                prefix += 1
            suffix = 0
            while (
                suffix < common_limit - prefix and
                base[len(base) - suffix - 1] ==
                candidate[len(candidate) - suffix - 1]
            ):
                suffix += 1
            base_hi = len(base) - suffix
            candidate_hi = len(candidate) - suffix
            if (
                base != candidate and
                base_hi - prefix + candidate_hi - prefix <=
                self.max_patch_bytes
            ):
                guard = 16
                safe_prefix = max(0, prefix - guard)
                safe_suffix = min(len(base), base_hi + guard)
                delta = len(candidate) - len(base)
                reusable_nodes: list[dict[str, Any]] = []
                invalidated_nodes: list[int] = []
                for node_index, node in enumerate(raw_trace["nodes"][:4096]):
                    if not isinstance(node, dict):
                        invalidated_nodes.append(node_index)
                        continue
                    try:
                        start = int(node.get("start"))
                        end = int(node.get("end"))
                    except (TypeError, ValueError, OverflowError):
                        invalidated_nodes.append(node_index)
                        continue
                    mapped_start = start
                    mapped_end = end
                    if end <= safe_prefix:
                        pass
                    elif start >= safe_suffix:
                        mapped_start += delta
                        mapped_end += delta
                    else:
                        invalidated_nodes.append(node_index)
                        continue
                    symbol = self._parser_label(node.get("symbol"))
                    state = self._parser_label(
                        node.get("state", ""), allow_empty=True)
                    epsilon = node.get("epsilon", start == end)
                    if (
                        symbol is None or state is None or
                        not isinstance(epsilon, bool) or
                        not 0 <= start <= end <= len(base) or
                        not 0 <= mapped_start <= mapped_end <= len(candidate) or
                        base[start:end] != candidate[
                            mapped_start:mapped_end]
                    ):
                        invalidated_nodes.append(node_index)
                        continue
                    if len(reusable_nodes) >= 512:
                        invalidated_nodes.append(node_index)
                        continue
                    reusable_nodes.append({
                        "base_index": node_index,
                        "symbol": symbol,
                        "state": state,
                        "start": mapped_start,
                        "end": mapped_end,
                        "epsilon": epsilon,
                        "yield_sha256": hashlib.sha256(
                            base[start:end]).hexdigest(),
                    })
                manifest.update({
                    "mode": "incremental",
                    "base_input_sha256": source_sha256,
                    "base_input_path": str(entry["input_path"]),
                    "base_trace_sha256": str(entry["trace_sha256"]),
                    "base_trace_path": str(entry["trace_path"]),
                    "edit": {
                        "offset": prefix,
                        "delete": base_hi - prefix,
                        "insert_hex": candidate[
                            prefix:candidate_hi].hex(),
                    },
                    "reusable_nodes": reusable_nodes,
                    "invalidated_nodes": invalidated_nodes,
                })
                entry["last_used"] = time.time()
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = os.path.join(
            self.root,
            f".parser-cache-manifest-{os.getpid()}-{time.time_ns()}.json",
        )
        try:
            with open(path, "xb") as stream:
                stream.write(encoded)
        except OSError:
            return "", manifest
        manifest["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
        return path, manifest

    def _store_parser_cache_entry(
        self,
        record: VerifiedProposal,
        trace_path: str,
        trace_sha256: str,
        trace: dict[str, Any],
    ) -> None:
        try:
            with open(trace_path, "rb") as stream:
                encoded = stream.read(1024 * 1024 + 1)
        except OSError:
            return
        if (
            len(encoded) > 1024 * 1024 or
            hashlib.sha256(encoded).hexdigest() != trace_sha256
        ):
            return
        while (
            record.candidate_sha256 not in self.parser_cache_entries and
            len(self.parser_cache_entries) >= self.parser_cache_limit
        ):
            victim = min(
                self.parser_cache_entries,
                key=lambda digest: (
                    float(self.parser_cache_entries[digest].get(
                        "last_used", 0.0)),
                    digest,
                ),
            )
            self._drop_parser_cache_entry(victim)
        cache_path = os.path.join(
            self.root,
            "parser_cache",
            f"{record.candidate_sha256}.trace.json",
        )
        temporary = f"{cache_path}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            with open(temporary, "xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, cache_path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return
        self.parser_cache_entries[record.candidate_sha256] = {
            "input_sha256": record.candidate_sha256,
            "input_path": record.candidate_path,
            "trace_sha256": trace_sha256,
            "trace_path": cache_path,
            "parser_command_sha256": self._parser_command_digest(),
            "parser": str(trace.get("parser", "")),
            "schema": str(trace.get("schema", "")),
            "nodes": len(trace.get("nodes", ())),
            "last_used": time.time(),
        }

    def _load_parser_trace(
        self,
        path: str,
        *,
        candidate_size: int,
        returncode: int,
        candidate: bytes | None = None,
        cache_manifest: dict[str, Any] | None = None,
        allow_cross: bool = True,
    ) -> tuple[dict[str, Any], str] | None:
        try:
            with open(path, "rb") as stream:
                encoded = stream.read(1024 * 1024 + 1)
            if len(encoded) > 1024 * 1024:
                return None
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        if (
            not isinstance(raw, dict) or
            raw.get("schema") not in {
                "symcc-parser-structural-trace-v1",
                "symcc-parser-structural-trace-v2",
                "symcc-parser-structural-trace-v3",
                "symcc-parser-structural-trace-v4",
            } or
            not isinstance(raw.get("accepted"), bool) or
            bool(raw["accepted"]) != (int(returncode) == 0)
        ):
            return None
        parser = self._parser_label(raw.get("parser", "unspecified"))
        nodes_raw = raw.get("nodes")
        if (
            parser is None or not isinstance(nodes_raw, list) or
            len(nodes_raw) > 4096
        ):
            return None
        trace_v2 = raw["schema"] == "symcc-parser-structural-trace-v2"
        trace_v3 = raw["schema"] in {
            "symcc-parser-structural-trace-v3",
            "symcc-parser-structural-trace-v4",
        }
        trace_v4 = raw["schema"] == "symcc-parser-structural-trace-v4"
        nodes: list[dict[str, Any]] = []
        for index, item in enumerate(nodes_raw):
            if not isinstance(item, dict):
                return None
            symbol = self._parser_label(item.get("symbol"))
            state = self._parser_label(
                item.get("state", ""), allow_empty=True)
            epsilon_raw = item.get("epsilon", False)
            if not isinstance(epsilon_raw, bool):
                return None
            epsilon = epsilon_raw
            try:
                start = int(item.get("start"))
                end = int(item.get("end"))
                parent = (
                    -2 if trace_v3 else
                    int(item.get("parent", -1))
                )
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                symbol is None or state is None or
                isinstance(item.get("start"), bool) or
                isinstance(item.get("end"), bool) or
                not (
                    0 <= start <= end <= candidate_size
                    if trace_v2 or trace_v3 else
                    0 <= start < end <= candidate_size
                ) or
                (trace_v2 or trace_v3) and
                epsilon != (start == end) or
                not (trace_v2 or trace_v3) and epsilon or
                trace_v3 and "parent" in item or
                not trace_v3 and not (-1 <= parent < index)
            ):
                return None
            if not trace_v3 and parent >= 0 and not (
                nodes[parent]["start"] <= start and
                end <= nodes[parent]["end"]
            ):
                return None
            nodes.append({
                "symbol": symbol,
                "state": state,
                "start": start,
                "end": end,
                "parent": parent,
                "epsilon": epsilon,
                "alternatives": [],
            })
        for index, (item, node) in enumerate(zip(nodes_raw, nodes)):
            alternatives_raw = item.get("alternatives")
            if trace_v3:
                alternatives_raw = (
                    [[]] if alternatives_raw is None else alternatives_raw)
                if (
                    not isinstance(alternatives_raw, list) or
                    not 1 <= len(alternatives_raw) <= 8
                ):
                    return None
                alternatives: list[list[int]] = []
                seen_alternatives: set[tuple[int, ...]] = set()
                for alternative_raw in alternatives_raw:
                    if (
                        not isinstance(alternative_raw, list) or
                        len(alternative_raw) > 64 or
                        any(
                            isinstance(child, bool) or
                            not isinstance(child, int) or
                            not index < child < len(nodes)
                            for child in alternative_raw
                        ) or
                        len(set(alternative_raw)) != len(alternative_raw)
                    ):
                        return None
                    alternative = sorted(
                        alternative_raw,
                        key=lambda child: (
                            nodes[child]["start"],
                            nodes[child]["end"],
                            child,
                        ),
                    )
                    if (
                        alternative != alternative_raw or
                        any(
                            not (
                                node["start"] <= nodes[child]["start"] and
                                nodes[child]["end"] <= node["end"]
                            )
                            for child in alternative
                        ) or
                        any(
                            nodes[left]["end"] >
                            nodes[right]["start"]
                            for left, right in zip(
                                alternative, alternative[1:])
                        )
                    ):
                        return None
                    key = tuple(alternative)
                    if key in seen_alternatives:
                        return None
                    seen_alternatives.add(key)
                    alternatives.append(alternative)
                if node["epsilon"] and any(alternatives):
                    return None
                node["alternatives"] = alternatives
                continue
            direct_children = {
                child_index
                for child_index, child in enumerate(nodes)
                if child["parent"] == index
            }
            if node["epsilon"] and direct_children:
                return None
            if alternatives_raw is None:
                continue
            if (
                not trace_v2 or
                not isinstance(alternatives_raw, list) or
                not 1 <= len(alternatives_raw) <= 8
            ):
                return None
            alternatives: list[list[int]] = []
            seen_alternatives: set[tuple[int, ...]] = set()
            represented: set[int] = set()
            for alternative_raw in alternatives_raw:
                if (
                    not isinstance(alternative_raw, list) or
                    len(alternative_raw) > 64 or
                    any(
                        isinstance(child, bool) or
                        not isinstance(child, int)
                        for child in alternative_raw
                    ) or
                    len(set(alternative_raw)) != len(alternative_raw) or
                    any(child not in direct_children
                        for child in alternative_raw)
                ):
                    return None
                alternative = sorted(
                    alternative_raw,
                    key=lambda child: (
                        nodes[child]["start"],
                        nodes[child]["end"],
                        child,
                    ),
                )
                if alternative != alternative_raw or any(
                    nodes[left]["end"] > nodes[right]["start"]
                    for left, right in zip(
                        alternative, alternative[1:])
                ):
                    return None
                key = tuple(alternative)
                if key in seen_alternatives:
                    return None
                seen_alternatives.add(key)
                represented.update(alternative)
                alternatives.append(alternative)
            if represented != direct_children:
                return None
            node["alternatives"] = alternatives
        roots: list[int] = []
        primary_nodes: set[int] = set()
        node_paths: dict[int, tuple[int, ...]] = {}
        if trace_v3:
            if sum(
                len(alternative)
                for current in nodes
                for alternative in current["alternatives"]
            ) > 16384:
                return None
            roots_raw = raw.get("roots")
            if (
                not isinstance(roots_raw, list) or
                not 1 <= len(roots_raw) <= 8 or
                any(
                    isinstance(root, bool) or
                    not isinstance(root, int) or
                    not 0 <= root < len(nodes)
                    for root in roots_raw
                ) or
                roots_raw != sorted(set(roots_raw))
            ):
                return None
            roots = list(roots_raw)
            incoming: set[int] = {
                child
                for current in nodes
                for alternative in current["alternatives"]
                for child in alternative
            }
            if any(root in incoming for root in roots):
                return None
            canonical_parents: dict[int, int] = {}
            for root in roots:
                node_paths[root] = (root,)
            for parent, current in enumerate(nodes):
                path = node_paths.get(parent)
                if path is None:
                    continue
                for alternative in current["alternatives"]:
                    for child in alternative:
                        candidate_path = path + (child,)
                        if (
                            child not in node_paths or
                            candidate_path < node_paths[child]
                        ):
                            node_paths[child] = candidate_path
                            canonical_parents[child] = parent
            if len(node_paths) != len(nodes):
                return None
            selected_parent: dict[int, int] = {}
            primary_nodes.add(roots[0])
            for parent, current in enumerate(nodes):
                if parent not in primary_nodes:
                    continue
                for child in current["alternatives"][0]:
                    previous = selected_parent.get(child)
                    if previous is not None and previous != parent:
                        return None
                    selected_parent[child] = parent
                    primary_nodes.add(child)
            for node_index, current in enumerate(nodes):
                if node_index == roots[0]:
                    current["parent"] = -1
                elif node_index in primary_nodes:
                    current["parent"] = selected_parent[node_index]
                else:
                    current["parent"] = canonical_parents.get(node_index, -1)
        nullable_certificate: dict[str, Any] | None = None
        if trace_v4:
            nullable_rules_raw = raw.get("nullable_rules")
            if (
                not isinstance(nullable_rules_raw, list) or
                not 1 <= len(nullable_rules_raw) <= 32
            ):
                return None
            nullable_certificate = self._nullable_certificate(
                parser, nullable_rules_raw)
            if nullable_certificate is None:
                return None
        cache_reused_nodes = 0
        reuse_proof = ""
        receipt = raw.get("incremental_cache")
        cache_receipt = receipt is not None
        if receipt is not None:
            if (
                cache_manifest is None or candidate is None or
                not isinstance(receipt, dict) or
                receipt.get("schema") !=
                "symcc-parser-incremental-reuse-v1" or
                receipt.get("manifest_sha256") !=
                cache_manifest.get("manifest_sha256") or
                not isinstance(receipt.get("reused_nodes"), list) or
                len(receipt["reused_nodes"]) > 512
            ):
                return None
            proof = receipt.get("proof", "")
            if proof not in {"", "tree-sitter-node-id-v1"}:
                return None
            reuse_proof = str(proof)
            expected_reuse = {
                int(item["base_index"]): item
                for item in cache_manifest.get("reusable_nodes", ())
                if (
                    isinstance(item, dict) and
                    isinstance(item.get("base_index"), int) and
                    not isinstance(item.get("base_index"), bool)
                )
            }
            seen_base: set[int] = set()
            seen_candidate: set[int] = set()
            for mapping in receipt["reused_nodes"]:
                if not isinstance(mapping, dict):
                    return None
                base_index = mapping.get("base_index")
                candidate_index = mapping.get("candidate_index")
                if (
                    not isinstance(base_index, int) or
                    isinstance(base_index, bool) or
                    not isinstance(candidate_index, int) or
                    isinstance(candidate_index, bool) or
                    base_index in seen_base or
                    candidate_index in seen_candidate or
                    base_index not in expected_reuse or
                    not 0 <= candidate_index < len(nodes)
                ):
                    return None
                expected = expected_reuse[base_index]
                current = nodes[candidate_index]
                if (
                    current["symbol"] != expected.get("symbol") or
                    current["state"] != expected.get("state") or
                    current["start"] != expected.get("start") or
                    current["end"] != expected.get("end") or
                    current["epsilon"] != expected.get("epsilon") or
                    hashlib.sha256(candidate[
                        current["start"]:current["end"]]).hexdigest() !=
                    expected.get("yield_sha256")
                ):
                    return None
                seen_base.add(base_index)
                seen_candidate.add(candidate_index)
            cache_reused_nodes = len(seen_base)
        reported_elapsed_us = 0
        telemetry = raw.get("incremental_telemetry")
        if telemetry is not None:
            if (
                not isinstance(telemetry, dict) or
                telemetry.get("schema") !=
                "symcc-tree-sitter-incremental-telemetry-v1" or
                telemetry.get("mode") not in {"cold", "incremental"}
            ):
                return None
            try:
                reported_elapsed_us = int(telemetry.get("elapsed_us", 0))
                reported_nodes = int(telemetry.get("nodes", 0))
                reported_reused = int(
                    telemetry.get("reused_node_ids", 0))
                reported_cache_entries = int(
                    telemetry.get("cache_entries_before_store", 0))
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                isinstance(telemetry.get("elapsed_us"), bool) or
                not 0 <= reported_elapsed_us <= 3600 * 1000 * 1000 or
                reported_nodes != len(nodes) or
                reported_reused != cache_reused_nodes or
                not 0 <= reported_cache_entries <= 4096 or
                (telemetry["mode"] == "incremental") != cache_receipt or
                reuse_proof != (
                    "tree-sitter-node-id-v1"
                    if cache_receipt else ""
                )
            ):
                return None
        forest_values = {
            "trace": 0,
            "complete": 0,
            "proof": 0,
            "raw_nodes": 0,
            "encoded_nodes": 0,
            "primary_clones": 0,
            "packed_alternatives": 0,
            "edges": 0,
            "nullable_rules": 0,
            "elapsed_us": 0,
            "grammar_sha256": "",
        }
        forest_telemetry = raw.get("forest_telemetry")
        if forest_telemetry is not None:
            forest_schema = (
                forest_telemetry.get("schema")
                if isinstance(forest_telemetry, dict) else "")
            if forest_schema == "symcc-lark-sppf-telemetry-v1":
                expected_forest_proof = (
                    "lark-earley-complete-sppf-v1")
                version_key = "lark_version"
            elif forest_schema == (
                    "symcc-parglare-glr-sppf-telemetry-v1"):
                expected_forest_proof = (
                    "parglare-glr-complete-sppf-v1")
                version_key = "parglare_version"
            else:
                return None
            if (
                telemetry is not None or
                not isinstance(forest_telemetry, dict) or
                forest_telemetry.get("proof") !=
                expected_forest_proof or
                not self._valid_digest(str(
                    forest_telemetry.get("grammar_sha256", ""))) or
                self._parser_label(
                    forest_telemetry.get(version_key)) is None or
                not isinstance(
                    forest_telemetry.get("complete"), bool)
            ):
                return None
            integer_fields = (
                "elapsed_us",
                "raw_nodes",
                "encoded_nodes",
                "primary_clones",
                "packed_alternatives",
                "edges",
                "nullable_rules",
            )
            if any(
                isinstance(forest_telemetry.get(key), bool) or
                not isinstance(forest_telemetry.get(key), int)
                for key in integer_fields
            ):
                return None
            elapsed_us = int(forest_telemetry["elapsed_us"])
            raw_nodes = int(forest_telemetry["raw_nodes"])
            encoded_nodes = int(forest_telemetry["encoded_nodes"])
            primary_clones = int(
                forest_telemetry["primary_clones"])
            packed_alternatives = int(
                forest_telemetry["packed_alternatives"])
            forest_edges = int(forest_telemetry["edges"])
            forest_nullable_rules = int(
                forest_telemetry["nullable_rules"])
            actual_alternatives = sum(
                len(node["alternatives"]) for node in nodes)
            actual_edges = sum(
                len(alternative)
                for node in nodes
                for alternative in node["alternatives"]
            )
            actual_nullable_rules = (
                len(raw.get("nullable_rules", ())) if trace_v4 else 0)
            complete = bool(forest_telemetry["complete"])
            if (
                complete != bool(raw["accepted"]) or
                not 0 <= elapsed_us <= 3600 * 1000 * 1000 or
                not 0 <= raw_nodes <= 4096 or
                encoded_nodes != len(nodes) or
                not 0 <= primary_clones <= encoded_nodes or
                packed_alternatives != actual_alternatives or
                forest_edges != actual_edges or
                forest_nullable_rules != actual_nullable_rules or
                not 0 <= forest_edges <= 16384 or
                not 0 <= forest_nullable_rules <= 32 or
                complete and raw_nodes == 0 or
                not complete and (
                    raw_nodes != 0 or
                    primary_clones != 0 or
                    forest_nullable_rules != 0
                ) or
                not trace_v4 and forest_nullable_rules != 0 or
                not forest_nullable_rules and
                encoded_nodes != raw_nodes + primary_clones and
                complete
            ):
                return None
            reported_elapsed_us = elapsed_us
            forest_values = {
                "trace": 1,
                "complete": int(complete),
                "proof": 1,
                "raw_nodes": raw_nodes,
                "encoded_nodes": encoded_nodes,
                "primary_clones": primary_clones,
                "packed_alternatives": packed_alternatives,
                "edges": forest_edges,
                "nullable_rules": forest_nullable_rules,
                "elapsed_us": elapsed_us,
                "grammar_sha256": str(
                    forest_telemetry["grammar_sha256"]),
            }
        cross_values: dict[str, Any] = {
            "trace": 0,
            "agreement": 0,
            "both_accept": 0,
            "primary_only": 0,
            "secondary_only": 0,
            "both_reject": 0,
            "primary_time_us": 0,
            "secondary_time_us": 0,
            "primary_command_sha256": "",
            "secondary_command_sha256": "",
            "structural_pair": 0,
            "primary_selected_spans": 0,
            "primary_forest_spans": 0,
            "secondary_spans": 0,
            "selected_shared_spans": 0,
            "forest_shared_spans": 0,
            "selected_union_spans": 0,
            "forest_union_spans": 0,
            "primary_boundaries": 0,
            "secondary_boundaries": 0,
            "shared_boundaries": 0,
            "union_boundaries": 0,
            "primary_symbols": 0,
            "secondary_symbols": 0,
            "symbol_correspondences": 0,
            "ambiguous_symbol_spans": 0,
            "production_correspondences": 0,
            "correspondence_sha256": "",
            "correspondence_json": "",
        }
        cross_telemetry = raw.get("cross_parser_telemetry")
        cross_trace = raw.get("cross_parser_trace")
        if (cross_telemetry is None) != (cross_trace is None):
            return None
        if cross_telemetry is not None:
            if (
                not allow_cross or
                candidate is None or
                not isinstance(cross_telemetry, dict) or
                not isinstance(cross_trace, dict) or
                cross_telemetry.get("schema") !=
                "symcc-cross-parser-telemetry-v1" or
                cross_telemetry.get("proof") !=
                "paired-independent-trace-v1"
            ):
                return None
            primary_command_sha256 = str(
                cross_telemetry.get("primary_command_sha256", ""))
            secondary_command_sha256 = str(
                cross_telemetry.get("secondary_command_sha256", ""))
            if (
                not self._valid_digest(primary_command_sha256) or
                not self._valid_digest(secondary_command_sha256) or
                primary_command_sha256 == secondary_command_sha256 or
                cross_telemetry.get("candidate_sha256") !=
                hashlib.sha256(candidate).hexdigest()
            ):
                return None
            integer_fields = (
                "primary_returncode",
                "secondary_returncode",
                "primary_elapsed_us",
                "secondary_elapsed_us",
            )
            boolean_fields = (
                "primary_accepted",
                "secondary_accepted",
            )
            if (
                any(
                    isinstance(cross_telemetry.get(key), bool) or
                    not isinstance(cross_telemetry.get(key), int)
                    for key in integer_fields
                ) or
                any(
                    not isinstance(cross_telemetry.get(key), bool)
                    for key in boolean_fields
                )
            ):
                return None
            primary_returncode = int(
                cross_telemetry["primary_returncode"])
            secondary_returncode = int(
                cross_telemetry["secondary_returncode"])
            primary_elapsed_us = int(
                cross_telemetry["primary_elapsed_us"])
            secondary_elapsed_us = int(
                cross_telemetry["secondary_elapsed_us"])
            primary_accepted = bool(
                cross_telemetry["primary_accepted"])
            secondary_accepted = bool(
                cross_telemetry["secondary_accepted"])
            if (
                primary_returncode != int(returncode) or
                primary_returncode not in {0, 1} or
                secondary_returncode not in {0, 1} or
                primary_accepted != bool(raw["accepted"]) or
                primary_accepted != (primary_returncode == 0) or
                secondary_accepted != (secondary_returncode == 0) or
                not 0 <= primary_elapsed_us <= 3600 * 1000 * 1000 or
                not 0 <= secondary_elapsed_us <= 3600 * 1000 * 1000 or
                cross_telemetry.get("primary_parser") != parser
            ):
                return None
            primary_core = dict(raw)
            primary_core.pop("cross_parser_telemetry", None)
            primary_core.pop("cross_parser_trace", None)
            try:
                primary_digest = hashlib.sha256(json.dumps(
                    primary_core,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                secondary_encoded = json.dumps(
                    cross_trace,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            except (TypeError, ValueError, UnicodeError):
                return None
            if (
                len(secondary_encoded) > 1024 * 1024 or
                cross_telemetry.get("primary_trace_sha256") !=
                primary_digest or
                cross_telemetry.get("secondary_trace_sha256") !=
                hashlib.sha256(secondary_encoded).hexdigest()
            ):
                return None
            secondary_path = ""
            try:
                fd, secondary_path = tempfile.mkstemp(
                    prefix=".cross-parser-secondary-",
                    suffix=".json",
                    dir=self.root,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secondary_encoded)
                secondary_result = self._load_parser_trace(
                    secondary_path,
                    candidate_size=candidate_size,
                    returncode=secondary_returncode,
                    candidate=candidate,
                    cache_manifest=None,
                    allow_cross=False,
                )
            except OSError:
                return None
            finally:
                if secondary_path:
                    try:
                        os.unlink(secondary_path)
                    except OSError:
                        pass
            if secondary_result is None:
                return None
            secondary_normalized = secondary_result[0]
            if (
                secondary_normalized["accepted"] != secondary_accepted or
                cross_telemetry.get("secondary_parser") !=
                secondary_normalized["parser"]
            ):
                return None
            structural_values = {
                "structural_pair": 0,
                "primary_selected_spans": 0,
                "primary_forest_spans": 0,
                "secondary_spans": 0,
                "selected_shared_spans": 0,
                "forest_shared_spans": 0,
                "selected_union_spans": 0,
                "forest_union_spans": 0,
                "primary_boundaries": 0,
                "secondary_boundaries": 0,
                "shared_boundaries": 0,
                "union_boundaries": 0,
                "primary_symbols": 0,
                "secondary_symbols": 0,
                "symbol_correspondences": 0,
                "ambiguous_symbol_spans": 0,
                "production_correspondences": 0,
                "correspondence_sha256": "",
                "correspondence_json": "",
            }
            if primary_accepted and secondary_accepted:
                primary_normalized = {
                    "schema": raw["schema"],
                    "parser": parser,
                    "nodes": nodes,
                    "primary_nodes": sorted(primary_nodes),
                }
                primary_selected = self._selected_trace_nodes(
                    primary_normalized)
                secondary_selected = self._selected_trace_nodes(
                    secondary_normalized)
                primary_selected_spans, _ = (
                    self._trace_span_features(
                        primary_normalized,
                        primary_selected,
                        candidate_size,
                    )
                )
                primary_forest_spans, primary_boundaries = (
                    self._trace_span_features(
                        primary_normalized,
                        set(range(len(nodes))),
                        candidate_size,
                    )
                )
                secondary_spans, secondary_boundaries = (
                    self._trace_span_features(
                        secondary_normalized,
                        secondary_selected,
                        candidate_size,
                    )
                )
                selected_shared = (
                    primary_selected_spans & secondary_spans)
                forest_shared = primary_forest_spans & secondary_spans
                selected_union = (
                    primary_selected_spans | secondary_spans)
                forest_union = primary_forest_spans | secondary_spans
                shared_boundaries = (
                    primary_boundaries & secondary_boundaries)
                union_boundaries = (
                    primary_boundaries | secondary_boundaries)
                try:
                    correspondence = self._cross_correspondence(
                        primary_normalized,
                        primary_selected,
                        secondary_normalized,
                        secondary_selected,
                    )
                except ValueError:
                    return None
                structural_values = {
                    "structural_pair": 1,
                    "primary_selected_spans":
                        len(primary_selected_spans),
                    "primary_forest_spans": len(primary_forest_spans),
                    "secondary_spans": len(secondary_spans),
                    "selected_shared_spans": len(selected_shared),
                    "forest_shared_spans": len(forest_shared),
                    "selected_union_spans": len(selected_union),
                    "forest_union_spans": len(forest_union),
                    "primary_boundaries": len(primary_boundaries),
                    "secondary_boundaries": len(secondary_boundaries),
                    "shared_boundaries": len(shared_boundaries),
                    "union_boundaries": len(union_boundaries),
                    "primary_symbols":
                        correspondence["primary_symbols"],
                    "secondary_symbols":
                        correspondence["secondary_symbols"],
                    "symbol_correspondences":
                        correspondence["symbol_observations"],
                    "ambiguous_symbol_spans":
                        correspondence["ambiguous_symbol_spans"],
                    "production_correspondences":
                        correspondence["production_observations"],
                    "correspondence_sha256":
                        correspondence["sha256"],
                    "correspondence_json":
                        correspondence["json"],
                }
            cross_values = {
                "trace": 1,
                "agreement": int(
                    primary_accepted == secondary_accepted),
                "both_accept": int(
                    primary_accepted and secondary_accepted),
                "primary_only": int(
                    primary_accepted and not secondary_accepted),
                "secondary_only": int(
                    not primary_accepted and secondary_accepted),
                "both_reject": int(
                    not primary_accepted and not secondary_accepted),
                "primary_time_us": primary_elapsed_us,
                "secondary_time_us": secondary_elapsed_us,
                "primary_command_sha256": primary_command_sha256,
                "secondary_command_sha256": secondary_command_sha256,
                **structural_values,
            }
        normalized = {
            "schema": raw["schema"],
            "parser": parser,
            "accepted": bool(raw["accepted"]),
            "nodes": nodes,
            "cache_reused_nodes": cache_reused_nodes,
            "cache_receipt": cache_receipt,
            "reuse_proof": reuse_proof,
            "reported_elapsed_us": reported_elapsed_us,
            "forest_values": forest_values,
            "cross_values": cross_values,
            "trace_bytes": len(encoded),
        }
        if trace_v3:
            normalized.update({
                "roots": roots,
                "primary_nodes": sorted(primary_nodes),
                "node_paths": {
                    str(node_index): list(path)
                    for node_index, path in node_paths.items()
                },
            })
        if nullable_certificate is not None:
            normalized["nullable_certificate"] = nullable_certificate
        return normalized, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _structural_context(
        trace: dict[str, Any],
        span_lo: int,
        span_hi: int,
        candidate: bytes,
        target_branch: int = 0,
    ) -> dict[str, Any] | None:
        if span_hi < span_lo:
            return None
        nodes = trace["nodes"]
        trace_v4 = (
            trace.get("schema") == "symcc-parser-structural-trace-v4")
        trace_v3 = trace.get("schema") in {
            "symcc-parser-structural-trace-v3",
            "symcc-parser-structural-trace-v4",
        }
        primary_nodes: set[int] = (
            set(trace.get("primary_nodes", ())) if trace_v3 else set()
        )
        if not trace_v3:
            for node_index, current in enumerate(nodes):
                parent = current["parent"]
                if parent < 0:
                    primary_nodes.add(node_index)
                    continue
                if parent not in primary_nodes:
                    continue
                alternatives = nodes[parent].get("alternatives")
                if (
                    not alternatives or
                    node_index in alternatives[0]
                ):
                    primary_nodes.add(node_index)
        enclosing = []
        for index, node in enumerate(nodes):
            if index not in primary_nodes:
                continue
            contains = (
                node["start"] <= span_lo and
                node["end"] >= span_hi
                if span_hi > span_lo else
                node["start"] <= span_lo <= node["end"]
            )
            if contains:
                enclosing.append((
                    node["end"] - node["start"], -index, index, node))
        if not enclosing:
            return None
        _, _, index, node = min(enclosing)
        ancestry: list[list[str]] = []
        ancestry_indices: list[int] = []
        cursor = index
        while cursor >= 0 and len(ancestry) < 32:
            current = nodes[cursor]
            ancestry.append([current["symbol"], current["state"]])
            ancestry_indices.append(cursor)
            cursor = current["parent"]
        if cursor >= 0:
            return None
        ancestry.reverse()
        ancestry_indices.reverse()
        evidence_indices = list(ancestry_indices)
        evidence_paths: dict[int, list[int]] = {
            node_index: ancestry_indices[:position + 1]
            for position, node_index in enumerate(ancestry_indices)
        }
        if trace_v3:
            queued = list(ancestry_indices)
            queue_position = 0
            while queue_position < len(queued) and len(evidence_indices) < 32:
                parent = queued[queue_position]
                queue_position += 1
                for alternative in nodes[parent]["alternatives"]:
                    for child in alternative:
                        if child in evidence_paths:
                            continue
                        evidence_paths[child] = evidence_paths[parent] + [child]
                        evidence_indices.append(child)
                        queued.append(child)
                        if len(evidence_indices) >= 32:
                            break
                    if len(evidence_indices) >= 32:
                        break
        payload = json.dumps(
            {
                "parser": trace["parser"],
                "ancestry": ancestry,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        def child_alternatives(parent: int) -> list[list[int]] | None:
            children = sorted(
                (
                    child_index
                    for child_index, child in enumerate(nodes)
                    if child["parent"] == parent
                ),
                key=lambda child_index: (
                    nodes[child_index]["start"],
                    nodes[child_index]["end"],
                    child_index,
                ),
            )
            alternatives = nodes[parent].get("alternatives") or [children]
            for alternative in alternatives:
                if any(
                    nodes[left]["end"] > nodes[right]["start"]
                    for left, right in zip(
                        alternative, alternative[1:])
                ):
                    return None
            return [list(alternative) for alternative in alternatives]

        def ordered_children(parent: int) -> list[int] | None:
            alternatives = child_alternatives(parent)
            return alternatives[0] if alternatives else None

        productions_by_node: dict[int, dict[str, Any]] = {}
        production_variants_by_node: dict[
            int, list[dict[str, Any]]
        ] = {}
        all_productions: dict[str, dict[str, Any]] = {}
        instances: list[dict[str, Any]] = []
        trace_v2 = trace.get("schema") in {
            "symcc-parser-structural-trace-v2",
            "symcc-parser-structural-trace-v3",
            "symcc-parser-structural-trace-v4",
        }
        packed_node_ids: dict[int, str] = {}
        packed_nodes: list[dict[str, Any]] = []
        if trace_v3:
            for node_index in evidence_indices:
                current = nodes[node_index]
                node_core = {
                    "schema": "symcc-parser-packed-node-v1",
                    "parser": trace["parser"],
                    "order": node_index,
                    "symbol": current["symbol"],
                    "state": current["state"],
                    "start": current["start"],
                    "end": current["end"],
                    "epsilon": bool(current["epsilon"]),
                    "yield_sha256": hashlib.sha256(candidate[
                        current["start"]:current["end"]]).hexdigest(),
                }
                node_id = hashlib.sha256(json.dumps(
                    node_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                packed_node_ids[node_index] = node_id
                packed_nodes.append({
                    "node_id": node_id,
                    **{
                        key: value
                        for key, value in node_core.items()
                        if key not in {"schema", "parser"}
                    },
                })
        for production_index in evidence_indices:
            production = nodes[production_index]
            alternatives = child_alternatives(production_index)
            if alternatives is None:
                return None
            variants: list[dict[str, Any]] = []
            for alternative_index, child_indices in enumerate(alternatives):
                rhs = [
                    {
                        "symbol": nodes[child_index]["symbol"],
                        "state": nodes[child_index]["state"],
                        "recursive": (
                            nodes[child_index]["symbol"] ==
                            production["symbol"]
                        ),
                    }
                    for child_index in child_indices
                ]
                terminal_gaps: list[str] = []
                terminal_gap_hex: list[str] = []
                gap_cursor = production["start"]
                for child_index in child_indices:
                    child = nodes[child_index]
                    gap = candidate[gap_cursor:child["start"]]
                    terminal_gaps.append(
                        hashlib.sha256(gap).hexdigest())
                    terminal_gap_hex.append(gap.hex())
                    gap_cursor = child["end"]
                gap = candidate[gap_cursor:production["end"]]
                terminal_gaps.append(hashlib.sha256(gap).hexdigest())
                terminal_gap_hex.append(gap.hex())
                epsilon = bool(
                    production.get("epsilon", False))
                production_schema = (
                    "symcc-parser-production-v2"
                    if trace_v2 else
                    "symcc-parser-production-v1"
                )
                shape_schema = (
                    "symcc-parser-production-shape-v2"
                    if trace_v2 else
                    "symcc-parser-production-shape-v1"
                )
                shape_core = {
                    "schema": shape_schema,
                    "parser": trace["parser"],
                    "lhs": production["symbol"],
                    "state": production["state"],
                    "rhs": rhs,
                }
                if trace_v2:
                    shape_core["epsilon"] = epsilon
                shape_id = hashlib.sha256(json.dumps(
                    shape_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                production_core = {
                    "schema": production_schema,
                    "parser": trace["parser"],
                    "lhs": production["symbol"],
                    "state": production["state"],
                    "rhs": rhs,
                    "terminal_gaps": terminal_gaps,
                }
                if trace_v2:
                    production_core["epsilon"] = epsilon
                production_id = hashlib.sha256(json.dumps(
                    production_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                variant: dict[str, Any] = {
                    "id": production_id,
                    "shape_id": shape_id,
                    "lhs": production["symbol"],
                    "state": production["state"],
                    "rhs": rhs,
                    "terminal_gaps": terminal_gaps,
                }
                if trace_v2:
                    variant.update({
                        "schema": production_schema,
                        "epsilon": epsilon,
                        "alternative": alternative_index,
                    })
                variants.append(variant)
                previous = all_productions.get(production_id)
                if (
                    previous is None or
                    int(variant.get("alternative", 0)) <
                    int(previous.get("alternative", 0))
                ):
                    all_productions[production_id] = variant
                subtree = candidate[
                    production["start"]:production["end"]]
                if (
                    len(subtree) <= 256 and
                    sum(len(bytes.fromhex(item))
                        for item in terminal_gap_hex) <= 256
                ):
                    instance_path_indices = evidence_paths[production_index]
                    instance_path = [
                        [nodes[path_index]["symbol"],
                         nodes[path_index]["state"]]
                        for path_index in instance_path_indices
                    ]
                    instance_core = {
                        "schema": (
                            "symcc-parser-subtree-instance-v2"
                            if trace_v3 else
                            "symcc-parser-subtree-instance-v1"
                        ),
                        "production_id": production_id,
                        "shape_id": shape_id,
                        "yield_sha256": hashlib.sha256(
                            subtree).hexdigest(),
                        "path": instance_path,
                    }
                    if trace_v3:
                        instance_core.update({
                            "node_id":
                                packed_node_ids[production_index],
                            "node_path": [
                                packed_node_ids[path_index]
                                for path_index in instance_path_indices
                            ],
                        })
                    instance_id = hashlib.sha256(json.dumps(
                        instance_core,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")).hexdigest()
                    instance = {
                        "instance_id": instance_id,
                        "production_id": production_id,
                        "shape_id": shape_id,
                        "path": instance_path,
                        "yield_hex": subtree.hex(),
                        "yield_sha256":
                            instance_core["yield_sha256"],
                        "terminal_gap_hex": terminal_gap_hex,
                        "selected": (
                            production_index == index and
                            alternative_index == 0
                        ),
                    }
                    if trace_v3:
                        instance.update({
                            "schema":
                                "symcc-parser-subtree-instance-v2",
                            "node_id": packed_node_ids[production_index],
                            "node_path": instance_core["node_path"],
                        })
                    instances.append(instance)
            if not variants:
                return None
            production_variants_by_node[production_index] = variants
            productions_by_node[production_index] = variants[0]

        packed_edges: list[dict[str, Any]] = []
        if trace_v3:
            edge_keys: set[tuple[int, int, int, int]] = set()

            def add_packed_edge(
                parent: int,
                child: int,
                alternative: int,
                slot: int,
            ) -> None:
                key = (parent, child, alternative, slot)
                if (
                    key in edge_keys or
                    len(packed_edges) >= 128 or
                    parent not in packed_node_ids or
                    child not in packed_node_ids
                ):
                    return
                edge_keys.add(key)
                edge_core = {
                    "schema": "symcc-parser-packed-edge-v1",
                    "parser": trace["parser"],
                    "parent_id": packed_node_ids[parent],
                    "child_id": packed_node_ids[child],
                    "alternative": alternative,
                    "slot": slot,
                }
                edge_id = hashlib.sha256(json.dumps(
                    edge_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                packed_edges.append({
                    "edge_id": edge_id,
                    "parent_id": edge_core["parent_id"],
                    "child_id": edge_core["child_id"],
                    "alternative": alternative,
                    "slot": slot,
                })

            # Preserve a complete derivation path for every retained node
            # before filling the remaining edge budget with shared branches.
            for path in evidence_paths.values():
                for parent, child in zip(path, path[1:]):
                    occurrences = [
                        (alternative_index, slot)
                        for alternative_index, alternative in enumerate(
                            nodes[parent]["alternatives"])
                        for slot, candidate_child in enumerate(alternative)
                        if candidate_child == child
                    ]
                    if occurrences:
                        alternative_index, slot = min(occurrences)
                        add_packed_edge(
                            parent, child, alternative_index, slot)
            for parent in evidence_indices:
                for alternative_index, alternative in enumerate(
                        nodes[parent]["alternatives"]):
                    for slot, child in enumerate(alternative):
                        add_packed_edge(
                            parent, child, alternative_index, slot)

        cycle_pairs: set[tuple[int, int]] = set()
        for ancestor_index in ancestry_indices:
            ancestor = nodes[ancestor_index]
            child_indices = ordered_children(ancestor_index)
            if child_indices is None:
                return None
            cycle_pairs.update(
                (ancestor_index, child_index)
                for child_index in child_indices
                if nodes[child_index]["symbol"] == ancestor["symbol"]
            )
        for outer_position, outer_index in enumerate(ancestry_indices):
            for descendant_index in ancestry_indices[outer_position + 1:]:
                if (
                    nodes[descendant_index]["symbol"] ==
                    nodes[outer_index]["symbol"]
                ):
                    cycle_pairs.add((outer_index, descendant_index))

        templates: list[dict[str, Any]] = []
        ancestry_positions = {
            node_index: position
            for position, node_index in enumerate(ancestry_indices)
        }
        for outer_index, descendant_index in sorted(cycle_pairs):
            if len(templates) >= 16:
                break
            outer = nodes[outer_index]
            descendant = nodes[descendant_index]
            if not (
                outer["start"] <= descendant["start"] and
                descendant["end"] <= outer["end"]
            ):
                continue
            prefix = candidate[outer["start"]:descendant["start"]]
            suffix = candidate[descendant["end"]:outer["end"]]
            if (
                not prefix and not suffix or
                len(prefix) + len(suffix) > 128
            ):
                continue
            if (
                outer_index in ancestry_positions and
                descendant_index in ancestry_positions
            ):
                outer_position = ancestry_positions[outer_index]
                descendant_position = ancestry_positions[descendant_index]
                if descendant_position <= outer_position:
                    continue
                path_indices = ancestry_indices[
                    outer_position:descendant_position + 1]
                first_child = path_indices[1]
            elif nodes[descendant_index]["parent"] == outer_index:
                path_indices = [outer_index, descendant_index]
                first_child = descendant_index
            else:
                continue
            children = ordered_children(outer_index)
            if children is None or first_child not in children:
                continue
            slot = children.index(first_child)
            direct_recursive_slots = sum(
                nodes[child_index]["symbol"] == outer["symbol"]
                for child_index in children
            )
            cycle_core = {
                "schema": "symcc-parser-cycle-v1",
                "parser": trace["parser"],
                "production_id": productions_by_node[outer_index]["id"],
                "slot": slot,
                "path": [
                    [nodes[path_index]["symbol"], nodes[path_index]["state"]]
                    for path_index in path_indices
                ],
            }
            cycle_id = hashlib.sha256(json.dumps(
                cycle_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            templates.append({
                "cycle_id": cycle_id,
                "production_id": productions_by_node[outer_index]["id"],
                "lhs": outer["symbol"],
                "slot": slot,
                "kind": (
                    "direct"
                    if len(path_indices) == 2 and
                    nodes[first_child]["symbol"] == outer["symbol"]
                    else "mutual"
                ),
                "path": cycle_core["path"],
                "recursive_slots": max(1, direct_recursive_slots),
                "prefix_hex": prefix.hex(),
                "suffix_hex": suffix.hex(),
            })

        templates = list({
            template["cycle_id"]: template for template in templates
        }.values())
        templates.sort(key=lambda item: (
            len(item["path"]),
            item["cycle_id"],
        ))
        primary_production = (
            next(
                productions_by_node[node_index]
                for node_index in ancestry_indices
                if templates and
                productions_by_node[node_index]["id"] ==
                templates[0]["production_id"]
            )
            if templates else productions_by_node[index]
        )
        # Every primary production is required by the selected ancestry and
        # cycle certificates. Fill the remaining bounded fragment budget with
        # packed alternatives in parser order, merging canonical duplicates.
        selected_productions: dict[str, dict[str, Any]] = {}
        for node_index in ancestry_indices:
            production = productions_by_node[node_index]
            selected_productions.setdefault(production["id"], production)
        for node_index in evidence_indices:
            if len(selected_productions) >= 32:
                break
            production = productions_by_node[node_index]
            selected_productions.setdefault(production["id"], production)
        for node_index in evidence_indices:
            for production in production_variants_by_node[node_index][1:]:
                if len(selected_productions) >= 32:
                    break
                selected_productions.setdefault(
                    production["id"],
                    all_productions[production["id"]],
                )
            if len(selected_productions) >= 32:
                break
        selected_production_ids = set(selected_productions)
        unique_instances: dict[str, dict[str, Any]] = {}
        for instance in instances:
            if instance["production_id"] not in selected_production_ids:
                continue
            previous = unique_instances.get(instance["instance_id"])
            if previous is None or (
                instance["selected"] and not previous["selected"]
            ):
                unique_instances[instance["instance_id"]] = instance
        instances = sorted(
            unique_instances.values(),
            key=lambda item: item["instance_id"],
        )
        packed_alternatives = sum(
            int(production.get("alternative", 0)) > 0
            for production in selected_productions.values()
        )
        epsilon_productions = sum(
            production.get("epsilon") is True
            for production in selected_productions.values()
        )
        cfg_fragment = {
            "schema": (
                "symcc-parser-cfg-fragment-v5"
                if trace_v4 else
                "symcc-parser-cfg-fragment-v4"
                if trace_v3 else
                "symcc-parser-cfg-fragment-v3"
                if trace_v2 else
                "symcc-parser-cfg-fragment-v2"
            ),
            "parser": trace["parser"],
            "context_id": hashlib.sha256(payload).hexdigest(),
            "productions": sorted(
                selected_productions.values(),
                key=lambda item: item["id"],
            ),
            "cycles": templates,
            "instances": instances,
        }
        if trace_v3:
            cfg_fragment.update({
                "packed_nodes": sorted(
                    packed_nodes,
                    key=lambda item: item["order"],
                ),
                "packed_edges": sorted(
                    packed_edges,
                    key=lambda item: (
                        item["parent_id"],
                        item["alternative"],
                        item["slot"],
                        item["child_id"],
                    ),
                ),
                "roots": [
                    packed_node_ids[root]
                    for root in trace["roots"]
                    if root in packed_node_ids
                ],
                "selected_path": [
                    packed_node_ids[node_index]
                    for node_index in ancestry_indices
                ],
                "truncated": (
                    len(evidence_indices) < len(nodes) or
                    len(packed_edges) < sum(
                        child in packed_node_ids
                        for parent in evidence_indices
                        for alternative in nodes[parent]["alternatives"]
                        for child in alternative
                    )
                ),
            })
        if trace_v4:
            cfg_fragment.update(trace["nullable_certificate"])
        cfg_fragment_json = json.dumps(
            cfg_fragment,
            sort_keys=True,
            separators=(",", ":"),
        )
        while (
            (
                len(cfg_fragment["instances"]) > 32 or
                len(cfg_fragment_json.encode("utf-8")) > 65536
            ) and
            cfg_fragment["instances"]
        ):
            removable = max(
                range(len(cfg_fragment["instances"])),
                key=lambda position: (
                    not cfg_fragment["instances"][position]["selected"],
                    len(cfg_fragment["instances"][position]["path"]),
                    len(cfg_fragment["instances"][position]["yield_hex"]),
                    cfg_fragment["instances"][position]["instance_id"],
                ),
            )
            del cfg_fragment["instances"][removable]
            cfg_fragment_json = json.dumps(
                cfg_fragment,
                sort_keys=True,
                separators=(",", ":"),
            )
        if len(cfg_fragment_json.encode("utf-8")) > 65536:
            return None
        primary_template = templates[0] if templates else None
        selected_instance = next(
            (
                instance for instance in cfg_fragment["instances"]
                if instance["selected"]
            ),
            None,
        )
        return {
            "context_id": hashlib.sha256(payload).hexdigest(),
            "symbol": str(node["symbol"]),
            "state": str(node["state"]),
            "production_id": primary_production["id"],
            "production_lhs": str(primary_production["lhs"]),
            "production_state": str(primary_production["state"]),
            "production_arity": len(primary_production["rhs"]),
            "recursive_depth": (
                sum(
                    path_item[0] == primary_template["lhs"]
                    for path_item in primary_template["path"]
                )
                if primary_template else 0
            ),
            "recursion_prefix_hex": (
                primary_template["prefix_hex"] if primary_template else ""),
            "recursion_suffix_hex": (
                primary_template["suffix_hex"] if primary_template else ""),
            "cfg_fragment_json": cfg_fragment_json,
            "cfg_fragment_sha256": hashlib.sha256(
                cfg_fragment_json.encode("utf-8")).hexdigest(),
            "cfg_productions": len(cfg_fragment["productions"]),
            "cfg_cycles": len(templates),
            "cfg_recursive_slots": sum(
                int(template["recursive_slots"]) for template in templates),
            "cfg_alternatives": packed_alternatives,
            "epsilon_productions": epsilon_productions,
            "packed_nodes": len(packed_nodes),
            "packed_edges": len(packed_edges),
            "nullable_rules": len(
                trace.get(
                    "nullable_certificate", {}).get(
                        "nullable_rules", ())),
            "nullable_proofs": len(
                trace.get(
                    "nullable_certificate", {}).get(
                        "nullable_proofs", ())),
            "nullable_sccs": len(
                trace.get(
                    "nullable_certificate", {}).get(
                        "nullable_sccs", ())),
            "candidate_context_id":
                VerifiedProposalManager._grammar_lexical_context_id(
                    candidate, span_lo, span_hi, target_branch),
            "ect_shape_id": (
                selected_instance["shape_id"] if selected_instance else ""),
            "ect_instance_id": (
                selected_instance["instance_id"] if selected_instance else ""),
            "ect_instances": len(cfg_fragment["instances"]),
        }

    @staticmethod
    def _decode_data(item: dict[str, Any]) -> bytes | None:
        if "hex" in item:
            try:
                return bytes.fromhex(str(item["hex"]))
            except ValueError:
                return None
        if "text" in item:
            return str(item["text"]).encode("utf-8")
        return None

    def _materialize(self, raw: dict[str, Any]) -> tuple[str, str] | None:
        direct = raw.get("candidate")
        if isinstance(direct, dict):
            content = self._decode_data(direct)
            if content is None:
                return None
        else:
            source = str(raw.get("source_path", "") or "")
            if not source or not os.path.isfile(source):
                return None
            try:
                with open(source, "rb") as stream:
                    content = stream.read(self.max_candidate_bytes + 1)
            except OSError:
                return None
            if len(content) > self.max_candidate_bytes:
                return None
            candidate = bytearray(content)
            patch_total = 0
            patches = raw.get("patches", ())
            if not isinstance(patches, (list, tuple)):
                return None
            for patch in patches:
                if not isinstance(patch, dict):
                    return None
                offset = _positive_int(patch.get("offset"))
                data = self._decode_data(patch)
                if data is None:
                    return None
                patch_total += len(data)
                if patch_total > self.max_patch_bytes:
                    return None
                end = offset + len(data)
                if end > self.max_candidate_bytes:
                    return None
                if end > len(candidate):
                    candidate.extend(b"\x00" * (end - len(candidate)))
                candidate[offset:end] = data
            append = raw.get("append")
            if append is not None:
                if not isinstance(append, dict):
                    return None
                data = self._decode_data(append)
                if data is None or patch_total + len(data) > self.max_patch_bytes:
                    return None
                candidate.extend(data)
            if "truncate" in raw:
                truncate = _positive_int(raw.get("truncate"))
                if truncate > len(candidate):
                    return None
                del candidate[truncate:]
            content = bytes(candidate)
        if len(content) > self.max_candidate_bytes:
            return None
        digest = hashlib.sha256(content).hexdigest()
        path = os.path.join(self.root, "candidates", digest)
        if not os.path.isfile(path):
            temporary = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
            try:
                with open(temporary, "xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except FileExistsError:
                pass
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return path, digest

    def ingest(self, raw: Any, *, now: float | None = None) -> str | None:
        if not isinstance(raw, dict):
            self.rejected_inputs += 1
            return None
        kind = str(raw.get("kind", "semantic")).strip().lower()
        target = _positive_int(raw.get("target_branch"))
        source = str(raw.get("source_path", "") or "")
        if kind not in _KINDS or (not source and "candidate" not in raw):
            self.rejected_inputs += 1
            return None
        proposal_id = str(raw.get("id", "") or _canonical_id(raw))
        if (len(proposal_id) != 64 or any(
                char not in "0123456789abcdef" for char in proposal_id)):
            proposal_id = _canonical_id(raw)
        if proposal_id in self.records:
            self.duplicate_inputs += 1
            return proposal_id
        now = time.time() if now is None else float(now)
        grammar_rule_id = str(raw.get("grammar_rule_id", "")).lower()
        if (
            len(grammar_rule_id) != 64 or
            any(char not in "0123456789abcdef" for char in grammar_rule_id)
        ):
            grammar_rule_id = ""
        grammar_context_id = str(
            raw.get("grammar_context_id", "")).lower()
        if grammar_context_id and (
            len(grammar_context_id) != 64 or
            any(char not in "0123456789abcdef"
                for char in grammar_context_id)
        ):
            grammar_context_id = ""
        grammar_source_context_id = str(
            raw.get("grammar_source_context_id", grammar_context_id)).lower()
        if grammar_source_context_id and not self._valid_digest(
                grammar_source_context_id):
            grammar_source_context_id = ""
        grammar_ect_shape_id = str(
            raw.get("grammar_ect_shape_id", "")).lower()
        grammar_ect_instance_id = str(
            raw.get("grammar_ect_instance_id", "")).lower()
        if grammar_ect_shape_id and not self._valid_digest(
                grammar_ect_shape_id):
            grammar_ect_shape_id = ""
        if grammar_ect_instance_id and not self._valid_digest(
                grammar_ect_instance_id):
            grammar_ect_instance_id = ""
        if bool(grammar_ect_shape_id) != bool(grammar_ect_instance_id):
            grammar_ect_shape_id = ""
            grammar_ect_instance_id = ""
        grammar_sync_transaction_id = str(
            raw.get("grammar_sync_transaction_id", "")).lower()
        if grammar_sync_transaction_id and (
            not grammar_rule_id or
            not self._valid_digest(grammar_sync_transaction_id) or
            grammar_ect_shape_id or grammar_ect_instance_id
        ):
            self.rejected_inputs += 1
            return None
        grammar_span_lo = 0
        grammar_span_hi = 0
        grammar_span_present = False
        grammar_span = raw.get("grammar_span")
        if grammar_rule_id and grammar_span is not None:
            direct = raw.get("candidate")
            direct_content = (
                self._decode_data(direct)
                if isinstance(direct, dict) else None
            )
            if (
                not isinstance(grammar_span, list) or
                len(grammar_span) != 2 or
                any(isinstance(value, bool) or not isinstance(value, int)
                    for value in grammar_span) or
                direct_content is None or
                not 0 <= grammar_span[0] <= grammar_span[1] <= len(
                    direct_content)
            ):
                self.rejected_inputs += 1
                return None
            grammar_span_lo, grammar_span_hi = grammar_span
            grammar_span_present = True
        if grammar_sync_transaction_id and not grammar_span_present:
            self.rejected_inputs += 1
            return None
        history_seed_id = str(raw.get("history_seed_id", "")).lower()
        if kind == "history_acquisition":
            if (
                len(history_seed_id) != 64 or
                any(char not in "0123456789abcdef"
                    for char in history_seed_id)
            ):
                self.rejected_inputs += 1
                return None
        else:
            history_seed_id = ""
        query_id = ""
        query_ir_verified = False
        query_hole_lo = 0
        query_hole_hi = 0
        query_hole_manifest_sha256 = ""
        if kind == "query_hole_completion":
            certificate = raw.get("query_hole_certificate")
            if not isinstance(certificate, dict):
                self.rejected_inputs += 1
                return None
            query_id = str(certificate.get("query_id", "")).lower()
            query_hole_manifest_sha256 = str(
                certificate.get("source_manifest_sha256", "")).lower()
            hole = certificate.get("hole")
            candidate_span = certificate.get("candidate_span")
            query_ir_verified = bool(
                certificate.get("query_ir_verified", False))
            if (
                certificate.get("schema") !=
                "symcc-query-grammar-hole-v1" or
                len(query_id) != 64 or
                any(char not in "0123456789abcdef" for char in query_id) or
                len(query_hole_manifest_sha256) != 64 or
                any(char not in "0123456789abcdef"
                    for char in query_hole_manifest_sha256) or
                not isinstance(hole, list) or len(hole) != 2 or
                any(isinstance(value, bool) or not isinstance(value, int)
                    for value in hole) or
                hole[0] < 0 or hole[1] <= hole[0] or
                hole[1] > self.max_candidate_bytes or
                not query_ir_verified or
                not bool(certificate.get("target_replay_required", False)) or
                not bool(
                    certificate.get("coverage_retention_required", False)) or
                str(certificate.get("grammar_rule_id", "")) !=
                grammar_rule_id or
                grammar_span_present and candidate_span != [
                    grammar_span_lo, grammar_span_hi] or
                certificate.get("grammar_context_id") is not None and
                str(certificate.get("grammar_context_id", "")) !=
                grammar_context_id or
                certificate.get("grammar_source_context_id") is not None and
                str(certificate.get("grammar_source_context_id", "")) !=
                grammar_source_context_id
            ):
                self.rejected_inputs += 1
                return None
            query_hole_lo, query_hole_hi = hole
        # Reject malformed provenance before writing candidate bytes to the
        # content-addressed store.
        materialized = self._materialize(raw)
        if materialized is None:
            self.rejected_inputs += 1
            return None
        path, digest = materialized
        if digest in {
                record.candidate_sha256 for record in self.records.values()}:
            self.duplicate_inputs += 1
            return None
        record = VerifiedProposal(
            proposal_id=proposal_id,
            kind=kind,
            source_path=source,
            candidate_path=path,
            candidate_sha256=digest,
            target_branch=target,
            generator=str(raw.get("generator", ""))[:128],
            grammar_rule_id=grammar_rule_id,
            grammar_context_id=grammar_context_id,
            grammar_source_context_id=grammar_source_context_id,
            grammar_ect_shape_id=grammar_ect_shape_id,
            grammar_ect_instance_id=grammar_ect_instance_id,
            grammar_sync_transaction_id=grammar_sync_transaction_id,
            grammar_span_present=grammar_span_present,
            grammar_span_lo=grammar_span_lo,
            grammar_span_hi=grammar_span_hi,
            query_id=query_id,
            query_ir_verified=query_ir_verified,
            query_hole_lo=query_hole_lo,
            query_hole_hi=query_hole_hi,
            query_hole_manifest_sha256=query_hole_manifest_sha256,
            history_seed_id=history_seed_id,
            created=now,
            updated=now,
        )
        self.records[proposal_id] = record
        self.path_records[path] = proposal_id
        return proposal_id

    def scan(self) -> int:
        if not self.proposal_path:
            return 0
        added = 0
        try:
            metadata = os.stat(self.proposal_path)
            identity = f"{metadata.st_dev}:{metadata.st_ino}"
            if identity != self.scan_identity or metadata.st_size < self.scan_offset:
                self.scan_offset = 0
                self.scan_identity = identity
            with open(
                    self.proposal_path,
                    encoding="utf-8", errors="replace") as stream:
                stream.seek(self.scan_offset)
                for line in stream:
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    before = len(self.records)
                    self.ingest(raw)
                    added += len(self.records) - before
                self.scan_offset = stream.tell()
        except OSError:
            return 0
        if added:
            self.save()
        return added

    def claim_pending(
        self,
        limit: int,
        *,
        now: float | None = None,
    ) -> list[VerifiedProposal]:
        now = time.time() if now is None else float(now)
        candidates = []
        for record in self.records.values():
            retryable = (
                record.status in {"pending", "retry"}
                and record.attempts < self.max_attempts
                and now - record.updated >= (
                    0.0 if record.status == "pending" else self.retry_delay)
            )
            if retryable and os.path.isfile(record.candidate_path):
                candidates.append(record)
        candidates.sort(key=lambda record: (
            record.attempts,
            record.created,
            record.proposal_id,
        ))
        selected = candidates[:max(0, int(limit))]
        for record in selected:
            record.status = "queued"
            record.updated = now
        return selected

    def for_path(self, path: str) -> VerifiedProposal | None:
        proposal_id = self.path_records.get(path)
        return self.records.get(proposal_id or "")

    def mark_dispatched(self, proposal_id: str) -> None:
        record = self.records.get(proposal_id)
        if record is None:
            return
        record.status = "dispatched"
        record.attempts += 1
        record.updated = time.time()

    def abandon_dispatch(
        self,
        proposal_id: str,
        *,
        executed: bool,
        now: float | None = None,
    ) -> bool:
        """Return a lost dispatch to a retryable state without validation."""
        record = self.records.get(proposal_id)
        if record is None or record.status != "dispatched":
            return False
        now = time.time() if now is None else float(now)
        if executed:
            record.status = (
                "retry" if record.attempts < self.max_attempts else "rejected")
            record.last_reason = "dispatch-result-lost"
        else:
            record.attempts = max(0, record.attempts - 1)
            record.status = "pending"
            record.last_reason = "dispatch-not-sent"
        record.updated = now
        return True

    def validate(
        self,
        proposal_id: str,
        telemetry: Any,
        *,
        retcode: int,
        killed: bool,
    ) -> bool:
        record = self.records.get(proposal_id)
        if record is None:
            return False
        record.validations += 1
        record.updated = time.time()
        if killed:
            valid = False
            reason = "execution-timeout"
        elif int(retcode) < 0:
            valid = False
            reason = "execution-failed"
        elif telemetry is None:
            valid = record.target_branch == 0
            reason = "concrete-ok" if valid else "telemetry-missing"
        elif record.target_branch:
            observed_target = _positive_int(
                getattr(telemetry, "target_branch", 0))
            reached = bool(getattr(telemetry, "target_reached", False))
            valid = reached and observed_target == record.target_branch
            reason = "target-reached" if valid else "target-not-reached"
        else:
            valid = True
            reason = "concrete-ok"
        if valid and self.parser_command:
            trace_path = ""
            cache_path = ""
            cache_manifest: dict[str, Any] | None = None
            record.parser_cache_manifest_sha256 = ""
            record.parser_cache_base_sha256 = ""
            record.parser_cache_reused_nodes = 0
            record.parser_cache_invalidated_nodes = 0
            record.parser_cache_hit = False
            trace_requested = "{trace}" in self.parser_command
            if trace_requested:
                trace_path = os.path.join(
                    self.root,
                    f".parser-trace-{os.getpid()}-{time.time_ns()}.json",
                )
            candidate_content = b""
            if trace_requested:
                try:
                    with open(record.candidate_path, "rb") as stream:
                        candidate_content = stream.read(
                            self.max_candidate_bytes + 1)
                except OSError:
                    candidate_content = b""
            cache_requested = (
                trace_requested and
                "{cache}" in self.parser_command and
                self.parser_cache_enabled
            )
            if cache_requested and len(
                    candidate_content) <= self.max_candidate_bytes:
                cache_path, cache_manifest = (
                    self._build_parser_cache_manifest(
                        record, candidate_content)
                )
                record.parser_cache_manifest_sha256 = str(
                    cache_manifest.get("manifest_sha256", ""))
                record.parser_cache_base_sha256 = str(
                    cache_manifest.get("base_input_sha256", ""))
                record.parser_cache_invalidated_nodes = len(
                    cache_manifest.get("invalidated_nodes", ()))
                record.parser_cache_invalidated_nodes_total += (
                    record.parser_cache_invalidated_nodes)
                record.parser_cache_requests += 1
                if record.parser_cache_base_sha256:
                    record.parser_cache_incremental_offers += 1
            command = [
                cache_path if item == "{cache}" else
                record.candidate_path if item == "{input}" else
                trace_path if item == "{trace}" else item
                for item in self.parser_command
            ]
            if "{input}" not in self.parser_command:
                command.append(record.candidate_path)
            record.parser_validations += 1
            parser_started = time.monotonic_ns()
            try:
                parser = subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.parser_timeout,
                )
                parser_valid = parser.returncode == 0
                trace_result = (
                    self._load_parser_trace(
                        trace_path,
                        candidate_size=len(candidate_content),
                        returncode=parser.returncode,
                        candidate=candidate_content,
                        cache_manifest=cache_manifest,
                    )
                    if trace_requested else None
                )
                trace_valid = not trace_requested or trace_result is not None
                if trace_result is not None:
                    trace, digest = trace_result
                    record.parser_trace_sha256 = digest
                    record.parser_trace_nodes = len(trace["nodes"])
                    record.parser_trace_bytes += int(
                        trace.get("trace_bytes", 0))
                    record.parser_cache_reused_nodes = int(
                        trace.get("cache_reused_nodes", 0))
                    record.parser_cache_reused_nodes_total += (
                        record.parser_cache_reused_nodes)
                    record.parser_cache_hit = (
                        record.parser_cache_reused_nodes > 0)
                    if trace.get("cache_receipt", False):
                        record.parser_incremental_receipts += 1
                        if not record.parser_cache_reused_nodes:
                            record.parser_incremental_zero_reuse += 1
                    if (
                        trace.get("reuse_proof") ==
                        "tree-sitter-node-id-v1"
                    ):
                        record.parser_node_id_proofs += 1
                    record.parser_reported_parse_time_us += int(
                        trace.get("reported_elapsed_us", 0))
                    forest = trace.get("forest_values", {})
                    forest_trace = int(forest.get("trace", 0))
                    forest_grammar = str(
                        forest.get("grammar_sha256", ""))
                    forest_identity_valid = (
                        not forest_trace or
                        not record.parser_forest_grammar_sha256 or
                        record.parser_forest_grammar_sha256 ==
                        forest_grammar
                    )
                    if not forest_identity_valid:
                        trace_valid = False
                    else:
                        record.parser_forest_traces += forest_trace
                        record.parser_forest_complete_traces += int(
                            forest.get("complete", 0))
                        record.parser_forest_proofs += int(
                            forest.get("proof", 0))
                        record.parser_forest_raw_nodes += int(
                            forest.get("raw_nodes", 0))
                        record.parser_forest_encoded_nodes += int(
                            forest.get("encoded_nodes", 0))
                        record.parser_forest_primary_clones += int(
                            forest.get("primary_clones", 0))
                        record.parser_forest_packed_alternatives += int(
                            forest.get("packed_alternatives", 0))
                        record.parser_forest_edges += int(
                            forest.get("edges", 0))
                        record.parser_forest_nullable_rules += int(
                            forest.get("nullable_rules", 0))
                        record.parser_forest_parse_time_us += int(
                            forest.get("elapsed_us", 0))
                        if forest_trace:
                            record.parser_forest_grammar_sha256 = (
                                forest_grammar)
                            self.parser_forest_grammar_sha256s.add(
                                forest_grammar)
                    cross = trace.get("cross_values", {})
                    cross_trace = int(cross.get("trace", 0))
                    cross_primary = str(
                        cross.get("primary_command_sha256", ""))
                    cross_secondary = str(
                        cross.get("secondary_command_sha256", ""))
                    cross_correspondence_json = str(
                        cross.get("correspondence_json", ""))
                    cross_correspondence_sha256 = str(
                        cross.get("correspondence_sha256", ""))
                    merged_correspondence = (
                        self._merge_cross_correspondence(
                            record.parser_cross_correspondence_json,
                            record.parser_cross_correspondence_sha256,
                            cross_correspondence_json,
                            cross_correspondence_sha256,
                        )
                        if cross_correspondence_json or
                        cross_correspondence_sha256 else
                        (
                            record.parser_cross_correspondence_json,
                            record.parser_cross_correspondence_sha256,
                        )
                    )
                    cross_identity_valid = (
                        not cross_trace or
                        (
                            not record.parser_cross_primary_command_sha256 or
                            record.parser_cross_primary_command_sha256 ==
                            cross_primary
                        ) and (
                            not record.parser_cross_secondary_command_sha256
                            or
                            record.parser_cross_secondary_command_sha256 ==
                            cross_secondary
                        ) and merged_correspondence is not None
                    )
                    if not cross_identity_valid:
                        trace_valid = False
                    else:
                        record.parser_cross_traces += cross_trace
                        record.parser_cross_agreements += int(
                            cross.get("agreement", 0))
                        record.parser_cross_both_accept += int(
                            cross.get("both_accept", 0))
                        record.parser_cross_primary_only += int(
                            cross.get("primary_only", 0))
                        record.parser_cross_secondary_only += int(
                            cross.get("secondary_only", 0))
                        record.parser_cross_both_reject += int(
                            cross.get("both_reject", 0))
                        record.parser_cross_primary_time_us += int(
                            cross.get("primary_time_us", 0))
                        record.parser_cross_secondary_time_us += int(
                            cross.get("secondary_time_us", 0))
                        record.parser_cross_structural_pairs += int(
                            cross.get("structural_pair", 0))
                        record.parser_cross_primary_selected_spans += int(
                            cross.get("primary_selected_spans", 0))
                        record.parser_cross_primary_forest_spans += int(
                            cross.get("primary_forest_spans", 0))
                        record.parser_cross_secondary_spans += int(
                            cross.get("secondary_spans", 0))
                        record.parser_cross_selected_shared_spans += int(
                            cross.get("selected_shared_spans", 0))
                        record.parser_cross_forest_shared_spans += int(
                            cross.get("forest_shared_spans", 0))
                        record.parser_cross_selected_union_spans += int(
                            cross.get("selected_union_spans", 0))
                        record.parser_cross_forest_union_spans += int(
                            cross.get("forest_union_spans", 0))
                        record.parser_cross_primary_boundaries += int(
                            cross.get("primary_boundaries", 0))
                        record.parser_cross_secondary_boundaries += int(
                            cross.get("secondary_boundaries", 0))
                        record.parser_cross_shared_boundaries += int(
                            cross.get("shared_boundaries", 0))
                        record.parser_cross_union_boundaries += int(
                            cross.get("union_boundaries", 0))
                        record.parser_cross_primary_symbols += int(
                            cross.get("primary_symbols", 0))
                        record.parser_cross_secondary_symbols += int(
                            cross.get("secondary_symbols", 0))
                        record.parser_cross_symbol_correspondences += int(
                            cross.get("symbol_correspondences", 0))
                        record.parser_cross_ambiguous_symbol_spans += int(
                            cross.get("ambiguous_symbol_spans", 0))
                        record.parser_cross_production_correspondences += int(
                            cross.get("production_correspondences", 0))
                        if merged_correspondence is not None:
                            (
                                record.parser_cross_correspondence_json,
                                record.parser_cross_correspondence_sha256,
                            ) = merged_correspondence
                        if cross_trace:
                            record.parser_cross_primary_command_sha256 = (
                                cross_primary)
                            record.parser_cross_secondary_command_sha256 = (
                                cross_secondary)
                            self.parser_cross_command_pairs.add((
                                cross_primary, cross_secondary))
                    has_grammar_span = record.grammar_span_present
                    structural = self._structural_context(
                        trace,
                        record.grammar_span_lo,
                        record.grammar_span_hi,
                        candidate_content,
                        record.target_branch,
                    ) if (
                        trace_valid and
                        record.grammar_rule_id and
                        has_grammar_span
                    ) else None
                    if structural is not None:
                        record.parser_context_id = structural["context_id"]
                        record.parser_symbol = structural["symbol"]
                        record.parser_state = structural["state"]
                        record.parser_production_id = structural[
                            "production_id"]
                        record.parser_production_lhs = structural[
                            "production_lhs"]
                        record.parser_production_state = structural[
                            "production_state"]
                        record.parser_production_arity = structural[
                            "production_arity"]
                        record.parser_recursive_depth = structural[
                            "recursive_depth"]
                        record.parser_recursion_prefix_hex = structural[
                            "recursion_prefix_hex"]
                        record.parser_recursion_suffix_hex = structural[
                            "recursion_suffix_hex"]
                        record.parser_cfg_fragment_sha256 = structural[
                            "cfg_fragment_sha256"]
                        record.parser_cfg_fragment_json = structural[
                            "cfg_fragment_json"]
                        record.parser_cfg_productions = structural[
                            "cfg_productions"]
                        record.parser_cfg_cycles = structural["cfg_cycles"]
                        record.parser_cfg_recursive_slots = structural[
                            "cfg_recursive_slots"]
                        record.parser_cfg_alternatives = structural[
                            "cfg_alternatives"]
                        record.parser_epsilon_productions = structural[
                            "epsilon_productions"]
                        record.parser_packed_nodes = structural[
                            "packed_nodes"]
                        record.parser_packed_edges = structural[
                            "packed_edges"]
                        record.parser_nullable_rules = structural[
                            "nullable_rules"]
                        record.parser_nullable_proofs = structural[
                            "nullable_proofs"]
                        record.parser_nullable_sccs = structural[
                            "nullable_sccs"]
                        record.grammar_candidate_context_id = structural[
                            "candidate_context_id"]
                        record.parser_ect_shape_id = structural[
                            "ect_shape_id"]
                        record.parser_ect_instance_id = structural[
                            "ect_instance_id"]
                        record.parser_ect_instances = structural[
                            "ect_instances"]
                    elif record.grammar_rule_id and has_grammar_span:
                        trace_valid = False
                parser_valid = parser_valid and trace_valid
                if (
                    parser_valid and trace_result is not None and
                    cache_requested
                ):
                    self._store_parser_cache_entry(
                        record,
                        trace_path,
                        trace_result[1],
                        trace_result[0],
                    )
                reason = (
                    "parser-accepted" if parser_valid else
                    "parser-trace-invalid" if not trace_valid else
                    f"parser-rejected-{parser.returncode}"
                )
            except (OSError, subprocess.TimeoutExpired):
                parser_valid = False
                reason = "parser-error-or-timeout"
            finally:
                record.parser_wall_time_us += (
                    time.monotonic_ns() - parser_started) // 1000
                if trace_path:
                    try:
                        os.unlink(trace_path)
                    except OSError:
                        pass
                if cache_path:
                    try:
                        os.unlink(cache_path)
                    except OSError:
                        pass
            if parser_valid:
                record.parser_accepted += 1
            valid = parser_valid
        record.last_reason = reason
        if valid:
            record.status = "verified"
            return True
        record.status = (
            "retry" if record.attempts < self.max_attempts else "rejected")
        return False

    def record_retention(
        self,
        proposal_id: str,
        coverage_features: int,
    ) -> None:
        record = self.records.get(proposal_id)
        if record is None:
            return
        delta = _positive_int(coverage_features)
        record.coverage_features += delta
        if delta:
            record.retained += 1
            record.status = "retained"
            record.last_reason = "coverage-retained"
        elif record.status == "verified":
            record.last_reason = "verified-no-global-novelty"
        record.updated = time.time()

    def _cross_correspondence_artifact(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        symbol_counts: dict[tuple[str, str, str, str], int] = {}
        production_counts: dict[
            tuple[str, str, str, str, str, str, str], int] = {}
        for record in self.records.values():
            correspondence = self._decode_cross_correspondence(
                record.parser_cross_correspondence_json,
                record.parser_cross_correspondence_sha256,
            )
            if correspondence is None:
                continue
            primary_parser = str(correspondence["primary_parser"])
            secondary_parser = str(correspondence["secondary_parser"])
            for primary_symbol, secondary_symbol, observations in (
                    correspondence["symbols"]):
                key = (
                    primary_parser,
                    secondary_parser,
                    str(primary_symbol),
                    str(secondary_symbol),
                )
                symbol_counts[key] = (
                    symbol_counts.get(key, 0) + int(observations))
            for item in correspondence["productions"]:
                key = (
                    primary_parser,
                    secondary_parser,
                    str(item[0]),
                    str(item[1]),
                    str(item[2]),
                    str(item[3]),
                    str(item[4]),
                )
                production_counts[key] = (
                    production_counts.get(key, 0) + int(item[5]))
        symbols = [
            {
                "primary_parser": key[0],
                "secondary_parser": key[1],
                "primary_symbol": key[2],
                "secondary_symbol": key[3],
                "observations": observations,
            }
            for key, observations in sorted(symbol_counts.items())
        ]
        productions = [
            {
                "primary_parser": key[0],
                "secondary_parser": key[1],
                "primary_symbol": key[2],
                "primary_state": key[3],
                "secondary_symbol": key[4],
                "secondary_state": key[5],
                "shape_sha256": key[6],
                "observations": observations,
            }
            for key, observations in sorted(production_counts.items())
        ]
        return symbols, productions

    @classmethod
    def validate_cross_correspondence_artifact(
        cls,
        symbols: Any,
        productions: Any,
        *,
        symbol_observations: int,
        production_observations: int,
        symbol_mappings: int,
        production_mappings: int,
    ) -> bool:
        if not isinstance(symbols, list) or not isinstance(
                productions, list):
            return False
        symbol_keys: list[tuple[str, str, str, str]] = []
        symbol_total = 0
        for item in symbols:
            if (
                not isinstance(item, dict) or
                set(item) != {
                    "primary_parser",
                    "secondary_parser",
                    "primary_symbol",
                    "secondary_symbol",
                    "observations",
                } or
                any(
                    cls._parser_label(item.get(key)) is None
                    for key in (
                        "primary_parser",
                        "secondary_parser",
                        "primary_symbol",
                        "secondary_symbol",
                    )
                ) or
                isinstance(item.get("observations"), bool) or
                not isinstance(item.get("observations"), int) or
                int(item["observations"]) <= 0
            ):
                return False
            symbol_keys.append((
                str(item["primary_parser"]),
                str(item["secondary_parser"]),
                str(item["primary_symbol"]),
                str(item["secondary_symbol"]),
            ))
            symbol_total += int(item["observations"])
        production_keys: list[
            tuple[str, str, str, str, str, str, str]] = []
        production_total = 0
        for item in productions:
            if (
                not isinstance(item, dict) or
                set(item) != {
                    "primary_parser",
                    "secondary_parser",
                    "primary_symbol",
                    "primary_state",
                    "secondary_symbol",
                    "secondary_state",
                    "shape_sha256",
                    "observations",
                } or
                any(
                    cls._parser_label(
                        item.get(key),
                        allow_empty=key.endswith("_state"),
                    ) is None
                    for key in (
                        "primary_parser",
                        "secondary_parser",
                        "primary_symbol",
                        "primary_state",
                        "secondary_symbol",
                        "secondary_state",
                    )
                ) or
                not isinstance(item.get("shape_sha256"), str) or
                not cls._valid_digest(item["shape_sha256"]) or
                isinstance(item.get("observations"), bool) or
                not isinstance(item.get("observations"), int) or
                int(item["observations"]) <= 0
            ):
                return False
            production_keys.append((
                str(item["primary_parser"]),
                str(item["secondary_parser"]),
                str(item["primary_symbol"]),
                str(item["primary_state"]),
                str(item["secondary_symbol"]),
                str(item["secondary_state"]),
                str(item["shape_sha256"]),
            ))
            production_total += int(item["observations"])
        return (
            symbol_keys == sorted(symbol_keys) and
            len(set(symbol_keys)) == len(symbol_keys) and
            production_keys == sorted(production_keys) and
            len(set(production_keys)) == len(production_keys) and
            symbol_total == symbol_observations and
            production_total == production_observations and
            len(symbol_keys) == symbol_mappings and
            len(production_keys) == production_mappings
        )

    def snapshot(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self.records.values():
            counts[record.status] = counts.get(record.status, 0) + 1
        symbol_mappings, production_mappings = (
            self._cross_correspondence_artifact())
        return {
            "schema": self.SCHEMA,
            "records": len(self.records),
            "status": counts,
            "rejected_inputs": self.rejected_inputs,
            "duplicate_inputs": self.duplicate_inputs,
            "query_hole_records": sum(
                record.kind == "query_hole_completion"
                for record in self.records.values()),
            "query_hole_query_ir_verified": sum(
                record.kind == "query_hole_completion"
                and record.query_ir_verified
                for record in self.records.values()),
            "history_acquisition_records": sum(
                record.kind == "history_acquisition"
                for record in self.records.values()),
            "parser_validations": sum(
                record.parser_validations
                for record in self.records.values()),
            "parser_accepted": sum(
                record.parser_accepted
                for record in self.records.values()),
            "parser_structural_contexts": sum(
                bool(record.parser_context_id)
                for record in self.records.values()),
            "parser_trace_nodes": sum(
                record.parser_trace_nodes
                for record in self.records.values()),
            "parser_productions": len({
                record.parser_production_id
                for record in self.records.values()
                if record.parser_production_id
            }),
            "parser_recursive_productions": sum(
                record.parser_recursive_depth > 0
                for record in self.records.values()),
            "parser_cfg_fragments": sum(
                bool(record.parser_cfg_fragment_sha256)
                for record in self.records.values()),
            "parser_cfg_productions": sum(
                record.parser_cfg_productions
                for record in self.records.values()),
            "parser_cfg_cycles": sum(
                record.parser_cfg_cycles
                for record in self.records.values()),
            "parser_cfg_recursive_slots": sum(
                record.parser_cfg_recursive_slots
                for record in self.records.values()),
            "parser_cfg_alternatives": sum(
                record.parser_cfg_alternatives
                for record in self.records.values()),
            "parser_epsilon_productions": sum(
                record.parser_epsilon_productions
                for record in self.records.values()),
            "parser_packed_nodes": sum(
                record.parser_packed_nodes
                for record in self.records.values()),
            "parser_packed_edges": sum(
                record.parser_packed_edges
                for record in self.records.values()),
            "parser_nullable_rules": sum(
                record.parser_nullable_rules
                for record in self.records.values()),
            "parser_nullable_proofs": sum(
                record.parser_nullable_proofs
                for record in self.records.values()),
            "parser_nullable_sccs": sum(
                record.parser_nullable_sccs
                for record in self.records.values()),
            "parser_ect_instances": sum(
                record.parser_ect_instances
                for record in self.records.values()),
            "parser_ect_shapes": len({
                record.parser_ect_shape_id
                for record in self.records.values()
                if record.parser_ect_shape_id
            }),
            "synchronized_transactions": sum(
                bool(record.grammar_sync_transaction_id)
                for record in self.records.values()),
            "parser_cache_entries": len(self.parser_cache_entries),
            "parser_cache_requests": sum(
                record.parser_cache_requests
                for record in self.records.values()),
            "parser_cache_incremental_offers": sum(
                record.parser_cache_incremental_offers
                for record in self.records.values()),
            "parser_cache_hits": sum(
                record.parser_cache_hit
                for record in self.records.values()),
            "parser_cache_reused_nodes": sum(
                record.parser_cache_reused_nodes_total
                for record in self.records.values()),
            "parser_cache_invalidated_nodes": sum(
                record.parser_cache_invalidated_nodes_total
                for record in self.records.values()),
            "parser_incremental_receipts": sum(
                record.parser_incremental_receipts
                for record in self.records.values()),
            "parser_incremental_zero_reuse": sum(
                record.parser_incremental_zero_reuse
                for record in self.records.values()),
            "parser_node_id_proofs": sum(
                record.parser_node_id_proofs
                for record in self.records.values()),
            "parser_wall_time_us": sum(
                record.parser_wall_time_us
                for record in self.records.values()),
            "parser_reported_parse_time_us": sum(
                record.parser_reported_parse_time_us
                for record in self.records.values()),
            "parser_trace_bytes": sum(
                record.parser_trace_bytes
                for record in self.records.values()),
            "parser_forest_traces": sum(
                record.parser_forest_traces
                for record in self.records.values()),
            "parser_forest_complete_traces": sum(
                record.parser_forest_complete_traces
                for record in self.records.values()),
            "parser_forest_proofs": sum(
                record.parser_forest_proofs
                for record in self.records.values()),
            "parser_forest_raw_nodes": sum(
                record.parser_forest_raw_nodes
                for record in self.records.values()),
            "parser_forest_encoded_nodes": sum(
                record.parser_forest_encoded_nodes
                for record in self.records.values()),
            "parser_forest_primary_clones": sum(
                record.parser_forest_primary_clones
                for record in self.records.values()),
            "parser_forest_packed_alternatives": sum(
                record.parser_forest_packed_alternatives
                for record in self.records.values()),
            "parser_forest_edges": sum(
                record.parser_forest_edges
                for record in self.records.values()),
            "parser_forest_nullable_rules": sum(
                record.parser_forest_nullable_rules
                for record in self.records.values()),
            "parser_forest_parse_time_us": sum(
                record.parser_forest_parse_time_us
                for record in self.records.values()),
            "parser_forest_grammars": len(
                self.parser_forest_grammar_sha256s),
            "parser_cross_traces": sum(
                record.parser_cross_traces
                for record in self.records.values()),
            "parser_cross_agreements": sum(
                record.parser_cross_agreements
                for record in self.records.values()),
            "parser_cross_both_accept": sum(
                record.parser_cross_both_accept
                for record in self.records.values()),
            "parser_cross_primary_only": sum(
                record.parser_cross_primary_only
                for record in self.records.values()),
            "parser_cross_secondary_only": sum(
                record.parser_cross_secondary_only
                for record in self.records.values()),
            "parser_cross_both_reject": sum(
                record.parser_cross_both_reject
                for record in self.records.values()),
            "parser_cross_primary_time_us": sum(
                record.parser_cross_primary_time_us
                for record in self.records.values()),
            "parser_cross_secondary_time_us": sum(
                record.parser_cross_secondary_time_us
                for record in self.records.values()),
            "parser_cross_structural_pairs": sum(
                record.parser_cross_structural_pairs
                for record in self.records.values()),
            "parser_cross_primary_selected_spans": sum(
                record.parser_cross_primary_selected_spans
                for record in self.records.values()),
            "parser_cross_primary_forest_spans": sum(
                record.parser_cross_primary_forest_spans
                for record in self.records.values()),
            "parser_cross_secondary_spans": sum(
                record.parser_cross_secondary_spans
                for record in self.records.values()),
            "parser_cross_selected_shared_spans": sum(
                record.parser_cross_selected_shared_spans
                for record in self.records.values()),
            "parser_cross_forest_shared_spans": sum(
                record.parser_cross_forest_shared_spans
                for record in self.records.values()),
            "parser_cross_selected_union_spans": sum(
                record.parser_cross_selected_union_spans
                for record in self.records.values()),
            "parser_cross_forest_union_spans": sum(
                record.parser_cross_forest_union_spans
                for record in self.records.values()),
            "parser_cross_primary_boundaries": sum(
                record.parser_cross_primary_boundaries
                for record in self.records.values()),
            "parser_cross_secondary_boundaries": sum(
                record.parser_cross_secondary_boundaries
                for record in self.records.values()),
            "parser_cross_shared_boundaries": sum(
                record.parser_cross_shared_boundaries
                for record in self.records.values()),
            "parser_cross_union_boundaries": sum(
                record.parser_cross_union_boundaries
                for record in self.records.values()),
            "parser_cross_primary_symbols": sum(
                record.parser_cross_primary_symbols
                for record in self.records.values()),
            "parser_cross_secondary_symbols": sum(
                record.parser_cross_secondary_symbols
                for record in self.records.values()),
            "parser_cross_symbol_correspondences": sum(
                record.parser_cross_symbol_correspondences
                for record in self.records.values()),
            "parser_cross_ambiguous_symbol_spans": sum(
                record.parser_cross_ambiguous_symbol_spans
                for record in self.records.values()),
            "parser_cross_production_correspondences": sum(
                record.parser_cross_production_correspondences
                for record in self.records.values()),
            "parser_cross_symbol_mappings": len(symbol_mappings),
            "parser_cross_production_mappings": len(
                production_mappings),
            "parser_cross_command_pairs": len(
                self.parser_cross_command_pairs),
            "parser_cache_enabled": int(self.parser_cache_enabled),
            "validated": sum(
                record.validations for record in self.records.values()),
            "retained": sum(
                record.retained for record in self.records.values()),
            "coverage_features": sum(
                record.coverage_features for record in self.records.values()),
        }

    def research_metrics(self) -> dict[str, int | float]:
        snapshot = self.snapshot()
        metrics: dict[str, int | float] = {
            f"proposal_{key}": value
            for key, value in snapshot.items()
            if (
                key != "schema" and
                isinstance(value, (int, float)) and
                not isinstance(value, bool)
            )
        }
        validations = int(snapshot["parser_validations"])
        offers = int(snapshot["parser_cache_incremental_offers"])
        receipts = int(snapshot["parser_incremental_receipts"])
        reused = int(snapshot["parser_cache_reused_nodes"])
        forest_traces = int(snapshot["parser_forest_traces"])
        cross_traces = int(snapshot["parser_cross_traces"])
        metrics.update({
            "proposal_parser_mean_wall_time_us": (
                float(snapshot["parser_wall_time_us"]) / validations
                if validations else 0.0
            ),
            "proposal_parser_mean_reported_parse_time_us": (
                float(snapshot["parser_reported_parse_time_us"]) / validations
                if validations else 0.0
            ),
            "proposal_parser_mean_trace_bytes": (
                float(snapshot["parser_trace_bytes"]) / validations
                if validations else 0.0
            ),
            "proposal_parser_incremental_receipt_rate": (
                receipts / offers if offers else 0.0
            ),
            "proposal_parser_mean_reused_nodes_per_receipt": (
                reused / receipts if receipts else 0.0
            ),
            "proposal_parser_forest_complete_rate": (
                float(snapshot["parser_forest_complete_traces"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_forest_mean_raw_nodes": (
                float(snapshot["parser_forest_raw_nodes"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_forest_mean_encoded_nodes": (
                float(snapshot["parser_forest_encoded_nodes"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_forest_mean_edges": (
                float(snapshot["parser_forest_edges"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_forest_mean_alternatives": (
                float(snapshot["parser_forest_packed_alternatives"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_forest_mean_parse_time_us": (
                float(snapshot["parser_forest_parse_time_us"]) /
                forest_traces if forest_traces else 0.0
            ),
            "proposal_parser_cross_agreement_rate": (
                float(snapshot["parser_cross_agreements"]) /
                cross_traces if cross_traces else 0.0
            ),
            "proposal_parser_cross_primary_accept_rate": (
                float(
                    snapshot["parser_cross_both_accept"] +
                    snapshot["parser_cross_primary_only"]
                ) / cross_traces if cross_traces else 0.0
            ),
            "proposal_parser_cross_secondary_accept_rate": (
                float(
                    snapshot["parser_cross_both_accept"] +
                    snapshot["parser_cross_secondary_only"]
                ) / cross_traces if cross_traces else 0.0
            ),
            "proposal_parser_cross_mean_primary_time_us": (
                float(snapshot["parser_cross_primary_time_us"]) /
                cross_traces if cross_traces else 0.0
            ),
            "proposal_parser_cross_mean_secondary_time_us": (
                float(snapshot["parser_cross_secondary_time_us"]) /
                cross_traces if cross_traces else 0.0
            ),
            "proposal_parser_cross_selected_span_precision": (
                float(snapshot["parser_cross_selected_shared_spans"]) /
                int(snapshot["parser_cross_secondary_spans"])
                if snapshot["parser_cross_secondary_spans"] else 0.0
            ),
            "proposal_parser_cross_selected_span_recall": (
                float(snapshot["parser_cross_selected_shared_spans"]) /
                int(snapshot["parser_cross_primary_selected_spans"])
                if snapshot["parser_cross_primary_selected_spans"] else 0.0
            ),
            "proposal_parser_cross_selected_span_jaccard": (
                float(snapshot["parser_cross_selected_shared_spans"]) /
                int(snapshot["parser_cross_selected_union_spans"])
                if snapshot["parser_cross_selected_union_spans"] else 0.0
            ),
            "proposal_parser_cross_forest_span_precision": (
                float(snapshot["parser_cross_forest_shared_spans"]) /
                int(snapshot["parser_cross_secondary_spans"])
                if snapshot["parser_cross_secondary_spans"] else 0.0
            ),
            "proposal_parser_cross_forest_span_recall": (
                float(snapshot["parser_cross_forest_shared_spans"]) /
                int(snapshot["parser_cross_primary_forest_spans"])
                if snapshot["parser_cross_primary_forest_spans"] else 0.0
            ),
            "proposal_parser_cross_forest_span_jaccard": (
                float(snapshot["parser_cross_forest_shared_spans"]) /
                int(snapshot["parser_cross_forest_union_spans"])
                if snapshot["parser_cross_forest_union_spans"] else 0.0
            ),
            "proposal_parser_cross_boundary_jaccard": (
                float(snapshot["parser_cross_shared_boundaries"]) /
                int(snapshot["parser_cross_union_boundaries"])
                if snapshot["parser_cross_union_boundaries"] else 0.0
            ),
            "proposal_parser_cross_primary_symbol_alignment_rate": (
                float(snapshot["parser_cross_symbol_correspondences"]) /
                int(snapshot["parser_cross_primary_symbols"])
                if snapshot["parser_cross_primary_symbols"] else 0.0
            ),
            "proposal_parser_cross_secondary_symbol_alignment_rate": (
                float(snapshot["parser_cross_symbol_correspondences"]) /
                int(snapshot["parser_cross_secondary_symbols"])
                if snapshot["parser_cross_secondary_symbols"] else 0.0
            ),
            "proposal_parser_cross_production_alignment_rate": (
                float(
                    snapshot[
                        "parser_cross_production_correspondences"]) /
                int(snapshot["parser_cross_symbol_correspondences"])
                if snapshot["parser_cross_symbol_correspondences"] else 0.0
            ),
        })
        return metrics

    def research_artifact(
        self,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed_metadata = (
            "experiment_id",
            "run_id",
            "pair_id",
            "phase",
            "configuration",
            "target",
            "random_seed",
            "cpu_budget_seconds",
            "cpu_cores",
            "parser_forest_mode",
            "parser_cross_mode",
        )
        normalized_metadata: dict[str, str | int | float] = {}
        for key in allowed_metadata:
            value = (metadata or {}).get(key)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                normalized_metadata[key] = value
            else:
                normalized_metadata[key] = str(value)[:1024]
        symbol_correspondence, production_correspondence = (
            self._cross_correspondence_artifact())
        core = {
            "schema": self.RESEARCH_ARTIFACT_SCHEMA,
            "proposal_state_schema": self.SCHEMA,
            "parser_cache_enabled": self.parser_cache_enabled,
            "parser_command_sha256": self._parser_command_digest(),
            "parser_forest_grammar_sha256s": sorted(
                self.parser_forest_grammar_sha256s),
            "parser_cross_command_pairs": [
                {
                    "primary_sha256": primary,
                    "secondary_sha256": secondary,
                }
                for primary, secondary in sorted(
                    self.parser_cross_command_pairs)
            ],
            "parser_cross_symbol_correspondence":
                symbol_correspondence,
            "parser_cross_production_correspondence":
                production_correspondence,
            "metadata": normalized_metadata,
            "metrics": self.research_metrics(),
        }
        digest = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        return {"artifact_sha256": digest, **core}

    @classmethod
    def verify_research_artifact(
        cls,
        artifact: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(artifact, dict) or
            artifact.get("schema") != cls.RESEARCH_ARTIFACT_SCHEMA or
            artifact.get("proposal_state_schema") != cls.SCHEMA or
            not isinstance(artifact.get("parser_cache_enabled"), bool) or
            not cls._valid_digest(str(
                artifact.get("parser_command_sha256", ""))) or
            not isinstance(
                artifact.get("parser_forest_grammar_sha256s"), list) or
            not isinstance(
                artifact.get("parser_cross_command_pairs"), list) or
            not isinstance(
                artifact.get(
                    "parser_cross_symbol_correspondence"), list) or
            not isinstance(
                artifact.get(
                    "parser_cross_production_correspondence"), list) or
            not isinstance(artifact.get("metadata"), dict) or
            not isinstance(artifact.get("metrics"), dict)
        ):
            return False
        core = dict(artifact)
        supplied = str(core.pop("artifact_sha256", ""))
        expected = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        metrics = artifact["metrics"]
        grammar_digests = artifact["parser_forest_grammar_sha256s"]
        command_pairs = artifact["parser_cross_command_pairs"]
        symbol_correspondence = artifact[
            "parser_cross_symbol_correspondence"]
        production_correspondence = artifact[
            "parser_cross_production_correspondence"]
        if supplied != expected or any(
            not isinstance(key, str) or
            not key.startswith("proposal_") or
            isinstance(value, bool) or
            not isinstance(value, (int, float)) or
            isinstance(value, float) and not math.isfinite(value) or
            value < 0
            for key, value in metrics.items()
        ) or grammar_digests != sorted(set(grammar_digests)) or any(
            not isinstance(value, str) or not cls._valid_digest(value)
            for value in grammar_digests
        ) or command_pairs != sorted(
            command_pairs,
            key=lambda item: (
                str(item.get("primary_sha256", ""))
                if isinstance(item, dict) else "",
                str(item.get("secondary_sha256", ""))
                if isinstance(item, dict) else "",
            ),
        ) or len(command_pairs) != len({
            (
                str(item.get("primary_sha256", "")),
                str(item.get("secondary_sha256", "")),
            )
            for item in command_pairs if isinstance(item, dict)
        }) or any(
            not isinstance(item, dict) or
            set(item) != {"primary_sha256", "secondary_sha256"} or
            not cls._valid_digest(str(item["primary_sha256"])) or
            not cls._valid_digest(str(item["secondary_sha256"])) or
            item["primary_sha256"] == item["secondary_sha256"]
            for item in command_pairs
        ):
            return False
        try:
            validations = int(metrics["proposal_parser_validations"])
            requests = int(metrics["proposal_parser_cache_requests"])
            offers = int(
                metrics["proposal_parser_cache_incremental_offers"])
            receipts = int(
                metrics["proposal_parser_incremental_receipts"])
            zero_reuse = int(
                metrics["proposal_parser_incremental_zero_reuse"])
            proofs = int(metrics["proposal_parser_node_id_proofs"])
            forest_traces = int(
                metrics["proposal_parser_forest_traces"])
            forest_complete = int(
                metrics["proposal_parser_forest_complete_traces"])
            forest_proofs = int(
                metrics["proposal_parser_forest_proofs"])
            forest_time = int(
                metrics["proposal_parser_forest_parse_time_us"])
            reported_time = int(
                metrics["proposal_parser_reported_parse_time_us"])
            cross_traces = int(
                metrics["proposal_parser_cross_traces"])
            cross_agreements = int(
                metrics["proposal_parser_cross_agreements"])
            cross_both_accept = int(
                metrics["proposal_parser_cross_both_accept"])
            cross_primary_only = int(
                metrics["proposal_parser_cross_primary_only"])
            cross_secondary_only = int(
                metrics["proposal_parser_cross_secondary_only"])
            cross_both_reject = int(
                metrics["proposal_parser_cross_both_reject"])
            cross_pairs = int(
                metrics["proposal_parser_cross_command_pairs"])
            cross_structural_pairs = int(
                metrics["proposal_parser_cross_structural_pairs"])
            primary_selected_spans = int(
                metrics[
                    "proposal_parser_cross_primary_selected_spans"])
            primary_forest_spans = int(
                metrics["proposal_parser_cross_primary_forest_spans"])
            secondary_spans = int(
                metrics["proposal_parser_cross_secondary_spans"])
            selected_shared_spans = int(
                metrics[
                    "proposal_parser_cross_selected_shared_spans"])
            forest_shared_spans = int(
                metrics["proposal_parser_cross_forest_shared_spans"])
            selected_union_spans = int(
                metrics[
                    "proposal_parser_cross_selected_union_spans"])
            forest_union_spans = int(
                metrics["proposal_parser_cross_forest_union_spans"])
            primary_boundaries = int(
                metrics["proposal_parser_cross_primary_boundaries"])
            secondary_boundaries = int(
                metrics["proposal_parser_cross_secondary_boundaries"])
            shared_boundaries = int(
                metrics["proposal_parser_cross_shared_boundaries"])
            union_boundaries = int(
                metrics["proposal_parser_cross_union_boundaries"])
            primary_symbols = int(
                metrics["proposal_parser_cross_primary_symbols"])
            secondary_symbols = int(
                metrics["proposal_parser_cross_secondary_symbols"])
            symbol_observations = int(
                metrics[
                    "proposal_parser_cross_symbol_correspondences"])
            production_observations = int(
                metrics[
                    "proposal_parser_cross_production_correspondences"])
            symbol_mappings = int(
                metrics["proposal_parser_cross_symbol_mappings"])
            production_mappings = int(
                metrics["proposal_parser_cross_production_mappings"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return (
            requests <= validations and
            offers <= requests and
            receipts <= offers and
            zero_reuse <= receipts and
            proofs <= receipts and
            forest_complete <= forest_proofs <= forest_traces <=
            validations and
            bool(grammar_digests) == bool(forest_traces) and
            forest_time <= reported_time and
            cross_agreements ==
            cross_both_accept + cross_both_reject and
            cross_traces == (
                cross_both_accept + cross_primary_only +
                cross_secondary_only + cross_both_reject
            ) and
            cross_traces <= validations and
            bool(command_pairs) == bool(cross_traces) and
            cross_pairs == len(command_pairs) and
            cross_structural_pairs == cross_both_accept and
            primary_selected_spans <= primary_forest_spans and
            selected_shared_spans <= primary_selected_spans and
            selected_shared_spans <= secondary_spans and
            forest_shared_spans <= primary_forest_spans and
            forest_shared_spans <= secondary_spans and
            selected_shared_spans <= forest_shared_spans and
            selected_union_spans == (
                primary_selected_spans + secondary_spans -
                selected_shared_spans
            ) and
            forest_union_spans == (
                primary_forest_spans + secondary_spans -
                forest_shared_spans
            ) and
            shared_boundaries <= primary_boundaries and
            shared_boundaries <= secondary_boundaries and
            union_boundaries == (
                primary_boundaries + secondary_boundaries -
                shared_boundaries
            ) and
            symbol_observations <= primary_symbols and
            symbol_observations <= secondary_symbols and
            production_observations <= symbol_observations and
            cls.validate_cross_correspondence_artifact(
                symbol_correspondence,
                production_correspondence,
                symbol_observations=symbol_observations,
                production_observations=production_observations,
                symbol_mappings=symbol_mappings,
                production_mappings=production_mappings,
            ) and
            int(metrics.get("proposal_parser_cache_enabled", -1)) ==
            int(artifact["parser_cache_enabled"])
        )

    def write_research_artifact(
        self,
        path: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not path:
            return False
        directory = os.path.dirname(path) or "."
        temporary = ""
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".parser-research-", dir=directory, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    self.research_artifact(metadata),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        except OSError:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return False

    def save(self) -> None:
        temporary = self.state_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({
                    "schema": self.SCHEMA,
                    "rejected_inputs": self.rejected_inputs,
                    "duplicate_inputs": self.duplicate_inputs,
                    "scan_offset": self.scan_offset,
                    "scan_identity": self.scan_identity,
                    "parser_forest_grammar_sha256s": sorted(
                        self.parser_forest_grammar_sha256s),
                    "parser_cross_command_pairs": [
                        {
                            "primary_sha256": primary,
                            "secondary_sha256": secondary,
                        }
                        for primary, secondary in sorted(
                            self.parser_cross_command_pairs)
                    ],
                    "parser_cache_entries": [
                        self.parser_cache_entries[input_sha256]
                        for input_sha256 in sorted(
                            self.parser_cache_entries)
                    ],
                    "records": [
                        asdict(record) for record in self.records.values()
                    ],
                }, stream, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(temporary, self.state_path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if (
            not isinstance(raw, dict) or
            raw.get("schema") not in {
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
        ):
            return
        self.rejected_inputs = _positive_int(raw.get("rejected_inputs"))
        self.duplicate_inputs = _positive_int(raw.get("duplicate_inputs"))
        self.scan_offset = _positive_int(raw.get("scan_offset"))
        self.scan_identity = str(raw.get("scan_identity", ""))
        stored_forest_grammars = raw.get(
            "parser_forest_grammar_sha256s", ())
        if (
            isinstance(stored_forest_grammars, list) and
            stored_forest_grammars ==
            sorted(set(stored_forest_grammars)) and
            all(
                isinstance(value, str) and self._valid_digest(value)
                for value in stored_forest_grammars
            )
        ):
            self.parser_forest_grammar_sha256s.update(
                stored_forest_grammars)
        stored_cross_pairs = raw.get(
            "parser_cross_command_pairs", ())
        if isinstance(stored_cross_pairs, list):
            normalized_pairs: set[tuple[str, str]] = set()
            valid_pairs = True
            for item in stored_cross_pairs:
                if not isinstance(item, dict):
                    valid_pairs = False
                    break
                primary = str(item.get("primary_sha256", ""))
                secondary = str(item.get("secondary_sha256", ""))
                if (
                    set(item) !=
                    {"primary_sha256", "secondary_sha256"} or
                    not self._valid_digest(primary) or
                    not self._valid_digest(secondary) or
                    primary == secondary
                ):
                    valid_pairs = False
                    break
                normalized_pairs.add((primary, secondary))
            if valid_pairs and len(normalized_pairs) == len(
                    stored_cross_pairs):
                self.parser_cross_command_pairs.update(normalized_pairs)
        cache_root = os.path.realpath(
            os.path.join(self.root, "parser_cache"))
        candidate_root = os.path.realpath(
            os.path.join(self.root, "candidates"))
        stored_cache_entries = raw.get("parser_cache_entries", ())
        if isinstance(stored_cache_entries, list):
            for entry in stored_cache_entries[-self.parser_cache_limit:]:
                if not isinstance(entry, dict):
                    continue
                input_sha256 = str(entry.get("input_sha256", ""))
                trace_sha256 = str(entry.get("trace_sha256", ""))
                command_sha256 = str(
                    entry.get("parser_command_sha256", ""))
                trace_path = str(entry.get("trace_path", ""))
                input_path = str(entry.get("input_path", ""))
                try:
                    nodes = int(entry.get("nodes", 0))
                    last_used = float(entry.get("last_used", 0.0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    not self._valid_digest(input_sha256) or
                    not self._valid_digest(trace_sha256) or
                    command_sha256 != self._parser_command_digest() or
                    not 0 <= nodes <= 4096 or
                    not math.isfinite(last_used) or last_used < 0 or
                    os.path.dirname(os.path.realpath(trace_path)) !=
                    cache_root or
                    os.path.basename(trace_path) !=
                    f"{input_sha256}.trace.json" or
                    os.path.dirname(os.path.realpath(input_path)) !=
                    candidate_root or
                    not os.path.isfile(trace_path) or
                    not os.path.isfile(input_path)
                ):
                    continue
                self.parser_cache_entries[input_sha256] = {
                    "input_sha256": input_sha256,
                    "input_path": input_path,
                    "trace_sha256": trace_sha256,
                    "trace_path": trace_path,
                    "parser_command_sha256": command_sha256,
                    "parser": str(entry.get("parser", ""))[:128],
                    "schema": str(entry.get("schema", ""))[:64],
                    "nodes": nodes,
                    "last_used": last_used,
                }
        for item in raw.get("records", ()):
            if not isinstance(item, dict):
                continue
            proposal_id = str(item.get("proposal_id", ""))
            candidate_path = str(item.get("candidate_path", ""))
            candidate_sha256 = str(item.get("candidate_sha256", ""))
            if not proposal_id or not candidate_path or not candidate_sha256:
                continue
            try:
                candidate_size = os.path.getsize(candidate_path)
            except OSError:
                continue
            try:
                with open(candidate_path, "rb") as stream:
                    candidate_content = stream.read(
                        self.max_candidate_bytes + 1)
            except OSError:
                continue
            if (
                len(candidate_content) != candidate_size or
                len(candidate_content) > self.max_candidate_bytes or
                hashlib.sha256(candidate_content).hexdigest() !=
                candidate_sha256
            ):
                continue
            record = VerifiedProposal(
                proposal_id=proposal_id,
                kind=str(item.get("kind", "semantic")),
                source_path=str(item.get("source_path", "")),
                candidate_path=candidate_path,
                candidate_sha256=candidate_sha256,
                target_branch=_positive_int(item.get("target_branch")),
                generator=str(item.get("generator", ""))[:128],
                grammar_rule_id=str(
                    item.get("grammar_rule_id", ""))[:64],
                grammar_context_id=str(
                    item.get("grammar_context_id", ""))[:64],
                grammar_source_context_id=str(
                    item.get("grammar_source_context_id", ""))[:64],
                grammar_candidate_context_id=str(
                    item.get("grammar_candidate_context_id", ""))[:64],
                grammar_ect_shape_id=str(
                    item.get("grammar_ect_shape_id", ""))[:64],
                grammar_ect_instance_id=str(
                    item.get("grammar_ect_instance_id", ""))[:64],
                grammar_sync_transaction_id=str(
                    item.get("grammar_sync_transaction_id", ""))[:64],
                grammar_span_present=bool(
                    item.get("grammar_span_present", False)),
                grammar_span_lo=_positive_int(
                    item.get("grammar_span_lo")),
                grammar_span_hi=_positive_int(
                    item.get("grammar_span_hi")),
                parser_context_id=str(
                    item.get("parser_context_id", ""))[:64],
                parser_symbol=str(item.get("parser_symbol", ""))[:128],
                parser_state=str(item.get("parser_state", ""))[:128],
                parser_trace_sha256=str(
                    item.get("parser_trace_sha256", ""))[:64],
                parser_trace_nodes=_positive_int(
                    item.get("parser_trace_nodes")),
                parser_production_id=str(
                    item.get("parser_production_id", ""))[:64],
                parser_production_lhs=str(
                    item.get("parser_production_lhs", ""))[:128],
                parser_production_state=str(
                    item.get("parser_production_state", ""))[:128],
                parser_production_arity=_positive_int(
                    item.get("parser_production_arity")),
                parser_recursive_depth=_positive_int(
                    item.get("parser_recursive_depth")),
                parser_recursion_prefix_hex=str(
                    item.get("parser_recursion_prefix_hex", ""))[:256],
                parser_recursion_suffix_hex=str(
                    item.get("parser_recursion_suffix_hex", ""))[:256],
                parser_cfg_fragment_sha256=str(
                    item.get("parser_cfg_fragment_sha256", ""))[:64],
                parser_cfg_fragment_json=str(
                    item.get("parser_cfg_fragment_json", ""))[:65536],
                parser_cfg_productions=_positive_int(
                    item.get("parser_cfg_productions")),
                parser_cfg_cycles=_positive_int(
                    item.get("parser_cfg_cycles")),
                parser_cfg_recursive_slots=_positive_int(
                    item.get("parser_cfg_recursive_slots")),
                parser_cfg_alternatives=_positive_int(
                    item.get("parser_cfg_alternatives")),
                parser_epsilon_productions=_positive_int(
                    item.get("parser_epsilon_productions")),
                parser_packed_nodes=_positive_int(
                    item.get("parser_packed_nodes")),
                parser_packed_edges=_positive_int(
                    item.get("parser_packed_edges")),
                parser_nullable_rules=_positive_int(
                    item.get("parser_nullable_rules")),
                parser_nullable_proofs=_positive_int(
                    item.get("parser_nullable_proofs")),
                parser_nullable_sccs=_positive_int(
                    item.get("parser_nullable_sccs")),
                parser_cache_manifest_sha256=str(
                    item.get(
                        "parser_cache_manifest_sha256", ""))[:64],
                parser_cache_base_sha256=str(
                    item.get("parser_cache_base_sha256", ""))[:64],
                parser_cache_reused_nodes=_positive_int(
                    item.get("parser_cache_reused_nodes")),
                parser_cache_invalidated_nodes=_positive_int(
                    item.get("parser_cache_invalidated_nodes")),
                parser_cache_hit=bool(
                    item.get("parser_cache_hit", False)),
                parser_wall_time_us=_positive_int(
                    item.get("parser_wall_time_us")),
                parser_trace_bytes=_positive_int(
                    item.get("parser_trace_bytes")),
                parser_cache_requests=_positive_int(
                    item.get("parser_cache_requests")),
                parser_cache_incremental_offers=_positive_int(
                    item.get("parser_cache_incremental_offers")),
                parser_incremental_receipts=_positive_int(
                    item.get("parser_incremental_receipts")),
                parser_incremental_zero_reuse=_positive_int(
                    item.get("parser_incremental_zero_reuse")),
                parser_node_id_proofs=_positive_int(
                    item.get("parser_node_id_proofs")),
                parser_reported_parse_time_us=_positive_int(
                    item.get("parser_reported_parse_time_us")),
                parser_cache_reused_nodes_total=_positive_int(
                    item.get(
                        "parser_cache_reused_nodes_total",
                        item.get("parser_cache_reused_nodes"))),
                parser_cache_invalidated_nodes_total=_positive_int(
                    item.get(
                        "parser_cache_invalidated_nodes_total",
                        item.get("parser_cache_invalidated_nodes"))),
                parser_forest_traces=_positive_int(
                    item.get("parser_forest_traces")),
                parser_forest_complete_traces=_positive_int(
                    item.get("parser_forest_complete_traces")),
                parser_forest_proofs=_positive_int(
                    item.get("parser_forest_proofs")),
                parser_forest_raw_nodes=_positive_int(
                    item.get("parser_forest_raw_nodes")),
                parser_forest_encoded_nodes=_positive_int(
                    item.get("parser_forest_encoded_nodes")),
                parser_forest_primary_clones=_positive_int(
                    item.get("parser_forest_primary_clones")),
                parser_forest_packed_alternatives=_positive_int(
                    item.get("parser_forest_packed_alternatives")),
                parser_forest_edges=_positive_int(
                    item.get("parser_forest_edges")),
                parser_forest_nullable_rules=_positive_int(
                    item.get("parser_forest_nullable_rules")),
                parser_forest_parse_time_us=_positive_int(
                    item.get("parser_forest_parse_time_us")),
                parser_forest_grammar_sha256=str(
                    item.get(
                        "parser_forest_grammar_sha256", ""))[:64],
                parser_cross_traces=_positive_int(
                    item.get("parser_cross_traces")),
                parser_cross_agreements=_positive_int(
                    item.get("parser_cross_agreements")),
                parser_cross_both_accept=_positive_int(
                    item.get("parser_cross_both_accept")),
                parser_cross_primary_only=_positive_int(
                    item.get("parser_cross_primary_only")),
                parser_cross_secondary_only=_positive_int(
                    item.get("parser_cross_secondary_only")),
                parser_cross_both_reject=_positive_int(
                    item.get("parser_cross_both_reject")),
                parser_cross_primary_time_us=_positive_int(
                    item.get("parser_cross_primary_time_us")),
                parser_cross_secondary_time_us=_positive_int(
                    item.get("parser_cross_secondary_time_us")),
                parser_cross_primary_command_sha256=str(
                    item.get(
                        "parser_cross_primary_command_sha256", ""))[:64],
                parser_cross_secondary_command_sha256=str(
                    item.get(
                        "parser_cross_secondary_command_sha256", ""))[:64],
                parser_cross_structural_pairs=_positive_int(
                    item.get(
                        "parser_cross_structural_pairs",
                        item.get("parser_cross_both_accept")
                        if int(raw.get("schema", 0)) < 14 else 0,
                    )),
                parser_cross_primary_selected_spans=_positive_int(
                    item.get(
                        "parser_cross_primary_selected_spans")),
                parser_cross_primary_forest_spans=_positive_int(
                    item.get("parser_cross_primary_forest_spans")),
                parser_cross_secondary_spans=_positive_int(
                    item.get("parser_cross_secondary_spans")),
                parser_cross_selected_shared_spans=_positive_int(
                    item.get("parser_cross_selected_shared_spans")),
                parser_cross_forest_shared_spans=_positive_int(
                    item.get("parser_cross_forest_shared_spans")),
                parser_cross_selected_union_spans=_positive_int(
                    item.get("parser_cross_selected_union_spans")),
                parser_cross_forest_union_spans=_positive_int(
                    item.get("parser_cross_forest_union_spans")),
                parser_cross_primary_boundaries=_positive_int(
                    item.get("parser_cross_primary_boundaries")),
                parser_cross_secondary_boundaries=_positive_int(
                    item.get("parser_cross_secondary_boundaries")),
                parser_cross_shared_boundaries=_positive_int(
                    item.get("parser_cross_shared_boundaries")),
                parser_cross_union_boundaries=_positive_int(
                    item.get("parser_cross_union_boundaries")),
                parser_cross_primary_symbols=_positive_int(
                    item.get("parser_cross_primary_symbols")),
                parser_cross_secondary_symbols=_positive_int(
                    item.get("parser_cross_secondary_symbols")),
                parser_cross_symbol_correspondences=_positive_int(
                    item.get(
                        "parser_cross_symbol_correspondences")),
                parser_cross_ambiguous_symbol_spans=_positive_int(
                    item.get(
                        "parser_cross_ambiguous_symbol_spans")),
                parser_cross_production_correspondences=_positive_int(
                    item.get(
                        "parser_cross_production_correspondences")),
                parser_cross_correspondence_sha256=str(
                    item.get(
                        "parser_cross_correspondence_sha256", ""))[:64],
                parser_cross_correspondence_json=str(
                    item.get(
                        "parser_cross_correspondence_json", ""))[
                            :2 * 1024 * 1024],
                parser_ect_shape_id=str(
                    item.get("parser_ect_shape_id", ""))[:64],
                parser_ect_instance_id=str(
                    item.get("parser_ect_instance_id", ""))[:64],
                parser_ect_instances=_positive_int(
                    item.get("parser_ect_instances")),
                query_id=str(item.get("query_id", ""))[:64],
                query_ir_verified=bool(
                    item.get("query_ir_verified", False)),
                query_hole_lo=_positive_int(
                    item.get("query_hole_lo")),
                query_hole_hi=_positive_int(
                    item.get("query_hole_hi")),
                query_hole_manifest_sha256=str(
                    item.get(
                        "query_hole_manifest_sha256", ""))[:64],
                history_seed_id=str(
                    item.get("history_seed_id", ""))[:64],
                parser_validations=_positive_int(
                    item.get("parser_validations")),
                parser_accepted=_positive_int(
                    item.get("parser_accepted")),
                status=str(item.get("status", "pending")),
                attempts=_positive_int(item.get("attempts")),
                validations=_positive_int(item.get("validations")),
                retained=_positive_int(item.get("retained")),
                coverage_features=_positive_int(
                    item.get("coverage_features")),
                created=float(item.get("created", 0.0) or 0.0),
                updated=float(item.get("updated", 0.0) or 0.0),
                last_reason=str(item.get("last_reason", "")),
            )
            if int(raw.get("schema", 0)) < 15:
                record.parser_cross_primary_symbols = 0
                record.parser_cross_secondary_symbols = 0
                record.parser_cross_symbol_correspondences = 0
                record.parser_cross_ambiguous_symbol_spans = 0
                record.parser_cross_production_correspondences = 0
                record.parser_cross_correspondence_sha256 = ""
                record.parser_cross_correspondence_json = ""
            correspondence = self._decode_cross_correspondence(
                record.parser_cross_correspondence_json,
                record.parser_cross_correspondence_sha256,
            )
            if (
                int(raw.get("schema", 0)) >= 15 and
                (
                    bool(record.parser_cross_both_accept) !=
                    bool(correspondence) or
                    correspondence is not None and (
                        correspondence["primary_symbols"] !=
                        record.parser_cross_primary_symbols or
                        correspondence["secondary_symbols"] !=
                        record.parser_cross_secondary_symbols or
                        correspondence["symbol_observations"] !=
                        record.parser_cross_symbol_correspondences or
                        correspondence["ambiguous_symbol_spans"] !=
                        record.parser_cross_ambiguous_symbol_spans or
                        correspondence["production_observations"] !=
                        record.parser_cross_production_correspondences
                    )
                )
            ):
                continue
            if record.kind == "query_hole_completion" and (
                len(record.query_id) != 64 or
                any(char not in "0123456789abcdef"
                    for char in record.query_id) or
                not record.query_ir_verified or
                record.query_hole_hi <= record.query_hole_lo or
                len(record.query_hole_manifest_sha256) != 64 or
                any(char not in "0123456789abcdef"
                    for char in record.query_hole_manifest_sha256)
            ):
                continue
            if record.kind == "history_acquisition" and (
                len(record.history_seed_id) != 64 or
                any(char not in "0123456789abcdef"
                    for char in record.history_seed_id)
            ):
                continue
            if (
                record.parser_accepted > record.parser_validations or
                record.grammar_span_hi < record.grammar_span_lo or
                record.grammar_span_present and (
                    record.grammar_span_hi > candidate_size
                ) or
                record.grammar_source_context_id and not self._valid_digest(
                    record.grammar_source_context_id) or
                record.grammar_candidate_context_id and not
                self._valid_digest(record.grammar_candidate_context_id) or
                record.grammar_ect_shape_id and not self._valid_digest(
                    record.grammar_ect_shape_id) or
                record.grammar_ect_instance_id and not self._valid_digest(
                    record.grammar_ect_instance_id) or
                bool(record.grammar_ect_shape_id) !=
                bool(record.grammar_ect_instance_id) or
                record.grammar_sync_transaction_id and (
                    not record.grammar_rule_id or
                    not record.grammar_span_present or
                    not self._valid_digest(
                        record.grammar_sync_transaction_id) or
                    bool(record.grammar_ect_shape_id) or
                    bool(record.grammar_ect_instance_id)
                ) or
                record.parser_context_id and not self._valid_digest(
                    record.parser_context_id) or
                record.parser_production_id and not self._valid_digest(
                    record.parser_production_id) or
                record.parser_production_id and not
                record.parser_production_lhs or
                not record.parser_production_id and (
                    bool(record.parser_production_lhs) or
                    bool(record.parser_production_state) or
                    record.parser_production_arity > 0 or
                    record.parser_recursive_depth > 0
                ) or
                self._parser_label(
                    record.parser_production_lhs,
                    allow_empty=True) is None or
                self._parser_label(
                    record.parser_production_state,
                    allow_empty=True) is None or
                record.parser_production_arity > 4096 or
                record.parser_recursive_depth > 32 or
                record.parser_cfg_fragment_sha256 and not self._valid_digest(
                    record.parser_cfg_fragment_sha256) or
                bool(record.parser_cfg_fragment_sha256) !=
                bool(record.parser_cfg_fragment_json) or
                record.parser_cfg_productions > 32 or
                record.parser_cfg_cycles > 16 or
                record.parser_cfg_recursive_slots > 256 or
                record.parser_cfg_alternatives > 32 or
                record.parser_epsilon_productions > 32 or
                record.parser_packed_nodes > 32 or
                record.parser_packed_edges > 128 or
                record.parser_nullable_rules > 32 or
                record.parser_nullable_proofs > 288 or
                record.parser_nullable_sccs > 288 or
                record.parser_cache_manifest_sha256 and not
                self._valid_digest(
                    record.parser_cache_manifest_sha256) or
                record.parser_cache_base_sha256 and not
                self._valid_digest(record.parser_cache_base_sha256) or
                record.parser_cache_reused_nodes > 512 or
                record.parser_cache_invalidated_nodes > 4096 or
                record.parser_cache_base_sha256 and not
                record.parser_cache_manifest_sha256 or
                record.parser_cache_reused_nodes and not
                record.parser_cache_manifest_sha256 or
                record.parser_cache_invalidated_nodes and not
                record.parser_cache_base_sha256 or
                record.parser_cache_hit != (
                    record.parser_cache_reused_nodes > 0) or
                record.parser_cache_hit and not
                record.parser_cache_base_sha256 or
                record.parser_cache_requests > record.parser_validations or
                record.parser_cache_incremental_offers >
                record.parser_cache_requests or
                record.parser_incremental_receipts >
                record.parser_cache_incremental_offers or
                record.parser_incremental_zero_reuse >
                record.parser_incremental_receipts or
                record.parser_node_id_proofs >
                record.parser_incremental_receipts or
                record.parser_forest_complete_traces >
                record.parser_forest_proofs or
                record.parser_forest_proofs >
                record.parser_forest_traces or
                record.parser_forest_traces >
                record.parser_validations or
                record.parser_forest_raw_nodes <
                record.parser_forest_complete_traces or
                record.parser_forest_encoded_nodes <
                record.parser_forest_traces or
                record.parser_forest_primary_clones >
                record.parser_forest_encoded_nodes or
                record.parser_forest_packed_alternatives <
                record.parser_forest_traces or
                record.parser_forest_nullable_rules >
                32 * record.parser_forest_complete_traces or
                record.parser_forest_parse_time_us >
                record.parser_reported_parse_time_us or
                bool(record.parser_forest_grammar_sha256) !=
                bool(record.parser_forest_traces) or
                record.parser_forest_grammar_sha256 and (
                    not self._valid_digest(
                        record.parser_forest_grammar_sha256) or
                    record.parser_forest_grammar_sha256 not in
                    self.parser_forest_grammar_sha256s
                ) or
                record.parser_cross_agreements != (
                    record.parser_cross_both_accept +
                    record.parser_cross_both_reject
                ) or
                record.parser_cross_traces != (
                    record.parser_cross_both_accept +
                    record.parser_cross_primary_only +
                    record.parser_cross_secondary_only +
                    record.parser_cross_both_reject
                ) or
                record.parser_cross_traces >
                record.parser_validations or
                bool(record.parser_cross_traces) != bool(
                    record.parser_cross_primary_command_sha256) or
                bool(record.parser_cross_traces) != bool(
                    record.parser_cross_secondary_command_sha256) or
                record.parser_cross_primary_command_sha256 and (
                    not self._valid_digest(
                        record.parser_cross_primary_command_sha256) or
                    not self._valid_digest(
                        record.parser_cross_secondary_command_sha256) or
                    record.parser_cross_primary_command_sha256 ==
                    record.parser_cross_secondary_command_sha256 or
                    (
                        record.parser_cross_primary_command_sha256,
                        record.parser_cross_secondary_command_sha256,
                    ) not in self.parser_cross_command_pairs
                ) or
                record.parser_cross_structural_pairs !=
                record.parser_cross_both_accept or
                record.parser_cross_primary_selected_spans >
                record.parser_cross_primary_forest_spans or
                record.parser_cross_selected_shared_spans >
                record.parser_cross_primary_selected_spans or
                record.parser_cross_selected_shared_spans >
                record.parser_cross_secondary_spans or
                record.parser_cross_forest_shared_spans >
                record.parser_cross_primary_forest_spans or
                record.parser_cross_forest_shared_spans >
                record.parser_cross_secondary_spans or
                record.parser_cross_selected_shared_spans >
                record.parser_cross_forest_shared_spans or
                record.parser_cross_selected_union_spans != (
                    record.parser_cross_primary_selected_spans +
                    record.parser_cross_secondary_spans -
                    record.parser_cross_selected_shared_spans
                ) or
                record.parser_cross_forest_union_spans != (
                    record.parser_cross_primary_forest_spans +
                    record.parser_cross_secondary_spans -
                    record.parser_cross_forest_shared_spans
                ) or
                record.parser_cross_shared_boundaries >
                record.parser_cross_primary_boundaries or
                record.parser_cross_shared_boundaries >
                record.parser_cross_secondary_boundaries or
                record.parser_cross_union_boundaries != (
                    record.parser_cross_primary_boundaries +
                    record.parser_cross_secondary_boundaries -
                    record.parser_cross_shared_boundaries
                ) or
                record.parser_cross_symbol_correspondences >
                record.parser_cross_primary_symbols or
                record.parser_cross_symbol_correspondences >
                record.parser_cross_secondary_symbols or
                record.parser_cross_production_correspondences >
                record.parser_cross_symbol_correspondences or
                bool(record.parser_cross_correspondence_json) !=
                bool(record.parser_cross_correspondence_sha256) or
                record.parser_cache_reused_nodes_total <
                record.parser_cache_reused_nodes or
                record.parser_cache_invalidated_nodes_total <
                record.parser_cache_invalidated_nodes or
                any(value > (1 << 63) - 1 for value in (
                    record.parser_wall_time_us,
                    record.parser_trace_bytes,
                    record.parser_reported_parse_time_us,
                    record.parser_cache_reused_nodes_total,
                    record.parser_cache_invalidated_nodes_total,
                    record.parser_forest_raw_nodes,
                    record.parser_forest_encoded_nodes,
                    record.parser_forest_primary_clones,
                    record.parser_forest_packed_alternatives,
                    record.parser_forest_edges,
                    record.parser_forest_nullable_rules,
                    record.parser_forest_parse_time_us,
                    record.parser_cross_primary_time_us,
                    record.parser_cross_secondary_time_us,
                    record.parser_cross_primary_selected_spans,
                    record.parser_cross_primary_forest_spans,
                    record.parser_cross_secondary_spans,
                    record.parser_cross_selected_shared_spans,
                    record.parser_cross_forest_shared_spans,
                    record.parser_cross_selected_union_spans,
                    record.parser_cross_forest_union_spans,
                    record.parser_cross_primary_boundaries,
                    record.parser_cross_secondary_boundaries,
                    record.parser_cross_shared_boundaries,
                    record.parser_cross_union_boundaries,
                    record.parser_cross_primary_symbols,
                    record.parser_cross_secondary_symbols,
                    record.parser_cross_symbol_correspondences,
                    record.parser_cross_ambiguous_symbol_spans,
                    record.parser_cross_production_correspondences,
                )) or
                record.parser_ect_shape_id and not self._valid_digest(
                    record.parser_ect_shape_id) or
                record.parser_ect_instance_id and not self._valid_digest(
                    record.parser_ect_instance_id) or
                record.parser_ect_instances > 32 or
                record.parser_trace_sha256 and not self._valid_digest(
                    record.parser_trace_sha256) or
                record.grammar_context_id and (
                    len(record.grammar_context_id) != 64 or
                    any(char not in "0123456789abcdef"
                        for char in record.grammar_context_id)
                )
            ):
                continue
            if (
                record.grammar_candidate_context_id and (
                    not record.grammar_span_present or
                    record.grammar_candidate_context_id !=
                    self._grammar_lexical_context_id(
                        candidate_content,
                        record.grammar_span_lo,
                        record.grammar_span_hi,
                        record.target_branch,
                    )
                )
            ):
                continue
            try:
                recursion_prefix = bytes.fromhex(
                    record.parser_recursion_prefix_hex)
                recursion_suffix = bytes.fromhex(
                    record.parser_recursion_suffix_hex)
            except ValueError:
                continue
            if (
                len(recursion_prefix) + len(recursion_suffix) > 128 or
                bool(record.parser_recursive_depth) !=
                bool(recursion_prefix or recursion_suffix)
            ):
                continue
            if record.parser_cfg_fragment_json:
                encoded_fragment = record.parser_cfg_fragment_json.encode(
                    "utf-8")
                if (
                    hashlib.sha256(encoded_fragment).hexdigest() !=
                    record.parser_cfg_fragment_sha256
                ):
                    continue
                try:
                    cfg_fragment = json.loads(record.parser_cfg_fragment_json)
                except (ValueError, TypeError):
                    continue
                if (
                    not isinstance(cfg_fragment, dict) or
                    cfg_fragment.get("schema") not in {
                        "symcc-parser-cfg-fragment-v1",
                        "symcc-parser-cfg-fragment-v2",
                        "symcc-parser-cfg-fragment-v3",
                        "symcc-parser-cfg-fragment-v4",
                        "symcc-parser-cfg-fragment-v5",
                    } or
                    not isinstance(cfg_fragment.get("productions"), list) or
                    len(cfg_fragment["productions"]) !=
                    record.parser_cfg_productions or
                    not isinstance(cfg_fragment.get("cycles"), list) or
                    len(cfg_fragment["cycles"]) != record.parser_cfg_cycles or
                    cfg_fragment.get("schema") in {
                        "symcc-parser-cfg-fragment-v2",
                        "symcc-parser-cfg-fragment-v3",
                        "symcc-parser-cfg-fragment-v4",
                        "symcc-parser-cfg-fragment-v5",
                    } and (
                        not isinstance(cfg_fragment.get("instances"), list) or
                        len(cfg_fragment["instances"]) !=
                        record.parser_ect_instances
                    )
                ):
                    continue
                fragment_schema = cfg_fragment.get("schema")
                if fragment_schema in {
                    "symcc-parser-cfg-fragment-v2",
                    "symcc-parser-cfg-fragment-v3",
                    "symcc-parser-cfg-fragment-v4",
                    "symcc-parser-cfg-fragment-v5",
                }:
                    selected_instances = [
                        instance
                        for instance in cfg_fragment["instances"]
                        if (
                            isinstance(instance, dict) and
                            instance.get("selected") is True
                        )
                    ]
                    if (
                        len(selected_instances) > 1 or
                        bool(selected_instances) !=
                        bool(record.parser_ect_instance_id) or
                        selected_instances and (
                            selected_instances[0].get("shape_id") !=
                            record.parser_ect_shape_id or
                            selected_instances[0].get("instance_id") !=
                            record.parser_ect_instance_id
                        )
                    ):
                        continue
                if fragment_schema in {
                    "symcc-parser-cfg-fragment-v3",
                    "symcc-parser-cfg-fragment-v4",
                    "symcc-parser-cfg-fragment-v5",
                }:
                    if any(
                        not isinstance(production, dict) or
                        production.get("schema") !=
                        "symcc-parser-production-v2" or
                        not isinstance(production.get("epsilon"), bool) or
                        not isinstance(
                            production.get("alternative"), int) or
                        isinstance(
                            production.get("alternative"), bool) or
                        not 0 <= production["alternative"] < 8
                        for production in cfg_fragment["productions"]
                    ):
                        continue
                    alternatives = sum(
                        isinstance(production, dict) and
                        isinstance(production.get("alternative"), int) and
                        not isinstance(
                            production.get("alternative"), bool) and
                        production["alternative"] > 0
                        for production in cfg_fragment["productions"]
                    )
                    epsilon_productions = sum(
                        isinstance(production, dict) and
                        production.get("epsilon") is True
                        for production in cfg_fragment["productions"]
                    )
                    if (
                        alternatives != record.parser_cfg_alternatives or
                        epsilon_productions !=
                        record.parser_epsilon_productions
                    ):
                        continue
                    if fragment_schema in {
                        "symcc-parser-cfg-fragment-v4",
                        "symcc-parser-cfg-fragment-v5",
                    }:
                        if (
                            not isinstance(
                                cfg_fragment.get("packed_nodes"), list) or
                            len(cfg_fragment["packed_nodes"]) !=
                            record.parser_packed_nodes or
                            not isinstance(
                                cfg_fragment.get("packed_edges"), list) or
                            len(cfg_fragment["packed_edges"]) !=
                            record.parser_packed_edges or
                            not isinstance(cfg_fragment.get("roots"), list) or
                            not isinstance(
                                cfg_fragment.get("selected_path"), list) or
                            not isinstance(
                                cfg_fragment.get("truncated"), bool)
                        ):
                            continue
                        if (
                            fragment_schema ==
                            "symcc-parser-cfg-fragment-v5" and (
                                not isinstance(
                                    cfg_fragment.get(
                                        "nullable_rules"), list) or
                                len(cfg_fragment["nullable_rules"]) !=
                                record.parser_nullable_rules or
                                not isinstance(
                                    cfg_fragment.get(
                                        "nullable_proofs"), list) or
                                len(cfg_fragment["nullable_proofs"]) !=
                                record.parser_nullable_proofs or
                                not isinstance(
                                    cfg_fragment.get(
                                        "nullable_sccs"), list) or
                                len(cfg_fragment["nullable_sccs"]) !=
                                record.parser_nullable_sccs
                            )
                        ):
                            continue
                elif (
                    record.parser_cfg_alternatives or
                    record.parser_epsilon_productions or
                    record.parser_packed_nodes or
                    record.parser_packed_edges or
                    record.parser_nullable_rules or
                    record.parser_nullable_proofs or
                    record.parser_nullable_sccs
                ):
                    continue
            if record.status in {"queued", "dispatched"}:
                record.status = "retry"
                record.updated = 0.0
            self.records[record.proposal_id] = record
            self.path_records[record.candidate_path] = record.proposal_id
        self.parser_forest_grammar_sha256s = {
            record.parser_forest_grammar_sha256
            for record in self.records.values()
            if record.parser_forest_grammar_sha256
        }
        self.parser_cross_command_pairs = {
            (
                record.parser_cross_primary_command_sha256,
                record.parser_cross_secondary_command_sha256,
            )
            for record in self.records.values()
            if record.parser_cross_primary_command_sha256
        }
