#!/usr/bin/env python3
"""Built-in data-only semantic proposal generation.

The generator extracts small comparison-taint cores, transfers high-progress
data-coverage exemplars, and mutates UCSan object partitions.  It never emits
code; every candidate remains subject to VerifiedProposalManager's concrete
execution and target-branch validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import json
import math
import os
import tempfile
from typing import Any, Iterable

from hybrid_feedback import SolverTelemetry
from query_store import QueryStore
from ucsan_seed import (
    MAGIC,
    OBJECT_ENTRY,
    SeedEntry,
    canonicalize,
    parse_seed,
    serialize_seed,
)
from verified_proposals import VerifiedProposalManager


@dataclass(frozen=True)
class ConstraintCore:
    site: int
    branch: int
    lo: int
    hi: int
    dependency_count: int
    interesting: bool

    @property
    def span(self) -> int:
        return self.hi - self.lo + 1


@dataclass
class FeatureExemplar:
    matched: int
    width: int
    fragment: bytes


@dataclass(frozen=True)
class RelevanceSlice:
    core: ConstraintCore
    score: float


@dataclass(frozen=True)
class TokenGrammarRule:
    kind: str
    prefix: bytes
    body: bytes
    suffix: bytes = b""

    def render(self, token: bytes | None = None) -> bytes:
        body = self.body if token is None else token
        return self.prefix + body + self.suffix


@dataclass(frozen=True)
class TokenSpan:
    lo: int
    hi: int
    token: bytes
    branch: int


@dataclass
class LearnedGrammarRule:
    rule_id: str
    kind: str
    prefix: bytes
    body: bytes
    suffix: bytes
    support: int = 0
    attempts: int = 0
    validations: int = 0
    verified: int = 0
    retained: int = 0
    coverage_features: int = 0
    parser_validations: int = 0
    parser_accepted: int = 0
    data_quality_sum: float = 0.0
    data_observations: int = 0
    string_queries: int = 0
    string_verified: int = 0
    last_attempt_observation: int = 0
    rejected_contexts: dict[str, int] | None = None
    structural_coverage: dict[str, int] | None = None

    def render(self) -> bytes:
        return self.prefix + self.body + self.suffix

    def context_rejected(self, context_id: str) -> bool:
        if not context_id or not self.rejected_contexts:
            return False
        return self.rejected_contexts.get(context_id, 0) >= 2

    def score_for_context(
        self,
        context_id: str,
        grammar_coverage: int = 0,
    ) -> float:
        features = (
            (self.structural_coverage or {}).get(context_id, 0)
            if context_id else 0
        )
        return (
            self.score +
            0.5 / math.sqrt(1.0 + features) +
            0.35 / math.sqrt(1.0 + max(0, int(grammar_coverage)))
        )

    @property
    def score(self) -> float:
        if self.attempts == 0:
            return 4.0 + math.log1p(self.support)
        validity = (self.verified + 1.0) / (self.validations + 2.0)
        retention = (self.retained + 1.0) / (self.validations + 2.0)
        parser_validity = (
            (self.parser_accepted + 1.0) /
            (self.parser_validations + 2.0)
        )
        exploration = 1.0 / math.sqrt(1.0 + self.attempts)
        return (
            1.4 * validity + 1.0 * retention + 0.5 * parser_validity +
            0.5 * exploration +
            0.1 * math.log1p(self.support) +
            0.05 * math.log1p(self.coverage_features)
        )


@dataclass
class HistorySeed:
    seed_id: str
    content: bytes
    coverage_features: int = 0
    retained: int = 0
    attempts: int = 0
    validations: int = 0
    verified: int = 0

    @property
    def score(self) -> float:
        validity = (self.verified + 1.0) / (self.validations + 2.0)
        exploration = 1.0 / math.sqrt(1.0 + self.attempts)
        return (
            math.log1p(self.coverage_features) +
            0.5 * math.log1p(self.retained) +
            validity +
            0.25 * exploration
        )


_BRACKETS = {
    ord('"'): ord('"'),
    ord("'"): ord("'"),
    ord("("): ord(")"),
    ord("["): ord("]"),
    ord("{"): ord("}"),
    ord("<"): ord(">"),
}

_TOKEN_DELIMITERS = frozenset(b" \t\r\n,;=:/\\()[]{}<>\"'")
_GRAMMAR_RULE_KINDS = frozenset({
    "literal",
    "delimited",
    "key_value",
    "segment",
    "choice",
    "sequence",
    "optional",
    "repetition",
    "recursive",
    "subtree",
    "epsilon",
    "synchronized",
})


def infer_token_spans(
    content: bytes,
    cores: Iterable[ConstraintCore],
    *,
    max_span: int = 128,
) -> tuple[TokenSpan, ...]:
    """Expand tainted byte cores to bounded lexical token boundaries."""
    spans: dict[tuple[int, int, int], TokenSpan] = {}
    size = len(content)
    for core in cores:
        lo = max(0, min(size, core.lo))
        hi = max(lo, min(size, core.hi + 1))
        closed = (
            hi - lo >= 2 and content[lo] in _BRACKETS and
            content[hi - 1] == _BRACKETS[content[lo]]
        )
        if not closed:
            while (
                lo > 0 and hi - lo < max_span and
                content[lo - 1] not in _TOKEN_DELIMITERS
            ):
                lo -= 1
            while (
                hi < size and hi - lo < max_span and
                content[hi] not in _TOKEN_DELIMITERS
            ):
                hi += 1
        if hi <= lo:
            continue
        token = content[lo:hi]
        if not token or len(token) > max_span:
            continue
        spans[(core.branch, lo, hi)] = TokenSpan(
            lo, hi, token, core.branch)
    return tuple(sorted(
        spans.values(),
        key=lambda span: (span.lo, span.hi, span.branch),
    ))


def _grammar_rule_id(
    kind: str,
    prefix: bytes,
    body: bytes,
    suffix: bytes,
) -> str:
    payload = b"\0".join((
        kind.encode("ascii", errors="ignore"), prefix, body, suffix))
    return hashlib.sha256(payload).hexdigest()


def _grammar_context_id(
    content: bytes,
    span: TokenSpan,
) -> str:
    left = content[max(0, span.lo - 16):span.lo]
    right = content[span.hi:min(len(content), span.hi + 16)]
    payload = b"\0".join((
        str(span.branch).encode("ascii"),
        str(min(63, int(math.log2(max(1, len(content)))))).encode("ascii"),
        left,
        right,
    ))
    return hashlib.sha256(payload).hexdigest()


def synthesize_token_rules(
    tokens: Iterable[bytes],
    *,
    limit: int = 256,
) -> tuple[TokenGrammarRule, ...]:
    """Infer a small online token grammar from observed dictionary fragments."""
    rules: list[TokenGrammarRule] = []
    seen: set[tuple[str, bytes, bytes, bytes]] = set()

    def add(kind: str, prefix: bytes, body: bytes, suffix: bytes = b"") -> None:
        if not body or len(prefix) + len(body) + len(suffix) > 128:
            return
        key = (kind, prefix, body, suffix)
        if key in seen:
            return
        seen.add(key)
        rules.append(TokenGrammarRule(kind, prefix, body, suffix))

    for raw in tokens:
        token = bytes(raw[:128])
        if not token:
            continue
        add("literal", b"", token)
        if len(token) >= 2 and token[0] in _BRACKETS:
            closing = _BRACKETS[token[0]]
            if token[-1] == closing:
                add("delimited", bytes([token[0]]), token[1:-1],
                    bytes([closing]))
        for separator in (b"=", b":"):
            if separator in token:
                left, right = token.split(separator, 1)
                if left and right:
                    add("key_value", left + separator, right)
        for separator in (b"/", b".", b"-", b"_"):
            if separator in token:
                parts = [part for part in token.split(separator) if part]
                for part in parts[:4]:
                    add("segment", b"", part)
        if len(rules) >= limit:
            break
    return tuple(rules[:limit])


def extract_constraint_cores(
    telemetry: SolverTelemetry,
    input_size: int,
    *,
    max_cores: int = 8,
    max_span: int = 64,
) -> tuple[ConstraintCore, ...]:
    """Compress comparison dependencies into bounded, ranked byte intervals."""
    selected: dict[tuple[int, int, int], ConstraintCore] = {}
    for site, branch, count, lo, hi, _taken, interesting in (
            telemetry.comparison_taints):
        if not site or not branch or lo >= input_size:
            continue
        hi = min(int(hi), input_size - 1)
        lo = max(0, int(lo))
        if hi < lo:
            continue
        if hi - lo + 1 > max_span:
            # Preserve a dense semantic core instead of mutating an entire
            # wide dependency cone.
            hi = min(hi, lo + max_span - 1)
        core = ConstraintCore(
            int(site), int(branch), lo, hi,
            max(1, int(count)), bool(interesting))
        key = (core.branch, core.lo, core.hi)
        old = selected.get(key)
        if old is None or core.dependency_count > old.dependency_count:
            selected[key] = core
    ranked = sorted(
        selected.values(),
        key=lambda core: (
            int(core.interesting),
            min(1.0, core.dependency_count / max(1, core.span)),
            -core.span,
            -core.lo,
        ),
        reverse=True,
    )
    return tuple(ranked[:max(1, int(max_cores))])


def ifss_relevance_slices(
    telemetry: SolverTelemetry,
    input_size: int,
    *,
    target_branch: int = 0,
    max_slices: int = 8,
    max_span: int = 64,
) -> tuple[RelevanceSlice, ...]:
    """Rank bounded IFSS-like byte slices for targeted data transformations."""
    slices: list[RelevanceSlice] = []
    for core in extract_constraint_cores(
            telemetry, input_size, max_cores=max_slices * 2,
            max_span=max_span):
        density = core.dependency_count / max(1, core.span)
        target_bonus = (
            1.0 if target_branch and core.branch == target_branch else 0.0)
        score = (
            (2.0 if core.interesting else 0.0)
            + target_bonus
            + min(2.0, density)
            + 1.0 / max(1, core.span)
        )
        slices.append(RelevanceSlice(core, score))
    slices.sort(key=lambda item: (
        item.score,
        item.core.dependency_count,
        -item.core.span,
        -item.core.lo,
    ), reverse=True)
    return tuple(slices[:max(1, int(max_slices))])


def _load_tokens(paths: Iterable[str], *, limit: int = 256) -> tuple[bytes, ...]:
    tokens: list[bytes] = []
    seen: set[bytes] = set()
    for raw_path in paths:
        path = str(raw_path)
        if not path:
            continue
        candidates: list[str]
        if os.path.isdir(path):
            try:
                candidates = [
                    os.path.join(path, name)
                    for name in sorted(os.listdir(path))[:limit]
                ]
            except OSError:
                continue
        else:
            candidates = [path]
        for candidate in candidates:
            try:
                with open(candidate, "rb") as stream:
                    token = stream.read(65)
            except OSError:
                continue
            if 0 < len(token) <= 64 and token not in seen:
                seen.add(token)
                tokens.append(token)
                if len(tokens) >= limit:
                    return tuple(tokens)
    return tuple(tokens)


class SemanticProposalGenerator:
    SCHEMA = 25
    RESEARCH_ARTIFACT_SCHEMA = "symcc-pcfg-research-artifact-v1"
    PCFG_CONTEXT_LEVELS = (
        "global",
        "parent",
        "circuit",
        "sibling",
        "history",
    )
    HARD_MAX_GRAMMAR_RULES = 8192
    GRAMMAR_BITMAP_SIZE = 1 << 16
    HARD_MAX_CFG_PRODUCTIONS = 4096
    HARD_MAX_CFG_CYCLES = 4096
    HARD_MAX_ECT_SHAPES = 4096
    HARD_MAX_ECT_INSTANCES = 8192
    HARD_MAX_PACKED_NODES = 8192
    HARD_MAX_PACKED_EDGES = 32768
    HARD_MAX_NULLABLE_RULES = 4096
    HARD_MAX_SYNC_TRANSACTIONS = 4096
    HARD_MAX_SLOT_RELATIONS = 4096
    HARD_MAX_SYNC_CANDIDATES_PER_PARENT = 256
    HARD_MAX_PCFG_OBSERVATIONS = 8192
    HARD_MAX_PCFG_CONTEXTS = 8192
    HARD_MAX_PCFG_CIRCUIT_CONTEXTS = 8192
    HARD_MAX_PCFG_SIBLING_CONTEXTS = 8192
    HARD_MAX_PCFG_HISTORY_CONTEXTS = 8192
    HARD_MAX_PCFG_CONTEXT_STATES = 32768
    HARD_MAX_PCFG_PREQUENTIAL_RECEIPTS = 32768
    HARD_MAX_PCFG_CIRCUIT_RECEIPTS = 32768
    HARD_MAX_PCFG_SIBLING_RECEIPTS = 32768
    HARD_MAX_PCFG_HISTORY_RECEIPTS = 32768
    HARD_MAX_PCFG_ANYTIME_CONTEXT_ALLOCATIONS = 32768
    PCFG_CALIBRATION_RECENT_FRAGMENTS = 16
    PCFG_ADAPTIVE_MAX_FRAGMENTS = 64
    PCFG_ADAPTIVE_MIN_FRAGMENTS = 8
    PCFG_ADAPTIVE_CLIP_BITS = 4.0
    PCFG_ADAPTIVE_DELTA = 0.05
    PCFG_GLOBAL_FWER_DELTA = 0.05
    PCFG_ANYTIME_SPENDING_SCALE = 6.0 / (math.pi ** 2)
    PCFG_DIRICHLET_ALPHA = 0.5
    PCFG_CONTEXT_PRIOR_STRENGTH = 2.0

    def __init__(
        self,
        state_path: str | None,
        *,
        token_paths: Iterable[str] = (),
        max_proposals_per_observation: int = 12,
        max_input_bytes: int = 16 * 1024 * 1024,
        max_records: int = 100_000,
        max_grammar_rules: int = 2048,
        max_grammar_span: int = 128,
        max_cfg_derivation_depth: int = 3,
        pareto_scheduling: bool = True,
        query_store_root: str | None = None,
        query_holes: bool = True,
        max_query_holes: int = 16,
        plateau_observations: int = 64,
        max_history_seeds: int = 128,
        max_history_seed_bytes: int = 4096,
        pcfg_context_order: int | str = "history",
        autosave: bool = True,
    ) -> None:
        self.state_path = state_path or ""
        self.autosave = bool(autosave)
        self.tokens = _load_tokens(token_paths)
        self.max_proposals = max(
            1, min(64, int(max_proposals_per_observation)))
        self.max_input_bytes = max(1, int(max_input_bytes))
        self.max_records = max(128, int(max_records))
        self.max_grammar_rules = max(
            32, min(self.HARD_MAX_GRAMMAR_RULES, int(max_grammar_rules)))
        self.max_grammar_span = max(
            8, min(1024, int(max_grammar_span)))
        self.max_cfg_derivation_depth = max(
            1, min(8, int(max_cfg_derivation_depth)))
        self.pcfg_context_order = self.normalize_pcfg_context_order(
            pcfg_context_order)
        self.pareto_scheduling = bool(pareto_scheduling)
        self.exemplars: dict[int, FeatureExemplar] = {}
        self.grammar_fragments: list[bytes] = []
        self.grammar_rules: dict[str, LearnedGrammarRule] = {}
        self.parser_context_aliases: dict[str, str] = {}
        self.parser_production_aliases: dict[str, str] = {}
        self.cfg_productions: dict[str, dict[str, Any]] = {}
        self.cfg_cycles: dict[str, dict[str, Any]] = {}
        self.recursive_rule_cycles: dict[str, set[str]] = {}
        self.ect_shapes: dict[str, set[str]] = {}
        self.ect_instances: dict[str, dict[str, Any]] = {}
        self.packed_nodes: dict[str, dict[str, Any]] = {}
        self.packed_edges: dict[str, dict[str, Any]] = {}
        self.packed_root_ids: set[str] = set()
        self.packed_root_evidence: dict[str, dict[str, str]] = {}
        self.nullable_rules: dict[str, dict[str, Any]] = {}
        self.nullable_proofs: dict[str, dict[str, Any]] = {}
        self.nullable_sccs: dict[str, dict[str, Any]] = {}
        self.nullable_shape_ids: set[str] = set()
        self.sync_transactions: dict[str, dict[str, Any]] = {}
        self.synchronized_rule_transactions: dict[str, set[str]] = {}
        self.slot_relations: dict[str, dict[str, Any]] = {}
        self.parent_shape_relations: dict[str, set[str]] = {}
        self.pcfg_families: dict[str, dict[str, Any]] = {}
        self.pcfg_counts: dict[str, dict[str, int]] = {}
        self.pcfg_observations: dict[str, list[list[str]]] = {}
        self.pcfg_contexts: dict[str, dict[str, Any]] = {}
        self.pcfg_context_counts: dict[str, dict[str, int]] = {}
        self.pcfg_context_observations: dict[
            str, list[list[str]]
        ] = {}
        self.pcfg_prequential_receipts: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_context_calibration_stats: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_adaptive_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_anytime_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_circuit_contexts: dict[str, dict[str, Any]] = {}
        self.pcfg_circuit_counts: dict[str, dict[str, int]] = {}
        self.pcfg_circuit_observations: dict[
            str, list[list[str]]
        ] = {}
        self.pcfg_circuit_receipts: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_circuit_calibration_stats: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_circuit_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_circuit_anytime_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_sibling_contexts: dict[str, dict[str, Any]] = {}
        self.pcfg_sibling_counts: dict[str, dict[str, int]] = {}
        self.pcfg_sibling_observations: dict[
            str, list[list[str]]
        ] = {}
        self.pcfg_sibling_receipts: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_sibling_calibration_stats: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_sibling_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_sibling_anytime_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_history_contexts: dict[str, dict[str, Any]] = {}
        self.pcfg_history_counts: dict[str, dict[str, int]] = {}
        self.pcfg_history_observations: dict[
            str, list[list[str]]
        ] = {}
        self.pcfg_history_receipts: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_history_calibration_stats: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_history_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_history_anytime_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_anytime_context_allocations: dict[
            str, dict[str, Any]
        ] = {}
        self.pcfg_global_anytime_certificates: dict[
            str, list[dict[str, Any]]
        ] = {}
        self.pcfg_anytime_allocation_ledger_valid = True
        self.packed_inside: dict[str, float] = {}
        self.packed_outside: dict[str, float] = {}
        self.packed_alternative_probabilities: dict[
            str, dict[str, Any]
        ] = {}
        self.packed_context_inside: dict[str, float] = {}
        self.packed_context_outside: dict[str, float] = {}
        self.packed_context_alternative_probabilities: dict[
            str, dict[str, Any]
        ] = {}
        self.subtree_rule_shapes: dict[str, set[str]] = {}
        self.subtree_rule_instances: dict[str, set[str]] = {}
        self.parser_shape_aliases: dict[str, set[str]] = {}
        self.pareto_rankings = 0
        self.pareto_frontier_rules = 0
        self.grammar_bitmap: dict[int, int] = {}
        self.grammar_bitmap_owners: dict[int, str] = {}
        self.grammar_bitmap_events = 0
        self.grammar_bitmap_collisions = 0
        self.query_store = (
            QueryStore(query_store_root)
            if query_store_root and query_holes else None
        )
        self.max_query_holes = max(0, min(64, int(max_query_holes)))
        self.plateau_observations = max(
            1, min(65536, int(plateau_observations)))
        self.max_history_seeds = max(
            1, min(4096, int(max_history_seeds)))
        self.max_history_seed_bytes = max(
            64, min(65536, int(max_history_seed_bytes)))
        self.history_seeds: dict[str, HistorySeed] = {}
        self.plateau_count = 0
        self.history_attempts = 0
        self.history_verified = 0
        self.history_retained = 0
        self.query_hole_attempts = 0
        self.query_hole_verified = 0
        self.query_hole_rejected = 0
        self.observations = 0
        self.generated = 0
        self.pcfg_state_order_mismatch_reset = False
        self._load()
        if self.pcfg_state_order_mismatch_reset:
            self._clear_pcfg_evidence()
            self._refresh_probabilistic_state()

    @classmethod
    def normalize_pcfg_context_order(cls, value: int | str) -> int:
        if isinstance(value, bool):
            raise ValueError("PCFG context order must be a level name or 0..4")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in cls.PCFG_CONTEXT_LEVELS:
                return cls.PCFG_CONTEXT_LEVELS.index(normalized)
            try:
                value = int(normalized)
            except ValueError as exc:
                raise ValueError(
                    "PCFG context order must be "
                    "global/parent/circuit/sibling/history or 0..4"
                ) from exc
        try:
            order = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "PCFG context order must be a level name or 0..4"
            ) from exc
        if not 0 <= order < len(cls.PCFG_CONTEXT_LEVELS):
            raise ValueError("PCFG context order must be between 0 and 4")
        return order

    @property
    def pcfg_context_level(self) -> str:
        return self.PCFG_CONTEXT_LEVELS[self.pcfg_context_order]

    def _clear_pcfg_evidence(self) -> None:
        stores = (
            self.pcfg_families,
            self.pcfg_counts,
            self.pcfg_observations,
            self.pcfg_contexts,
            self.pcfg_context_counts,
            self.pcfg_context_observations,
            self.pcfg_prequential_receipts,
            self.pcfg_context_calibration_stats,
            self.pcfg_adaptive_certificates,
            self.pcfg_anytime_certificates,
            self.pcfg_circuit_contexts,
            self.pcfg_circuit_counts,
            self.pcfg_circuit_observations,
            self.pcfg_circuit_receipts,
            self.pcfg_circuit_calibration_stats,
            self.pcfg_circuit_certificates,
            self.pcfg_circuit_anytime_certificates,
            self.pcfg_sibling_contexts,
            self.pcfg_sibling_counts,
            self.pcfg_sibling_observations,
            self.pcfg_sibling_receipts,
            self.pcfg_sibling_calibration_stats,
            self.pcfg_sibling_certificates,
            self.pcfg_sibling_anytime_certificates,
            self.pcfg_history_contexts,
            self.pcfg_history_counts,
            self.pcfg_history_observations,
            self.pcfg_history_receipts,
            self.pcfg_history_calibration_stats,
            self.pcfg_history_certificates,
            self.pcfg_history_anytime_certificates,
            self.pcfg_anytime_context_allocations,
            self.pcfg_global_anytime_certificates,
            self.packed_inside,
            self.packed_outside,
            self.packed_alternative_probabilities,
            self.packed_context_inside,
            self.packed_context_outside,
            self.packed_context_alternative_probabilities,
        )
        for store in stores:
            store.clear()
        self.pcfg_anytime_allocation_ledger_valid = True

    @staticmethod
    def _patch(content: bytes, offset: int, replacement: bytes) -> bytes:
        candidate = bytearray(content)
        end = min(len(candidate), offset + len(replacement))
        if end > offset:
            candidate[offset:end] = replacement[:end - offset]
        return bytes(candidate)

    @staticmethod
    def _splice(
        content: bytes,
        lo: int,
        hi: int,
        replacement: bytes,
    ) -> bytes:
        lo = max(0, min(len(content), int(lo)))
        hi = max(lo, min(len(content), int(hi)))
        return content[:lo] + replacement + content[hi:]

    def _learn_rule(
        self,
        kind: str,
        prefix: bytes,
        body: bytes,
        suffix: bytes = b"",
    ) -> LearnedGrammarRule | None:
        if (
            kind not in _GRAMMAR_RULE_KINDS or
            not body and kind not in {"optional", "recursive", "epsilon"} or
            kind == "recursive" and not prefix and not suffix or
            kind == "epsilon" and (prefix or body or suffix) or
            len(prefix) + len(body) + len(suffix) > 256
        ):
            return None
        rule_id = _grammar_rule_id(kind, prefix, body, suffix)
        rule = self.grammar_rules.get(rule_id)
        if rule is None:
            if len(self.grammar_rules) >= self.max_grammar_rules:
                removable = min(
                    self.grammar_rules.values(),
                    key=lambda item: (
                        item.retained > 0,
                        item.score,
                        item.support,
                        item.rule_id,
                    ),
                )
                self.grammar_rules.pop(removable.rule_id, None)
                self.recursive_rule_cycles.pop(removable.rule_id, None)
                self.subtree_rule_shapes.pop(removable.rule_id, None)
                self.subtree_rule_instances.pop(removable.rule_id, None)
                for transaction_id in (
                    self.synchronized_rule_transactions.pop(
                        removable.rule_id, set())
                ):
                    self.sync_transactions.pop(transaction_id, None)
            rule = LearnedGrammarRule(
                rule_id, kind, prefix, body, suffix)
            self.grammar_rules[rule_id] = rule
        rule.support += 1
        return rule

    def _learn_online_grammar(
        self,
        content: bytes,
        cores: tuple[ConstraintCore, ...],
    ) -> tuple[TokenSpan, ...]:
        spans = infer_token_spans(
            content, cores, max_span=self.max_grammar_span)
        observed: list[TokenGrammarRule] = []
        for span in spans:
            observed.extend(synthesize_token_rules((span.token,), limit=16))
            self._learn_rule("optional", b"", span.token)
            if len(span.token) <= 64:
                self._learn_rule("repetition", b"", span.token + span.token)
        for fragment in self.tokens[:32]:
            observed.extend(synthesize_token_rules((fragment,), limit=16))
        for fragment in self.grammar_fragments[-32:]:
            observed.extend(synthesize_token_rules((fragment,), limit=16))
        for rule in observed[:512]:
            self._learn_rule(
                rule.kind, rule.prefix, rule.body, rule.suffix)
        terminal_bodies = {
            rule.body
            for rule in observed
            if rule.kind in {"literal", "segment"} and rule.body
        }
        for span in spans:
            if (
                len(span.token) >= 2 and
                span.token[0] in _BRACKETS and
                span.token[-1] == _BRACKETS[span.token[0]]
            ):
                wrapper = bytes((span.token[0],))
                for body in sorted(terminal_bodies)[:32]:
                    self._learn_rule("choice", wrapper, body, wrapper)

        ordered = sorted(spans, key=lambda span: (span.lo, span.hi))
        for left, right in zip(ordered, ordered[1:]):
            if right.lo < left.hi or right.hi - left.lo > 256:
                continue
            sequence = content[left.lo:right.hi]
            self._learn_rule("sequence", b"", sequence)

        groups: dict[tuple[bytes, bytes], list[bytes]] = {}
        for rule in self.grammar_rules.values():
            if rule.kind in {"literal", "delimited", "key_value", "segment"}:
                groups.setdefault((rule.prefix, rule.suffix), []).append(
                    rule.body)
        for (prefix, suffix), bodies in groups.items():
            unique = sorted(set(bodies), key=lambda item: (len(item), item))
            if len(unique) < 2:
                continue
            for body in unique[:32]:
                self._learn_rule("choice", prefix, body, suffix)
        return spans

    def _rule_renderings(
        self,
        rule: LearnedGrammarRule,
        token: bytes,
    ) -> tuple[tuple[bytes, int], ...]:
        if rule.kind in {"optional", "epsilon"}:
            return ((b"", 1),)
        if rule.kind == "recursive":
            rendered = token
            results: list[tuple[bytes, int]] = []
            for depth in range(1, self.max_cfg_derivation_depth + 1):
                rendered = rule.prefix + rendered + rule.suffix
                if len(rendered) > self.max_input_bytes:
                    break
                results.append((rendered, depth))
            return tuple(results)
        return ((rule.render(), 1),)

    def _rule_matches_source_shape(
        self,
        rule: LearnedGrammarRule,
        source_context_id: str,
        source_token: bytes | None = None,
    ) -> bool:
        if rule.kind not in {"subtree", "epsilon", "synchronized"}:
            return True
        matched_shapes = (
            self.subtree_rule_shapes.get(rule.rule_id, set()) &
            self.parser_shape_aliases.get(source_context_id, set())
        )
        if not matched_shapes:
            return False
        if rule.kind != "synchronized":
            return True
        if source_token is None:
            return False
        for transaction_id in self.synchronized_rule_transactions.get(
                rule.rule_id, ()):
            transaction = self.sync_transactions.get(transaction_id)
            if (
                transaction is None or
                transaction.get("parent_shape_id") not in matched_shapes
            ):
                continue
            parent = self.ect_instances.get(
                str(transaction.get("parent_instance_id", "")))
            if parent is not None and parent.get(
                    "yield_hex") == source_token.hex():
                return True
        return False

    def _pareto_objectives(
        self,
        rule: LearnedGrammarRule,
        source_context_id: str,
    ) -> tuple[float, ...]:
        context_id = self._resolved_parser_context(
            rule.rule_id, source_context_id)
        production_id = self._resolved_parser_production(
            rule.rule_id, source_context_id)
        validity = (rule.verified + 1.0) / (rule.validations + 2.0)
        retention = (rule.retained + 1.0) / (rule.validations + 2.0)
        feature_rate = 1.0 - math.exp(
            -rule.coverage_features / max(1.0, 4.0 * rule.attempts))
        edge_yield = 0.5 * retention + 0.5 * feature_rate
        data_progress = (
            rule.data_quality_sum + 0.5
        ) / (rule.data_observations + 1.0)
        string_yield = (
            rule.string_verified + 0.5
        ) / (rule.string_queries + 1.0)
        grammar_novelty = 1.0 / math.sqrt(
            1.0 + self._grammar_bitmap_count(
                rule.rule_id, context_id, production_id))
        pcfg_likelihood = 0.5
        pcfg_information = 0.5
        if rule.kind in {"subtree", "epsilon", "synchronized"}:
            matching_shapes = (
                self.subtree_rule_shapes.get(rule.rule_id, set()) &
                self.parser_shape_aliases.get(source_context_id, set())
            )
            if rule.kind == "synchronized":
                matching_instances = len(
                    self.synchronized_rule_transactions.get(
                        rule.rule_id, ()))
            else:
                matching_instances = sum(
                    len(self.ect_shapes.get(shape_id, ()))
                    for shape_id in matching_shapes
                )
            ect_novelty = (
                1.0 / math.sqrt(1.0 + rule.attempts) +
                1.0 / math.sqrt(1.0 + matching_instances)
            ) / 2.0
            shape_statistics = [
                self._pcfg_shape_statistics(shape_id)
                for shape_id in matching_shapes
            ]
            if shape_statistics:
                pcfg_likelihood = max(
                    item[0] for item in shape_statistics)
                pcfg_information = max(
                    (
                        1.0 / math.sqrt(1.0 + item[1]) *
                        (0.5 + 0.5 * math.sqrt(item[2]))
                    )
                    for item in shape_statistics
                )
        else:
            ect_novelty = 0.5
        age = max(
            0, self.observations - rule.last_attempt_observation)
        fairness = (age + 1.0) / (age + 32.0)
        return (
            max(0.0, min(1.0, validity)),
            max(0.0, min(1.0, edge_yield)),
            max(0.0, min(1.0, data_progress)),
            max(0.0, min(1.0, string_yield)),
            max(0.0, min(1.0, grammar_novelty)),
            max(0.0, min(1.0, ect_novelty)),
            max(0.0, min(1.0, pcfg_likelihood)),
            max(0.0, min(1.0, pcfg_information)),
            max(0.0, min(1.0, fairness)),
        )

    @staticmethod
    def _pareto_dominates(
        left: tuple[float, ...],
        right: tuple[float, ...],
    ) -> bool:
        epsilon = 1e-12
        return (
            all(a + epsilon >= b for a, b in zip(left, right)) and
            any(a > b + epsilon for a, b in zip(left, right))
        )

    def _pareto_rank_rules(
        self,
        rules: Iterable[LearnedGrammarRule],
        source_context_id: str,
    ) -> list[LearnedGrammarRule]:
        candidates = list(rules)
        if not self.pareto_scheduling or len(candidates) < 2:
            return sorted(
                candidates,
                key=lambda rule: (
                    rule.score_for_context(
                        self._resolved_parser_context(
                            rule.rule_id, source_context_id),
                        self._grammar_bitmap_count(
                            rule.rule_id,
                            self._resolved_parser_context(
                                rule.rule_id, source_context_id),
                            self._resolved_parser_production(
                                rule.rule_id, source_context_id),
                        ),
                    ),
                    rule.support,
                    rule.rule_id,
                ),
                reverse=True,
            )
        objectives = {
            rule.rule_id: self._pareto_objectives(
                rule, source_context_id)
            for rule in candidates
        }
        if len(candidates) > 256:
            dimensions = len(next(iter(objectives.values())))
            by_dimension = [
                sorted(
                    candidates,
                    key=lambda rule: (
                        objectives[rule.rule_id][dimension],
                        rule.rule_id,
                    ),
                    reverse=True,
                )
                for dimension in range(dimensions)
            ]
            selected: list[LearnedGrammarRule] = []
            selected_ids: set[str] = set()
            position = 0
            while len(selected) < 256 and position < len(candidates):
                for ordered in by_dimension:
                    rule = ordered[position]
                    if rule.rule_id in selected_ids:
                        continue
                    selected_ids.add(rule.rule_id)
                    selected.append(rule)
                    if len(selected) >= 256:
                        break
                position += 1
            candidates = selected
            objectives = {
                rule.rule_id: objectives[rule.rule_id]
                for rule in candidates
            }
        remaining = {rule.rule_id for rule in candidates}
        rank: dict[str, int] = {}
        crowding: dict[str, float] = {}
        frontier_size = 0
        front_index = 0
        while remaining:
            front = {
                rule_id
                for rule_id in remaining
                if not any(
                    self._pareto_dominates(
                        objectives[other], objectives[rule_id])
                    for other in remaining
                    if other != rule_id
                )
            }
            if not front:
                front = {min(remaining)}
            if front_index == 0:
                frontier_size = len(front)
            for rule_id in front:
                rank[rule_id] = front_index
                crowding[rule_id] = 0.0
            if len(front) <= 2:
                for rule_id in front:
                    crowding[rule_id] = math.inf
            else:
                dimensions = len(next(iter(objectives.values())))
                for dimension in range(dimensions):
                    ordered = sorted(
                        front,
                        key=lambda rule_id: (
                            objectives[rule_id][dimension], rule_id),
                    )
                    lower = objectives[ordered[0]][dimension]
                    upper = objectives[ordered[-1]][dimension]
                    crowding[ordered[0]] = math.inf
                    crowding[ordered[-1]] = math.inf
                    if upper <= lower:
                        continue
                    for position in range(1, len(ordered) - 1):
                        rule_id = ordered[position]
                        if math.isinf(crowding[rule_id]):
                            continue
                        crowding[rule_id] += (
                            objectives[ordered[position + 1]][dimension] -
                            objectives[ordered[position - 1]][dimension]
                        ) / (upper - lower)
            remaining.difference_update(front)
            front_index += 1
        self.pareto_rankings = min(
            (1 << 63) - 1, self.pareto_rankings + 1)
        self.pareto_frontier_rules = min(
            (1 << 63) - 1,
            self.pareto_frontier_rules + frontier_size,
        )
        return sorted(
            candidates,
            key=lambda rule: (
                rank[rule.rule_id],
                -crowding[rule.rule_id],
                rule.rule_id,
            ),
        )

    @staticmethod
    def _valid_digest(value: str) -> bool:
        return (
            len(value) == 64 and
            all(character in "0123456789abcdef" for character in value)
        )

    @classmethod
    def _grammar_bitmap_feature(
        cls,
        rule_id: str,
        context_id: str,
        production_id: str,
    ) -> tuple[int, str]:
        payload = b"\0".join((
            b"symcc-grammar-coverage-v1",
            rule_id.encode("ascii"),
            context_id.encode("ascii"),
            production_id.encode("ascii"),
        ))
        digest = hashlib.sha256(payload).hexdigest()
        return int(digest[:4], 16) % cls.GRAMMAR_BITMAP_SIZE, digest

    def _grammar_bitmap_count(
        self,
        rule_id: str,
        context_id: str,
        production_id: str,
    ) -> int:
        index, _digest = self._grammar_bitmap_feature(
            rule_id, context_id, production_id)
        return self.grammar_bitmap.get(index, 0)

    def _observe_grammar_bitmap(
        self,
        rule_id: str,
        context_id: str,
        production_id: str,
    ) -> None:
        index, owner = self._grammar_bitmap_feature(
            rule_id, context_id, production_id)
        previous_owner = self.grammar_bitmap_owners.get(index)
        if previous_owner is not None and previous_owner != owner:
            self.grammar_bitmap_collisions += 1
        else:
            self.grammar_bitmap_owners[index] = owner
        self.grammar_bitmap[index] = min(
            255, self.grammar_bitmap.get(index, 0) + 1)
        self.grammar_bitmap_events += 1

    def _inverse_candidates(
        self,
        content: bytes,
        core: ConstraintCore,
    ) -> Iterable[tuple[str, bytes]]:
        fragment = content[core.lo:core.hi + 1]
        if not fragment:
            return
        width = min(8, len(fragment))
        head = fragment[:width]
        yield "bitwise", self._patch(
            content, core.lo, bytes(value ^ 0xFF for value in head))
        for byteorder in ("little", "big"):
            value = int.from_bytes(head, byteorder)
            modulus = 1 << (8 * width)
            for delta, label in ((1, "increment"), (-1, "decrement")):
                replacement = ((value + delta) % modulus).to_bytes(
                    width, byteorder)
                yield f"{label}-{byteorder}", self._patch(
                    content, core.lo, replacement)
        for token in self.tokens[:8]:
            yield "dictionary", self._patch(content, core.lo, token)

    def _surrogate_candidates(
        self,
        content: bytes,
        cores: tuple[ConstraintCore, ...],
        telemetry: SolverTelemetry,
    ) -> Iterable[tuple[int, str, bytes]]:
        if not cores:
            return
        for feature, matched, width in telemetry.data_features[:32]:
            exemplar = self.exemplars.get(int(feature))
            if exemplar is None or exemplar.matched <= matched:
                continue
            core = cores[0]
            candidate = self._patch(content, core.lo, exemplar.fragment)
            yield core.branch, f"feature-{feature:x}", candidate

    def _solve_complete_candidates(
        self,
        content: bytes,
        cores: tuple[ConstraintCore, ...],
        spans: tuple[TokenSpan, ...],
    ) -> Iterable[
        tuple[int, str, bytes, str, str, str, tuple[int, int]]
    ]:
        if not cores or not spans or not self.grammar_rules:
            return
        rules = tuple(self.grammar_rules.values())
        for span in spans[:8]:
            source_context_id = _grammar_context_id(content, span)
            contextual_rules = self._pareto_rank_rules(
                (
                    rule for rule in rules
                    if self._rule_matches_source_shape(
                        rule, source_context_id, span.token)
                ),
                source_context_id,
            )
            for rule in contextual_rules:
                context_id = self._resolved_parser_context(
                    rule.rule_id, source_context_id)
                if rule.context_rejected(context_id):
                    continue
                for rendered, depth in self._rule_renderings(
                        rule, span.token):
                    candidate = self._splice(
                        content, span.lo, span.hi, rendered)
                    if (
                        candidate == content or
                        len(candidate) > self.max_input_bytes
                    ):
                        continue
                    yield (
                        span.branch,
                        (
                            f"grammar-{rule.kind}-d{depth}"
                            if rule.kind == "recursive" else
                            f"grammar-{rule.kind}"
                        ),
                        candidate,
                        rule.rule_id,
                        context_id,
                        source_context_id,
                        (span.lo, span.lo + len(rendered)),
                    )

    def _load_query_candidate_manifest(
        self,
        source_path: str,
        content: bytes,
    ) -> tuple[str, tuple[int, ...], str] | None:
        if self.query_store is None:
            return None
        directory = os.path.dirname(source_path)
        basename = os.path.basename(source_path)
        stem, _extension = os.path.splitext(source_path)
        candidates = (
            f"{stem}.json",
            os.path.join(directory, f".{basename}.query.json"),
            f"{source_path}.query.json",
        )
        manifest: dict[str, Any] | None = None
        manifest_path = ""
        for path in candidates:
            try:
                with open(path, encoding="ascii") as stream:
                    raw = json.load(stream)
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(raw, dict):
                manifest = raw
                manifest_path = path
                break
        if (
            manifest is None or
            manifest.get("schema") != "symcc-query-candidate-v1" or
            not bool(manifest.get("query_ir_verified", False)) or
            manifest.get("candidate_sha256") !=
            hashlib.sha256(content).hexdigest()
        ):
            return None
        query_id = str(manifest.get("query_id", "")).lower()
        if (
            len(query_id) != 64 or
            any(character not in "0123456789abcdef"
                for character in query_id)
        ):
            return None
        raw_offsets = manifest.get("query_input_offsets", ())
        if (
            not isinstance(raw_offsets, list) or
            len(raw_offsets) > 65536 or
            any(
                isinstance(offset, bool) or not isinstance(offset, int) or
                offset < 0 or offset >= len(content)
                for offset in raw_offsets
            )
        ):
            return None
        offsets = tuple(sorted(set(raw_offsets)))
        if offsets != self.query_store.query_input_offsets(query_id):
            return None
        if not self.query_store.validate_candidate(query_id, content):
            return None
        try:
            with open(manifest_path, "rb") as stream:
                manifest_digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            return None
        return query_id, offsets, manifest_digest

    def _query_hole_candidates(
        self,
        source_path: str,
        content: bytes,
        spans: tuple[TokenSpan, ...],
    ) -> Iterable[
        tuple[int, str, bytes, str, str, dict[str, Any]]
    ]:
        loaded = self._load_query_candidate_manifest(source_path, content)
        if loaded is None or not spans or not self.grammar_rules:
            return
        query_id, constrained_offsets, manifest_digest = loaded
        constrained = set(constrained_offsets)
        rules = tuple(self.grammar_rules.values())
        emitted = 0
        for span in spans[:16]:
            if any(offset in constrained for offset in range(span.lo, span.hi)):
                continue
            source_context_id = _grammar_context_id(content, span)
            contextual_rules = self._pareto_rank_rules(
                (
                    rule for rule in rules
                    if self._rule_matches_source_shape(
                        rule, source_context_id, span.token)
                ),
                source_context_id,
            )
            for rule in contextual_rules:
                context_id = self._resolved_parser_context(
                    rule.rule_id, source_context_id)
                if rule.context_rejected(context_id):
                    continue
                for rendered, depth in self._rule_renderings(
                        rule, span.token):
                    candidate = self._splice(
                        content, span.lo, span.hi, rendered)
                    if (
                        candidate == content or
                        len(candidate) > self.max_input_bytes
                    ):
                        continue
                    self.query_hole_attempts += 1
                    if not self.query_store.validate_candidate(
                            query_id, candidate):
                        self.query_hole_rejected += 1
                        continue
                    self.query_hole_verified += 1
                    certificate = {
                        "schema": "symcc-query-grammar-hole-v1",
                        "query_id": query_id,
                        "source_candidate_sha256": hashlib.sha256(
                            content).hexdigest(),
                        "source_manifest_sha256": manifest_digest,
                        "query_input_offsets": list(constrained_offsets),
                        "hole": [span.lo, span.hi],
                        "candidate_span": [span.lo, span.lo + len(rendered)],
                        "grammar_rule_id": rule.rule_id,
                        "grammar_context_id": context_id,
                        "grammar_source_context_id": source_context_id,
                        "query_ir_verified": True,
                        "target_replay_required": True,
                        "coverage_retention_required": True,
                    }
                    yield (
                        span.branch,
                        (
                            f"query-hole-{rule.kind}-d{depth}"
                            if rule.kind == "recursive" else
                            f"query-hole-{rule.kind}"
                        ),
                        candidate,
                        rule.rule_id,
                        context_id,
                        certificate,
                    )
                    emitted += 1
                    if emitted >= 16:
                        return

    @staticmethod
    def _history_tokens(content: bytes) -> tuple[bytes, ...]:
        tokens: list[bytes] = []
        seen: set[bytes] = set()
        start = 0
        for index in range(len(content) + 1):
            boundary = (
                index == len(content) or
                content[index] in _TOKEN_DELIMITERS
            )
            if not boundary:
                continue
            token = content[start:index]
            if 0 < len(token) <= 128 and token not in seen:
                seen.add(token)
                tokens.append(token)
            start = index + 1
        for index, byte in enumerate(content):
            closing = _BRACKETS.get(byte)
            if closing is None:
                continue
            end = content.find(bytes((closing,)), index + 1)
            if end < 0 or end - index + 1 > 128:
                continue
            token = content[index:end + 1]
            if token not in seen:
                seen.add(token)
                tokens.append(token)
        return tuple(tokens[:256])

    def observe_history_seed(
        self,
        path: str,
        coverage_features: int,
    ) -> str:
        features = max(0, int(coverage_features))
        if features <= 0:
            return ""
        try:
            with open(path, "rb") as stream:
                content = stream.read(self.max_history_seed_bytes + 1)
        except OSError:
            return ""
        if not content or len(content) > self.max_history_seed_bytes:
            return ""
        seed_id = hashlib.sha256(content).hexdigest()
        seed = self.history_seeds.get(seed_id)
        if seed is None:
            if len(self.history_seeds) >= self.max_history_seeds:
                victim = min(
                    self.history_seeds.values(),
                    key=lambda item: (
                        item.retained > 0,
                        item.score,
                        item.coverage_features,
                        item.seed_id,
                    ),
                )
                self.history_seeds.pop(victim.seed_id, None)
            seed = HistorySeed(seed_id, content)
            self.history_seeds[seed_id] = seed
        seed.coverage_features += features
        seed.retained += 1
        self.save()
        return seed_id

    def observe_history_validation(
        self,
        seed_id: str,
        *,
        valid: bool,
    ) -> bool:
        seed = self.history_seeds.get(str(seed_id))
        if seed is None:
            return False
        seed.validations += 1
        if valid:
            seed.verified += 1
            self.history_verified += 1
        if valid or seed.validations % 8 == 0:
            self.save()
        return True

    def observe_history_retention(
        self,
        seed_id: str,
        coverage_features: int,
    ) -> bool:
        seed = self.history_seeds.get(str(seed_id))
        if seed is None:
            return False
        features = max(0, int(coverage_features))
        if features:
            seed.retained += 1
            seed.coverage_features += features
            self.history_retained += 1
            self.save()
        return True

    def _history_acquisition_candidates(
        self,
        content: bytes,
        spans: tuple[TokenSpan, ...],
    ) -> Iterable[tuple[int, str, bytes, str]]:
        if (
            self.plateau_count < self.plateau_observations or
            not spans or not self.history_seeds
        ):
            return
        seeds = sorted(
            self.history_seeds.values(),
            key=lambda item: (item.score, item.seed_id),
            reverse=True,
        )[:32]
        emitted = 0
        for seed in seeds:
            for token in self._history_tokens(seed.content):
                for span in spans[:8]:
                    if token == span.token:
                        continue
                    candidate = self._splice(
                        content, span.lo, span.hi, token)
                    if (
                        candidate == content or
                        len(candidate) > self.max_input_bytes
                    ):
                        continue
                    yield (
                        span.branch,
                        "retained-history-token",
                        candidate,
                        seed.seed_id,
                    )
                    emitted += 1
                    if emitted >= 32:
                        return

    def _targeted_transform_candidates(
        self,
        content: bytes,
        telemetry: SolverTelemetry,
    ) -> Iterable[tuple[int, str, bytes]]:
        slices = ifss_relevance_slices(
            telemetry, len(content),
            target_branch=int(telemetry.target_branch or 0),
            max_slices=8)
        cores = tuple(item.core for item in slices)
        if not cores:
            return

        fragments = {
            core: content[core.lo:core.hi + 1]
            for core in cores
            if core.span > 0
        }

        for dst in cores[:6]:
            dst_fragment = fragments.get(dst, b"")
            if not dst_fragment:
                continue
            for src in cores[:6]:
                if src == dst or src.span != dst.span:
                    continue
                src_fragment = fragments.get(src, b"")
                if src_fragment and src_fragment != dst_fragment:
                    yield dst.branch, "hydra-copy-core", self._patch(
                        content, dst.lo, src_fragment)

        for left_index, left in enumerate(cores[:6]):
            left_fragment = fragments.get(left, b"")
            if not left_fragment:
                continue
            for right in cores[left_index + 1:6]:
                if right.span != left.span:
                    continue
                right_fragment = fragments.get(right, b"")
                if not right_fragment or right_fragment == left_fragment:
                    continue
                candidate = bytearray(content)
                candidate[left.lo:left.hi + 1] = right_fragment
                candidate[right.lo:right.hi + 1] = left_fragment
                target = (
                    left.branch
                    if left.interesting or left.branch == telemetry.target_branch
                    else right.branch
                )
                yield target, "hydra-swap-cores", bytes(candidate)

        for core in cores[:8]:
            positions = (
                (core.lo,) if core.lo == core.hi else (core.lo, core.hi))
            for position in positions:
                current = content[position]
                for label, value in (
                        ("hydra-boundary-zero", 0x00),
                        ("hydra-boundary-ff", 0xFF),
                        ("hydra-boundary-highbit", current ^ 0x80),
                        ("hydra-boundary-step", (current + 1) & 0xFF)):
                    if value != current:
                        yield core.branch, label, self._patch(
                            content, position, bytes((value,)))

        for core in cores[:4]:
            truncate_at = core.hi + 1
            if core.interesting and truncate_at < len(content):
                yield (
                    core.branch, "hydra-truncate-after-core",
                    content[:truncate_at])

    @staticmethod
    def _heap_partition_candidates(
        content: bytes,
    ) -> Iterable[tuple[str, bytes]]:
        if not content.startswith(MAGIC):
            return
        try:
            entries = canonicalize(parse_seed(content))
        except ValueError:
            return
        objects: dict[int, list[SeedEntry]] = {}
        payloads: dict[int, tuple[int, bytes]] = {}
        for entry in entries:
            if not (entry.flags & OBJECT_ENTRY):
                continue
            objects.setdefault(entry.object_id, []).append(entry)
            if entry.data:
                payloads[entry.object_id] = (entry.lower, entry.data)

        for object_id, group in sorted(objects.items()):
            if len(group) < 2:
                continue
            selected = sorted(group, key=lambda item: item.path)[-1]
            lower, data = payloads.get(object_id, (selected.lower, selected.data))
            next_id = max(objects, default=0) + 1
            split = [
                entry for entry in entries
                if not (
                    entry.flags & OBJECT_ENTRY
                    and entry.object_id == object_id
                    and entry.path == selected.path)
            ]
            split.append(SeedEntry(
                OBJECT_ENTRY, next_id, lower, selected.path, data))
            try:
                yield "split-alias-class", serialize_seed(canonicalize(split))
            except ValueError:
                pass
            break

        object_ids = sorted(objects)
        for left_index, left in enumerate(object_ids):
            left_payload = payloads.get(left)
            if left_payload is None:
                continue
            for right in object_ids[left_index + 1:]:
                right_payload = payloads.get(right)
                if right_payload is None or (
                        len(left_payload[1]) != len(right_payload[1])):
                    continue
                merged: list[SeedEntry] = []
                for entry in entries:
                    if not (entry.flags & OBJECT_ENTRY):
                        merged.append(entry)
                    elif entry.object_id == right:
                        merged.append(SeedEntry(
                            entry.flags, left, 0, entry.path, b""))
                    else:
                        merged.append(entry)
                try:
                    yield "merge-compatible-classes", serialize_seed(
                        canonicalize(merged))
                except ValueError:
                    pass
                return

    def _update_exemplars(
        self,
        content: bytes,
        cores: tuple[ConstraintCore, ...],
        telemetry: SolverTelemetry,
    ) -> None:
        if not cores:
            return
        core = cores[0]
        fragment = content[core.lo:core.hi + 1][:64]
        if not fragment:
            return
        if fragment not in self.grammar_fragments:
            self.grammar_fragments.append(fragment)
            if len(self.grammar_fragments) > 512:
                del self.grammar_fragments[:-512]
        for feature, matched, width in telemetry.data_features[:64]:
            current = self.exemplars.get(int(feature))
            if current is None or int(matched) > current.matched:
                self.exemplars[int(feature)] = FeatureExemplar(
                    int(matched), int(width), fragment)
        if len(self.exemplars) > 8192:
            self.exemplars = dict(list(self.exemplars.items())[-8192:])

    def propose(
        self,
        source_path: str,
        telemetry: SolverTelemetry,
        *,
        coverage_delta: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            with open(source_path, "rb") as stream:
                content = stream.read(self.max_input_bytes + 1)
        except OSError:
            return []
        if len(content) > self.max_input_bytes:
            return []
        if coverage_delta is not None:
            if int(coverage_delta) > 0:
                self.plateau_count = 0
            else:
                self.plateau_count = min(
                    self.plateau_observations * 4,
                    self.plateau_count + 1,
                )
        cores = extract_constraint_cores(telemetry, len(content))
        spans = self._learn_online_grammar(content, cores)
        source_tokens_by_context: dict[str, set[bytes]] = {}
        for source_span in spans:
            source_tokens_by_context.setdefault(
                _grammar_context_id(content, source_span), set()).add(
                    source_span.token)
        candidates: list[tuple[str, int, str, bytes, str]] = []
        candidate_metadata: dict[tuple[bytes, str], dict[str, Any]] = {}
        query_hole_budget = min(
            self.max_query_holes,
            max(1, self.max_proposals // 4),
        )
        query_hole_count = 0
        if query_hole_budget:
            for (
                branch,
                label,
                candidate,
                rule_id,
                context_id,
                certificate,
            ) in (
                    self._query_hole_candidates(
                        source_path, content, spans)):
                candidates.append((
                    "query_hole_completion",
                    branch,
                    label,
                    candidate,
                    rule_id,
                ))
                candidate_metadata[(candidate, rule_id)] = {
                    "query_hole_certificate": certificate,
                    "grammar_context_id": context_id,
                    "grammar_source_context_id": str(
                        certificate["grammar_source_context_id"]),
                    "grammar_span": list(certificate["candidate_span"]),
                }
                query_hole_count += 1
                if (
                    len(candidates) >= self.max_proposals or
                    query_hole_count >= query_hole_budget
                ):
                    break
        history_budget = min(
            16, max(1, self.max_proposals // 4))
        history_count = 0
        for branch, label, candidate, seed_id in (
                self._history_acquisition_candidates(content, spans)):
            candidates.append((
                "history_acquisition",
                branch,
                label,
                candidate,
                "",
            ))
            candidate_metadata[(candidate, "")] = {
                "history_seed_id": seed_id,
            }
            history_count += 1
            if (
                len(candidates) >= self.max_proposals or
                history_count >= history_budget
            ):
                break
        for core in cores:
            for label, candidate in self._inverse_candidates(content, core):
                candidates.append(
                    ("inverse", core.branch, label, candidate, ""))
                if len(candidates) >= self.max_proposals:
                    break
            if len(candidates) >= self.max_proposals:
                break
        if len(candidates) < self.max_proposals:
            for branch, label, candidate in self._surrogate_candidates(
                    content, cores, telemetry):
                candidates.append(
                    ("surrogate", branch, label, candidate, ""))
                if len(candidates) >= self.max_proposals:
                    break
        if len(candidates) < self.max_proposals:
            targeted_budget = min(
                self.max_proposals - len(candidates),
                max(1, min(8, self.max_proposals // 4)))
            targeted_count = 0
            for branch, label, candidate in self._targeted_transform_candidates(
                    content, telemetry):
                candidates.append(
                    ("targeted_transform", branch, label, candidate, ""))
                targeted_count += 1
                if (len(candidates) >= self.max_proposals
                        or targeted_count >= targeted_budget):
                    break
        if len(candidates) < self.max_proposals:
            for (
                branch,
                label,
                candidate,
                rule_id,
                context_id,
                source_context_id,
                grammar_span,
            ) in (
                    self._solve_complete_candidates(content, cores, spans)):
                candidates.append((
                    "solve_complete", branch, label, candidate, rule_id))
                candidate_metadata.setdefault(
                    (candidate, rule_id), {}
                ).setdefault("grammar_context_id", context_id)
                candidate_metadata[(candidate, rule_id)].setdefault(
                    "grammar_source_context_id", source_context_id)
                candidate_metadata[(candidate, rule_id)].setdefault(
                    "grammar_span", list(grammar_span))
                matched_shapes = (
                    self.subtree_rule_shapes.get(rule_id, set()) &
                    self.parser_shape_aliases.get(source_context_id, set())
                )
                if matched_shapes:
                    shape_id = min(matched_shapes)
                    rule = self.grammar_rules.get(rule_id)
                    if rule is not None and rule.kind == "synchronized":
                        transactions = sorted(
                            transaction_id
                            for transaction_id in
                            self.synchronized_rule_transactions.get(
                                rule_id, ())
                            if self.sync_transactions.get(
                                transaction_id, {}).get(
                                    "parent_shape_id") in matched_shapes and
                            self.ect_instances.get(str(
                                self.sync_transactions.get(
                                    transaction_id, {}).get(
                                        "parent_instance_id", "")), {}).get(
                                            "yield_hex") in {
                                                token.hex()
                                                for token in
                                                source_tokens_by_context.get(
                                                    source_context_id, ())
                                            }
                        )
                        if transactions:
                            candidate_metadata[
                                (candidate, rule_id)].setdefault(
                                    "grammar_sync_transaction_id",
                                    transactions[0],
                                )
                    else:
                        candidate_metadata[(candidate, rule_id)].setdefault(
                            "grammar_ect_shape_id", shape_id)
                        matching_instances = sorted(
                            instance_id
                            for instance_id in
                            self.subtree_rule_instances.get(
                                rule_id, set())
                            if self.ect_instances.get(
                                instance_id, {}).get("shape_id") == shape_id
                        )
                        if matching_instances:
                            candidate_metadata[
                                (candidate, rule_id)].setdefault(
                                    "grammar_ect_instance_id",
                                    matching_instances[0],
                                )
                if len(candidates) >= self.max_proposals:
                    break
        if len(candidates) < self.max_proposals:
            heap_target = (
                telemetry.target_branch
                or (cores[0].branch if cores else 0))
            for label, candidate in self._heap_partition_candidates(content):
                candidates.append(
                    ("heap_partition", heap_target, label, candidate, ""))
                if len(candidates) >= self.max_proposals:
                    break

        proposals: list[dict[str, Any]] = []
        seen = {content}
        for kind, target, label, candidate, rule_id in candidates:
            if candidate in seen or len(candidate) > self.max_input_bytes:
                continue
            seen.add(candidate)
            proposal = {
                "kind": kind,
                "source_path": source_path,
                "target_branch": int(target),
                "candidate": {"hex": candidate.hex()},
                "generator": label,
            }
            if rule_id:
                proposal["grammar_rule_id"] = rule_id
                rule = self.grammar_rules.get(rule_id)
                if rule is not None:
                    rule.attempts += 1
                    rule.last_attempt_observation = min(
                        (1 << 63) - 1, self.observations + 1)
            proposal.update(
                candidate_metadata.get((candidate, rule_id), {}))
            history_seed_id = str(
                proposal.get("history_seed_id", ""))
            if history_seed_id:
                seed = self.history_seeds.get(history_seed_id)
                if seed is not None:
                    seed.attempts += 1
                    self.history_attempts += 1
            proposals.append(proposal)
        self._update_exemplars(content, cores, telemetry)
        self.observations += 1
        return proposals[:self.max_proposals]

    def generate_into(
        self,
        manager: VerifiedProposalManager,
        source_path: str,
        telemetry: SolverTelemetry,
        *,
        coverage_delta: int | None = None,
    ) -> int:
        if (len(manager.records) >= self.max_records
                or manager.for_path(source_path) is not None):
            return 0
        added = 0
        for proposal in self.propose(
                source_path, telemetry, coverage_delta=coverage_delta):
            before = len(manager.records)
            manager.ingest(proposal)
            added += len(manager.records) - before
        self.generated += added
        if self.autosave and (added or self.observations % 16 == 0):
            self.save()
        return added

    @staticmethod
    def _cfg_label(value: Any, *, allow_empty: bool = False) -> str | None:
        if (
            not isinstance(value, str) or
            not (0 if allow_empty else 1) <= len(value) <= 128 or
            not value.isascii() or
            any(ord(character) < 32 or ord(character) == 127
                for character in value)
        ):
            return None
        return value

    def _drop_ect_instance(
        self,
        instance_id: str,
        *,
        refresh_derived: bool = True,
    ) -> None:
        instance = self.ect_instances.pop(instance_id, None)
        if instance is None:
            return
        shape_id = str(instance.get("shape_id", ""))
        if shape_id in self.ect_shapes:
            self.ect_shapes[shape_id].discard(instance_id)
            if not self.ect_shapes[shape_id]:
                del self.ect_shapes[shape_id]
                if shape_id not in self.nullable_shape_ids:
                    for rule_id in tuple(self.subtree_rule_shapes):
                        self.subtree_rule_shapes[rule_id].discard(shape_id)
                        if not self.subtree_rule_shapes[rule_id]:
                            del self.subtree_rule_shapes[rule_id]
                    for context_id in tuple(self.parser_shape_aliases):
                        self.parser_shape_aliases[context_id].discard(shape_id)
                        if not self.parser_shape_aliases[context_id]:
                            del self.parser_shape_aliases[context_id]
        for rule_id in tuple(self.subtree_rule_instances):
            self.subtree_rule_instances[rule_id].discard(instance_id)
            if not self.subtree_rule_instances[rule_id]:
                del self.subtree_rule_instances[rule_id]
                self.subtree_rule_shapes.pop(rule_id, None)
                continue
            remaining_shapes = {
                str(self.ect_instances[remaining].get("shape_id", ""))
                for remaining in self.subtree_rule_instances[rule_id]
                if remaining in self.ect_instances
            }
            if rule_id in self.subtree_rule_shapes:
                self.subtree_rule_shapes[rule_id].intersection_update(
                    remaining_shapes)
                if not self.subtree_rule_shapes[rule_id]:
                    del self.subtree_rule_shapes[rule_id]
        if refresh_derived:
            if self.nullable_rules:
                self._refresh_nullable_state()
            if self.sync_transactions:
                self._refresh_synchronized_state()
            if self.pcfg_families or self.packed_inside:
                self._refresh_probabilistic_state()

    def _drop_ect_shape(self, shape_id: str) -> None:
        for instance_id in tuple(self.ect_shapes.get(shape_id, ())):
            self._drop_ect_instance(
                instance_id, refresh_derived=False)
        if self.nullable_rules:
            self._refresh_nullable_state()
        if self.sync_transactions:
            self._refresh_synchronized_state()
        if self.pcfg_families or self.packed_inside:
            self._refresh_probabilistic_state()

    def _drop_packed_node(self, node_id: str) -> None:
        self.packed_nodes.pop(node_id, None)
        self.packed_root_ids.discard(node_id)
        self.packed_root_evidence.pop(node_id, None)
        for edge_id, edge in tuple(self.packed_edges.items()):
            if node_id in {edge.get("parent_id"), edge.get("child_id")}:
                self.packed_edges.pop(edge_id, None)
        for instance_id, instance in tuple(self.ect_instances.items()):
            if (
                instance.get("node_id") == node_id or
                node_id in instance.get("node_path", ())
            ):
                self._drop_ect_instance(
                    instance_id, refresh_derived=False)
        if self.nullable_rules:
            self._refresh_nullable_state()
        if self.sync_transactions:
            self._refresh_synchronized_state()
        if self.pcfg_families or self.packed_inside:
            self._refresh_probabilistic_state()

    def _drop_packed_edge(self, edge_id: str) -> None:
        edge = self.packed_edges.pop(edge_id, None)
        if edge is None:
            return
        for instance_id, instance in tuple(self.ect_instances.items()):
            if edge_id in instance.get("child_edge_ids", ()):
                self._drop_ect_instance(
                    instance_id, refresh_derived=False)
        remaining_pairs = {
            (item.get("parent_id"), item.get("child_id"))
            for item in self.packed_edges.values()
        }
        pair = (edge.get("parent_id"), edge.get("child_id"))
        if pair in remaining_pairs:
            if self.sync_transactions:
                self._refresh_synchronized_state()
            if self.pcfg_families or self.packed_inside:
                self._refresh_probabilistic_state()
            return
        for instance_id, instance in tuple(self.ect_instances.items()):
            node_path = instance.get("node_path", ())
            if pair in set(zip(node_path, node_path[1:])):
                self._drop_ect_instance(
                    instance_id, refresh_derived=False)
        if self.nullable_rules:
            self._refresh_nullable_state()
        if self.sync_transactions:
            self._refresh_synchronized_state()
        if self.pcfg_families or self.packed_inside:
            self._refresh_probabilistic_state()

    def _refresh_nullable_state(self) -> None:
        self.nullable_proofs.clear()
        self.nullable_sccs.clear()
        nullable_by_parser: dict[str, set[tuple[str, str]]] = {}
        rules_by_parser: dict[str, list[dict[str, Any]]] = {}
        for rule in self.nullable_rules.values():
            rules_by_parser.setdefault(
                str(rule["parser"]), []).append(rule)
        for parser, rules in rules_by_parser.items():
            certificate = VerifiedProposalManager._nullable_certificate(
                parser, rules)
            if certificate is None:
                continue
            for proof in certificate["nullable_proofs"]:
                proof_core = {
                    "schema": "symcc-parser-nullable-proof-v1",
                    "parser": parser,
                    **proof,
                }
                proof_id = hashlib.sha256(json.dumps(
                    proof_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                self.nullable_proofs[proof_id] = {
                    "proof_id": proof_id,
                    "parser": parser,
                    **proof,
                }
                nullable_by_parser.setdefault(parser, set()).add(
                    (proof["symbol"], proof["state"]))
            for scc in certificate["nullable_sccs"]:
                self.nullable_sccs[scc["scc_id"]] = {
                    "parser": parser,
                    **scc,
                }
        self.nullable_shape_ids = {
            str(production["shape_id"])
            for production in self.cfg_productions.values()
            if (
                (
                    str(production["lhs"]),
                    str(production["state"]),
                ) in nullable_by_parser.get(
                    str(production["parser"]), set())
            )
        }
        epsilon_id = _grammar_rule_id("epsilon", b"", b"", b"")
        instance_shapes = {
            str(self.ect_instances[instance_id]["shape_id"])
            for instance_id in self.subtree_rule_instances.get(
                epsilon_id, set())
            if (
                instance_id in self.ect_instances and
                self.cfg_productions.get(
                    str(self.ect_instances[instance_id][
                        "production_id"]), {}).get("epsilon") is True
            )
        }
        combined_shapes = instance_shapes | self.nullable_shape_ids
        if combined_shapes:
            epsilon_rule = self.grammar_rules.get(epsilon_id)
            if epsilon_rule is None:
                epsilon_rule = self._learn_rule(
                    "epsilon", b"", b"")
            if epsilon_rule is not None:
                self.subtree_rule_shapes[
                    epsilon_rule.rule_id] = combined_shapes
        elif epsilon_id in self.subtree_rule_shapes:
            self.subtree_rule_shapes.pop(epsilon_id, None)

    @staticmethod
    def _instance_alternative(
        instance: dict[str, Any],
        production: dict[str, Any],
    ) -> int:
        return int(instance.get(
            "alternative", production.get("alternative", 0)))

    def _synchronized_parent_components(
        self,
        parent: dict[str, Any],
        primary_instances_by_node: dict[
            str, list[dict[str, Any]]
        ],
    ) -> dict[str, Any] | None:
        parent_node_id = str(parent.get("node_id", ""))
        production = self.cfg_productions.get(
            str(parent.get("production_id", "")))
        if (
            not parent_node_id or production is None or
            production.get("epsilon") is True or
            self._instance_alternative(parent, production) != 0 or
            not 2 <= len(production.get("rhs", ())) <= 8
        ):
            return None
        arity = len(production["rhs"])
        child_edge_ids = parent.get("child_edge_ids")
        if isinstance(child_edge_ids, list):
            edges = [
                self.packed_edges.get(str(edge_id))
                for edge_id in child_edge_ids
            ]
            if any(edge is None for edge in edges):
                return None
            edges.sort(key=lambda edge: (
                int(edge.get("slot", -1)),
                str(edge.get("edge_id", "")),
            ))
        else:
            # Schema <=15 did not bind the concrete edge sequence to an
            # instance. Retain the old exact alternative-zero fallback only
            # for migration.
            edges = sorted(
                (
                    edge for edge in self.packed_edges.values()
                    if (
                        edge.get("parent_id") == parent_node_id and
                        int(edge.get("alternative", -1)) == 0
                    )
                ),
                key=lambda edge: (
                    int(edge.get("slot", -1)),
                    str(edge.get("edge_id", "")),
                ),
            )
        if (
            len(edges) != arity or
            [int(edge.get("slot", -1)) for edge in edges] !=
            list(range(arity)) or
            any(
                edge.get("parent_id") != parent_node_id or
                int(edge.get("alternative", -1)) != 0
                for edge in edges
            )
        ):
            return None
        try:
            gaps = [
                bytes.fromhex(str(gap))
                for gap in parent.get("terminal_gap_hex", ())
            ]
            parent_yield = bytes.fromhex(
                str(parent.get("yield_hex", "")))
        except ValueError:
            return None
        if len(gaps) != arity + 1:
            return None

        source_instances: list[dict[str, Any]] = []
        for slot, edge in enumerate(edges):
            expected = production["rhs"][slot]
            candidates = primary_instances_by_node.get(
                str(edge["child_id"]), ())
            source = next(
                (
                    candidate for candidate in candidates
                    if (
                        self.cfg_productions.get(
                            str(candidate["production_id"]), {}).get(
                                "parser") == production["parser"] and
                        self.packed_nodes.get(
                            str(candidate["node_id"]), {}).get(
                                "symbol") == expected["symbol"] and
                        self.packed_nodes.get(
                            str(candidate["node_id"]), {}).get(
                                "state") == expected["state"]
                    )
                ),
                None,
            )
            if source is None:
                return None
            source_instances.append(source)
        try:
            source_yields = [
                bytes.fromhex(str(instance["yield_hex"]))
                for instance in source_instances
            ]
        except ValueError:
            return None
        reconstructed_source = gaps[0] + b"".join(
            source_yields[slot] + gaps[slot + 1]
            for slot in range(arity)
        )
        if reconstructed_source != parent_yield:
            return None
        return {
            "parent": parent,
            "production": production,
            "edges": edges,
            "gaps": gaps,
            "source_instances": source_instances,
            "source_yields": source_yields,
        }

    @staticmethod
    def _ascii_uint(value: bytes) -> int | None:
        if (
            not 1 <= len(value) <= 20 or
            any(byte < ord("0") or byte > ord("9") for byte in value)
        ):
            return None
        parsed = int(value)
        return parsed if parsed <= (1 << 64) - 1 else None

    @classmethod
    def _slot_relation_holds(
        cls,
        kind: str,
        left: bytes,
        right: bytes,
    ) -> bool:
        left_uint = cls._ascii_uint(left)
        right_uint = cls._ascii_uint(right)
        if kind == "bytes_equal":
            return left == right
        if kind == "ascii_uint_equal":
            return (
                left_uint is not None and
                right_uint is not None and
                left_uint == right_uint
            )
        if kind == "left_uint_is_right_length":
            return left_uint is not None and left_uint == len(right)
        if kind == "right_uint_is_left_length":
            return right_uint is not None and right_uint == len(left)
        if kind == "length_equal":
            return len(left) == len(right)
        return False

    def _refresh_slot_relations(
        self,
        parent_components: list[dict[str, Any]],
    ) -> None:
        self.slot_relations.clear()
        self.parent_shape_relations.clear()
        observations_by_shape: dict[
            str, list[dict[str, Any]]
        ] = {}
        for components in parent_components:
            parent = components["parent"]
            observations_by_shape.setdefault(
                str(parent["shape_id"]), []).append(components)
        relation_kinds = (
            "bytes_equal",
            "ascii_uint_equal",
            "left_uint_is_right_length",
            "right_uint_is_left_length",
            "length_equal",
        )
        for parent_shape_id, observations in sorted(
                observations_by_shape.items()):
            if len(observations) < 2:
                continue
            arity = len(observations[0]["source_yields"])
            parser = str(observations[0]["production"]["parser"])
            for left_slot, right_slot in itertools.combinations(
                    range(arity), 2):
                kind = next(
                    (
                        candidate_kind
                        for candidate_kind in relation_kinds
                        if all(self._slot_relation_holds(
                            candidate_kind,
                            components["source_yields"][left_slot],
                            components["source_yields"][right_slot],
                        ) for components in observations)
                    ),
                    "",
                )
                if not kind:
                    continue
                relation_core = {
                    "schema": "symcc-parser-slot-relation-v1",
                    "parser": parser,
                    "parent_shape_id": parent_shape_id,
                    "left_slot": left_slot,
                    "right_slot": right_slot,
                    "kind": kind,
                }
                relation_id = hashlib.sha256(json.dumps(
                    relation_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if (
                    relation_id not in self.slot_relations and
                    len(self.slot_relations) >=
                    self.HARD_MAX_SLOT_RELATIONS
                ):
                    return
                relation = {
                    "relation_id": relation_id,
                    **{
                        key: value
                        for key, value in relation_core.items()
                        if key != "schema"
                    },
                    "support": len(observations),
                    "violations": 0,
                    "evidence_parent_instance_ids": sorted(
                        str(components["parent"]["instance_id"])
                        for components in observations
                    )[:64],
                }
                self.slot_relations[relation_id] = relation
                self.parent_shape_relations.setdefault(
                    parent_shape_id, set()).add(relation_id)

    def _refresh_synchronized_state(self) -> None:
        previous_rule_ids = {
            rule_id
            for rule_id, rule in self.grammar_rules.items()
            if rule.kind == "synchronized"
        }
        for rule_id in previous_rule_ids:
            self.subtree_rule_shapes.pop(rule_id, None)
        self.sync_transactions.clear()
        self.synchronized_rule_transactions.clear()

        primary_instances_by_node: dict[str, list[dict[str, Any]]] = {}
        for instance in self.ect_instances.values():
            node_id = str(instance.get("node_id", ""))
            production = self.cfg_productions.get(
                str(instance.get("production_id", "")))
            if (
                not node_id or production is None or
                self._instance_alternative(instance, production) != 0
            ):
                continue
            primary_instances_by_node.setdefault(
                node_id, []).append(instance)
        for instances in primary_instances_by_node.values():
            instances.sort(key=lambda item: item["instance_id"])

        parent_components = [
            components
            for parent in sorted(
                self.ect_instances.values(),
                key=lambda item: item["instance_id"])
            if (components := self._synchronized_parent_components(
                parent, primary_instances_by_node)) is not None
        ]
        self._refresh_slot_relations(parent_components)

        instances_by_shape_yield: dict[
            str, dict[bytes, dict[str, Any]]
        ] = {}
        for instance in sorted(
                self.ect_instances.values(),
                key=lambda item: item["instance_id"]):
            try:
                subtree = bytes.fromhex(str(instance.get("yield_hex", "")))
            except ValueError:
                continue
            instances_by_shape_yield.setdefault(
                str(instance.get("shape_id", "")), {}).setdefault(
                    subtree, instance)
        observed_parent_yields = {
            shape_id: set(by_yield)
            for shape_id, by_yield in instances_by_shape_yield.items()
        }

        for components in parent_components:
            if len(self.sync_transactions) >= self.HARD_MAX_SYNC_TRANSACTIONS:
                break
            parent = components["parent"]
            production = components["production"]
            edges = components["edges"]
            gaps = components["gaps"]
            source_instances = components["source_instances"]
            source_yields = components["source_yields"]
            arity = len(source_instances)
            alternatives_by_slot: list[list[dict[str, Any]]] = []
            mutable_slots: list[int] = []
            for slot, source in enumerate(source_instances):
                source_yield = source_yields[slot]
                alternatives = {
                    yield_value: alternative
                    for yield_value, alternative in
                    instances_by_shape_yield.get(
                        str(source["shape_id"]), {}).items()
                    if yield_value != source_yield
                }
                ordered = [
                    alternatives[yield_value]
                    for yield_value in sorted(alternatives)[:2]
                ]
                alternatives_by_slot.append(ordered)
                if ordered:
                    mutable_slots.append(slot)
            if len(mutable_slots) < 2:
                continue

            relation_ids = sorted(
                self.parent_shape_relations.get(
                    str(parent["shape_id"]), ()))
            relation_candidates: list[dict[str, Any]] = []
            for changed_count in range(
                    2, min(4, len(mutable_slots)) + 1):
                for changed_slots in itertools.combinations(
                        mutable_slots, changed_count):
                    for selected_targets in itertools.product(*(
                            alternatives_by_slot[slot]
                            for slot in changed_slots)):
                        target_instances = list(source_instances)
                        for slot, target in zip(
                                changed_slots, selected_targets):
                            target_instances[slot] = target
                        try:
                            target_yields = [
                                bytes.fromhex(str(instance["yield_hex"]))
                                for instance in target_instances
                            ]
                        except ValueError:
                            continue
                        result = gaps[0] + b"".join(
                            target_yields[slot] + gaps[slot + 1]
                            for slot in range(arity)
                        )
                        if (
                            not result or len(result) > 256 or
                            result in observed_parent_yields.get(
                                str(parent["shape_id"]), set())
                        ):
                            continue
                        relation_evidence = []
                        for relation_id in relation_ids:
                            relation = self.slot_relations[relation_id]
                            satisfied = self._slot_relation_holds(
                                str(relation["kind"]),
                                target_yields[int(
                                    relation["left_slot"])],
                                target_yields[int(
                                    relation["right_slot"])],
                            )
                            relation_evidence.append({
                                "relation_id": relation_id,
                                "satisfied": satisfied,
                            })
                        relation_matches = sum(
                            evidence["satisfied"]
                            for evidence in relation_evidence)
                        relation_total = len(relation_evidence)
                        relation_candidates.append({
                            "changed_slots": changed_slots,
                            "target_instances": target_instances,
                            "target_yields": target_yields,
                            "result": result,
                            "relation_evidence": relation_evidence,
                            "relation_matches": relation_matches,
                            "relation_total": relation_total,
                            "relation_score": (
                                relation_matches / relation_total
                                if relation_total else 0.5
                            ),
                        })
                        if (
                            len(relation_candidates) >=
                            self.HARD_MAX_SYNC_CANDIDATES_PER_PARENT
                        ):
                            break
                    if (
                        len(relation_candidates) >=
                        self.HARD_MAX_SYNC_CANDIDATES_PER_PARENT
                    ):
                        break
                if (
                    len(relation_candidates) >=
                    self.HARD_MAX_SYNC_CANDIDATES_PER_PARENT
                ):
                    break

            ranked = sorted(
                relation_candidates,
                key=lambda candidate: (
                    -float(candidate["relation_score"]),
                    -int(candidate["relation_matches"]),
                    len(candidate["changed_slots"]),
                    candidate["result"],
                    tuple(
                        str(instance["instance_id"])
                        for instance in candidate["target_instances"]
                    ),
                ),
            )
            selected = ranked[:3]
            selected_ids = {id(candidate) for candidate in selected}
            exploratory = min(
                (
                    candidate for candidate in ranked
                    if (
                        id(candidate) not in selected_ids and
                        int(candidate["relation_total"]) > 0 and
                        int(candidate["relation_matches"]) <
                        int(candidate["relation_total"])
                    )
                ),
                key=lambda candidate: (
                    float(candidate["relation_score"]),
                    len(candidate["changed_slots"]),
                    candidate["result"],
                ),
                default=None,
            )
            if exploratory is not None:
                selected.append(exploratory)
                selected_ids.add(id(exploratory))
            for candidate in ranked:
                if len(selected) >= 4:
                    break
                if id(candidate) not in selected_ids:
                    selected.append(candidate)
                    selected_ids.add(id(candidate))

            for candidate in selected:
                if (
                    len(self.sync_transactions) >=
                    self.HARD_MAX_SYNC_TRANSACTIONS
                ):
                    break
                changed_slots = candidate["changed_slots"]
                target_instances = candidate["target_instances"]
                result = candidate["result"]
                assignments = [
                    {
                        "slot": slot,
                        "edge_id": str(edges[slot]["edge_id"]),
                        "shape_id":
                            str(source_instances[slot]["shape_id"]),
                        "source_instance_id": str(
                            source_instances[slot]["instance_id"]),
                        "target_instance_id": str(
                            target_instances[slot]["instance_id"]),
                        "changed": slot in changed_slots,
                    }
                    for slot in range(arity)
                ]
                transaction_core = {
                    "schema":
                        "symcc-parser-sync-transaction-v2",
                    "parser": str(production["parser"]),
                    "parent_shape_id": str(parent["shape_id"]),
                    "parent_production_id":
                        str(parent["production_id"]),
                    "parent_instance_id":
                        str(parent["instance_id"]),
                    "terminal_gap_hex": [
                        gap.hex() for gap in gaps],
                    "slots": assignments,
                    "slot_relations":
                        candidate["relation_evidence"],
                    "yield_sha256":
                        hashlib.sha256(result).hexdigest(),
                }
                transaction_id = hashlib.sha256(json.dumps(
                    transaction_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if transaction_id in self.sync_transactions:
                    continue
                rule_id = _grammar_rule_id(
                    "synchronized", b"", result, b"")
                rule = self.grammar_rules.get(rule_id)
                if rule is None:
                    rule = self._learn_rule(
                        "synchronized", b"", result)
                if rule is None:
                    continue
                transaction = {
                    "transaction_id": transaction_id,
                    **{
                        key: value
                        for key, value in transaction_core.items()
                        if key != "schema"
                    },
                    "yield_hex": result.hex(),
                    "rule_id": rule.rule_id,
                    "changed_slots": len(changed_slots),
                    "relation_score":
                        candidate["relation_score"],
                    "relation_matches":
                        candidate["relation_matches"],
                    "relation_total":
                        candidate["relation_total"],
                    "relation_exploration":
                        candidate is exploratory,
                }
                self.sync_transactions[
                    transaction_id] = transaction
                self.synchronized_rule_transactions.setdefault(
                    rule.rule_id, set()).add(transaction_id)
                self.subtree_rule_shapes.setdefault(
                    rule.rule_id, set()).add(
                        str(parent["shape_id"]))

        active_rule_ids = {
            rule_id
            for rule_id in self.synchronized_rule_transactions
            if rule_id in self.grammar_rules
        }
        for transaction_id, transaction in tuple(
                self.sync_transactions.items()):
            if transaction["rule_id"] not in active_rule_ids:
                del self.sync_transactions[transaction_id]
        for rule_id in tuple(self.synchronized_rule_transactions):
            if rule_id not in active_rule_ids:
                del self.synchronized_rule_transactions[rule_id]
                self.subtree_rule_shapes.pop(rule_id, None)
        for rule_id in previous_rule_ids - active_rule_ids:
            self.grammar_rules.pop(rule_id, None)
            self.subtree_rule_shapes.pop(rule_id, None)
            self.subtree_rule_instances.pop(rule_id, None)

    @staticmethod
    def _pcfg_family(
        parser: str,
        lhs: str,
        state: str,
    ) -> tuple[str, dict[str, str]]:
        core = {
            "schema": "symcc-parser-production-family-v1",
            "parser": parser,
            "lhs": lhs,
            "state": state,
        }
        family_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return family_id, {
            "family_id": family_id,
            "parser": parser,
            "lhs": lhs,
            "state": state,
        }

    @staticmethod
    def _pcfg_context(
        parser: str,
        family_id: str,
        parent_shape_id: str,
        slot: int,
    ) -> tuple[str, dict[str, Any]]:
        core = {
            "schema": "symcc-parser-production-context-v1",
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
        }
        context_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return context_id, {
            "context_id": context_id,
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
        }

    @staticmethod
    def _pcfg_circuit_context(
        parser: str,
        family_id: str,
        parent_shape_id: str,
        parent_slot: int,
        ancestor_shape_id: str,
        ancestor_slot: int,
    ) -> tuple[str, dict[str, Any]]:
        core = {
            "schema": "symcc-parser-production-circuit-context-v1",
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "parent_slot": parent_slot,
            "ancestor_shape_id": ancestor_shape_id,
            "ancestor_slot": ancestor_slot,
        }
        context_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return context_id, {
            "context_id": context_id,
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "parent_slot": parent_slot,
            "ancestor_shape_id": ancestor_shape_id,
            "ancestor_slot": ancestor_slot,
        }

    @staticmethod
    def _pcfg_sibling_context(
        parser: str,
        family_id: str,
        parent_shape_id: str,
        slot: int,
        left_sibling_shape_id: str,
    ) -> tuple[str, dict[str, Any]]:
        core = {
            "schema": "symcc-parser-production-sibling-context-v1",
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
            "left_sibling_shape_id": left_sibling_shape_id,
        }
        context_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return context_id, {
            "context_id": context_id,
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
            "left_sibling_shape_id": left_sibling_shape_id,
        }

    @staticmethod
    def _pcfg_history_context(
        parser: str,
        family_id: str,
        parent_shape_id: str,
        slot: int,
        older_sibling_shape_id: str,
        left_sibling_shape_id: str,
    ) -> tuple[str, dict[str, Any]]:
        core = {
            "schema": "symcc-parser-production-history-context-v1",
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
            "older_sibling_shape_id": older_sibling_shape_id,
            "left_sibling_shape_id": left_sibling_shape_id,
        }
        context_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return context_id, {
            "context_id": context_id,
            "parser": parser,
            "family_id": family_id,
            "parent_shape_id": parent_shape_id,
            "slot": slot,
            "older_sibling_shape_id": older_sibling_shape_id,
            "left_sibling_shape_id": left_sibling_shape_id,
        }

    @staticmethod
    def _pcfg_context_state_id(
        node_id: str,
        context_id: str,
        circuit_context_id: str = "",
        sibling_context_id: str = "",
        history_context_id: str = "",
    ) -> str:
        core = {
            "schema": (
                "symcc-parser-context-state-v4"
                if history_context_id else
                "symcc-parser-context-state-v3"
                if sibling_context_id else
                "symcc-parser-context-state-v2"
                if circuit_context_id else
                "symcc-parser-context-state-v1"
            ),
            "node_id": node_id,
            "context_id": context_id,
        }
        if circuit_context_id:
            core["circuit_context_id"] = circuit_context_id
        if sibling_context_id:
            core["sibling_context_id"] = sibling_context_id
        if history_context_id:
            core["history_context_id"] = history_context_id
        return hashlib.sha256(json.dumps(
            core, sort_keys=True, separators=(",", ":")).encode(
            "utf-8")).hexdigest()

    @staticmethod
    def _pcfg_prequential_receipt(
        *,
        fragment_sha256: str,
        node_id: str,
        context: dict[str, Any],
        shape_id: str,
        global_selected_before: int,
        global_total_before: int,
        known_shapes: int,
        context_selected_before: int,
        context_total_before: int,
        observation_index: int | None = None,
    ) -> dict[str, Any]:
        core = {
            "schema": (
                "symcc-parser-pcfg-prequential-v2"
                if observation_index is not None else
                "symcc-parser-pcfg-prequential-v1"
            ),
            "fragment_sha256": fragment_sha256,
            "node_id": node_id,
            "context_id": str(context["context_id"]),
            "family_id": str(context["family_id"]),
            "parent_shape_id": str(context["parent_shape_id"]),
            "slot": int(context["slot"]),
            "shape_id": shape_id,
            "global_selected_before": global_selected_before,
            "global_total_before": global_total_before,
            "known_shapes": known_shapes,
            "context_selected_before": context_selected_before,
            "context_total_before": context_total_before,
        }
        if observation_index is not None:
            core["observation_index"] = observation_index
        receipt_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "receipt_id": receipt_id,
            **{
                key: value
                for key, value in core.items()
                if key != "schema"
            },
        }

    @staticmethod
    def _pcfg_circuit_receipt(
        *,
        fragment_sha256: str,
        node_id: str,
        context: dict[str, Any],
        parent_context_id: str,
        shape_id: str,
        global_selected_before: int,
        global_total_before: int,
        known_shapes: int,
        parent_selected_before: int,
        parent_total_before: int,
        circuit_selected_before: int,
        circuit_total_before: int,
        observation_index: int,
    ) -> dict[str, Any]:
        core = {
            "schema": "symcc-parser-pcfg-circuit-prequential-v1",
            "fragment_sha256": fragment_sha256,
            "node_id": node_id,
            "context_id": str(context["context_id"]),
            "parent_context_id": parent_context_id,
            "family_id": str(context["family_id"]),
            "parent_shape_id": str(context["parent_shape_id"]),
            "parent_slot": int(context["parent_slot"]),
            "ancestor_shape_id": str(context["ancestor_shape_id"]),
            "ancestor_slot": int(context["ancestor_slot"]),
            "shape_id": shape_id,
            "global_selected_before": global_selected_before,
            "global_total_before": global_total_before,
            "known_shapes": known_shapes,
            "parent_selected_before": parent_selected_before,
            "parent_total_before": parent_total_before,
            "circuit_selected_before": circuit_selected_before,
            "circuit_total_before": circuit_total_before,
            "observation_index": observation_index,
        }
        receipt_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "receipt_id": receipt_id,
            **{
                key: value
                for key, value in core.items()
                if key != "schema"
            },
        }

    @staticmethod
    def _pcfg_sibling_receipt(
        *,
        fragment_sha256: str,
        node_id: str,
        context: dict[str, Any],
        parent_context_id: str,
        circuit_context_id: str,
        shape_id: str,
        global_selected_before: int,
        global_total_before: int,
        known_shapes: int,
        parent_selected_before: int,
        parent_total_before: int,
        circuit_selected_before: int,
        circuit_total_before: int,
        sibling_selected_before: int,
        sibling_total_before: int,
        observation_index: int,
    ) -> dict[str, Any]:
        core = {
            "schema": "symcc-parser-pcfg-sibling-prequential-v1",
            "fragment_sha256": fragment_sha256,
            "node_id": node_id,
            "context_id": str(context["context_id"]),
            "parent_context_id": parent_context_id,
            "circuit_context_id": circuit_context_id,
            "family_id": str(context["family_id"]),
            "parent_shape_id": str(context["parent_shape_id"]),
            "slot": int(context["slot"]),
            "left_sibling_shape_id":
                str(context["left_sibling_shape_id"]),
            "shape_id": shape_id,
            "global_selected_before": global_selected_before,
            "global_total_before": global_total_before,
            "known_shapes": known_shapes,
            "parent_selected_before": parent_selected_before,
            "parent_total_before": parent_total_before,
            "circuit_selected_before": circuit_selected_before,
            "circuit_total_before": circuit_total_before,
            "sibling_selected_before": sibling_selected_before,
            "sibling_total_before": sibling_total_before,
            "observation_index": observation_index,
        }
        receipt_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "receipt_id": receipt_id,
            **{
                key: value
                for key, value in core.items()
                if key != "schema"
            },
        }

    @staticmethod
    def _pcfg_history_receipt(
        *,
        fragment_sha256: str,
        node_id: str,
        context: dict[str, Any],
        parent_context_id: str,
        circuit_context_id: str,
        sibling_context_id: str,
        shape_id: str,
        global_selected_before: int,
        global_total_before: int,
        known_shapes: int,
        parent_selected_before: int,
        parent_total_before: int,
        circuit_selected_before: int,
        circuit_total_before: int,
        sibling_selected_before: int,
        sibling_total_before: int,
        history_selected_before: int,
        history_total_before: int,
        observation_index: int,
    ) -> dict[str, Any]:
        core = {
            "schema": "symcc-parser-pcfg-history-prequential-v1",
            "fragment_sha256": fragment_sha256,
            "node_id": node_id,
            "context_id": str(context["context_id"]),
            "parent_context_id": parent_context_id,
            "circuit_context_id": circuit_context_id,
            "sibling_context_id": sibling_context_id,
            "family_id": str(context["family_id"]),
            "parent_shape_id": str(context["parent_shape_id"]),
            "slot": int(context["slot"]),
            "older_sibling_shape_id":
                str(context["older_sibling_shape_id"]),
            "left_sibling_shape_id":
                str(context["left_sibling_shape_id"]),
            "shape_id": shape_id,
            "global_selected_before": global_selected_before,
            "global_total_before": global_total_before,
            "known_shapes": known_shapes,
            "parent_selected_before": parent_selected_before,
            "parent_total_before": parent_total_before,
            "circuit_selected_before": circuit_selected_before,
            "circuit_total_before": circuit_total_before,
            "sibling_selected_before": sibling_selected_before,
            "sibling_total_before": sibling_total_before,
            "history_selected_before": history_selected_before,
            "history_total_before": history_total_before,
            "observation_index": observation_index,
        }
        receipt_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "receipt_id": receipt_id,
            **{
                key: value
                for key, value in core.items()
                if key != "schema"
            },
        }

    @staticmethod
    def _packed_root_certificate(
        fragment_sha256: str,
        parser: str,
        root_id: str,
        *,
        source: str = "accepted-fragment",
    ) -> dict[str, str]:
        core = {
            "schema": "symcc-parser-packed-root-evidence-v1",
            "source": source,
            "fragment_sha256": fragment_sha256,
            "parser": parser,
            "root_id": root_id,
        }
        evidence_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "evidence_id": evidence_id,
            "source": source,
            "fragment_sha256": fragment_sha256,
            "parser": parser,
            "root_id": root_id,
        }

    def _pcfg_probability(
        self,
        family_id: str,
        shape_id: str,
    ) -> float:
        counts = self.pcfg_counts.get(family_id, {})
        known_shapes = self._pcfg_known_shapes(family_id)
        known_shapes.add(shape_id)
        alpha = self.PCFG_DIRICHLET_ALPHA
        total = sum(max(0, int(value)) for value in counts.values())
        return (
            max(0, int(counts.get(shape_id, 0))) + alpha
        ) / (total + alpha * max(1, len(known_shapes)))

    def _pcfg_known_shapes(self, family_id: str) -> set[str]:
        known_shapes = set(self.pcfg_counts.get(family_id, {}))
        family = self.pcfg_families.get(family_id)
        if family is not None:
            known_shapes.update(
                str(production["shape_id"])
                for production in self.cfg_productions.values()
                if (
                    production["parser"] == family["parser"] and
                    production["lhs"] == family["lhs"] and
                    production["state"] == family["state"]
                )
            )
        return known_shapes

    def _pcfg_context_probability(
        self,
        context_id: str,
        shape_id: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> float:
        if context is None:
            context = self.pcfg_contexts.get(context_id)
        if context is None:
            return 0.5
        family_id = str(context["family_id"])
        global_probability = self._pcfg_probability(
            family_id, shape_id)
        if (
            not context["parent_shape_id"] and
            int(context["slot"]) == -1
        ):
            return global_probability
        counts = self.pcfg_context_counts.get(context_id, {})
        total = sum(max(0, int(value)) for value in counts.values())
        prior = self.PCFG_CONTEXT_PRIOR_STRENGTH
        return (
            max(0, int(counts.get(shape_id, 0))) +
            prior * global_probability
        ) / (total + prior)

    def _pcfg_circuit_probability(
        self,
        context_id: str,
        shape_id: str,
        *,
        context: dict[str, Any],
        parent_context: dict[str, Any],
    ) -> float:
        parent_probability = self._pcfg_context_probability(
            str(parent_context["context_id"]),
            shape_id,
            context=parent_context,
        )
        counts = self.pcfg_circuit_counts.get(context_id, {})
        total = sum(max(0, int(value)) for value in counts.values())
        prior = self.PCFG_CONTEXT_PRIOR_STRENGTH
        return (
            max(0, int(counts.get(shape_id, 0))) +
            prior * parent_probability
        ) / (total + prior)

    def _pcfg_sibling_probability(
        self,
        context_id: str,
        shape_id: str,
        *,
        context: dict[str, Any],
        parent_context: dict[str, Any],
        circuit_context: dict[str, Any] | None,
    ) -> float:
        if circuit_context is not None:
            prior_probability = self._pcfg_circuit_probability(
                str(circuit_context["context_id"]),
                shape_id,
                context=circuit_context,
                parent_context=parent_context,
            )
        else:
            prior_probability = self._pcfg_context_probability(
                str(parent_context["context_id"]),
                shape_id,
                context=parent_context,
            )
        counts = self.pcfg_sibling_counts.get(context_id, {})
        total = sum(max(0, int(value)) for value in counts.values())
        prior = self.PCFG_CONTEXT_PRIOR_STRENGTH
        return (
            max(0, int(counts.get(shape_id, 0))) +
            prior * prior_probability
        ) / (total + prior)

    def _pcfg_history_probability(
        self,
        context_id: str,
        sibling_context_id: str,
        shape_id: str,
        *,
        context: dict[str, Any],
        sibling_context: dict[str, Any],
        parent_context: dict[str, Any],
        circuit_context: dict[str, Any] | None,
    ) -> float:
        sibling_probability = self._pcfg_sibling_probability(
            sibling_context_id,
            shape_id,
            context=sibling_context,
            parent_context=parent_context,
            circuit_context=circuit_context,
        )
        counts = self.pcfg_history_counts.get(context_id, {})
        total = sum(max(0, int(value)) for value in counts.values())
        prior = self.PCFG_CONTEXT_PRIOR_STRENGTH
        return (
            max(0, int(counts.get(shape_id, 0))) +
            prior * sibling_probability
        ) / (total + prior)

    @classmethod
    def _pcfg_prequential_probabilities(
        cls,
        receipt: dict[str, Any],
    ) -> tuple[float, float]:
        alpha = cls.PCFG_DIRICHLET_ALPHA
        global_probability = (
            int(receipt["global_selected_before"]) + alpha
        ) / (
            int(receipt["global_total_before"]) +
            alpha * int(receipt["known_shapes"])
        )
        if (
            not receipt["parent_shape_id"] and
            int(receipt["slot"]) == -1
        ):
            return global_probability, global_probability
        prior = cls.PCFG_CONTEXT_PRIOR_STRENGTH
        context_probability = (
            int(receipt["context_selected_before"]) +
            prior * global_probability
        ) / (
            int(receipt["context_total_before"]) + prior
        )
        return global_probability, context_probability

    @classmethod
    def _pcfg_circuit_prequential_probabilities(
        cls,
        receipt: dict[str, Any],
    ) -> tuple[float, float, float]:
        alpha = cls.PCFG_DIRICHLET_ALPHA
        global_probability = (
            int(receipt["global_selected_before"]) + alpha
        ) / (
            int(receipt["global_total_before"]) +
            alpha * int(receipt["known_shapes"])
        )
        prior = cls.PCFG_CONTEXT_PRIOR_STRENGTH
        parent_probability = (
            int(receipt["parent_selected_before"]) +
            prior * global_probability
        ) / (
            int(receipt["parent_total_before"]) + prior
        )
        circuit_probability = (
            int(receipt["circuit_selected_before"]) +
            prior * parent_probability
        ) / (
            int(receipt["circuit_total_before"]) + prior
        )
        return (
            global_probability,
            parent_probability,
            circuit_probability,
        )

    @classmethod
    def _pcfg_sibling_prequential_probabilities(
        cls,
        receipt: dict[str, Any],
    ) -> tuple[float, float, float, float]:
        alpha = cls.PCFG_DIRICHLET_ALPHA
        global_probability = (
            int(receipt["global_selected_before"]) + alpha
        ) / (
            int(receipt["global_total_before"]) +
            alpha * int(receipt["known_shapes"])
        )
        prior = cls.PCFG_CONTEXT_PRIOR_STRENGTH
        parent_probability = (
            int(receipt["parent_selected_before"]) +
            prior * global_probability
        ) / (
            int(receipt["parent_total_before"]) + prior
        )
        if str(receipt.get("circuit_context_id", "")):
            circuit_probability = (
                int(receipt["circuit_selected_before"]) +
                prior * parent_probability
            ) / (
                int(receipt["circuit_total_before"]) + prior
            )
        else:
            circuit_probability = parent_probability
        sibling_probability = (
            int(receipt["sibling_selected_before"]) +
            prior * circuit_probability
        ) / (
            int(receipt["sibling_total_before"]) + prior
        )
        return (
            global_probability,
            parent_probability,
            circuit_probability,
            sibling_probability,
        )

    @classmethod
    def _pcfg_history_prequential_probabilities(
        cls,
        receipt: dict[str, Any],
    ) -> tuple[float, float, float, float, float]:
        (
            global_probability,
            parent_probability,
            circuit_probability,
            sibling_probability,
        ) = cls._pcfg_sibling_prequential_probabilities(receipt)
        prior = cls.PCFG_CONTEXT_PRIOR_STRENGTH
        history_probability = (
            int(receipt["history_selected_before"]) +
            prior * sibling_probability
        ) / (
            int(receipt["history_total_before"]) + prior
        )
        return (
            global_probability,
            parent_probability,
            circuit_probability,
            sibling_probability,
            history_probability,
        )

    @staticmethod
    def _pcfg_adaptive_units(
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for event in sorted(
            events,
            key=lambda item: (
                int(item["observation_index"]),
                str(item["receipt_id"]),
            ),
        ):
            observation_index = int(event["observation_index"])
            if (
                not units or
                int(units[-1]["observation_index"]) != observation_index
            ):
                units.append({
                    "observation_index": observation_index,
                    "receipt_ids": [],
                    "clipped_gains": [],
                    "global_nll_bits": [],
                    "context_nll_bits": [],
                })
            units[-1]["receipt_ids"].append(str(event["receipt_id"]))
            units[-1]["clipped_gains"].append(
                float(event["clipped_gain_bits"]))
            units[-1]["global_nll_bits"].append(
                float(event["global_nll_bits"]))
            units[-1]["context_nll_bits"].append(
                float(event["context_nll_bits"]))
        for unit in units:
            count = len(unit["receipt_ids"])
            unit["clipped_gain_bits"] = (
                sum(unit.pop("clipped_gains")) / count
            )
            unit["mean_global_nll_bits"] = (
                sum(unit.pop("global_nll_bits")) / count
            )
            unit["mean_context_nll_bits"] = (
                sum(unit.pop("context_nll_bits")) / count
            )
        return units

    @staticmethod
    def _pcfg_circuit_adaptive_units(
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for event in sorted(
            events,
            key=lambda item: (
                int(item["observation_index"]),
                str(item["receipt_id"]),
            ),
        ):
            observation_index = int(event["observation_index"])
            if (
                not units or
                int(units[-1]["observation_index"]) != observation_index
            ):
                units.append({
                    "observation_index": observation_index,
                    "receipt_ids": [],
                    "clipped_gains": [],
                    "global_nll_bits": [],
                    "parent_nll_bits": [],
                    "circuit_nll_bits": [],
                })
            units[-1]["receipt_ids"].append(str(event["receipt_id"]))
            units[-1]["clipped_gains"].append(
                float(event["clipped_gain_bits"]))
            units[-1]["global_nll_bits"].append(
                float(event["global_nll_bits"]))
            units[-1]["parent_nll_bits"].append(
                float(event["parent_nll_bits"]))
            units[-1]["circuit_nll_bits"].append(
                float(event["circuit_nll_bits"]))
        for unit in units:
            count = len(unit["receipt_ids"])
            unit["clipped_gain_bits"] = (
                sum(unit.pop("clipped_gains")) / count
            )
            unit["mean_global_nll_bits"] = (
                sum(unit.pop("global_nll_bits")) / count
            )
            unit["mean_parent_nll_bits"] = (
                sum(unit.pop("parent_nll_bits")) / count
            )
            unit["mean_circuit_nll_bits"] = (
                sum(unit.pop("circuit_nll_bits")) / count
            )
        return units

    @staticmethod
    def _pcfg_sibling_adaptive_units(
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for event in sorted(
            events,
            key=lambda item: (
                int(item["observation_index"]),
                str(item["receipt_id"]),
            ),
        ):
            observation_index = int(event["observation_index"])
            if (
                not units or
                int(units[-1]["observation_index"]) != observation_index
            ):
                units.append({
                    "observation_index": observation_index,
                    "receipt_ids": [],
                    "clipped_gains": [],
                    "global_nll_bits": [],
                    "parent_nll_bits": [],
                    "circuit_nll_bits": [],
                    "sibling_nll_bits": [],
                })
            unit = units[-1]
            unit["receipt_ids"].append(str(event["receipt_id"]))
            unit["clipped_gains"].append(
                float(event["clipped_gain_bits"]))
            for model in (
                "global", "parent", "circuit", "sibling"
            ):
                unit[f"{model}_nll_bits"].append(
                    float(event[f"{model}_nll_bits"]))
        for unit in units:
            count = len(unit["receipt_ids"])
            unit["clipped_gain_bits"] = (
                sum(unit.pop("clipped_gains")) / count
            )
            for model in (
                "global", "parent", "circuit", "sibling"
            ):
                unit[f"mean_{model}_nll_bits"] = (
                    sum(unit.pop(f"{model}_nll_bits")) / count
                )
        return units

    @staticmethod
    def _pcfg_history_adaptive_units(
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        models = (
            "global", "parent", "circuit", "sibling", "history"
        )
        units: list[dict[str, Any]] = []
        for event in sorted(
            events,
            key=lambda item: (
                int(item["observation_index"]),
                str(item["receipt_id"]),
            ),
        ):
            observation_index = int(event["observation_index"])
            if (
                not units or
                int(units[-1]["observation_index"]) != observation_index
            ):
                units.append({
                    "observation_index": observation_index,
                    "receipt_ids": [],
                    "clipped_gains": [],
                    **{
                        f"{model}_nll_bits": []
                        for model in models
                    },
                })
            unit = units[-1]
            unit["receipt_ids"].append(str(event["receipt_id"]))
            unit["clipped_gains"].append(
                float(event["clipped_gain_bits"]))
            for model in models:
                unit[f"{model}_nll_bits"].append(
                    float(event[f"{model}_nll_bits"]))
        for unit in units:
            count = len(unit["receipt_ids"])
            unit["clipped_gain_bits"] = (
                sum(unit.pop("clipped_gains")) / count
            )
            for model in models:
                unit[f"mean_{model}_nll_bits"] = (
                    sum(unit.pop(f"{model}_nll_bits")) / count
                )
        return units

    @classmethod
    def _pcfg_adaptive_cut_statistics(
        cls,
        units: list[dict[str, Any]],
        split: int,
        multiple_tests: int,
    ) -> dict[str, float]:
        left = units[:split]
        right = units[split:]
        left_mean = sum(
            float(unit["clipped_gain_bits"]) for unit in left
        ) / len(left)
        right_mean = sum(
            float(unit["clipped_gain_bits"]) for unit in right
        ) / len(right)
        adjusted_delta = (
            cls.PCFG_ADAPTIVE_DELTA / max(1, multiple_tests)
        )
        epsilon = cls.PCFG_ADAPTIVE_CLIP_BITS * math.sqrt(
            2.0 * math.log(2.0 / adjusted_delta) *
            (1.0 / len(left) + 1.0 / len(right))
        )
        difference = abs(left_mean - right_mean)
        return {
            "left_mean_gain_bits": left_mean,
            "right_mean_gain_bits": right_mean,
            "difference_bits": difference,
            "epsilon_bits": epsilon,
            "margin_bits": difference - epsilon,
            "adjusted_delta": adjusted_delta,
        }

    @classmethod
    def _pcfg_adaptive_certificate(
        cls,
        context_id: str,
        units: list[dict[str, Any]],
        split: int,
        multiple_tests: int,
        *,
        schema: str = "symcc-parser-pcfg-adaptive-cut-v1",
    ) -> dict[str, Any]:
        statistics = cls._pcfg_adaptive_cut_statistics(
            units, split, multiple_tests)
        receipt_ids = [
            str(receipt_id)
            for unit in units
            for receipt_id in unit["receipt_ids"]
        ]
        evidence_sha256 = hashlib.sha256(json.dumps(
            receipt_ids,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        core = {
            "schema": schema,
            "context_id": context_id,
            "window_start_index": int(
                units[0]["observation_index"]),
            "window_end_index": int(
                units[-1]["observation_index"]),
            "cut_observation_index": int(
                units[split]["observation_index"]),
            "left_fragments": split,
            "right_fragments": len(units) - split,
            "left_receipts": sum(
                len(unit["receipt_ids"]) for unit in units[:split]),
            "right_receipts": sum(
                len(unit["receipt_ids"]) for unit in units[split:]),
            "left_mean_gain_bits": round(
                statistics["left_mean_gain_bits"], 12),
            "right_mean_gain_bits": round(
                statistics["right_mean_gain_bits"], 12),
            "difference_bits": round(
                statistics["difference_bits"], 12),
            "epsilon_bits": round(
                statistics["epsilon_bits"], 12),
            "margin_bits": round(
                statistics["margin_bits"], 12),
            "clip_bound_bits": cls.PCFG_ADAPTIVE_CLIP_BITS,
            "delta": cls.PCFG_ADAPTIVE_DELTA,
            "adjusted_delta": round(
                statistics["adjusted_delta"], 12),
            "multiple_tests": multiple_tests,
            "evidence_sha256": evidence_sha256,
        }
        certificate_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {"certificate_id": certificate_id, **core}

    @classmethod
    def _pcfg_anytime_running_interval(
        cls,
        normalized_gains: list[float],
        launch_ordinal: int,
        *,
        delta: float | None = None,
    ) -> dict[str, Any] | None:
        if launch_ordinal <= 0 or not normalized_gains:
            return None
        allocated_delta = (
            cls.PCFG_ADAPTIVE_DELTA
            if delta is None else float(delta)
        )
        if (
            not math.isfinite(allocated_delta) or
            not 0.0 < allocated_delta < 1.0
        ):
            return None
        launch_alpha = (
            allocated_delta *
            cls.PCFG_ANYTIME_SPENDING_SCALE /
            (launch_ordinal ** 2)
        )
        lower = 0.0
        upper = 1.0
        total = 0.0
        lower_tightening_age = 0
        upper_tightening_age = 0
        final_epsilon = 0.0
        final_look_alpha = 0.0
        for age, value in enumerate(normalized_gains, 1):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                return None
            total += value
            look_alpha = (
                launch_alpha *
                cls.PCFG_ANYTIME_SPENDING_SCALE /
                (age ** 2)
            )
            epsilon = math.sqrt(
                math.log(2.0 / look_alpha) / (2.0 * age)
            )
            mean = total / age
            candidate_lower = max(0.0, mean - epsilon)
            candidate_upper = min(1.0, mean + epsilon)
            if candidate_lower > lower:
                lower = candidate_lower
                lower_tightening_age = age
            if candidate_upper < upper:
                upper = candidate_upper
                upper_tightening_age = age
            final_epsilon = epsilon
            final_look_alpha = look_alpha
        return {
            "launch_ordinal": launch_ordinal,
            "samples": len(normalized_gains),
            "mean": total / len(normalized_gains),
            "running_lower": lower,
            "running_upper": upper,
            "launch_alpha": launch_alpha,
            "final_look_alpha": final_look_alpha,
            "final_epsilon": final_epsilon,
            "lower_tightening_age": lower_tightening_age,
            "upper_tightening_age": upper_tightening_age,
        }

    @staticmethod
    def _pcfg_round_anytime_interval(
        interval: dict[str, Any],
        observation_index: int,
    ) -> dict[str, Any]:
        return {
            "launch_ordinal": int(interval["launch_ordinal"]),
            "launch_observation_index": observation_index,
            "samples": int(interval["samples"]),
            "mean": round(float(interval["mean"]), 12),
            "running_lower": round(
                float(interval["running_lower"]), 12),
            "running_upper": round(
                float(interval["running_upper"]), 12),
            "launch_alpha": round(
                float(interval["launch_alpha"]), 18),
            "final_look_alpha": round(
                float(interval["final_look_alpha"]), 18),
            "final_epsilon": round(
                float(interval["final_epsilon"]), 12),
            "lower_tightening_age": int(
                interval["lower_tightening_age"]),
            "upper_tightening_age": int(
                interval["upper_tightening_age"]),
        }

    @classmethod
    def _pcfg_anytime_certificate(
        cls,
        context_id: str,
        all_units: list[dict[str, Any]],
        *,
        schema: str,
        delta: float | None = None,
        guarantee: str = "context-wise-infinite-horizon-pfa",
        null_assumption: str =
            "bounded-constant-conditional-mean",
        construction: str =
            "repeated-forward-hoeffding-alpha-spending",
        extra_core: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        allocated_delta = (
            cls.PCFG_ADAPTIVE_DELTA
            if delta is None else float(delta)
        )
        if (
            not math.isfinite(allocated_delta) or
            not 0.0 < allocated_delta < 1.0
        ):
            return None
        bounded_units = all_units[
            -cls.PCFG_ADAPTIVE_MAX_FRAGMENTS:]
        minimum = cls.PCFG_ADAPTIVE_MIN_FRAGMENTS
        if len(bounded_units) < 2 * minimum:
            return None
        window_start_ordinal = (
            len(all_units) - len(bounded_units) + 1
        )
        normalized = [
            (
                float(unit["clipped_gain_bits"]) +
                cls.PCFG_ADAPTIVE_CLIP_BITS
            ) / (2.0 * cls.PCFG_ADAPTIVE_CLIP_BITS)
            for unit in bounded_units
        ]
        intervals: list[tuple[int, dict[str, Any]]] = []
        for local_start in range(
            0, len(bounded_units) - minimum + 1
        ):
            interval = cls._pcfg_anytime_running_interval(
                normalized[local_start:],
                window_start_ordinal + local_start,
                delta=allocated_delta,
            )
            if (
                interval is not None and
                int(interval["samples"]) >= minimum
            ):
                intervals.append((local_start, interval))
        best: tuple[
            float,
            int,
            int,
            dict[str, Any],
            dict[str, Any],
        ] | None = None
        for earlier_index, earlier in intervals:
            for later_index, later in intervals:
                if later_index - earlier_index < minimum:
                    continue
                gap = max(
                    float(earlier["running_lower"]) -
                    float(later["running_upper"]),
                    float(later["running_lower"]) -
                    float(earlier["running_upper"]),
                )
                if gap <= 1e-12:
                    continue
                candidate = (
                    gap,
                    -later_index,
                    -earlier_index,
                    earlier,
                    later,
                )
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
        if best is None:
            return None
        gap, neg_later_index, neg_earlier_index, earlier, later = best
        earlier_index = -neg_earlier_index
        later_index = -neg_later_index
        receipt_ids = [
            str(receipt_id)
            for unit in bounded_units
            for receipt_id in unit["receipt_ids"]
        ]
        evidence_sha256 = hashlib.sha256(json.dumps(
            receipt_ids,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        core = {
            "schema": schema,
            "context_id": context_id,
            "guarantee": guarantee,
            "null_assumption": null_assumption,
            "construction": construction,
            "delta": round(allocated_delta, 18),
            "spending_scale": round(
                cls.PCFG_ANYTIME_SPENDING_SCALE, 18),
            "spending_exponent": 2,
            "clip_lower_bits": -cls.PCFG_ADAPTIVE_CLIP_BITS,
            "clip_upper_bits": cls.PCFG_ADAPTIVE_CLIP_BITS,
            "window_start_ordinal": window_start_ordinal,
            "window_start_index": int(
                bounded_units[0]["observation_index"]),
            "window_end_index": int(
                bounded_units[-1]["observation_index"]),
            "detection_observation_index": int(
                bounded_units[-1]["observation_index"]),
            "cut_observation_index": int(
                bounded_units[later_index]["observation_index"]),
            "minimum_fragments": minimum,
            "separation_gap": round(gap, 12),
            "earlier": cls._pcfg_round_anytime_interval(
                earlier,
                int(bounded_units[earlier_index][
                    "observation_index"]),
            ),
            "later": cls._pcfg_round_anytime_interval(
                later,
                int(bounded_units[later_index][
                    "observation_index"]),
            ),
            "evidence_sha256": evidence_sha256,
        }
        if extra_core:
            core.update(extra_core)
        certificate_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {"certificate_id": certificate_id, **core}

    @classmethod
    def _pcfg_verify_anytime_units(
        cls,
        certificate: dict[str, Any],
        all_units: list[dict[str, Any]],
        *,
        schema: str,
        delta: float | None = None,
        guarantee: str = "context-wise-infinite-horizon-pfa",
        null_assumption: str =
            "bounded-constant-conditional-mean",
        construction: str =
            "repeated-forward-hoeffding-alpha-spending",
        extra_core: dict[str, Any] | None = None,
    ) -> bool:
        if (
            not isinstance(certificate, dict) or
            certificate.get("schema") != schema
        ):
            return False
        try:
            context_id = str(certificate["context_id"])
            end = int(certificate["window_end_index"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        end_position = next((
            index
            for index, unit in enumerate(all_units)
            if int(unit["observation_index"]) == end
        ), -1)
        if end_position < 0:
            return False
        expected = cls._pcfg_anytime_certificate(
            context_id,
            all_units[:end_position + 1],
            schema=schema,
            delta=delta,
            guarantee=guarantee,
            null_assumption=null_assumption,
            construction=construction,
            extra_core=extra_core,
        )
        return (
            expected is not None and
            expected == certificate and
            float(expected["separation_gap"]) > 0.0
        )

    @staticmethod
    def _pcfg_anytime_allocation_key(
        context_kind: str,
        context_id: str,
    ) -> str:
        return f"{context_kind}:{context_id}"

    @classmethod
    def _pcfg_anytime_allocation(
        cls,
        context_kind: str,
        context_id: str,
        ordinal: int,
        first_observation_index: int,
    ) -> dict[str, Any]:
        context_delta = (
            cls.PCFG_GLOBAL_FWER_DELTA *
            cls.PCFG_ANYTIME_SPENDING_SCALE /
            (ordinal ** 2)
        )
        core = {
            "schema":
                "symcc-parser-pcfg-anytime-context-allocation-v1",
            "context_kind": context_kind,
            "context_id": context_id,
            "ordinal": ordinal,
            "first_observation_index": first_observation_index,
            "global_delta": cls.PCFG_GLOBAL_FWER_DELTA,
            "spending_scale": round(
                cls.PCFG_ANYTIME_SPENDING_SCALE, 18),
            "spending_exponent": 2,
            "context_delta": round(context_delta, 18),
            "contract": "append-only-no-alpha-recycling",
        }
        allocation_id = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {"allocation_id": allocation_id, **core}

    def _allocate_pcfg_anytime_contexts(
        self,
        contexts: Iterable[tuple[str, str, int]],
    ) -> None:
        if not self.pcfg_anytime_allocation_ledger_valid:
            return
        kind_order = {
            "parent": 0,
            "circuit": 1,
            "sibling": 2,
            "history": 3,
        }
        for context_kind, context_id, observation_index in sorted(
            {
                (str(kind), str(identifier), int(index))
                for kind, identifier, index in contexts
                if (
                    str(kind) in kind_order and
                    self._valid_digest(str(identifier)) and
                    isinstance(index, int) and
                    not isinstance(index, bool) and
                    index >= 0
                )
            },
            key=lambda item: (
                int(item[2]),
                kind_order[item[0]],
                item[1],
            ),
        ):
            allocation_key = self._pcfg_anytime_allocation_key(
                context_kind, context_id)
            if allocation_key in self.pcfg_anytime_context_allocations:
                continue
            if (
                len(self.pcfg_anytime_context_allocations) >=
                self.HARD_MAX_PCFG_ANYTIME_CONTEXT_ALLOCATIONS
            ):
                break
            ordinal = len(
                self.pcfg_anytime_context_allocations) + 1
            self.pcfg_anytime_context_allocations[
                allocation_key
            ] = self._pcfg_anytime_allocation(
                context_kind,
                context_id,
                ordinal,
                observation_index,
            )

    def _pcfg_anytime_evidenced_contexts(
        self,
    ) -> list[tuple[str, str, int]]:
        first_indexes: dict[tuple[str, str], int] = {}
        stores = (
            ("parent", self.pcfg_prequential_receipts),
            ("circuit", self.pcfg_circuit_receipts),
            ("sibling", self.pcfg_sibling_receipts),
            ("history", self.pcfg_history_receipts),
        )
        for context_kind, receipts in stores:
            for receipt in receipts.values():
                context_id = str(receipt.get("context_id", ""))
                observation_index = receipt.get(
                    "observation_index")
                if (
                    not self._valid_digest(context_id) or
                    not isinstance(observation_index, int) or
                    isinstance(observation_index, bool) or
                    observation_index < 0
                ):
                    continue
                key = (context_kind, context_id)
                first_indexes[key] = min(
                    first_indexes.get(key, observation_index),
                    observation_index,
                )
        return [
            (context_kind, context_id, observation_index)
            for (
                context_kind,
                context_id,
            ), observation_index in first_indexes.items()
        ]

    def _restore_pcfg_anytime_context_allocations(
        self,
        raw: dict[str, Any],
    ) -> None:
        self.pcfg_anytime_context_allocations.clear()
        self.pcfg_anytime_allocation_ledger_valid = True
        evidenced = self._pcfg_anytime_evidenced_contexts()
        if int(raw.get("schema", 0)) < 25:
            self._allocate_pcfg_anytime_contexts(evidenced)
            return
        stored = raw.get("pcfg_anytime_context_allocations")
        if (
            not isinstance(stored, list) or
            len(stored) >
            self.HARD_MAX_PCFG_ANYTIME_CONTEXT_ALLOCATIONS
        ):
            self.pcfg_anytime_allocation_ledger_valid = False
            return
        restored: dict[str, dict[str, Any]] = {}
        for ordinal, item in enumerate(stored, 1):
            if not isinstance(item, dict):
                self.pcfg_anytime_allocation_ledger_valid = False
                break
            context_kind = str(item.get("context_kind", ""))
            context_id = str(item.get("context_id", ""))
            first_observation_index = item.get(
                "first_observation_index")
            if (
                context_kind not in {
                    "parent", "circuit", "sibling", "history",
                } or
                not self._valid_digest(context_id) or
                not isinstance(first_observation_index, int) or
                isinstance(first_observation_index, bool) or
                not 0 <= first_observation_index <
                len(self.pcfg_observations)
            ):
                self.pcfg_anytime_allocation_ledger_valid = False
                break
            expected = self._pcfg_anytime_allocation(
                context_kind,
                context_id,
                ordinal,
                first_observation_index,
            )
            allocation_key = self._pcfg_anytime_allocation_key(
                context_kind, context_id)
            if item != expected or allocation_key in restored:
                self.pcfg_anytime_allocation_ledger_valid = False
                break
            restored[allocation_key] = expected
        if not self.pcfg_anytime_allocation_ledger_valid:
            self.pcfg_anytime_context_allocations.clear()
            return
        self.pcfg_anytime_context_allocations = restored
        for context_kind, context_id, first_index in evidenced:
            allocation = restored.get(
                self._pcfg_anytime_allocation_key(
                    context_kind, context_id))
            if (
                allocation is None or
                int(allocation["first_observation_index"]) !=
                first_index
            ):
                self.pcfg_anytime_context_allocations.clear()
                self.pcfg_anytime_allocation_ledger_valid = False
                return

    def _pcfg_anytime_allocation_prefix_sha256(
        self,
        ordinal: int,
    ) -> str:
        allocations = sorted(
            self.pcfg_anytime_context_allocations.values(),
            key=lambda item: int(item["ordinal"]),
        )
        if (
            ordinal <= 0 or ordinal > len(allocations) or
            [int(item["ordinal"]) for item in allocations] !=
            list(range(1, len(allocations) + 1))
        ):
            return ""
        return hashlib.sha256(json.dumps(
            [
                str(item["allocation_id"])
                for item in allocations[:ordinal]
            ],
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _pcfg_global_anytime_units(
        self,
        context_kind: str,
        context_id: str,
    ) -> list[dict[str, Any]] | None:
        events: list[dict[str, Any]] = []
        if context_kind == "parent":
            for receipt_id in sorted(
                    self.pcfg_prequential_receipts):
                receipt = self.pcfg_prequential_receipts[receipt_id]
                observation_index = receipt.get(
                    "observation_index")
                if (
                    str(receipt.get("context_id", "")) !=
                    context_id or
                    not isinstance(observation_index, int) or
                    isinstance(observation_index, bool)
                ):
                    continue
                try:
                    global_probability, context_probability = (
                        self._pcfg_prequential_probabilities(
                            receipt)
                    )
                    nlls = (
                        -math.log2(global_probability),
                        -math.log2(context_probability),
                    )
                except (
                    KeyError, TypeError, ValueError,
                    ZeroDivisionError,
                ):
                    return None
                events.append({
                    "receipt_id": receipt_id,
                    "observation_index": observation_index,
                    "clipped_gain_bits": max(
                        -self.PCFG_ADAPTIVE_CLIP_BITS,
                        min(
                            self.PCFG_ADAPTIVE_CLIP_BITS,
                            nlls[0] - nlls[1],
                        ),
                    ),
                    "global_nll_bits": nlls[0],
                    "context_nll_bits": nlls[1],
                })
            return self._pcfg_adaptive_units(events)
        if context_kind == "circuit":
            for receipt_id in sorted(self.pcfg_circuit_receipts):
                receipt = self.pcfg_circuit_receipts[receipt_id]
                observation_index = receipt.get(
                    "observation_index")
                if (
                    str(receipt.get("context_id", "")) !=
                    context_id or
                    not isinstance(observation_index, int) or
                    isinstance(observation_index, bool)
                ):
                    continue
                try:
                    probabilities = (
                        self._pcfg_circuit_prequential_probabilities(
                            receipt)
                    )
                    nlls = tuple(
                        -math.log2(value)
                        for value in probabilities
                    )
                except (
                    KeyError, TypeError, ValueError,
                    ZeroDivisionError,
                ):
                    return None
                events.append({
                    "receipt_id": receipt_id,
                    "observation_index": observation_index,
                    "clipped_gain_bits": max(
                        -self.PCFG_ADAPTIVE_CLIP_BITS,
                        min(
                            self.PCFG_ADAPTIVE_CLIP_BITS,
                            min(
                                nlls[0] - nlls[2],
                                nlls[1] - nlls[2],
                            ),
                        ),
                    ),
                    "global_nll_bits": nlls[0],
                    "parent_nll_bits": nlls[1],
                    "circuit_nll_bits": nlls[2],
                })
            return self._pcfg_circuit_adaptive_units(events)
        if context_kind == "sibling":
            for receipt_id in sorted(self.pcfg_sibling_receipts):
                receipt = self.pcfg_sibling_receipts[receipt_id]
                observation_index = receipt.get(
                    "observation_index")
                if (
                    str(receipt.get("context_id", "")) !=
                    context_id or
                    not isinstance(observation_index, int) or
                    isinstance(observation_index, bool)
                ):
                    continue
                try:
                    probabilities = (
                        self._pcfg_sibling_prequential_probabilities(
                            receipt)
                    )
                    nlls = tuple(
                        -math.log2(value)
                        for value in probabilities
                    )
                except (
                    KeyError, TypeError, ValueError,
                    ZeroDivisionError,
                ):
                    return None
                event = {
                    "receipt_id": receipt_id,
                    "observation_index": observation_index,
                    "clipped_gain_bits": max(
                        -self.PCFG_ADAPTIVE_CLIP_BITS,
                        min(
                            self.PCFG_ADAPTIVE_CLIP_BITS,
                            min(
                                nlls[index] - nlls[3]
                                for index in range(3)
                            ),
                        ),
                    ),
                }
                for model, nll in zip(
                    ("global", "parent", "circuit", "sibling"),
                    nlls,
                ):
                    event[f"{model}_nll_bits"] = nll
                events.append(event)
            return self._pcfg_sibling_adaptive_units(events)
        if context_kind == "history":
            history_events = self._pcfg_history_events(context_id)
            return (
                self._pcfg_history_adaptive_units(history_events)
                if history_events is not None else None
            )
        return None

    def _pcfg_global_anytime_extra_core(
        self,
        allocation: dict[str, Any],
    ) -> dict[str, Any] | None:
        ordinal = int(allocation["ordinal"])
        prefix_sha256 = (
            self._pcfg_anytime_allocation_prefix_sha256(
                ordinal)
        )
        if not prefix_sha256:
            return None
        return {
            "context_kind": str(allocation["context_kind"]),
            "allocation_id": str(allocation["allocation_id"]),
            "context_ordinal": ordinal,
            "context_delta": float(
                allocation["context_delta"]),
            "global_delta": self.PCFG_GLOBAL_FWER_DELTA,
            "context_spending_scale": round(
                self.PCFG_ANYTIME_SPENDING_SCALE, 18),
            "context_spending_exponent": 2,
            "allocation_prefix_sha256": prefix_sha256,
            "allocation_contract":
                "append-only-no-alpha-recycling",
            "registration_observation_excluded": True,
            "cross_context_dependence_assumption": "none",
        }

    def _refresh_pcfg_global_anytime_certificates(self) -> None:
        self.pcfg_global_anytime_certificates.clear()
        if not self.pcfg_anytime_allocation_ledger_valid:
            return
        for allocation_key, allocation in sorted(
            self.pcfg_anytime_context_allocations.items(),
            key=lambda item: int(item[1]["ordinal"]),
        ):
            context_kind = str(allocation["context_kind"])
            if (
                context_kind not in self.PCFG_CONTEXT_LEVELS or
                self.PCFG_CONTEXT_LEVELS.index(context_kind) >
                self.pcfg_context_order
            ):
                continue
            context_id = str(allocation["context_id"])
            units = self._pcfg_global_anytime_units(
                context_kind, context_id)
            extra_core = self._pcfg_global_anytime_extra_core(
                allocation)
            if units is None or extra_core is None:
                continue
            units = [
                unit for unit in units
                if int(unit["observation_index"]) >
                int(allocation["first_observation_index"])
            ]
            certificate = self._pcfg_anytime_certificate(
                context_id,
                units,
                schema=
                    "symcc-parser-pcfg-global-anytime-cut-v1",
                delta=float(allocation["context_delta"]),
                guarantee=
                    "cross-context-infinite-horizon-fwer",
                null_assumption=(
                    "all-true-null-contexts-bounded-constant-"
                    "conditional-mean"
                ),
                construction=(
                    "online-context-and-repeated-forward-"
                    "hoeffding-alpha-spending"
                ),
                extra_core=extra_core,
            )
            if certificate is not None:
                self.pcfg_global_anytime_certificates[
                    allocation_key] = [certificate]

    def verify_pcfg_global_anytime_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if (
            not self.pcfg_anytime_allocation_ledger_valid or
            not isinstance(certificate, dict) or
            certificate.get("schema") !=
            "symcc-parser-pcfg-global-anytime-cut-v1"
        ):
            return False
        context_kind = str(
            certificate.get("context_kind", ""))
        context_id = str(certificate.get("context_id", ""))
        allocation_key = self._pcfg_anytime_allocation_key(
            context_kind, context_id)
        allocation = self.pcfg_anytime_context_allocations.get(
            allocation_key)
        if allocation is None:
            return False
        extra_core = self._pcfg_global_anytime_extra_core(
            allocation)
        units = self._pcfg_global_anytime_units(
            context_kind, context_id)
        if units is None or extra_core is None:
            return False
        units = [
            unit for unit in units
            if int(unit["observation_index"]) >
            int(allocation["first_observation_index"])
        ]
        return self._pcfg_verify_anytime_units(
            certificate,
            units,
            schema="symcc-parser-pcfg-global-anytime-cut-v1",
            delta=float(allocation["context_delta"]),
            guarantee="cross-context-infinite-horizon-fwer",
            null_assumption=(
                "all-true-null-contexts-bounded-constant-"
                "conditional-mean"
            ),
            construction=(
                "online-context-and-repeated-forward-"
                "hoeffding-alpha-spending"
            ),
            extra_core=extra_core,
        )

    def _pcfg_detect_adaptive_cuts(
        self,
        context_id: str,
        all_units: list[dict[str, Any]],
        *,
        certificate_schema: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        bounded_units = all_units[
            -self.PCFG_ADAPTIVE_MAX_FRAGMENTS:]
        units: list[dict[str, Any]] = []
        certificates: list[dict[str, Any]] = []
        for unit in bounded_units:
            units.append(unit)
            while (
                len(units) >=
                2 * self.PCFG_ADAPTIVE_MIN_FRAGMENTS
            ):
                multiple_tests = max(
                    1,
                    len(units) -
                    2 * self.PCFG_ADAPTIVE_MIN_FRAGMENTS + 1,
                )
                prefix_gains = [0.0]
                for candidate_unit in units:
                    prefix_gains.append(
                        prefix_gains[-1] +
                        float(candidate_unit["clipped_gain_bits"])
                    )
                total_gain = prefix_gains[-1]
                adjusted_delta = (
                    self.PCFG_ADAPTIVE_DELTA / multiple_tests
                )
                threshold_scale = (
                    self.PCFG_ADAPTIVE_CLIP_BITS *
                    math.sqrt(
                        2.0 * math.log(2.0 / adjusted_delta)
                    )
                )
                best: tuple[float, int] | None = None
                for split in range(
                    self.PCFG_ADAPTIVE_MIN_FRAGMENTS,
                    len(units) -
                    self.PCFG_ADAPTIVE_MIN_FRAGMENTS + 1,
                ):
                    left_mean = prefix_gains[split] / split
                    right_mean = (
                        total_gain - prefix_gains[split]
                    ) / (len(units) - split)
                    epsilon = threshold_scale * math.sqrt(
                        1.0 / split +
                        1.0 / (len(units) - split)
                    )
                    margin = abs(left_mean - right_mean) - epsilon
                    candidate = (margin, split)
                    if best is None or candidate > best:
                        best = candidate
                if best is None or best[0] <= 1e-12:
                    break
                split = best[1]
                certificates.append(
                    self._pcfg_adaptive_certificate(
                        context_id,
                        units,
                        split,
                        multiple_tests,
                        schema=certificate_schema,
                    )
                )
                units = units[split:]
        return units, certificates

    def verify_pcfg_adaptive_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(certificate, dict) or
            certificate.get("schema") !=
            "symcc-parser-pcfg-adaptive-cut-v1"
        ):
            return False
        try:
            context_id = str(certificate["context_id"])
            start = int(certificate["window_start_index"])
            end = int(certificate["window_end_index"])
            cut = int(certificate["cut_observation_index"])
            multiple_tests = int(certificate["multiple_tests"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        events: list[dict[str, Any]] = []
        for receipt_id, receipt in self.pcfg_prequential_receipts.items():
            observation_index = receipt.get("observation_index")
            if (
                str(receipt.get("context_id", "")) != context_id or
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool) or
                not start <= observation_index <= end
            ):
                continue
            try:
                global_probability, context_probability = (
                    self._pcfg_prequential_probabilities(receipt)
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            gain = math.log2(
                context_probability / global_probability)
            events.append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, gain),
                ),
                "global_nll_bits": -math.log2(global_probability),
                "context_nll_bits": -math.log2(context_probability),
            })
        units = self._pcfg_adaptive_units(events)
        split = next((
            index
            for index, unit in enumerate(units)
            if int(unit["observation_index"]) == cut
        ), -1)
        if (
            split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS or
            len(units) - split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS
        ):
            return False
        expected = self._pcfg_adaptive_certificate(
            context_id, units, split, multiple_tests)
        return (
            expected == certificate and
            float(expected["margin_bits"]) > 0.0
        )

    def verify_pcfg_anytime_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if not isinstance(certificate, dict):
            return False
        events: list[dict[str, Any]] = []
        context_id = str(certificate.get("context_id", ""))
        for receipt_id, receipt in self.pcfg_prequential_receipts.items():
            if str(receipt.get("context_id", "")) != context_id:
                continue
            observation_index = receipt.get("observation_index")
            if (
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool)
            ):
                continue
            try:
                global_probability, context_probability = (
                    self._pcfg_prequential_probabilities(receipt)
                )
                gain = math.log2(
                    context_probability / global_probability)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            events.append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, gain),
                ),
                "global_nll_bits": -math.log2(global_probability),
                "context_nll_bits": -math.log2(context_probability),
            })
        return self._pcfg_verify_anytime_units(
            certificate,
            self._pcfg_adaptive_units(events),
            schema="symcc-parser-pcfg-anytime-cut-v1",
        )

    def verify_pcfg_circuit_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(certificate, dict) or
            certificate.get("schema") !=
            "symcc-parser-pcfg-circuit-cut-v1"
        ):
            return False
        try:
            context_id = str(certificate["context_id"])
            start = int(certificate["window_start_index"])
            end = int(certificate["window_end_index"])
            cut = int(certificate["cut_observation_index"])
            multiple_tests = int(certificate["multiple_tests"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        events: list[dict[str, Any]] = []
        for receipt_id, receipt in self.pcfg_circuit_receipts.items():
            observation_index = receipt.get("observation_index")
            if (
                str(receipt.get("context_id", "")) != context_id or
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool) or
                not start <= observation_index <= end
            ):
                continue
            try:
                (
                    global_probability,
                    parent_probability,
                    circuit_probability,
                ) = self._pcfg_circuit_prequential_probabilities(
                    receipt)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            robust_gain = min(
                math.log2(
                    circuit_probability / global_probability),
                math.log2(
                    circuit_probability / parent_probability),
            )
            events.append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
                "global_nll_bits": -math.log2(global_probability),
                "parent_nll_bits": -math.log2(parent_probability),
                "circuit_nll_bits": -math.log2(circuit_probability),
            })
        units = self._pcfg_circuit_adaptive_units(events)
        split = next((
            index
            for index, unit in enumerate(units)
            if int(unit["observation_index"]) == cut
        ), -1)
        if (
            split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS or
            len(units) - split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS
        ):
            return False
        expected = self._pcfg_adaptive_certificate(
            context_id,
            units,
            split,
            multiple_tests,
            schema="symcc-parser-pcfg-circuit-cut-v1",
        )
        return (
            expected == certificate and
            float(expected["margin_bits"]) > 0.0
        )

    def verify_pcfg_circuit_anytime_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if not isinstance(certificate, dict):
            return False
        events: list[dict[str, Any]] = []
        context_id = str(certificate.get("context_id", ""))
        for receipt_id, receipt in self.pcfg_circuit_receipts.items():
            if str(receipt.get("context_id", "")) != context_id:
                continue
            observation_index = receipt.get("observation_index")
            if (
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool)
            ):
                continue
            try:
                probabilities = (
                    self._pcfg_circuit_prequential_probabilities(
                        receipt)
                )
                nlls = [-math.log2(value) for value in probabilities]
                robust_gain = min(
                    nlls[0] - nlls[2],
                    nlls[1] - nlls[2],
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            events.append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
                "global_nll_bits": nlls[0],
                "parent_nll_bits": nlls[1],
                "circuit_nll_bits": nlls[2],
            })
        return self._pcfg_verify_anytime_units(
            certificate,
            self._pcfg_circuit_adaptive_units(events),
            schema="symcc-parser-pcfg-circuit-anytime-cut-v1",
        )

    def _refresh_pcfg_circuit_calibration(self) -> None:
        self.pcfg_circuit_calibration_stats.clear()
        self.pcfg_circuit_certificates.clear()
        self.pcfg_circuit_anytime_certificates.clear()
        sequenced_events: dict[str, list[dict[str, Any]]] = {}
        sequenced_indexes = [
            int(receipt["observation_index"])
            for receipt in self.pcfg_circuit_receipts.values()
            if (
                isinstance(receipt.get("observation_index"), int) and
                not isinstance(
                    receipt.get("observation_index"), bool)
            )
        ]
        newest_index = max(
            max(sequenced_indexes, default=-1),
            len(self.pcfg_observations) - 1,
        )
        recent_start = max(
            0,
            newest_index -
            self.PCFG_CALIBRATION_RECENT_FRAGMENTS + 1,
        )
        for receipt_id in sorted(self.pcfg_circuit_receipts):
            receipt = self.pcfg_circuit_receipts[receipt_id]
            try:
                (
                    global_probability,
                    parent_probability,
                    circuit_probability,
                ) = self._pcfg_circuit_prequential_probabilities(
                    receipt)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if any(
                not 0.0 < probability <= 1.0
                for probability in (
                    global_probability,
                    parent_probability,
                    circuit_probability,
                )
            ):
                continue
            context_id = str(receipt["context_id"])
            stats = self.pcfg_circuit_calibration_stats.setdefault(
                context_id,
                {
                    "observations": 0,
                    "global_nll_bits": 0.0,
                    "parent_nll_bits": 0.0,
                    "circuit_nll_bits": 0.0,
                    "global_gain_bits": 0.0,
                    "parent_gain_bits": 0.0,
                    "robust_gain_bits": 0.0,
                    "circuit_wins_global": 0,
                    "circuit_wins_parent": 0,
                    "recent_observations": 0,
                    "recent_global_nll_bits": 0.0,
                    "recent_parent_nll_bits": 0.0,
                    "recent_circuit_nll_bits": 0.0,
                    "recent_robust_gain_bits": 0.0,
                    "recent_start": recent_start,
                    "latest_observation_index": -1,
                    "stale": 0,
                    "adaptive_fragments": 0,
                    "adaptive_receipts": 0,
                    "adaptive_global_nll_bits": 0.0,
                    "adaptive_parent_nll_bits": 0.0,
                    "adaptive_circuit_nll_bits": 0.0,
                    "adaptive_global_gain_bits": 0.0,
                    "adaptive_parent_gain_bits": 0.0,
                    "adaptive_robust_gain_bits": 0.0,
                    "adaptive_start": -1,
                    "adaptive_cut_count": 0,
                    "adaptive_truncated_fragments": 0,
                    "anytime_certified": 0,
                    "anytime_cut_observation_index": -1,
                    "anytime_separation_gap": 0.0,
                    "weight": 0.0,
                },
            )
            global_nll = -math.log2(global_probability)
            parent_nll = -math.log2(parent_probability)
            circuit_nll = -math.log2(circuit_probability)
            stats["observations"] = int(stats["observations"]) + 1
            stats["global_nll_bits"] = (
                float(stats["global_nll_bits"]) + global_nll
            )
            stats["parent_nll_bits"] = (
                float(stats["parent_nll_bits"]) + parent_nll
            )
            stats["circuit_nll_bits"] = (
                float(stats["circuit_nll_bits"]) + circuit_nll
            )
            if circuit_probability > global_probability:
                stats["circuit_wins_global"] = int(
                    stats["circuit_wins_global"]) + 1
            if circuit_probability > parent_probability:
                stats["circuit_wins_parent"] = int(
                    stats["circuit_wins_parent"]) + 1
            observation_index = int(receipt["observation_index"])
            stats["latest_observation_index"] = max(
                int(stats["latest_observation_index"]),
                observation_index,
            )
            robust_gain = min(
                global_nll - circuit_nll,
                parent_nll - circuit_nll,
            )
            sequenced_events.setdefault(context_id, []).append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
                "global_nll_bits": global_nll,
                "parent_nll_bits": parent_nll,
                "circuit_nll_bits": circuit_nll,
            })
            if observation_index >= recent_start:
                stats["recent_observations"] = int(
                    stats["recent_observations"]) + 1
                stats["recent_global_nll_bits"] = (
                    float(stats["recent_global_nll_bits"]) +
                    global_nll
                )
                stats["recent_parent_nll_bits"] = (
                    float(stats["recent_parent_nll_bits"]) +
                    parent_nll
                )
                stats["recent_circuit_nll_bits"] = (
                    float(stats["recent_circuit_nll_bits"]) +
                    circuit_nll
                )
        for context_id, stats in (
                self.pcfg_circuit_calibration_stats.items()):
            stats["global_gain_bits"] = (
                float(stats["global_nll_bits"]) -
                float(stats["circuit_nll_bits"])
            )
            stats["parent_gain_bits"] = (
                float(stats["parent_nll_bits"]) -
                float(stats["circuit_nll_bits"])
            )
            stats["robust_gain_bits"] = min(
                float(stats["global_gain_bits"]),
                float(stats["parent_gain_bits"]),
            )
            recent_global_gain = (
                float(stats["recent_global_nll_bits"]) -
                float(stats["recent_circuit_nll_bits"])
            )
            recent_parent_gain = (
                float(stats["recent_parent_nll_bits"]) -
                float(stats["recent_circuit_nll_bits"])
            )
            stats["recent_robust_gain_bits"] = min(
                recent_global_gain, recent_parent_gain)
            stats["stale"] = int(
                int(stats["recent_observations"]) == 0)
            all_units = self._pcfg_circuit_adaptive_units(
                sequenced_events.get(context_id, []))
            units, certificates = self._pcfg_detect_adaptive_cuts(
                context_id,
                all_units,
                certificate_schema=
                    "symcc-parser-pcfg-circuit-cut-v1",
            )
            if certificates:
                self.pcfg_circuit_certificates[
                    context_id] = certificates
            anytime_certificate = self._pcfg_anytime_certificate(
                context_id,
                all_units,
                schema=
                    "symcc-parser-pcfg-circuit-anytime-cut-v1",
            )
            if anytime_certificate is not None:
                self.pcfg_circuit_anytime_certificates[
                    context_id] = [anytime_certificate]
                stats["anytime_certified"] = 1
                stats["anytime_cut_observation_index"] = int(
                    anytime_certificate["cut_observation_index"])
                stats["anytime_separation_gap"] = float(
                    anytime_certificate["separation_gap"])
            stats["adaptive_fragments"] = len(units)
            stats["adaptive_receipts"] = sum(
                len(unit["receipt_ids"]) for unit in units)
            for model in ("global", "parent", "circuit"):
                stats[f"adaptive_{model}_nll_bits"] = sum(
                    float(unit[f"mean_{model}_nll_bits"])
                    for unit in units
                )
            stats["adaptive_global_gain_bits"] = (
                float(stats["adaptive_global_nll_bits"]) -
                float(stats["adaptive_circuit_nll_bits"])
            )
            stats["adaptive_parent_gain_bits"] = (
                float(stats["adaptive_parent_nll_bits"]) -
                float(stats["adaptive_circuit_nll_bits"])
            )
            stats["adaptive_robust_gain_bits"] = min(
                float(stats["adaptive_global_gain_bits"]),
                float(stats["adaptive_parent_gain_bits"]),
            )
            stats["adaptive_start"] = (
                int(units[0]["observation_index"])
                if units else -1
            )
            stats["adaptive_cut_count"] = len(certificates)
            stats["adaptive_truncated_fragments"] = (
                len(all_units) - len(units)
            )
            observations = int(stats["adaptive_fragments"])
            gate_gain = float(stats["adaptive_robust_gain_bits"])
            if int(stats["stale"]) > 0:
                observations = 0
                gate_gain = 0.0
            stats["weight"] = (
                min(1.0, observations / 8.0) *
                min(1.0, max(0.0, gate_gain) / 2.0)
                if observations >= 2 else 0.0
            )

    def verify_pcfg_sibling_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(certificate, dict) or
            certificate.get("schema") !=
            "symcc-parser-pcfg-sibling-cut-v1"
        ):
            return False
        try:
            context_id = str(certificate["context_id"])
            start = int(certificate["window_start_index"])
            end = int(certificate["window_end_index"])
            cut = int(certificate["cut_observation_index"])
            multiple_tests = int(certificate["multiple_tests"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        events: list[dict[str, Any]] = []
        for receipt_id, receipt in self.pcfg_sibling_receipts.items():
            observation_index = receipt.get("observation_index")
            if (
                str(receipt.get("context_id", "")) != context_id or
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool) or
                not start <= observation_index <= end
            ):
                continue
            try:
                probabilities = (
                    self._pcfg_sibling_prequential_probabilities(
                        receipt)
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            nlls = [-math.log2(value) for value in probabilities]
            robust_gain = min(
                nlls[index] - nlls[3] for index in range(3))
            events.append({
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
                "global_nll_bits": nlls[0],
                "parent_nll_bits": nlls[1],
                "circuit_nll_bits": nlls[2],
                "sibling_nll_bits": nlls[3],
            })
        units = self._pcfg_sibling_adaptive_units(events)
        split = next((
            index
            for index, unit in enumerate(units)
            if int(unit["observation_index"]) == cut
        ), -1)
        if (
            split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS or
            len(units) - split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS
        ):
            return False
        expected = self._pcfg_adaptive_certificate(
            context_id,
            units,
            split,
            multiple_tests,
            schema="symcc-parser-pcfg-sibling-cut-v1",
        )
        return (
            expected == certificate and
            float(expected["margin_bits"]) > 0.0
        )

    def verify_pcfg_sibling_anytime_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if not isinstance(certificate, dict):
            return False
        events: list[dict[str, Any]] = []
        context_id = str(certificate.get("context_id", ""))
        for receipt_id, receipt in self.pcfg_sibling_receipts.items():
            if str(receipt.get("context_id", "")) != context_id:
                continue
            observation_index = receipt.get("observation_index")
            if (
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool)
            ):
                continue
            try:
                probabilities = (
                    self._pcfg_sibling_prequential_probabilities(
                        receipt)
                )
                nlls = [-math.log2(value) for value in probabilities]
                robust_gain = min(
                    nlls[index] - nlls[3] for index in range(3))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return False
            event = {
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
            }
            for model, nll in zip(
                ("global", "parent", "circuit", "sibling"),
                nlls,
            ):
                event[f"{model}_nll_bits"] = nll
            events.append(event)
        return self._pcfg_verify_anytime_units(
            certificate,
            self._pcfg_sibling_adaptive_units(events),
            schema="symcc-parser-pcfg-sibling-anytime-cut-v1",
        )

    def _refresh_pcfg_sibling_calibration(self) -> None:
        self.pcfg_sibling_calibration_stats.clear()
        self.pcfg_sibling_certificates.clear()
        self.pcfg_sibling_anytime_certificates.clear()
        events_by_context: dict[str, list[dict[str, Any]]] = {}
        indexes = [
            int(receipt["observation_index"])
            for receipt in self.pcfg_sibling_receipts.values()
            if (
                isinstance(receipt.get("observation_index"), int) and
                not isinstance(
                    receipt.get("observation_index"), bool)
            )
        ]
        newest_index = max(
            max(indexes, default=-1),
            len(self.pcfg_observations) - 1,
        )
        recent_start = max(
            0,
            newest_index -
            self.PCFG_CALIBRATION_RECENT_FRAGMENTS + 1,
        )
        models = ("global", "parent", "circuit", "sibling")
        for receipt_id in sorted(self.pcfg_sibling_receipts):
            receipt = self.pcfg_sibling_receipts[receipt_id]
            try:
                probabilities = (
                    self._pcfg_sibling_prequential_probabilities(
                        receipt)
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if any(
                not 0.0 < probability <= 1.0
                for probability in probabilities
            ):
                continue
            nlls = [-math.log2(value) for value in probabilities]
            context_id = str(receipt["context_id"])
            stats = self.pcfg_sibling_calibration_stats.setdefault(
                context_id,
                {
                    "observations": 0,
                    **{
                        f"{model}_nll_bits": 0.0
                        for model in models
                    },
                    "robust_gain_bits": 0.0,
                    "recent_observations": 0,
                    **{
                        f"recent_{model}_nll_bits": 0.0
                        for model in models
                    },
                    "recent_robust_gain_bits": 0.0,
                    "recent_start": recent_start,
                    "latest_observation_index": -1,
                    "stale": 0,
                    "adaptive_fragments": 0,
                    "adaptive_receipts": 0,
                    **{
                        f"adaptive_{model}_nll_bits": 0.0
                        for model in models
                    },
                    "adaptive_robust_gain_bits": 0.0,
                    "adaptive_start": -1,
                    "adaptive_cut_count": 0,
                    "adaptive_truncated_fragments": 0,
                    "anytime_certified": 0,
                    "anytime_cut_observation_index": -1,
                    "anytime_separation_gap": 0.0,
                    "weight": 0.0,
                },
            )
            stats["observations"] = int(stats["observations"]) + 1
            for model, nll in zip(models, nlls):
                stats[f"{model}_nll_bits"] = (
                    float(stats[f"{model}_nll_bits"]) + nll
                )
            observation_index = int(receipt["observation_index"])
            stats["latest_observation_index"] = max(
                int(stats["latest_observation_index"]),
                observation_index,
            )
            robust_gain = min(
                nlls[index] - nlls[3] for index in range(3))
            event = {
                "receipt_id": receipt_id,
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
            }
            for model, nll in zip(models, nlls):
                event[f"{model}_nll_bits"] = nll
            events_by_context.setdefault(context_id, []).append(event)
            if observation_index >= recent_start:
                stats["recent_observations"] = int(
                    stats["recent_observations"]) + 1
                for model, nll in zip(models, nlls):
                    key = f"recent_{model}_nll_bits"
                    stats[key] = float(stats[key]) + nll
        for context_id, stats in (
                self.pcfg_sibling_calibration_stats.items()):
            stats["robust_gain_bits"] = min(
                float(stats[f"{model}_nll_bits"]) -
                float(stats["sibling_nll_bits"])
                for model in models[:3]
            )
            stats["recent_robust_gain_bits"] = min(
                float(stats[f"recent_{model}_nll_bits"]) -
                float(stats["recent_sibling_nll_bits"])
                for model in models[:3]
            )
            stats["stale"] = int(
                int(stats["recent_observations"]) == 0)
            all_units = self._pcfg_sibling_adaptive_units(
                events_by_context.get(context_id, []))
            units, certificates = self._pcfg_detect_adaptive_cuts(
                context_id,
                all_units,
                certificate_schema=
                    "symcc-parser-pcfg-sibling-cut-v1",
            )
            if certificates:
                self.pcfg_sibling_certificates[
                    context_id] = certificates
            anytime_certificate = self._pcfg_anytime_certificate(
                context_id,
                all_units,
                schema=
                    "symcc-parser-pcfg-sibling-anytime-cut-v1",
            )
            if anytime_certificate is not None:
                self.pcfg_sibling_anytime_certificates[
                    context_id] = [anytime_certificate]
                stats["anytime_certified"] = 1
                stats["anytime_cut_observation_index"] = int(
                    anytime_certificate["cut_observation_index"])
                stats["anytime_separation_gap"] = float(
                    anytime_certificate["separation_gap"])
            stats["adaptive_fragments"] = len(units)
            stats["adaptive_receipts"] = sum(
                len(unit["receipt_ids"]) for unit in units)
            for model in models:
                stats[f"adaptive_{model}_nll_bits"] = sum(
                    float(unit[f"mean_{model}_nll_bits"])
                    for unit in units
                )
            stats["adaptive_robust_gain_bits"] = min(
                float(stats[f"adaptive_{model}_nll_bits"]) -
                float(stats["adaptive_sibling_nll_bits"])
                for model in models[:3]
            )
            stats["adaptive_start"] = (
                int(units[0]["observation_index"])
                if units else -1
            )
            stats["adaptive_cut_count"] = len(certificates)
            stats["adaptive_truncated_fragments"] = (
                len(all_units) - len(units)
            )
            observations = int(stats["adaptive_fragments"])
            gate_gain = float(stats["adaptive_robust_gain_bits"])
            if int(stats["stale"]) > 0:
                observations = 0
                gate_gain = 0.0
            stats["weight"] = (
                min(1.0, observations / 8.0) *
                min(1.0, max(0.0, gate_gain) / 2.0)
                if observations >= 2 else 0.0
            )

    def _pcfg_history_events(
        self,
        context_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        models = (
            "global", "parent", "circuit", "sibling", "history"
        )
        events: list[dict[str, Any]] = []
        for receipt_id in sorted(self.pcfg_history_receipts):
            receipt = self.pcfg_history_receipts[receipt_id]
            if (
                context_id is not None and
                str(receipt.get("context_id", "")) != context_id
            ):
                continue
            observation_index = receipt.get("observation_index")
            if (
                not isinstance(observation_index, int) or
                isinstance(observation_index, bool)
            ):
                continue
            try:
                probabilities = (
                    self._pcfg_history_prequential_probabilities(
                        receipt)
                )
                if any(
                    not 0.0 < probability <= 1.0
                    for probability in probabilities
                ):
                    return None
                nlls = [-math.log2(value) for value in probabilities]
                robust_gain = min(
                    nlls[index] - nlls[4] for index in range(4))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return None
            event = {
                "receipt_id": receipt_id,
                "context_id": str(receipt["context_id"]),
                "observation_index": observation_index,
                "clipped_gain_bits": max(
                    -self.PCFG_ADAPTIVE_CLIP_BITS,
                    min(self.PCFG_ADAPTIVE_CLIP_BITS, robust_gain),
                ),
            }
            for model, nll in zip(models, nlls):
                event[f"{model}_nll_bits"] = nll
            events.append(event)
        return events

    def verify_pcfg_history_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(certificate, dict) or
            certificate.get("schema") !=
            "symcc-parser-pcfg-history-cut-v1"
        ):
            return False
        try:
            context_id = str(certificate["context_id"])
            start = int(certificate["window_start_index"])
            end = int(certificate["window_end_index"])
            cut = int(certificate["cut_observation_index"])
            multiple_tests = int(certificate["multiple_tests"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        events = self._pcfg_history_events(context_id)
        if events is None:
            return False
        units = self._pcfg_history_adaptive_units([
            event
            for event in events
            if start <= int(event["observation_index"]) <= end
        ])
        split = next((
            index
            for index, unit in enumerate(units)
            if int(unit["observation_index"]) == cut
        ), -1)
        if (
            split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS or
            len(units) - split < self.PCFG_ADAPTIVE_MIN_FRAGMENTS
        ):
            return False
        expected = self._pcfg_adaptive_certificate(
            context_id,
            units,
            split,
            multiple_tests,
            schema="symcc-parser-pcfg-history-cut-v1",
        )
        return (
            expected == certificate and
            float(expected["margin_bits"]) > 0.0
        )

    def verify_pcfg_history_anytime_certificate(
        self,
        certificate: dict[str, Any],
    ) -> bool:
        if not isinstance(certificate, dict):
            return False
        context_id = str(certificate.get("context_id", ""))
        events = self._pcfg_history_events(context_id)
        if events is None:
            return False
        return self._pcfg_verify_anytime_units(
            certificate,
            self._pcfg_history_adaptive_units(events),
            schema="symcc-parser-pcfg-history-anytime-cut-v1",
        )

    def _refresh_pcfg_history_calibration(self) -> None:
        self.pcfg_history_calibration_stats.clear()
        self.pcfg_history_certificates.clear()
        self.pcfg_history_anytime_certificates.clear()
        models = (
            "global", "parent", "circuit", "sibling", "history"
        )
        events = self._pcfg_history_events()
        if events is None:
            return
        indexes = [
            int(event["observation_index"]) for event in events
        ]
        newest_index = max(
            max(indexes, default=-1),
            len(self.pcfg_observations) - 1,
        )
        recent_start = max(
            0,
            newest_index -
            self.PCFG_CALIBRATION_RECENT_FRAGMENTS + 1,
        )
        events_by_context: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            context_id = str(event["context_id"])
            stats = self.pcfg_history_calibration_stats.setdefault(
                context_id,
                {
                    "observations": 0,
                    **{
                        f"{model}_nll_bits": 0.0
                        for model in models
                    },
                    "robust_gain_bits": 0.0,
                    "recent_observations": 0,
                    **{
                        f"recent_{model}_nll_bits": 0.0
                        for model in models
                    },
                    "recent_robust_gain_bits": 0.0,
                    "recent_start": recent_start,
                    "latest_observation_index": -1,
                    "stale": 0,
                    "adaptive_fragments": 0,
                    "adaptive_receipts": 0,
                    **{
                        f"adaptive_{model}_nll_bits": 0.0
                        for model in models
                    },
                    "adaptive_robust_gain_bits": 0.0,
                    "adaptive_start": -1,
                    "adaptive_cut_count": 0,
                    "adaptive_truncated_fragments": 0,
                    "anytime_certified": 0,
                    "anytime_cut_observation_index": -1,
                    "anytime_separation_gap": 0.0,
                    "weight": 0.0,
                },
            )
            stats["observations"] = int(stats["observations"]) + 1
            for model in models:
                key = f"{model}_nll_bits"
                stats[key] = (
                    float(stats[key]) + float(event[key])
                )
            observation_index = int(event["observation_index"])
            stats["latest_observation_index"] = max(
                int(stats["latest_observation_index"]),
                observation_index,
            )
            events_by_context.setdefault(context_id, []).append(event)
            if observation_index >= recent_start:
                stats["recent_observations"] = int(
                    stats["recent_observations"]) + 1
                for model in models:
                    key = f"recent_{model}_nll_bits"
                    stats[key] = (
                        float(stats[key]) +
                        float(event[f"{model}_nll_bits"])
                    )
        for context_id, stats in (
                self.pcfg_history_calibration_stats.items()):
            stats["robust_gain_bits"] = min(
                float(stats[f"{model}_nll_bits"]) -
                float(stats["history_nll_bits"])
                for model in models[:4]
            )
            stats["recent_robust_gain_bits"] = min(
                float(stats[f"recent_{model}_nll_bits"]) -
                float(stats["recent_history_nll_bits"])
                for model in models[:4]
            )
            stats["stale"] = int(
                int(stats["recent_observations"]) == 0)
            all_units = self._pcfg_history_adaptive_units(
                events_by_context.get(context_id, []))
            units, certificates = self._pcfg_detect_adaptive_cuts(
                context_id,
                all_units,
                certificate_schema=
                    "symcc-parser-pcfg-history-cut-v1",
            )
            if certificates:
                self.pcfg_history_certificates[
                    context_id] = certificates
            anytime_certificate = self._pcfg_anytime_certificate(
                context_id,
                all_units,
                schema=
                    "symcc-parser-pcfg-history-anytime-cut-v1",
            )
            if anytime_certificate is not None:
                self.pcfg_history_anytime_certificates[
                    context_id] = [anytime_certificate]
                stats["anytime_certified"] = 1
                stats["anytime_cut_observation_index"] = int(
                    anytime_certificate["cut_observation_index"])
                stats["anytime_separation_gap"] = float(
                    anytime_certificate["separation_gap"])
            stats["adaptive_fragments"] = len(units)
            stats["adaptive_receipts"] = sum(
                len(unit["receipt_ids"]) for unit in units)
            for model in models:
                stats[f"adaptive_{model}_nll_bits"] = sum(
                    float(unit[f"mean_{model}_nll_bits"])
                    for unit in units
                )
            stats["adaptive_robust_gain_bits"] = min(
                float(stats[f"adaptive_{model}_nll_bits"]) -
                float(stats["adaptive_history_nll_bits"])
                for model in models[:4]
            )
            stats["adaptive_start"] = (
                int(units[0]["observation_index"])
                if units else -1
            )
            stats["adaptive_cut_count"] = len(certificates)
            stats["adaptive_truncated_fragments"] = (
                len(all_units) - len(units)
            )
            observations = int(stats["adaptive_fragments"])
            gate_gain = float(stats["adaptive_robust_gain_bits"])
            if int(stats["stale"]) > 0:
                observations = 0
                gate_gain = 0.0
            stats["weight"] = (
                min(1.0, observations / 8.0) *
                min(1.0, max(0.0, gate_gain) / 2.0)
                if observations >= 2 else 0.0
            )

    def _refresh_pcfg_context_calibration(self) -> None:
        self.pcfg_context_calibration_stats.clear()
        self.pcfg_adaptive_certificates.clear()
        self.pcfg_anytime_certificates.clear()
        sequenced_events: dict[str, list[dict[str, Any]]] = {}
        sequenced_indexes = [
            int(receipt["observation_index"])
            for receipt in self.pcfg_prequential_receipts.values()
            if (
                isinstance(receipt.get("observation_index"), int) and
                not isinstance(
                    receipt.get("observation_index"), bool)
            )
        ]
        newest_index = max(
            max(sequenced_indexes, default=-1),
            len(self.pcfg_observations) - 1,
        )
        recent_start = max(
            0,
            newest_index -
            self.PCFG_CALIBRATION_RECENT_FRAGMENTS + 1,
        )
        for receipt_id in sorted(self.pcfg_prequential_receipts):
            receipt = self.pcfg_prequential_receipts[receipt_id]
            try:
                global_probability, context_probability = (
                    self._pcfg_prequential_probabilities(receipt)
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if (
                not 0.0 < global_probability <= 1.0 or
                not 0.0 < context_probability <= 1.0
            ):
                continue
            context_id = str(receipt["context_id"])
            stats = self.pcfg_context_calibration_stats.setdefault(
                context_id,
                {
                    "observations": 0,
                    "global_nll_bits": 0.0,
                    "context_nll_bits": 0.0,
                    "context_wins": 0,
                    "gain_bits": 0.0,
                    "sequenced_observations": 0,
                    "recent_observations": 0,
                    "recent_global_nll_bits": 0.0,
                    "recent_context_nll_bits": 0.0,
                    "recent_context_wins": 0,
                    "recent_gain_bits": 0.0,
                    "recent_start": recent_start,
                    "latest_observation_index": -1,
                    "recency_mode": 0,
                    "stale": 0,
                    "adaptive_fragments": 0,
                    "adaptive_receipts": 0,
                    "adaptive_global_nll_bits": 0.0,
                    "adaptive_context_nll_bits": 0.0,
                    "adaptive_gain_bits": 0.0,
                    "adaptive_wins": 0,
                    "adaptive_start": -1,
                    "adaptive_cut_count": 0,
                    "adaptive_truncated_fragments": 0,
                    "anytime_certified": 0,
                    "anytime_cut_observation_index": -1,
                    "anytime_separation_gap": 0.0,
                    "weight": 0.0,
                },
            )
            stats["observations"] = int(
                stats["observations"]) + 1
            stats["global_nll_bits"] = float(
                stats["global_nll_bits"]
            ) - math.log2(global_probability)
            stats["context_nll_bits"] = float(
                stats["context_nll_bits"]
            ) - math.log2(context_probability)
            if context_probability > global_probability:
                stats["context_wins"] = int(
                    stats["context_wins"]) + 1
            observation_index = receipt.get("observation_index")
            if (
                isinstance(observation_index, int) and
                not isinstance(observation_index, bool)
            ):
                stats["sequenced_observations"] = int(
                    stats["sequenced_observations"]) + 1
                stats["latest_observation_index"] = max(
                    int(stats["latest_observation_index"]),
                    observation_index,
                )
                gain = math.log2(
                    context_probability / global_probability)
                sequenced_events.setdefault(context_id, []).append({
                    "receipt_id": receipt_id,
                    "observation_index": observation_index,
                    "clipped_gain_bits": max(
                        -self.PCFG_ADAPTIVE_CLIP_BITS,
                        min(self.PCFG_ADAPTIVE_CLIP_BITS, gain),
                    ),
                    "global_nll_bits": -math.log2(
                        global_probability),
                    "context_nll_bits": -math.log2(
                        context_probability),
                })
                if observation_index >= recent_start:
                    stats["recent_observations"] = int(
                        stats["recent_observations"]) + 1
                    stats["recent_global_nll_bits"] = float(
                        stats["recent_global_nll_bits"]
                    ) - math.log2(global_probability)
                    stats["recent_context_nll_bits"] = float(
                        stats["recent_context_nll_bits"]
                    ) - math.log2(context_probability)
                    if context_probability > global_probability:
                        stats["recent_context_wins"] = int(
                            stats["recent_context_wins"]) + 1
        for context_id, stats in (
                self.pcfg_context_calibration_stats.items()):
            gain = (
                float(stats["global_nll_bits"]) -
                float(stats["context_nll_bits"])
            )
            stats["gain_bits"] = gain
            recent_gain = (
                float(stats["recent_global_nll_bits"]) -
                float(stats["recent_context_nll_bits"])
            )
            stats["recent_gain_bits"] = recent_gain
            if int(stats["sequenced_observations"]) > 0:
                stats["recency_mode"] = 1
                stats["stale"] = int(
                    int(stats["recent_observations"]) == 0)
                all_units = self._pcfg_adaptive_units(
                    sequenced_events.get(context_id, []))
                units, certificates = (
                    self._pcfg_detect_adaptive_cuts(
                        context_id,
                        all_units,
                        certificate_schema=
                            "symcc-parser-pcfg-adaptive-cut-v1",
                    )
                )
                if certificates:
                    self.pcfg_adaptive_certificates[
                        context_id] = certificates
                anytime_certificate = self._pcfg_anytime_certificate(
                    context_id,
                    all_units,
                    schema=
                        "symcc-parser-pcfg-anytime-cut-v1",
                )
                if anytime_certificate is not None:
                    self.pcfg_anytime_certificates[
                        context_id] = [anytime_certificate]
                    stats["anytime_certified"] = 1
                    stats["anytime_cut_observation_index"] = int(
                        anytime_certificate[
                            "cut_observation_index"])
                    stats["anytime_separation_gap"] = float(
                        anytime_certificate["separation_gap"])
                stats["adaptive_fragments"] = len(units)
                stats["adaptive_receipts"] = sum(
                    len(unit["receipt_ids"]) for unit in units)
                stats["adaptive_global_nll_bits"] = sum(
                    float(unit["mean_global_nll_bits"])
                    for unit in units
                )
                stats["adaptive_context_nll_bits"] = sum(
                    float(unit["mean_context_nll_bits"])
                    for unit in units
                )
                stats["adaptive_gain_bits"] = (
                    float(stats["adaptive_global_nll_bits"]) -
                    float(stats["adaptive_context_nll_bits"])
                )
                stats["adaptive_wins"] = sum(
                    float(unit["mean_context_nll_bits"]) <
                    float(unit["mean_global_nll_bits"])
                    for unit in units
                )
                stats["adaptive_start"] = (
                    int(units[0]["observation_index"])
                    if units else -1
                )
                stats["adaptive_cut_count"] = len(certificates)
                stats["adaptive_truncated_fragments"] = (
                    len(all_units) - len(units)
                )
                observations = int(stats["adaptive_fragments"])
                gate_gain = float(stats["adaptive_gain_bits"])
                if int(stats["stale"]) > 0:
                    observations = 0
                    gate_gain = 0.0
            else:
                observations = int(stats["observations"])
                gate_gain = gain
            stats["weight"] = (
                min(1.0, observations / 8.0) *
                min(1.0, max(0.0, gate_gain) / 2.0)
                if observations >= 2 else 0.0
            )

    def _pcfg_effective_context_probability(
        self,
        context_id: str,
        shape_id: str,
        *,
        context: dict[str, Any],
    ) -> tuple[float, float, float]:
        family_id = str(context["family_id"])
        global_probability = self._pcfg_probability(
            family_id, shape_id)
        raw_probability = self._pcfg_context_probability(
            context_id, shape_id, context=context)
        weight = float(
            self.pcfg_context_calibration_stats.get(
                context_id, {}).get("weight", 0.0)
        )
        effective = (
            global_probability +
            weight * (raw_probability - global_probability)
        )
        return (
            max(0.0, min(1.0, effective)),
            raw_probability,
            weight,
        )

    def _pcfg_effective_circuit_probability(
        self,
        parent_context_id: str,
        circuit_context_id: str,
        shape_id: str,
        *,
        parent_context: dict[str, Any],
        circuit_context: dict[str, Any],
    ) -> tuple[float, float, float, float, float]:
        (
            parent_effective,
            parent_raw,
            parent_weight,
        ) = self._pcfg_effective_context_probability(
            parent_context_id,
            shape_id,
            context=parent_context,
        )
        circuit_raw = self._pcfg_circuit_probability(
            circuit_context_id,
            shape_id,
            context=circuit_context,
            parent_context=parent_context,
        )
        circuit_weight = float(
            self.pcfg_circuit_calibration_stats.get(
                circuit_context_id, {}).get("weight", 0.0)
        )
        effective = (
            (1.0 - circuit_weight) * parent_effective +
            circuit_weight * circuit_raw
        )
        return (
            max(0.0, min(1.0, effective)),
            parent_raw,
            circuit_raw,
            parent_weight,
            circuit_weight,
        )

    def _pcfg_effective_sibling_probability(
        self,
        parent_context_id: str,
        circuit_context_id: str,
        sibling_context_id: str,
        shape_id: str,
        *,
        parent_context: dict[str, Any],
        circuit_context: dict[str, Any] | None,
        sibling_context: dict[str, Any],
    ) -> tuple[float, float, float, float, float, float, float]:
        if circuit_context_id and circuit_context is not None:
            (
                base_effective,
                parent_raw,
                circuit_raw,
                parent_weight,
                circuit_weight,
            ) = self._pcfg_effective_circuit_probability(
                parent_context_id,
                circuit_context_id,
                shape_id,
                parent_context=parent_context,
                circuit_context=circuit_context,
            )
        else:
            (
                base_effective,
                parent_raw,
                parent_weight,
            ) = self._pcfg_effective_context_probability(
                parent_context_id,
                shape_id,
                context=parent_context,
            )
            circuit_raw = parent_raw
            circuit_weight = 0.0
        sibling_raw = self._pcfg_sibling_probability(
            sibling_context_id,
            shape_id,
            context=sibling_context,
            parent_context=parent_context,
            circuit_context=circuit_context,
        )
        sibling_weight = float(
            self.pcfg_sibling_calibration_stats.get(
                sibling_context_id, {}).get("weight", 0.0)
        )
        effective = (
            (1.0 - sibling_weight) * base_effective +
            sibling_weight * sibling_raw
        )
        return (
            max(0.0, min(1.0, effective)),
            parent_raw,
            circuit_raw,
            sibling_raw,
            parent_weight,
            circuit_weight,
            sibling_weight,
        )

    def _pcfg_effective_history_probability(
        self,
        parent_context_id: str,
        circuit_context_id: str,
        sibling_context_id: str,
        history_context_id: str,
        shape_id: str,
        *,
        parent_context: dict[str, Any],
        circuit_context: dict[str, Any] | None,
        sibling_context: dict[str, Any],
        history_context: dict[str, Any],
    ) -> tuple[
        float, float, float, float, float,
        float, float, float, float,
    ]:
        (
            sibling_effective,
            parent_raw,
            circuit_raw,
            sibling_raw,
            parent_weight,
            circuit_weight,
            sibling_weight,
        ) = self._pcfg_effective_sibling_probability(
            parent_context_id,
            circuit_context_id,
            sibling_context_id,
            shape_id,
            parent_context=parent_context,
            circuit_context=circuit_context,
            sibling_context=sibling_context,
        )
        history_raw = self._pcfg_history_probability(
            history_context_id,
            sibling_context_id,
            shape_id,
            context=history_context,
            sibling_context=sibling_context,
            parent_context=parent_context,
            circuit_context=circuit_context,
        )
        history_weight = float(
            self.pcfg_history_calibration_stats.get(
                history_context_id, {}).get("weight", 0.0)
        )
        effective = (
            (1.0 - history_weight) * sibling_effective +
            history_weight * history_raw
        )
        return (
            max(0.0, min(1.0, effective)),
            parent_raw,
            circuit_raw,
            sibling_raw,
            history_raw,
            parent_weight,
            circuit_weight,
            sibling_weight,
            history_weight,
        )

    def _pcfg_context_is_active(
        self,
        context: dict[str, Any],
        active_shapes: dict[str, set[str]],
    ) -> bool:
        family_id = str(context.get("family_id", ""))
        family = self.pcfg_families.get(family_id)
        parent_shape_id = str(
            context.get("parent_shape_id", ""))
        slot = context.get("slot")
        if (
            family is None or
            family_id not in active_shapes or
            not isinstance(slot, int) or isinstance(slot, bool)
        ):
            return False
        if not parent_shape_id:
            return slot == -1
        for production in self.cfg_productions.values():
            rhs = production["rhs"]
            if (
                production["parser"] == family["parser"] and
                production["shape_id"] == parent_shape_id and
                0 <= slot < len(rhs) and
                rhs[slot]["symbol"] == family["lhs"] and
                rhs[slot]["state"] == family["state"]
            ):
                return True
        return False

    def _pcfg_circuit_context_is_active(
        self,
        context: dict[str, Any],
        active_shapes: dict[str, set[str]],
    ) -> bool:
        family_id = str(context.get("family_id", ""))
        family = self.pcfg_families.get(family_id)
        parent_shape_id = str(
            context.get("parent_shape_id", ""))
        ancestor_shape_id = str(
            context.get("ancestor_shape_id", ""))
        parent_slot = context.get("parent_slot")
        ancestor_slot = context.get("ancestor_slot")
        if (
            family is None or
            family_id not in active_shapes or
            not parent_shape_id or not ancestor_shape_id or
            not isinstance(parent_slot, int) or
            isinstance(parent_slot, bool) or
            not isinstance(ancestor_slot, int) or
            isinstance(ancestor_slot, bool)
        ):
            return False
        parent_productions = [
            production
            for production in self.cfg_productions.values()
            if (
                production["parser"] == family["parser"] and
                production["shape_id"] == parent_shape_id and
                0 <= parent_slot < len(production["rhs"]) and
                production["rhs"][parent_slot]["symbol"] ==
                family["lhs"] and
                production["rhs"][parent_slot]["state"] ==
                family["state"]
            )
        ]
        for parent in parent_productions:
            for ancestor in self.cfg_productions.values():
                if (
                    ancestor["parser"] == family["parser"] and
                    ancestor["shape_id"] == ancestor_shape_id and
                    0 <= ancestor_slot < len(ancestor["rhs"]) and
                    ancestor["rhs"][ancestor_slot]["symbol"] ==
                    parent["lhs"] and
                    ancestor["rhs"][ancestor_slot]["state"] ==
                    parent["state"]
                ):
                    return True
        return False

    def _pcfg_sibling_context_is_active(
        self,
        context: dict[str, Any],
        active_shapes: dict[str, set[str]],
    ) -> bool:
        family_id = str(context.get("family_id", ""))
        family = self.pcfg_families.get(family_id)
        parent_shape_id = str(
            context.get("parent_shape_id", ""))
        left_shape_id = str(
            context.get("left_sibling_shape_id", ""))
        slot = context.get("slot")
        if (
            family is None or
            family_id not in active_shapes or
            not parent_shape_id or
            not self._valid_digest(left_shape_id) or
            not isinstance(slot, int) or isinstance(slot, bool) or
            not 1 <= slot < 64
        ):
            return False
        for parent in self.cfg_productions.values():
            rhs = parent["rhs"]
            if (
                parent["parser"] != family["parser"] or
                parent["shape_id"] != parent_shape_id or
                not slot < len(rhs) or
                rhs[slot]["symbol"] != family["lhs"] or
                rhs[slot]["state"] != family["state"]
            ):
                continue
            left_spec = rhs[slot - 1]
            for left in self.cfg_productions.values():
                if (
                    left["parser"] == family["parser"] and
                    left["shape_id"] == left_shape_id and
                    left["lhs"] == left_spec["symbol"] and
                    left["state"] == left_spec["state"]
                ):
                    return True
        return False

    def _pcfg_history_context_is_active(
        self,
        context: dict[str, Any],
        active_shapes: dict[str, set[str]],
    ) -> bool:
        family_id = str(context.get("family_id", ""))
        family = self.pcfg_families.get(family_id)
        parent_shape_id = str(
            context.get("parent_shape_id", ""))
        older_shape_id = str(
            context.get("older_sibling_shape_id", ""))
        left_shape_id = str(
            context.get("left_sibling_shape_id", ""))
        slot = context.get("slot")
        if (
            family is None or
            family_id not in active_shapes or
            not self._valid_digest(parent_shape_id) or
            not self._valid_digest(older_shape_id) or
            not self._valid_digest(left_shape_id) or
            not isinstance(slot, int) or isinstance(slot, bool) or
            not 2 <= slot < 64
        ):
            return False
        for parent in self.cfg_productions.values():
            rhs = parent["rhs"]
            if (
                parent["parser"] != family["parser"] or
                parent["shape_id"] != parent_shape_id or
                not slot < len(rhs) or
                rhs[slot]["symbol"] != family["lhs"] or
                rhs[slot]["state"] != family["state"]
            ):
                continue
            expected = (
                (older_shape_id, rhs[slot - 2]),
                (left_shape_id, rhs[slot - 1]),
            )
            if all(any(
                production["parser"] == family["parser"] and
                production["shape_id"] == shape_id and
                production["lhs"] == spec["symbol"] and
                production["state"] == spec["state"]
                for production in self.cfg_productions.values()
            ) for shape_id, spec in expected):
                return True
        return False

    @staticmethod
    def _reconcile_pcfg_evidence(
        counts_by_group: dict[str, dict[str, int]],
        observations: dict[str, list[list[str]]],
    ) -> tuple[
        dict[str, dict[str, int]],
        dict[str, list[list[str]]],
    ]:
        valid_keys = {
            (group_id, shape_id)
            for group_id, counts in counts_by_group.items()
            for shape_id in counts
        }
        while True:
            tallies: dict[tuple[str, str], int] = {}
            for selections in observations.values():
                if any(
                    (str(group_id), str(shape_id)) not in valid_keys
                    for group_id, shape_id in selections
                ):
                    continue
                for group_id, shape_id in selections:
                    key = (str(group_id), str(shape_id))
                    tallies[key] = tallies.get(key, 0) + 1
            retained_keys = {
                key
                for key in valid_keys
                if tallies.get(key, 0) ==
                counts_by_group[key[0]][key[1]]
            }
            if retained_keys == valid_keys:
                break
            valid_keys = retained_keys
        reconciled_counts = {
            group_id: {
                shape_id: count
                for shape_id, count in counts.items()
                if (group_id, shape_id) in valid_keys
            }
            for group_id, counts in counts_by_group.items()
        }
        reconciled_counts = {
            group_id: counts
            for group_id, counts in reconciled_counts.items()
            if counts
        }
        reconciled_observations = {
            fragment_sha256: (
                selections
                if all(
                    (str(group_id), str(shape_id)) in valid_keys
                    for group_id, shape_id in selections
                ) else []
            )
            for fragment_sha256, selections in observations.items()
        }
        return reconciled_counts, reconciled_observations

    def _prune_pcfg_state(self) -> None:
        active_shapes: dict[str, set[str]] = {}
        active_families: dict[str, dict[str, str]] = {}
        for production in self.cfg_productions.values():
            family_id, family = self._pcfg_family(
                str(production["parser"]),
                str(production["lhs"]),
                str(production["state"]),
            )
            active_families[family_id] = family
            active_shapes.setdefault(family_id, set()).add(
                str(production["shape_id"]))
        for family_id in tuple(self.pcfg_families):
            if family_id not in active_families:
                del self.pcfg_families[family_id]
        for family_id, family in active_families.items():
            self.pcfg_families.setdefault(family_id, family)
        for family_id in tuple(self.pcfg_counts):
            if family_id not in active_shapes:
                del self.pcfg_counts[family_id]
                continue
            self.pcfg_counts[family_id] = {
                shape_id: count
                for shape_id, count in self.pcfg_counts[family_id].items()
                if shape_id in active_shapes[family_id]
            }
            if not self.pcfg_counts[family_id]:
                del self.pcfg_counts[family_id]
        for fragment_sha256, selections in self.pcfg_observations.items():
            self.pcfg_observations[fragment_sha256] = [
                [family_id, shape_id]
                for family_id, shape_id in selections
                if shape_id in active_shapes.get(family_id, set())
            ]
        for context_id in tuple(self.pcfg_contexts):
            if not self._pcfg_context_is_active(
                    self.pcfg_contexts[context_id], active_shapes):
                del self.pcfg_contexts[context_id]
                self.pcfg_context_counts.pop(context_id, None)
        for context_id in tuple(self.pcfg_context_counts):
            context = self.pcfg_contexts.get(context_id)
            if context is None:
                del self.pcfg_context_counts[context_id]
                continue
            valid_shapes = active_shapes.get(
                str(context["family_id"]), set())
            self.pcfg_context_counts[context_id] = {
                shape_id: count
                for shape_id, count in
                self.pcfg_context_counts[context_id].items()
                if shape_id in valid_shapes
            }
            if not self.pcfg_context_counts[context_id]:
                del self.pcfg_context_counts[context_id]
        for fragment_sha256, selections in (
                self.pcfg_context_observations.items()):
            self.pcfg_context_observations[fragment_sha256] = [
                [context_id, shape_id]
                for context_id, shape_id in selections
                if (
                    context_id in self.pcfg_contexts and
                    shape_id in active_shapes.get(
                        str(self.pcfg_contexts[
                            context_id]["family_id"]), set())
                )
            ]
        for receipt_id, receipt in tuple(
                self.pcfg_prequential_receipts.items()):
            context_id = str(receipt.get("context_id", ""))
            shape_id = str(receipt.get("shape_id", ""))
            fragment_sha256 = str(
                receipt.get("fragment_sha256", ""))
            context = self.pcfg_contexts.get(context_id)
            if (
                context is None or
                shape_id not in active_shapes.get(
                    str(context["family_id"]), set()) or
                [context_id, shape_id] not in
                self.pcfg_context_observations.get(
                    fragment_sha256, ())
            ):
                del self.pcfg_prequential_receipts[receipt_id]
        for context_id in tuple(self.pcfg_circuit_contexts):
            if not self._pcfg_circuit_context_is_active(
                    self.pcfg_circuit_contexts[context_id], active_shapes):
                del self.pcfg_circuit_contexts[context_id]
                self.pcfg_circuit_counts.pop(context_id, None)
        for context_id in tuple(self.pcfg_circuit_counts):
            context = self.pcfg_circuit_contexts.get(context_id)
            if context is None:
                del self.pcfg_circuit_counts[context_id]
                continue
            valid_shapes = active_shapes.get(
                str(context["family_id"]), set())
            self.pcfg_circuit_counts[context_id] = {
                shape_id: count
                for shape_id, count in
                self.pcfg_circuit_counts[context_id].items()
                if shape_id in valid_shapes
            }
            if not self.pcfg_circuit_counts[context_id]:
                del self.pcfg_circuit_counts[context_id]
        for fragment_sha256, selections in (
                self.pcfg_circuit_observations.items()):
            self.pcfg_circuit_observations[fragment_sha256] = [
                [context_id, shape_id]
                for context_id, shape_id in selections
                if (
                    context_id in self.pcfg_circuit_contexts and
                    shape_id in active_shapes.get(
                        str(self.pcfg_circuit_contexts[
                            context_id]["family_id"]), set())
                )
            ]
        for receipt_id, receipt in tuple(
                self.pcfg_circuit_receipts.items()):
            context_id = str(receipt.get("context_id", ""))
            parent_context_id = str(
                receipt.get("parent_context_id", ""))
            shape_id = str(receipt.get("shape_id", ""))
            fragment_sha256 = str(
                receipt.get("fragment_sha256", ""))
            context = self.pcfg_circuit_contexts.get(context_id)
            if (
                context is None or
                parent_context_id not in self.pcfg_contexts or
                shape_id not in active_shapes.get(
                    str(context["family_id"]), set()) or
                [context_id, shape_id] not in
                self.pcfg_circuit_observations.get(
                    fragment_sha256, ())
            ):
                del self.pcfg_circuit_receipts[receipt_id]
        for context_id in tuple(self.pcfg_sibling_contexts):
            if not self._pcfg_sibling_context_is_active(
                    self.pcfg_sibling_contexts[context_id], active_shapes):
                del self.pcfg_sibling_contexts[context_id]
                self.pcfg_sibling_counts.pop(context_id, None)
        for context_id in tuple(self.pcfg_sibling_counts):
            context = self.pcfg_sibling_contexts.get(context_id)
            if context is None:
                del self.pcfg_sibling_counts[context_id]
                continue
            valid_shapes = active_shapes.get(
                str(context["family_id"]), set())
            self.pcfg_sibling_counts[context_id] = {
                shape_id: count
                for shape_id, count in
                self.pcfg_sibling_counts[context_id].items()
                if shape_id in valid_shapes
            }
            if not self.pcfg_sibling_counts[context_id]:
                del self.pcfg_sibling_counts[context_id]
        for fragment_sha256, selections in (
                self.pcfg_sibling_observations.items()):
            self.pcfg_sibling_observations[fragment_sha256] = [
                [context_id, shape_id]
                for context_id, shape_id in selections
                if (
                    context_id in self.pcfg_sibling_contexts and
                    shape_id in active_shapes.get(
                        str(self.pcfg_sibling_contexts[
                            context_id]["family_id"]), set())
                )
            ]
        for receipt_id, receipt in tuple(
                self.pcfg_sibling_receipts.items()):
            context_id = str(receipt.get("context_id", ""))
            parent_context_id = str(
                receipt.get("parent_context_id", ""))
            circuit_context_id = str(
                receipt.get("circuit_context_id", ""))
            shape_id = str(receipt.get("shape_id", ""))
            fragment_sha256 = str(
                receipt.get("fragment_sha256", ""))
            context = self.pcfg_sibling_contexts.get(context_id)
            if (
                context is None or
                parent_context_id not in self.pcfg_contexts or
                circuit_context_id and
                circuit_context_id not in self.pcfg_circuit_contexts or
                shape_id not in active_shapes.get(
                    str(context["family_id"]), set()) or
                [context_id, shape_id] not in
                self.pcfg_sibling_observations.get(
                    fragment_sha256, ())
            ):
                del self.pcfg_sibling_receipts[receipt_id]
        for context_id in tuple(self.pcfg_history_contexts):
            if not self._pcfg_history_context_is_active(
                    self.pcfg_history_contexts[context_id],
                    active_shapes):
                del self.pcfg_history_contexts[context_id]
                self.pcfg_history_counts.pop(context_id, None)
        for context_id in tuple(self.pcfg_history_counts):
            context = self.pcfg_history_contexts.get(context_id)
            if context is None:
                del self.pcfg_history_counts[context_id]
                continue
            valid_shapes = active_shapes.get(
                str(context["family_id"]), set())
            self.pcfg_history_counts[context_id] = {
                shape_id: count
                for shape_id, count in
                self.pcfg_history_counts[context_id].items()
                if shape_id in valid_shapes
            }
            if not self.pcfg_history_counts[context_id]:
                del self.pcfg_history_counts[context_id]
        for fragment_sha256, selections in (
                self.pcfg_history_observations.items()):
            self.pcfg_history_observations[fragment_sha256] = [
                [context_id, shape_id]
                for context_id, shape_id in selections
                if (
                    context_id in self.pcfg_history_contexts and
                    shape_id in active_shapes.get(
                        str(self.pcfg_history_contexts[
                            context_id]["family_id"]), set())
                )
            ]
        for receipt_id, receipt in tuple(
                self.pcfg_history_receipts.items()):
            context_id = str(receipt.get("context_id", ""))
            parent_context_id = str(
                receipt.get("parent_context_id", ""))
            circuit_context_id = str(
                receipt.get("circuit_context_id", ""))
            sibling_context_id = str(
                receipt.get("sibling_context_id", ""))
            shape_id = str(receipt.get("shape_id", ""))
            fragment_sha256 = str(
                receipt.get("fragment_sha256", ""))
            context = self.pcfg_history_contexts.get(context_id)
            if (
                context is None or
                parent_context_id not in self.pcfg_contexts or
                sibling_context_id not in self.pcfg_sibling_contexts or
                circuit_context_id and
                circuit_context_id not in self.pcfg_circuit_contexts or
                shape_id not in active_shapes.get(
                    str(context["family_id"]), set()) or
                [context_id, shape_id] not in
                self.pcfg_history_observations.get(
                    fragment_sha256, ())
            ):
                del self.pcfg_history_receipts[receipt_id]

    def _observe_pcfg_fragment(
        self,
        fragment_sha256: str,
        parser: str,
        productions: dict[str, dict[str, Any]],
        instances: list[dict[str, Any]],
        packed_edges: dict[str, dict[str, Any]],
        packed_roots: list[str],
    ) -> None:
        if (
            fragment_sha256 in self.pcfg_observations or
            not packed_roots or
            len(self.pcfg_observations) >=
            self.HARD_MAX_PCFG_OBSERVATIONS
        ):
            self._refresh_probabilistic_state()
            return
        primary_instances: dict[str, dict[str, Any]] = {}
        for instance in sorted(
                instances, key=lambda item: item["instance_id"]):
            production = productions.get(
                str(instance.get("production_id", "")))
            node_id = str(instance.get("node_id", ""))
            if (
                node_id and production is not None and
                int(instance.get(
                    "alternative",
                    production.get("alternative", 0),
                )) == 0
            ):
                primary_instances.setdefault(node_id, instance)
        primary_edges: dict[str, list[dict[str, Any]]] = {}
        for edge in packed_edges.values():
            if int(edge["alternative"]) != 0:
                continue
            primary_edges.setdefault(
                str(edge["parent_id"]), []).append(edge)
        for edges in primary_edges.values():
            edges.sort(key=lambda edge: (
                int(edge["slot"]), str(edge["edge_id"])))

        selections: list[list[str]] = []
        observed_families: dict[str, dict[str, str]] = {}
        context_selections: list[
            tuple[str, dict[str, Any], str]
        ] = []
        circuit_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                str,
            ]
        ] = []
        sibling_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                dict[str, Any] | None,
                str,
            ]
        ] = []
        history_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                dict[str, Any] | None,
                dict[str, Any],
                str,
            ]
        ] = []
        pending: list[
            tuple[str, str, int, str, int, tuple[str, ...]]
        ] = [
            (str(packed_roots[0]), "", -1, "", -1, ())
        ]
        selected_nodes: set[str] = set()
        complete = True
        while pending:
            (
                node_id,
                parent_shape_id,
                parent_slot,
                ancestor_shape_id,
                ancestor_slot,
                sibling_history,
            ) = pending.pop(0)
            if node_id in selected_nodes:
                complete = False
                break
            selected_nodes.add(node_id)
            instance = primary_instances.get(node_id)
            if instance is None:
                complete = False
                break
            production = productions.get(
                str(instance["production_id"]))
            if production is None:
                complete = False
                break
            edges = primary_edges.get(node_id, [])
            if (
                [int(edge["slot"]) for edge in edges] !=
                list(range(len(production["rhs"]))) or
                isinstance(instance.get("child_edge_ids"), list) and
                [str(edge["edge_id"]) for edge in edges] !=
                list(instance["child_edge_ids"])
            ):
                complete = False
                break
            family_id, family = self._pcfg_family(
                parser,
                str(production["lhs"]),
                str(production["state"]),
            )
            shape_id = str(production["shape_id"])
            observed_families[family_id] = family
            selections.append([family_id, shape_id])
            context: dict[str, Any] | None = None
            if self.pcfg_context_order >= 1:
                _, context = self._pcfg_context(
                    parser,
                    family_id,
                    parent_shape_id,
                    parent_slot,
                )
                context_selections.append(
                    (node_id, context, shape_id))
            circuit_context = None
            if (
                self.pcfg_context_order >= 2 and
                context is not None and
                parent_shape_id and ancestor_shape_id
            ):
                _, circuit_context = self._pcfg_circuit_context(
                    parser,
                    family_id,
                    parent_shape_id,
                    parent_slot,
                    ancestor_shape_id,
                    ancestor_slot,
                )
                circuit_selections.append((
                    node_id,
                    circuit_context,
                    context,
                    shape_id,
                ))
            sibling_context = None
            if (
                self.pcfg_context_order >= 3 and
                context is not None and
                parent_shape_id and sibling_history
            ):
                _, sibling_context = self._pcfg_sibling_context(
                    parser,
                    family_id,
                    parent_shape_id,
                    parent_slot,
                    sibling_history[-1],
                )
                sibling_selections.append((
                    node_id,
                    sibling_context,
                    context,
                    circuit_context,
                    shape_id,
                ))
            if (
                self.pcfg_context_order >= 4 and
                parent_shape_id and
                len(sibling_history) >= 2 and
                context is not None and
                sibling_context is not None
            ):
                _, history_context = self._pcfg_history_context(
                    parser,
                    family_id,
                    parent_shape_id,
                    parent_slot,
                    sibling_history[-2],
                    sibling_history[-1],
                )
                history_selections.append((
                    node_id,
                    history_context,
                    context,
                    circuit_context,
                    sibling_context,
                    shape_id,
                ))
            previous_child_shape_ids: list[str] = []
            for edge in edges:
                child_id = str(edge["child_id"])
                pending.append((
                    child_id,
                    shape_id,
                    int(edge["slot"]),
                    parent_shape_id,
                    parent_slot,
                    tuple(previous_child_shape_ids[-2:]),
                ))
                child_instance = primary_instances.get(child_id)
                child_production = (
                    productions.get(str(
                        child_instance.get("production_id", "")))
                    if child_instance is not None else None
                )
                if child_production is not None:
                    previous_child_shape_ids.append(
                        str(child_production["shape_id"]))

        if not complete:
            selections.clear()
            context_selections.clear()
            circuit_selections.clear()
            sibling_selections.clear()
            history_selections.clear()
        for family_id in {
                family_id for family_id, _ in selections}:
            self.pcfg_families[
                family_id] = observed_families[family_id]

        retained_context_selections: list[
            tuple[str, dict[str, Any], str]
        ] = []
        for node_id, context, shape_id in context_selections:
            context_id = str(context["context_id"])
            if (
                context_id not in self.pcfg_contexts and
                len(self.pcfg_contexts) >=
                self.HARD_MAX_PCFG_CONTEXTS
            ):
                continue
            self.pcfg_contexts[context_id] = context
            retained_context_selections.append(
                (node_id, context, shape_id))

        retained_parent_contexts = {
            (node_id, str(context["context_id"]))
            for node_id, context, _ in retained_context_selections
        }
        retained_circuit_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                str,
            ]
        ] = []
        for node_id, circuit_context, parent_context, shape_id in (
                circuit_selections):
            context_id = str(circuit_context["context_id"])
            if (
                (node_id, str(parent_context["context_id"])) not in
                retained_parent_contexts or
                context_id not in self.pcfg_circuit_contexts and
                len(self.pcfg_circuit_contexts) >=
                self.HARD_MAX_PCFG_CIRCUIT_CONTEXTS
            ):
                continue
            self.pcfg_circuit_contexts[
                context_id] = circuit_context
            retained_circuit_selections.append((
                node_id,
                circuit_context,
                parent_context,
                shape_id,
            ))

        retained_circuit_contexts = {
            (node_id, str(circuit_context["context_id"])):
                circuit_context
            for (
                node_id,
                circuit_context,
                _,
                _,
            ) in retained_circuit_selections
        }
        retained_sibling_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                dict[str, Any] | None,
                str,
            ]
        ] = []
        for (
            node_id,
            sibling_context,
            parent_context,
            circuit_context,
            shape_id,
        ) in sibling_selections:
            context_id = str(sibling_context["context_id"])
            if (
                (node_id, str(parent_context["context_id"])) not in
                retained_parent_contexts or
                context_id not in self.pcfg_sibling_contexts and
                len(self.pcfg_sibling_contexts) >=
                self.HARD_MAX_PCFG_SIBLING_CONTEXTS
            ):
                continue
            if circuit_context is not None:
                circuit_context = retained_circuit_contexts.get((
                    node_id,
                    str(circuit_context["context_id"]),
                ))
            self.pcfg_sibling_contexts[
                context_id] = sibling_context
            retained_sibling_selections.append((
                node_id,
                sibling_context,
                parent_context,
                circuit_context,
                shape_id,
            ))

        retained_sibling_contexts = {
            (node_id, str(sibling_context["context_id"])):
                sibling_context
            for (
                node_id,
                sibling_context,
                _,
                _,
                _,
            ) in retained_sibling_selections
        }
        retained_history_selections: list[
            tuple[
                str,
                dict[str, Any],
                dict[str, Any],
                dict[str, Any] | None,
                dict[str, Any],
                str,
            ]
        ] = []
        for (
            node_id,
            history_context,
            parent_context,
            circuit_context,
            sibling_context,
            shape_id,
        ) in history_selections:
            context_id = str(history_context["context_id"])
            sibling_context = retained_sibling_contexts.get((
                node_id,
                str(sibling_context["context_id"]),
            ))
            if (
                sibling_context is None or
                context_id not in self.pcfg_history_contexts and
                len(self.pcfg_history_contexts) >=
                self.HARD_MAX_PCFG_HISTORY_CONTEXTS
            ):
                continue
            if circuit_context is not None:
                circuit_context = retained_circuit_contexts.get((
                    node_id,
                    str(circuit_context["context_id"]),
                ))
            self.pcfg_history_contexts[
                context_id] = history_context
            retained_history_selections.append((
                node_id,
                history_context,
                parent_context,
                circuit_context,
                sibling_context,
                shape_id,
            ))

        observation_index = len(self.pcfg_observations)
        self._allocate_pcfg_anytime_contexts(
            [
                (
                    "parent",
                    str(context["context_id"]),
                    observation_index,
                )
                for _, context, _ in retained_context_selections
            ] + [
                (
                    "circuit",
                    str(context["context_id"]),
                    observation_index,
                )
                for _, context, _, _ in
                retained_circuit_selections
            ] + [
                (
                    "sibling",
                    str(context["context_id"]),
                    observation_index,
                )
                for _, context, _, _, _ in
                retained_sibling_selections
            ] + [
                (
                    "history",
                    str(context["context_id"]),
                    observation_index,
                )
                for _, context, _, _, _, _ in
                retained_history_selections
            ]
        )

        for node_id, context, shape_id in (
                retained_context_selections):
            if (
                len(self.pcfg_prequential_receipts) >=
                self.HARD_MAX_PCFG_PREQUENTIAL_RECEIPTS
            ):
                break
            family_id = str(context["family_id"])
            global_counts = self.pcfg_counts.get(family_id, {})
            context_id = str(context["context_id"])
            context_counts = self.pcfg_context_counts.get(
                context_id, {})
            receipt = self._pcfg_prequential_receipt(
                fragment_sha256=fragment_sha256,
                node_id=node_id,
                context=context,
                shape_id=shape_id,
                global_selected_before=max(
                    0, int(global_counts.get(shape_id, 0))),
                global_total_before=sum(
                    max(0, int(count))
                    for count in global_counts.values()),
                known_shapes=max(
                    1, len(self._pcfg_known_shapes(family_id))),
                context_selected_before=max(
                    0, int(context_counts.get(shape_id, 0))),
                context_total_before=sum(
                    max(0, int(count))
                    for count in context_counts.values()),
                observation_index=len(self.pcfg_observations),
            )
            self.pcfg_prequential_receipts[
                str(receipt["receipt_id"])] = receipt

        for (
            node_id,
            circuit_context,
            parent_context,
            shape_id,
        ) in retained_circuit_selections:
            if (
                len(self.pcfg_circuit_receipts) >=
                self.HARD_MAX_PCFG_CIRCUIT_RECEIPTS
            ):
                break
            family_id = str(circuit_context["family_id"])
            parent_context_id = str(parent_context["context_id"])
            circuit_context_id = str(
                circuit_context["context_id"])
            global_counts = self.pcfg_counts.get(family_id, {})
            parent_counts = self.pcfg_context_counts.get(
                parent_context_id, {})
            circuit_counts = self.pcfg_circuit_counts.get(
                circuit_context_id, {})
            receipt = self._pcfg_circuit_receipt(
                fragment_sha256=fragment_sha256,
                node_id=node_id,
                context=circuit_context,
                parent_context_id=parent_context_id,
                shape_id=shape_id,
                global_selected_before=max(
                    0, int(global_counts.get(shape_id, 0))),
                global_total_before=sum(
                    max(0, int(count))
                    for count in global_counts.values()),
                known_shapes=max(
                    1, len(self._pcfg_known_shapes(family_id))),
                parent_selected_before=max(
                    0, int(parent_counts.get(shape_id, 0))),
                parent_total_before=sum(
                    max(0, int(count))
                    for count in parent_counts.values()),
                circuit_selected_before=max(
                    0, int(circuit_counts.get(shape_id, 0))),
                circuit_total_before=sum(
                    max(0, int(count))
                    for count in circuit_counts.values()),
                observation_index=len(self.pcfg_observations),
            )
            self.pcfg_circuit_receipts[
                str(receipt["receipt_id"])] = receipt

        for (
            node_id,
            sibling_context,
            parent_context,
            circuit_context,
            shape_id,
        ) in retained_sibling_selections:
            if (
                len(self.pcfg_sibling_receipts) >=
                self.HARD_MAX_PCFG_SIBLING_RECEIPTS
            ):
                break
            family_id = str(sibling_context["family_id"])
            parent_context_id = str(parent_context["context_id"])
            circuit_context_id = (
                str(circuit_context["context_id"])
                if circuit_context is not None else ""
            )
            sibling_context_id = str(
                sibling_context["context_id"])
            global_counts = self.pcfg_counts.get(family_id, {})
            parent_counts = self.pcfg_context_counts.get(
                parent_context_id, {})
            circuit_counts = self.pcfg_circuit_counts.get(
                circuit_context_id, {})
            sibling_counts = self.pcfg_sibling_counts.get(
                sibling_context_id, {})
            receipt = self._pcfg_sibling_receipt(
                fragment_sha256=fragment_sha256,
                node_id=node_id,
                context=sibling_context,
                parent_context_id=parent_context_id,
                circuit_context_id=circuit_context_id,
                shape_id=shape_id,
                global_selected_before=max(
                    0, int(global_counts.get(shape_id, 0))),
                global_total_before=sum(
                    max(0, int(count))
                    for count in global_counts.values()),
                known_shapes=max(
                    1, len(self._pcfg_known_shapes(family_id))),
                parent_selected_before=max(
                    0, int(parent_counts.get(shape_id, 0))),
                parent_total_before=sum(
                    max(0, int(count))
                    for count in parent_counts.values()),
                circuit_selected_before=max(
                    0, int(circuit_counts.get(shape_id, 0))),
                circuit_total_before=sum(
                    max(0, int(count))
                    for count in circuit_counts.values()),
                sibling_selected_before=max(
                    0, int(sibling_counts.get(shape_id, 0))),
                sibling_total_before=sum(
                    max(0, int(count))
                    for count in sibling_counts.values()),
                observation_index=len(self.pcfg_observations),
            )
            self.pcfg_sibling_receipts[
                str(receipt["receipt_id"])] = receipt

        for (
            node_id,
            history_context,
            parent_context,
            circuit_context,
            sibling_context,
            shape_id,
        ) in retained_history_selections:
            if (
                len(self.pcfg_history_receipts) >=
                self.HARD_MAX_PCFG_HISTORY_RECEIPTS
            ):
                break
            family_id = str(history_context["family_id"])
            parent_context_id = str(parent_context["context_id"])
            circuit_context_id = (
                str(circuit_context["context_id"])
                if circuit_context is not None else ""
            )
            sibling_context_id = str(
                sibling_context["context_id"])
            history_context_id = str(
                history_context["context_id"])
            global_counts = self.pcfg_counts.get(family_id, {})
            parent_counts = self.pcfg_context_counts.get(
                parent_context_id, {})
            circuit_counts = self.pcfg_circuit_counts.get(
                circuit_context_id, {})
            sibling_counts = self.pcfg_sibling_counts.get(
                sibling_context_id, {})
            history_counts = self.pcfg_history_counts.get(
                history_context_id, {})
            receipt = self._pcfg_history_receipt(
                fragment_sha256=fragment_sha256,
                node_id=node_id,
                context=history_context,
                parent_context_id=parent_context_id,
                circuit_context_id=circuit_context_id,
                sibling_context_id=sibling_context_id,
                shape_id=shape_id,
                global_selected_before=max(
                    0, int(global_counts.get(shape_id, 0))),
                global_total_before=sum(
                    max(0, int(count))
                    for count in global_counts.values()),
                known_shapes=max(
                    1, len(self._pcfg_known_shapes(family_id))),
                parent_selected_before=max(
                    0, int(parent_counts.get(shape_id, 0))),
                parent_total_before=sum(
                    max(0, int(count))
                    for count in parent_counts.values()),
                circuit_selected_before=max(
                    0, int(circuit_counts.get(shape_id, 0))),
                circuit_total_before=sum(
                    max(0, int(count))
                    for count in circuit_counts.values()),
                sibling_selected_before=max(
                    0, int(sibling_counts.get(shape_id, 0))),
                sibling_total_before=sum(
                    max(0, int(count))
                    for count in sibling_counts.values()),
                history_selected_before=max(
                    0, int(history_counts.get(shape_id, 0))),
                history_total_before=sum(
                    max(0, int(count))
                    for count in history_counts.values()),
                observation_index=len(self.pcfg_observations),
            )
            self.pcfg_history_receipts[
                str(receipt["receipt_id"])] = receipt

        for family_id, shape_id in selections:
            counts = self.pcfg_counts.setdefault(family_id, {})
            counts[shape_id] = min(
                (1 << 31) - 1,
                max(0, int(counts.get(shape_id, 0))) + 1,
            )
        self.pcfg_observations[fragment_sha256] = sorted(selections)

        retained_context_observations: list[list[str]] = []
        for _, context, shape_id in retained_context_selections:
            context_id = str(context["context_id"])
            counts = self.pcfg_context_counts.setdefault(
                context_id, {})
            counts[shape_id] = min(
                (1 << 31) - 1,
                max(0, int(counts.get(shape_id, 0))) + 1,
            )
            retained_context_observations.append(
                [context_id, shape_id])
        self.pcfg_context_observations[
            fragment_sha256] = sorted(
                retained_context_observations)
        retained_circuit_observations: list[list[str]] = []
        for _, circuit_context, _, shape_id in (
                retained_circuit_selections):
            context_id = str(circuit_context["context_id"])
            counts = self.pcfg_circuit_counts.setdefault(
                context_id, {})
            counts[shape_id] = min(
                (1 << 31) - 1,
                max(0, int(counts.get(shape_id, 0))) + 1,
            )
            retained_circuit_observations.append(
                [context_id, shape_id])
        self.pcfg_circuit_observations[
            fragment_sha256] = sorted(
                retained_circuit_observations)
        retained_sibling_observations: list[list[str]] = []
        for _, sibling_context, _, _, shape_id in (
                retained_sibling_selections):
            context_id = str(sibling_context["context_id"])
            counts = self.pcfg_sibling_counts.setdefault(
                context_id, {})
            counts[shape_id] = min(
                (1 << 31) - 1,
                max(0, int(counts.get(shape_id, 0))) + 1,
            )
            retained_sibling_observations.append(
                [context_id, shape_id])
        self.pcfg_sibling_observations[
            fragment_sha256] = sorted(
                retained_sibling_observations)
        retained_history_observations: list[list[str]] = []
        for _, history_context, _, _, _, shape_id in (
                retained_history_selections):
            context_id = str(history_context["context_id"])
            counts = self.pcfg_history_counts.setdefault(
                context_id, {})
            counts[shape_id] = min(
                (1 << 31) - 1,
                max(0, int(counts.get(shape_id, 0))) + 1,
            )
            retained_history_observations.append(
                [context_id, shape_id])
        self.pcfg_history_observations[
            fragment_sha256] = sorted(
                retained_history_observations)
        self._refresh_probabilistic_state()

    def _refresh_probabilistic_state(self) -> None:
        self._prune_pcfg_state()
        if self.pcfg_context_order >= 1:
            self._refresh_pcfg_context_calibration()
        else:
            self.pcfg_context_calibration_stats.clear()
            self.pcfg_adaptive_certificates.clear()
            self.pcfg_anytime_certificates.clear()
        if self.pcfg_context_order >= 2:
            self._refresh_pcfg_circuit_calibration()
        else:
            self.pcfg_circuit_calibration_stats.clear()
            self.pcfg_circuit_certificates.clear()
            self.pcfg_circuit_anytime_certificates.clear()
        if self.pcfg_context_order >= 3:
            self._refresh_pcfg_sibling_calibration()
        else:
            self.pcfg_sibling_calibration_stats.clear()
            self.pcfg_sibling_certificates.clear()
            self.pcfg_sibling_anytime_certificates.clear()
        if self.pcfg_context_order >= 4:
            self._refresh_pcfg_history_calibration()
        else:
            self.pcfg_history_calibration_stats.clear()
            self.pcfg_history_certificates.clear()
            self.pcfg_history_anytime_certificates.clear()
        self._refresh_pcfg_global_anytime_certificates()
        self.packed_inside.clear()
        self.packed_outside.clear()
        self.packed_alternative_probabilities.clear()
        self.packed_context_inside.clear()
        self.packed_context_outside.clear()
        self.packed_context_alternative_probabilities.clear()
        instances_by_node: dict[str, list[dict[str, Any]]] = {}
        for instance in self.ect_instances.values():
            node_id = str(instance.get("node_id", ""))
            if node_id:
                instances_by_node.setdefault(node_id, []).append(instance)
        edge_groups: dict[
            str, dict[int, list[dict[str, Any]]]
        ] = {}
        for edge in self.packed_edges.values():
            edge_groups.setdefault(
                str(edge["parent_id"]), {}
            ).setdefault(int(edge["alternative"]), []).append(edge)
        for groups in edge_groups.values():
            for alternative in groups:
                groups[alternative].sort(key=lambda edge: (
                    int(edge["slot"]), str(edge["edge_id"])))
        alternatives_by_node: dict[
            str, list[dict[str, Any]]
        ] = {}
        for node_id, instances in instances_by_node.items():
            node = self.packed_nodes.get(node_id)
            if node is None:
                continue
            alternatives: dict[str, dict[str, Any]] = {}
            for instance in sorted(
                    instances, key=lambda item: item["instance_id"]):
                production = self.cfg_productions.get(
                    str(instance.get("production_id", "")))
                if (
                    production is None or
                    production["parser"] != node["parser"] or
                    [production["lhs"], production["state"]] !=
                    [node["symbol"], node["state"]]
                ):
                    continue
                family_id, family = self._pcfg_family(
                    str(production["parser"]),
                    str(production["lhs"]),
                    str(production["state"]),
                )
                self.pcfg_families.setdefault(family_id, family)
                compatible_groups: dict[
                    tuple[str, ...], tuple[int, list[dict[str, Any]]]
                ] = {}
                if production["rhs"]:
                    for ordinal, edges in edge_groups.get(
                            node_id, {}).items():
                        if [
                            int(edge["slot"]) for edge in edges
                        ] != list(range(len(production["rhs"]))):
                            continue
                        children = [
                            self.packed_nodes.get(
                                str(edge["child_id"]))
                            for edge in edges
                        ]
                        if any(child is None for child in children):
                            continue
                        if any(
                            [
                                child_spec["symbol"],
                                child_spec["state"],
                                bool(child_spec["recursive"]),
                            ] != [
                                child["symbol"],
                                child["state"],
                                child["symbol"] == production["lhs"],
                            ]
                            for child_spec, child in zip(
                                production["rhs"], children)
                        ):
                            continue
                        child_ids = tuple(
                            str(edge["child_id"]) for edge in edges)
                        previous = compatible_groups.get(child_ids)
                        if previous is None or ordinal < previous[0]:
                            compatible_groups[child_ids] = (
                                ordinal, edges)
                else:
                    compatible_groups[()] = (
                        int(instance.get(
                            "alternative",
                            production.get("alternative", 0),
                        )),
                        [],
                    )
                for child_ids, (ordinal, edges) in sorted(
                        compatible_groups.items()):
                    alternative_id = hashlib.sha256(json.dumps({
                        "schema":
                            "symcc-parser-packed-alternative-probability-v2",
                        "node_id": node_id,
                        "production_id": production["id"],
                        "children": list(child_ids),
                    }, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8")).hexdigest()
                    alternatives.setdefault(alternative_id, {
                        "alternative_id": alternative_id,
                        "node_id": node_id,
                        "production_id": str(production["id"]),
                        "shape_id": str(production["shape_id"]),
                        "family_id": family_id,
                        "alternative": ordinal,
                        "edge_ids": [
                            str(edge["edge_id"]) for edge in edges],
                        "children": list(child_ids),
                        "posterior": self._pcfg_probability(
                            family_id, str(production["shape_id"])),
                    })
            if alternatives:
                alternatives_by_node[node_id] = [
                    alternatives[key] for key in sorted(alternatives)]

        for node in sorted(
                self.packed_nodes.values(),
                key=lambda item: int(item["order"]),
                reverse=True):
            node_id = str(node["node_id"])
            total = 0.0
            for alternative in alternatives_by_node.get(node_id, ()):
                child_mass = 1.0
                valid = True
                for child_id in alternative["children"]:
                    if child_id not in self.packed_inside:
                        valid = False
                        break
                    child_mass *= self.packed_inside[child_id]
                if not valid:
                    continue
                inside_mass = max(
                    0.0,
                    min(1.0, float(alternative["posterior"])) *
                    child_mass,
                )
                alternative["inside_mass"] = inside_mass
                total += inside_mass
            if total > 0.0:
                self.packed_inside[node_id] = min(1.0, total)

        incoming = {
            str(edge["child_id"])
            for edge in self.packed_edges.values()
        }
        for node_id in self.packed_inside:
            if (
                node_id in self.packed_root_ids or
                not self.packed_root_ids and node_id not in incoming
            ):
                self.packed_outside[node_id] = 1.0
        for node in sorted(
                self.packed_nodes.values(),
                key=lambda item: int(item["order"])):
            node_id = str(node["node_id"])
            outside = self.packed_outside.get(node_id, 0.0)
            for alternative in alternatives_by_node.get(node_id, ()):
                inside_mass = float(alternative.get("inside_mass", 0.0))
                alternative["outside_mass"] = min(
                    1.0, outside * inside_mass)
                children = alternative["children"]
                for slot, child_id in enumerate(children):
                    contribution = (
                        outside * float(alternative["posterior"])
                    )
                    for sibling_slot, sibling_id in enumerate(children):
                        if sibling_slot != slot:
                            contribution *= self.packed_inside.get(
                                sibling_id, 0.0)
                    self.packed_outside[child_id] = min(
                        1.0,
                        self.packed_outside.get(child_id, 0.0) +
                        contribution,
                    )
                self.packed_alternative_probabilities[
                    alternative["alternative_id"]] = alternative
        if self.pcfg_context_order >= 1:
            self._refresh_contextual_probabilistic_state(
                alternatives_by_node)

    def _refresh_contextual_probabilistic_state(
        self,
        alternatives_by_node: dict[str, list[dict[str, Any]]],
    ) -> None:
        incoming = {
            str(edge["child_id"])
            for edge in self.packed_edges.values()
        }
        root_ids = sorted(
            node_id
            for node_id in alternatives_by_node
            if (
                node_id in self.packed_root_ids or
                not self.packed_root_ids and node_id not in incoming
            )
        )
        states_by_node: dict[
            str, dict[str, dict[str, Any]]
        ] = {}
        context_states = 0

        def add_state(
            node_id: str,
            parent_shape_id: str,
            slot: int,
            ancestor_shape_id: str = "",
            ancestor_slot: int = -1,
            left_sibling_shape_id: str = "",
            older_sibling_shape_id: str = "",
        ) -> dict[str, Any] | None:
            nonlocal context_states
            node = self.packed_nodes.get(node_id)
            if node is None:
                return None
            family_id, family = self._pcfg_family(
                str(node["parser"]),
                str(node["symbol"]),
                str(node["state"]),
            )
            self.pcfg_families.setdefault(family_id, family)
            context_id, context = self._pcfg_context(
                str(node["parser"]),
                family_id,
                parent_shape_id,
                slot,
            )
            circuit_context: dict[str, Any] | None = None
            circuit_context_id = ""
            if (
                self.pcfg_context_order >= 2 and
                parent_shape_id and ancestor_shape_id
            ):
                (
                    circuit_context_id,
                    circuit_context,
                ) = self._pcfg_circuit_context(
                    str(node["parser"]),
                    family_id,
                    parent_shape_id,
                    slot,
                    ancestor_shape_id,
                    ancestor_slot,
                )
            sibling_context: dict[str, Any] | None = None
            sibling_context_id = ""
            if (
                self.pcfg_context_order >= 3 and
                parent_shape_id and left_sibling_shape_id
            ):
                (
                    sibling_context_id,
                    sibling_context,
                ) = self._pcfg_sibling_context(
                    str(node["parser"]),
                    family_id,
                    parent_shape_id,
                    slot,
                    left_sibling_shape_id,
                )
            history_context: dict[str, Any] | None = None
            history_context_id = ""
            if (
                self.pcfg_context_order >= 4 and
                parent_shape_id and
                older_sibling_shape_id and
                left_sibling_shape_id
            ):
                (
                    history_context_id,
                    history_context,
                ) = self._pcfg_history_context(
                    str(node["parser"]),
                    family_id,
                    parent_shape_id,
                    slot,
                    older_sibling_shape_id,
                    left_sibling_shape_id,
                )
            state_id = self._pcfg_context_state_id(
                node_id,
                context_id,
                circuit_context_id,
                sibling_context_id,
                history_context_id,
            )
            node_states = states_by_node.setdefault(node_id, {})
            existing = node_states.get(state_id)
            if existing is not None:
                return existing
            if context_states >= self.HARD_MAX_PCFG_CONTEXT_STATES:
                return None
            state = {
                "state_id": state_id,
                "context_id": context_id,
                "context": context,
                "circuit_context_id": circuit_context_id,
                "circuit_context": circuit_context,
                "sibling_context_id": sibling_context_id,
                "sibling_context": sibling_context,
                "history_context_id": history_context_id,
                "history_context": history_context,
            }
            node_states[state_id] = state
            context_states += 1
            return state

        for root_id in root_ids:
            add_state(root_id, "", -1)
        ordered_nodes = sorted(
            self.packed_nodes.values(),
            key=lambda item: (
                int(item["order"]), str(item["node_id"])),
        )
        for node in ordered_nodes:
            node_id = str(node["node_id"])
            if node_id not in states_by_node:
                continue
            for state in tuple(states_by_node[node_id].values()):
                parent_context = state["context"]
                for alternative in alternatives_by_node.get(node_id, ()):
                    children = alternative["children"]
                    for slot, child_id in enumerate(children):
                        if self.pcfg_context_order < 3 or slot == 0:
                            histories = [()]
                        elif self.pcfg_context_order == 3 or slot == 1:
                            histories = [
                                (shape_id,)
                                for shape_id in sorted({
                                    str(left_alternative["shape_id"])
                                    for left_alternative in
                                    alternatives_by_node.get(
                                        str(children[slot - 1]), ())
                                })
                            ]
                        else:
                            older_shapes = sorted({
                                str(older_alternative["shape_id"])
                                for older_alternative in
                                alternatives_by_node.get(
                                    str(children[slot - 2]), ())
                            })
                            left_shapes = sorted({
                                str(left_alternative["shape_id"])
                                for left_alternative in
                                alternatives_by_node.get(
                                    str(children[slot - 1]), ())
                            })
                            histories = [
                                (older_shape_id, left_shape_id)
                                for older_shape_id in older_shapes
                                for left_shape_id in left_shapes
                            ]
                        for history in histories:
                            add_state(
                                str(child_id),
                                str(alternative["shape_id"]),
                                slot,
                                str(parent_context["parent_shape_id"]),
                                int(parent_context["slot"]),
                                history[-1] if history else "",
                                history[-2]
                                if len(history) >= 2 else "",
                            )

        contextual_alternatives: dict[
            str, list[dict[str, Any]]
        ] = {}
        for node in reversed(ordered_nodes):
            node_id = str(node["node_id"])
            for state_id, state in sorted(
                    states_by_node.get(node_id, {}).items()):
                context_id = str(state["context_id"])
                context = state["context"]
                circuit_context_id = str(
                    state["circuit_context_id"])
                circuit_context = state["circuit_context"]
                sibling_context_id = str(
                    state["sibling_context_id"])
                sibling_context = state["sibling_context"]
                history_context_id = str(
                    state["history_context_id"])
                history_context = state["history_context"]
                total = 0.0
                state_alternatives: list[dict[str, Any]] = []
                for alternative in alternatives_by_node.get(node_id, ()):
                    child_state_ids: set[str] = set()
                    child_factor_steps: list[
                        dict[str, Any]
                    ] = []
                    frontier: dict[tuple[str, ...], float] = {
                        (): 1.0
                    }
                    valid = True
                    for slot, child_id in enumerate(
                            alternative["children"]):
                        transitions: list[dict[str, Any]] = []
                        next_frontier: dict[
                            tuple[str, ...], float
                        ] = {}
                        for sibling_history, prefix_mass in sorted(
                                frontier.items()):
                            conditioned_left_shape = (
                                sibling_history[-1]
                                if sibling_history else "")
                            conditioned_older_shape = (
                                sibling_history[-2]
                                if len(sibling_history) >= 2 else "")
                            child_state = add_state(
                                str(child_id),
                                str(alternative["shape_id"]),
                                slot,
                                str(context["parent_shape_id"]),
                                int(context["slot"]),
                                conditioned_left_shape,
                                conditioned_older_shape,
                            )
                            if child_state is None:
                                continue
                            child_state_id = str(
                                child_state["state_id"])
                            child_alternatives = (
                                contextual_alternatives.get(
                                    child_state_id, ())
                            )
                            for child_alternative in child_alternatives:
                                child_inside = float(
                                    child_alternative.get(
                                        "inside_mass", 0.0))
                                if child_inside <= 0.0:
                                    continue
                                child_shape_id = str(
                                    child_alternative["shape_id"])
                                history_width = (
                                    2 if self.pcfg_context_order >= 4
                                    else 1
                                    if self.pcfg_context_order >= 3
                                    else 0
                                )
                                next_history = (
                                    sibling_history +
                                    (child_shape_id,)
                                )[-history_width:] if history_width else ()
                                transitions.append({
                                    "previous_sibling_shape_id":
                                        conditioned_left_shape,
                                    "previous_sibling_shape_ids":
                                        list(sibling_history),
                                    "selected_shape_id":
                                        child_shape_id,
                                    "next_sibling_shape_ids":
                                        list(next_history),
                                    "child_context_state_id":
                                        child_state_id,
                                    "child_artifact_id": str(
                                        child_alternative[
                                            "artifact_id"]),
                                    "inside_mass": child_inside,
                                })
                                next_frontier[next_history] = (
                                    next_frontier.get(
                                        next_history, 0.0) +
                                    prefix_mass * child_inside
                                )
                                child_state_ids.add(child_state_id)
                        if not next_frontier:
                            valid = False
                            break
                        child_factor_steps.append({
                            "slot": slot,
                            "child_id": str(child_id),
                            "transitions": transitions,
                        })
                        frontier = next_frontier
                    if not valid:
                        continue
                    child_mass = sum(frontier.values())
                    circuit_raw_posterior = 0.0
                    circuit_calibration_weight = 0.0
                    sibling_raw_posterior = 0.0
                    sibling_calibration_weight = 0.0
                    history_raw_posterior = 0.0
                    history_calibration_weight = 0.0
                    if (
                        history_context_id and
                        history_context is not None and
                        sibling_context_id and
                        sibling_context is not None
                    ):
                        (
                            posterior,
                            raw_posterior,
                            circuit_raw_posterior,
                            sibling_raw_posterior,
                            history_raw_posterior,
                            calibration_weight,
                            circuit_calibration_weight,
                            sibling_calibration_weight,
                            history_calibration_weight,
                        ) = self._pcfg_effective_history_probability(
                            context_id,
                            circuit_context_id,
                            sibling_context_id,
                            history_context_id,
                            str(alternative["shape_id"]),
                            parent_context=context,
                            circuit_context=circuit_context,
                            sibling_context=sibling_context,
                            history_context=history_context,
                        )
                    elif (
                        sibling_context_id and
                        sibling_context is not None
                    ):
                        (
                            posterior,
                            raw_posterior,
                            circuit_raw_posterior,
                            sibling_raw_posterior,
                            calibration_weight,
                            circuit_calibration_weight,
                            sibling_calibration_weight,
                        ) = self._pcfg_effective_sibling_probability(
                            context_id,
                            circuit_context_id,
                            sibling_context_id,
                            str(alternative["shape_id"]),
                            parent_context=context,
                            circuit_context=circuit_context,
                            sibling_context=sibling_context,
                        )
                    elif (
                        circuit_context_id and
                        circuit_context is not None
                    ):
                        (
                            posterior,
                            raw_posterior,
                            circuit_raw_posterior,
                            calibration_weight,
                            circuit_calibration_weight,
                        ) = self._pcfg_effective_circuit_probability(
                            context_id,
                            circuit_context_id,
                            str(alternative["shape_id"]),
                            parent_context=context,
                            circuit_context=circuit_context,
                        )
                    else:
                        (
                            posterior,
                            raw_posterior,
                            calibration_weight,
                        ) = self._pcfg_effective_context_probability(
                            context_id,
                            str(alternative["shape_id"]),
                            context=context,
                        )
                    inside_mass = max(
                        0.0, min(1.0, posterior * child_mass))
                    artifact_id = hashlib.sha256(json.dumps({
                        "schema":
                            "symcc-parser-context-alternative-v4",
                        "context_state_id": state_id,
                        "alternative_id":
                            alternative["alternative_id"],
                    }, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8")).hexdigest()
                    state_alternatives.append({
                        "artifact_id": artifact_id,
                        "context_state_id": state_id,
                        "context_id": context_id,
                        "circuit_context_id":
                            circuit_context_id,
                        "sibling_context_id":
                            sibling_context_id,
                        "history_context_id":
                            history_context_id,
                        "node_id": node_id,
                        "alternative_id":
                            alternative["alternative_id"],
                        "production_id":
                            alternative["production_id"],
                        "shape_id": alternative["shape_id"],
                        "family_id": alternative["family_id"],
                        "children": list(alternative["children"]),
                        "child_context_state_ids":
                            sorted(child_state_ids),
                        "child_factor_steps":
                            child_factor_steps,
                        "posterior": posterior,
                        "raw_posterior": raw_posterior,
                        "circuit_raw_posterior":
                            circuit_raw_posterior,
                        "sibling_raw_posterior":
                            sibling_raw_posterior,
                        "history_raw_posterior":
                            history_raw_posterior,
                        "calibration_weight":
                            calibration_weight,
                        "circuit_calibration_weight":
                            circuit_calibration_weight,
                        "sibling_calibration_weight":
                            sibling_calibration_weight,
                        "history_calibration_weight":
                            history_calibration_weight,
                        "inside_mass": inside_mass,
                    })
                    total += inside_mass
                if total > 0.0:
                    self.packed_context_inside[state_id] = min(
                        1.0, total)
                    contextual_alternatives[
                        state_id] = state_alternatives

        for root_id in root_ids:
            root_state = add_state(root_id, "", -1)
            if root_state is None:
                continue
            state_id = str(root_state["state_id"])
            if state_id in self.packed_context_inside:
                self.packed_context_outside[state_id] = 1.0
        artifact_external: dict[str, float] = {}
        for root_id in root_ids:
            root_state = add_state(root_id, "", -1)
            if root_state is None:
                continue
            for alternative in contextual_alternatives.get(
                    str(root_state["state_id"]), ()):
                artifact_external[
                    str(alternative["artifact_id"])] = 1.0
        for node in ordered_nodes:
            node_id = str(node["node_id"])
            for state_id in sorted(
                    states_by_node.get(node_id, {})):
                for alternative in contextual_alternatives.get(
                        state_id, ()):
                    artifact_id = str(alternative["artifact_id"])
                    outside = artifact_external.get(
                        artifact_id, 0.0)
                    alternative["outside_mass"] = min(
                        1.0,
                        outside * float(
                            alternative["inside_mass"]),
                    )
                    factor_steps = alternative[
                        "child_factor_steps"]
                    forward: list[
                        dict[tuple[str, ...], float]
                    ] = [
                        {(): 1.0}
                    ]
                    for step in factor_steps:
                        next_forward: dict[
                            tuple[str, ...], float
                        ] = {}
                        for transition in step["transitions"]:
                            previous_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "previous_sibling_shape_ids"]
                            )
                            next_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "next_sibling_shape_ids"]
                            )
                            prefix_mass = forward[-1].get(
                                previous_history, 0.0)
                            next_forward[next_history] = (
                                next_forward.get(
                                    next_history, 0.0) +
                                prefix_mass *
                                float(transition["inside_mass"])
                            )
                        forward.append(next_forward)
                    backward: list[
                        dict[tuple[str, ...], float]
                    ] = [
                        {} for _ in range(len(factor_steps) + 1)
                    ]
                    if factor_steps:
                        backward[-1] = {
                            tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "next_sibling_shape_ids"]
                            ): 1.0
                            for transition in
                            factor_steps[-1]["transitions"]
                        }
                    for slot in range(
                            len(factor_steps) - 1, -1, -1):
                        step = factor_steps[slot]
                        for transition in step["transitions"]:
                            previous_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "previous_sibling_shape_ids"]
                            )
                            next_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "next_sibling_shape_ids"]
                            )
                            backward[slot][previous_history] = (
                                backward[slot].get(
                                    previous_history, 0.0) +
                                float(transition["inside_mass"]) *
                                backward[slot + 1].get(
                                    next_history, 0.0)
                            )
                    for slot, step in enumerate(factor_steps):
                        for transition in step["transitions"]:
                            previous_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "previous_sibling_shape_ids"]
                            )
                            next_history = tuple(
                                str(shape_id)
                                for shape_id in transition[
                                    "next_sibling_shape_ids"]
                            )
                            child_artifact_id = str(
                                transition["child_artifact_id"])
                            child_state_id = str(
                                transition[
                                    "child_context_state_id"])
                            contribution = (
                                outside *
                                float(alternative["posterior"]) *
                                forward[slot].get(
                                    previous_history, 0.0) *
                                backward[slot + 1].get(
                                    next_history, 0.0)
                            )
                            artifact_external[
                                child_artifact_id] = min(
                                    1.0,
                                    artifact_external.get(
                                        child_artifact_id, 0.0) +
                                    contribution,
                                )
                            self.packed_context_outside[
                                child_state_id] = min(
                                    1.0,
                                    self.packed_context_outside.get(
                                        child_state_id, 0.0) +
                                    contribution,
                                )
                    self.packed_context_alternative_probabilities[
                        artifact_id] = alternative

    def _pcfg_shape_statistics(
        self,
        shape_id: str,
    ) -> tuple[float, int, float]:
        family_ids = {
            family_id
            for family_id, counts in self.pcfg_counts.items()
            if shape_id in counts
        }
        for production in self.cfg_productions.values():
            if production["shape_id"] != shape_id:
                continue
            family_id, _ = self._pcfg_family(
                str(production["parser"]),
                str(production["lhs"]),
                str(production["state"]),
            )
            family_ids.add(family_id)
        probabilities: list[float] = []
        count = 0
        for family_id in sorted(family_ids):
            probabilities.append(
                self._pcfg_probability(family_id, shape_id))
            count += max(
                0, int(self.pcfg_counts.get(
                    family_id, {}).get(shape_id, 0)))
        probabilities.extend(
            float(alternative["posterior"])
            for alternative in
            self.packed_context_alternative_probabilities.values()
            if alternative.get("shape_id") == shape_id
        )
        influence = max(
            itertools.chain(
                (
                float(alternative.get("outside_mass", 0.0))
                for alternative in
                self.packed_alternative_probabilities.values()
                if alternative.get("shape_id") == shape_id
                ),
                (
                    float(alternative.get("outside_mass", 0.0))
                    for alternative in
                    self.packed_context_alternative_probabilities.values()
                    if alternative.get("shape_id") == shape_id
                ),
            ),
            default=0.0,
        )
        return (
            max(probabilities, default=0.5),
            count,
            max(0.0, min(1.0, influence)),
        )

    def _drop_cfg_production(self, production_id: str) -> None:
        self.cfg_productions.pop(production_id, None)
        for instance_id, instance in tuple(self.ect_instances.items()):
            if instance.get("production_id") == production_id:
                self._drop_ect_instance(
                    instance_id, refresh_derived=False)
        for cycle_id, cycle in tuple(self.cfg_cycles.items()):
            if cycle.get("production_id") != production_id:
                continue
            self.cfg_cycles.pop(cycle_id, None)
            for rule_id in tuple(self.recursive_rule_cycles):
                self.recursive_rule_cycles[rule_id].discard(cycle_id)
                if not self.recursive_rule_cycles[rule_id]:
                    del self.recursive_rule_cycles[rule_id]
        for alias_key, alias_production in tuple(
                self.parser_production_aliases.items()):
            if alias_production == production_id:
                del self.parser_production_aliases[alias_key]
        if self.nullable_rules:
            self._refresh_nullable_state()
        if self.sync_transactions:
            self._refresh_synchronized_state()
        if self.pcfg_families or self.packed_inside:
            self._refresh_probabilistic_state()

    def _ingest_cfg_fragment(
        self,
        fragment_json: str,
        fragment_sha256: str,
        *,
        context_id: str,
        source_context_id: str,
        candidate_context_id: str = "",
    ) -> int:
        encoded = str(fragment_json).encode("utf-8")
        if (
            not encoded or len(encoded) > 65536 or
            not self._valid_digest(fragment_sha256) or
            hashlib.sha256(encoded).hexdigest() != fragment_sha256
        ):
            return 0
        try:
            raw = json.loads(encoded)
        except (ValueError, TypeError):
            return 0
        if (
            not isinstance(raw, dict) or
            raw.get("schema") not in {
                "symcc-parser-cfg-fragment-v1",
                "symcc-parser-cfg-fragment-v2",
                "symcc-parser-cfg-fragment-v3",
                "symcc-parser-cfg-fragment-v4",
                "symcc-parser-cfg-fragment-v5",
            } or
            self._cfg_label(raw.get("parser")) is None or
            raw.get("context_id") != context_id or
            not isinstance(raw.get("productions"), list) or
            not 1 <= len(raw["productions"]) <= 32 or
            not isinstance(raw.get("cycles"), list) or
            len(raw["cycles"]) > 16
        ):
            return 0
        fragment_v5 = raw["schema"] == "symcc-parser-cfg-fragment-v5"
        fragment_v4 = raw["schema"] in {
            "symcc-parser-cfg-fragment-v4",
            "symcc-parser-cfg-fragment-v5",
        }
        fragment_v3 = raw["schema"] in {
            "symcc-parser-cfg-fragment-v3",
            "symcc-parser-cfg-fragment-v4",
            "symcc-parser-cfg-fragment-v5",
        }
        fragment_has_instances = raw["schema"] in {
            "symcc-parser-cfg-fragment-v2",
            "symcc-parser-cfg-fragment-v3",
            "symcc-parser-cfg-fragment-v4",
            "symcc-parser-cfg-fragment-v5",
        }
        if fragment_has_instances and (
            not isinstance(raw.get("instances"), list) or
            len(raw["instances"]) > 32
        ):
            return 0
        parser = str(raw["parser"])
        productions: dict[str, dict[str, Any]] = {}
        for item in raw["productions"]:
            if (
                not isinstance(item, dict) or
                not self._valid_digest(str(item.get("id", ""))) or
                self._cfg_label(item.get("lhs")) is None or
                self._cfg_label(
                    item.get("state"), allow_empty=True) is None or
                not isinstance(item.get("rhs"), list) or
                len(item["rhs"]) > 64 or
                not isinstance(item.get("terminal_gaps"), list) or
                len(item["terminal_gaps"]) != len(item["rhs"]) + 1 or
                any(not self._valid_digest(str(gap))
                    for gap in item["terminal_gaps"])
            ):
                return 0
            rhs: list[dict[str, Any]] = []
            for child in item["rhs"]:
                if (
                    not isinstance(child, dict) or
                    self._cfg_label(child.get("symbol")) is None or
                    self._cfg_label(
                        child.get("state"), allow_empty=True) is None or
                    not isinstance(child.get("recursive"), bool)
                ):
                    return 0
                rhs.append({
                    "symbol": str(child["symbol"]),
                    "state": str(child["state"]),
                    "recursive": bool(child["recursive"]),
                })
            epsilon = item.get("epsilon", False)
            alternative = item.get("alternative", 0)
            if fragment_v3 and (
                item.get("schema") != "symcc-parser-production-v2" or
                not isinstance(epsilon, bool) or
                not isinstance(alternative, int) or
                isinstance(alternative, bool) or
                not 0 <= alternative < 8
            ):
                return 0
            if not fragment_v3 and (
                "epsilon" in item or
                "alternative" in item and item.get("alternative") != 0
            ):
                return 0
            epsilon = bool(epsilon) if fragment_v3 else False
            alternative = int(alternative) if fragment_v3 else 0
            empty_digest = hashlib.sha256(b"").hexdigest()
            if epsilon and (
                rhs or
                [str(gap) for gap in item["terminal_gaps"]] != [empty_digest]
            ):
                return 0
            production_schema = (
                "symcc-parser-production-v2"
                if fragment_v3 else
                "symcc-parser-production-v1"
            )
            shape_schema = (
                "symcc-parser-production-shape-v2"
                if fragment_v3 else
                "symcc-parser-production-shape-v1"
            )
            core = {
                "schema": production_schema,
                "parser": parser,
                "lhs": str(item["lhs"]),
                "state": str(item["state"]),
                "rhs": rhs,
                "terminal_gaps": [
                    str(gap) for gap in item["terminal_gaps"]],
            }
            if fragment_v3:
                core["epsilon"] = epsilon
            production_id = hashlib.sha256(json.dumps(
                core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if production_id != item["id"] or production_id in productions:
                return 0
            shape_core = {
                "schema": shape_schema,
                "parser": parser,
                "lhs": core["lhs"],
                "state": core["state"],
                "rhs": rhs,
            }
            if fragment_v3:
                shape_core["epsilon"] = epsilon
            shape_id = hashlib.sha256(json.dumps(
                shape_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if fragment_has_instances and item.get("shape_id") != shape_id:
                return 0
            productions[production_id] = {
                "id": production_id,
                "shape_id": shape_id,
                "parser": parser,
                "lhs": core["lhs"],
                "state": core["state"],
                "rhs": rhs,
                "terminal_gaps": core["terminal_gaps"],
                "schema": production_schema,
                "epsilon": epsilon,
                "alternative": alternative,
            }

        packed_nodes: dict[str, dict[str, Any]] = {}
        packed_edges: dict[str, dict[str, Any]] = {}
        packed_roots: list[str] = []
        selected_node_path: list[str] = []
        if fragment_v4:
            nodes_raw = raw.get("packed_nodes")
            edges_raw = raw.get("packed_edges")
            roots_raw = raw.get("roots")
            selected_path_raw = raw.get("selected_path")
            truncated = raw.get("truncated")
            if (
                not isinstance(nodes_raw, list) or
                not 1 <= len(nodes_raw) <= 32 or
                not isinstance(edges_raw, list) or
                len(edges_raw) > 128 or
                not isinstance(roots_raw, list) or
                not 1 <= len(roots_raw) <= 8 or
                not isinstance(selected_path_raw, list) or
                not 1 <= len(selected_path_raw) <= 32 or
                not isinstance(truncated, bool)
            ):
                return 0
            orders: set[int] = set()
            empty_digest = hashlib.sha256(b"").hexdigest()
            for item in nodes_raw:
                if (
                    not isinstance(item, dict) or
                    not self._valid_digest(str(item.get("node_id", ""))) or
                    not isinstance(item.get("order"), int) or
                    isinstance(item.get("order"), bool) or
                    not 0 <= item["order"] < 4096 or
                    item["order"] in orders or
                    self._cfg_label(item.get("symbol")) is None or
                    self._cfg_label(
                        item.get("state"), allow_empty=True) is None or
                    not isinstance(item.get("start"), int) or
                    isinstance(item.get("start"), bool) or
                    not isinstance(item.get("end"), int) or
                    isinstance(item.get("end"), bool) or
                    not 0 <= item["start"] <= item["end"] or
                    not isinstance(item.get("epsilon"), bool) or
                    item["epsilon"] !=
                    (item["start"] == item["end"]) or
                    not self._valid_digest(
                        str(item.get("yield_sha256", ""))) or
                    item["epsilon"] and
                    item["yield_sha256"] != empty_digest
                ):
                    return 0
                orders.add(item["order"])
                node_core = {
                    "schema": "symcc-parser-packed-node-v1",
                    "parser": parser,
                    "order": item["order"],
                    "symbol": str(item["symbol"]),
                    "state": str(item["state"]),
                    "start": item["start"],
                    "end": item["end"],
                    "epsilon": bool(item["epsilon"]),
                    "yield_sha256": str(item["yield_sha256"]),
                }
                node_id = hashlib.sha256(json.dumps(
                    node_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if node_id != item["node_id"] or node_id in packed_nodes:
                    return 0
                packed_nodes[node_id] = {
                    "node_id": node_id,
                    "parser": parser,
                    **{
                        key: value
                        for key, value in node_core.items()
                        if key not in {"schema", "parser"}
                    },
                }
            grouped_edges: dict[
                tuple[str, int], list[dict[str, Any]]
            ] = {}
            incoming: set[str] = set()
            for item in edges_raw:
                if (
                    not isinstance(item, dict) or
                    not self._valid_digest(str(item.get("edge_id", ""))) or
                    str(item.get("parent_id", "")) not in packed_nodes or
                    str(item.get("child_id", "")) not in packed_nodes or
                    not isinstance(item.get("alternative"), int) or
                    isinstance(item.get("alternative"), bool) or
                    not 0 <= item["alternative"] < 8 or
                    not isinstance(item.get("slot"), int) or
                    isinstance(item.get("slot"), bool) or
                    not 0 <= item["slot"] < 64
                ):
                    return 0
                parent = packed_nodes[str(item["parent_id"])]
                child = packed_nodes[str(item["child_id"])]
                if (
                    parent["order"] >= child["order"] or
                    not (
                        parent["start"] <= child["start"] and
                        child["end"] <= parent["end"]
                    ) or
                    parent["epsilon"]
                ):
                    return 0
                edge_core = {
                    "schema": "symcc-parser-packed-edge-v1",
                    "parser": parser,
                    "parent_id": parent["node_id"],
                    "child_id": child["node_id"],
                    "alternative": item["alternative"],
                    "slot": item["slot"],
                }
                edge_id = hashlib.sha256(json.dumps(
                    edge_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if edge_id != item["edge_id"] or edge_id in packed_edges:
                    return 0
                edge = {
                    "edge_id": edge_id,
                    "parser": parser,
                    "parent_id": parent["node_id"],
                    "child_id": child["node_id"],
                    "alternative": item["alternative"],
                    "slot": item["slot"],
                }
                packed_edges[edge_id] = edge
                grouped_edges.setdefault(
                    (edge["parent_id"], edge["alternative"]), []).append(
                        edge)
                incoming.add(edge["child_id"])
            for group in grouped_edges.values():
                ordered = sorted(group, key=lambda edge: edge["slot"])
                slots = [edge["slot"] for edge in ordered]
                if (
                    len(slots) != len(set(slots)) or
                    not truncated and slots != list(range(len(slots))) or
                    any(
                        packed_nodes[left["child_id"]]["end"] >
                        packed_nodes[right["child_id"]]["start"]
                        for left, right in zip(ordered, ordered[1:])
                    )
                ):
                    return 0
            packed_roots = [str(root) for root in roots_raw]
            selected_node_path = [
                str(node_id) for node_id in selected_path_raw]
            if (
                len(set(packed_roots)) != len(packed_roots) or
                any(root not in packed_nodes or root in incoming
                    for root in packed_roots) or
                any(node_id not in packed_nodes
                    for node_id in selected_node_path) or
                selected_node_path[0] not in packed_roots
            ):
                return 0
            edge_triples = {
                (
                    edge["parent_id"],
                    edge["child_id"],
                    edge["alternative"],
                )
                for edge in packed_edges.values()
            }
            if any(
                (parent, child, 0) not in edge_triples
                for parent, child in zip(
                    selected_node_path, selected_node_path[1:])
            ):
                return 0
            reachable = set(packed_roots)
            for node in sorted(
                    packed_nodes.values(), key=lambda item: item["order"]):
                if node["node_id"] not in reachable:
                    continue
                reachable.update(
                    edge["child_id"]
                    for edge in packed_edges.values()
                    if edge["parent_id"] == node["node_id"]
                )
            if reachable != set(packed_nodes):
                return 0

        nullable_certificate: dict[str, Any] = {
            "nullable_rules": [],
            "nullable_proofs": [],
            "nullable_sccs": [],
        }
        if fragment_v5:
            nullable_rules_raw = raw.get("nullable_rules")
            nullable_proofs_raw = raw.get("nullable_proofs")
            nullable_sccs_raw = raw.get("nullable_sccs")
            if (
                not isinstance(nullable_rules_raw, list) or
                not 1 <= len(nullable_rules_raw) <= 32 or
                not isinstance(nullable_proofs_raw, list) or
                len(nullable_proofs_raw) > 288 or
                not isinstance(nullable_sccs_raw, list) or
                len(nullable_sccs_raw) > 288
            ):
                return 0
            nullable_certificate_raw = (
                VerifiedProposalManager._nullable_certificate(
                    parser, nullable_rules_raw)
            )
            if (
                nullable_certificate_raw is None or
                nullable_rules_raw !=
                nullable_certificate_raw["nullable_rules"] or
                nullable_proofs_raw !=
                nullable_certificate_raw["nullable_proofs"] or
                nullable_sccs_raw !=
                nullable_certificate_raw["nullable_sccs"]
            ):
                return 0
            nullable_certificate = nullable_certificate_raw

        normalized_cycles: list[dict[str, Any]] = []
        for item in raw["cycles"]:
            if (
                not isinstance(item, dict) or
                not self._valid_digest(str(item.get("cycle_id", ""))) or
                str(item.get("production_id", "")) not in productions or
                self._cfg_label(item.get("lhs")) is None or
                item.get("kind") not in {"direct", "mutual"} or
                not isinstance(item.get("path"), list) or
                not 2 <= len(item["path"]) <= 32 or
                not isinstance(item.get("slot"), int) or
                isinstance(item.get("slot"), bool) or
                not isinstance(item.get("recursive_slots"), int) or
                isinstance(item.get("recursive_slots"), bool) or
                not 1 <= item["recursive_slots"] <= 64
            ):
                return 0
            production = productions[str(item["production_id"])]
            if (
                item["lhs"] != production["lhs"] or
                not 0 <= item["slot"] < len(production["rhs"])
            ):
                return 0
            path: list[list[str]] = []
            for path_item in item["path"]:
                if (
                    not isinstance(path_item, list) or len(path_item) != 2 or
                    self._cfg_label(path_item[0]) is None or
                    self._cfg_label(
                        path_item[1], allow_empty=True) is None
                ):
                    return 0
                path.append([str(path_item[0]), str(path_item[1])])
            if path[0][0] != production["lhs"] or path[-1][0] != path[0][0]:
                return 0
            try:
                prefix = bytes.fromhex(str(item.get("prefix_hex", "")))
                suffix = bytes.fromhex(str(item.get("suffix_hex", "")))
            except ValueError:
                return 0
            if (
                not prefix and not suffix or
                len(prefix) + len(suffix) > 128
            ):
                return 0
            cycle_core = {
                "schema": "symcc-parser-cycle-v1",
                "parser": parser,
                "production_id": production["id"],
                "slot": item["slot"],
                "path": path,
            }
            cycle_id = hashlib.sha256(json.dumps(
                cycle_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if cycle_id != item["cycle_id"]:
                return 0
            normalized_cycles.append({
                "cycle_id": cycle_id,
                "production_id": production["id"],
                "lhs": production["lhs"],
                "slot": item["slot"],
                "kind": item["kind"],
                "path": path,
                "recursive_slots": item["recursive_slots"],
                "prefix_hex": prefix.hex(),
                "suffix_hex": suffix.hex(),
            })

        normalized_instances: list[dict[str, Any]] = []
        selected_instances: list[dict[str, Any]] = []
        for item in raw.get("instances", ()):
            if (
                not isinstance(item, dict) or
                not self._valid_digest(str(item.get("instance_id", ""))) or
                not self._valid_digest(str(item.get("shape_id", ""))) or
                not self._valid_digest(str(item.get("yield_sha256", ""))) or
                not isinstance(item.get("selected"), bool) or
                not isinstance(item.get("path"), list) or
                not 1 <= len(item["path"]) <= 32 or
                not isinstance(item.get("terminal_gap_hex"), list)
            ):
                return 0
            node_id = str(item.get("node_id", ""))
            node_path = item.get("node_path")
            if fragment_v4 and (
                item.get("schema") !=
                "symcc-parser-subtree-instance-v2" or
                node_id not in packed_nodes or
                not isinstance(node_path, list) or
                not 1 <= len(node_path) <= 32 or
                any(str(path_node) not in packed_nodes
                    for path_node in node_path)
            ):
                return 0
            if not fragment_v4:
                node_id = ""
                node_path = []
            production = productions.get(str(item.get("production_id", "")))
            if (
                production is None or
                item["shape_id"] != production["shape_id"] or
                len(item["terminal_gap_hex"]) !=
                len(production["terminal_gaps"])
            ):
                return 0
            try:
                subtree = bytes.fromhex(str(item.get("yield_hex", "")))
                terminal_gaps = [
                    bytes.fromhex(str(gap))
                    for gap in item["terminal_gap_hex"]
                ]
            except ValueError:
                return 0
            if (
                len(subtree) > 256 or
                production["epsilon"] and bool(subtree) or
                not production["epsilon"] and not subtree or
                sum(len(gap) for gap in terminal_gaps) > 256 or
                hashlib.sha256(subtree).hexdigest() !=
                item["yield_sha256"] or
                [
                    hashlib.sha256(gap).hexdigest()
                    for gap in terminal_gaps
                ] != production["terminal_gaps"]
            ):
                return 0
            if production["epsilon"] and any(terminal_gaps):
                return 0
            path: list[list[str]] = []
            for path_item in item["path"]:
                if (
                    not isinstance(path_item, list) or
                    len(path_item) != 2 or
                    self._cfg_label(path_item[0]) is None or
                    self._cfg_label(
                        path_item[1], allow_empty=True) is None
                ):
                    return 0
                path.append([str(path_item[0]), str(path_item[1])])
            if path[-1] != [production["lhs"], production["state"]]:
                return 0
            if fragment_v4:
                normalized_node_path = [
                    str(path_node) for path_node in node_path]
                node = packed_nodes[node_id]
                edge_pairs = {
                    (edge["parent_id"], edge["child_id"])
                    for edge in packed_edges.values()
                }
                if (
                    normalized_node_path[0] not in packed_roots or
                    normalized_node_path[-1] != node_id or
                    len(normalized_node_path) != len(path) or
                    [
                        [
                            packed_nodes[path_node]["symbol"],
                            packed_nodes[path_node]["state"],
                        ]
                        for path_node in normalized_node_path
                    ] != path or
                    any(
                        (parent, child) not in edge_pairs
                        for parent, child in zip(
                            normalized_node_path,
                            normalized_node_path[1:],
                        )
                    ) or
                    node["yield_sha256"] != item["yield_sha256"] or
                    [production["lhs"], production["state"]] !=
                    [node["symbol"], node["state"]]
                ):
                    return 0
            instance_core = {
                "schema": (
                    "symcc-parser-subtree-instance-v2"
                    if fragment_v4 else
                    "symcc-parser-subtree-instance-v1"
                ),
                "production_id": production["id"],
                "shape_id": production["shape_id"],
                "yield_sha256": item["yield_sha256"],
                "path": path,
            }
            if fragment_v4:
                instance_core.update({
                    "node_id": node_id,
                    "node_path": normalized_node_path,
                })
            instance_id = hashlib.sha256(json.dumps(
                instance_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if (
                instance_id != item["instance_id"] or
                any(
                    previous["instance_id"] == instance_id
                    for previous in normalized_instances
                )
            ):
                return 0
            normalized = {
                "instance_id": instance_id,
                "production_id": production["id"],
                "shape_id": production["shape_id"],
                "parser": parser,
                "path": path,
                "yield_hex": subtree.hex(),
                "yield_sha256": item["yield_sha256"],
                "terminal_gap_hex": [
                    gap.hex() for gap in terminal_gaps],
                "selected": bool(item["selected"]),
            }
            if fragment_v4:
                instance_edges = sorted(
                    (
                        edge for edge in packed_edges.values()
                        if (
                            edge["parent_id"] == node_id and
                            int(edge["alternative"]) ==
                            int(production["alternative"])
                        )
                    ),
                    key=lambda edge: (
                        int(edge["slot"]), str(edge["edge_id"])),
                )
                normalized.update({
                    "schema": "symcc-parser-subtree-instance-v2",
                    "node_id": node_id,
                    "node_path": normalized_node_path,
                    # Alternative numbers belong to this packed node, not to
                    # the globally deduplicated production shape.
                    "alternative": int(production["alternative"]),
                    "child_edge_ids": [
                        str(edge["edge_id"])
                        for edge in instance_edges
                    ],
                })
            normalized_instances.append(normalized)
            if normalized["selected"]:
                selected_instances.append(normalized)
        if len(selected_instances) > 1:
            return 0
        if (
            fragment_v4 and selected_instances and
            selected_instances[0]["node_path"] != selected_node_path
        ):
            return 0

        if fragment_v4:
            incoming_node_ids = set(packed_nodes)
            while (
                len(self.packed_nodes | packed_nodes) >
                self.HARD_MAX_PACKED_NODES
            ):
                victims = sorted(
                    set(self.packed_nodes) - incoming_node_ids)
                if not victims:
                    return 0
                self._drop_packed_node(victims[0])
            incoming_edge_ids = set(packed_edges)
            while (
                len(self.packed_edges | packed_edges) >
                self.HARD_MAX_PACKED_EDGES
            ):
                victims = sorted(
                    set(self.packed_edges) - incoming_edge_ids)
                if not victims:
                    return 0
                self._drop_packed_edge(victims[0])
            self.packed_nodes.update(packed_nodes)
            self.packed_edges.update(packed_edges)
            self.packed_root_ids.update(packed_roots)
            for root_id in packed_roots:
                certificate = self._packed_root_certificate(
                    fragment_sha256, parser, root_id)
                previous = self.packed_root_evidence.get(root_id)
                if (
                    previous is None or
                    certificate["fragment_sha256"] <
                    previous["fragment_sha256"]
                ):
                    self.packed_root_evidence[root_id] = certificate
        for production_id, production in productions.items():
            if (
                production_id not in self.cfg_productions and
                len(self.cfg_productions) >= self.HARD_MAX_CFG_PRODUCTIONS
            ):
                self._drop_cfg_production(min(self.cfg_productions))
            self.cfg_productions[production_id] = production
        for instance in normalized_instances:
            instance_id = instance["instance_id"]
            shape_id = instance["shape_id"]
            if (
                shape_id not in self.ect_shapes and
                len(self.ect_shapes) >= self.HARD_MAX_ECT_SHAPES
            ):
                self._drop_ect_shape(min(self.ect_shapes))
            if (
                instance_id not in self.ect_instances and
                len(self.ect_instances) >= self.HARD_MAX_ECT_INSTANCES
            ):
                self._drop_ect_instance(min(self.ect_instances))
            self.ect_instances[instance_id] = instance
            self.ect_shapes.setdefault(shape_id, set()).add(instance_id)
            production = productions[instance["production_id"]]
            subtree_rule = self._learn_rule(
                "epsilon" if production["epsilon"] else "subtree",
                b"",
                bytes.fromhex(instance["yield_hex"]),
            )
            if subtree_rule is not None:
                self.subtree_rule_shapes.setdefault(
                    subtree_rule.rule_id, set()).add(shape_id)
                self.subtree_rule_instances.setdefault(
                    subtree_rule.rule_id, set()).add(instance_id)
        if fragment_v5:
            incoming_rule_ids = {
                rule["rule_id"]
                for rule in nullable_certificate["nullable_rules"]
            }
            while (
                len(set(self.nullable_rules) | incoming_rule_ids) >
                self.HARD_MAX_NULLABLE_RULES
            ):
                victims = sorted(
                    set(self.nullable_rules) - incoming_rule_ids)
                if not victims:
                    return 0
                self.nullable_rules.pop(victims[0], None)
            for nullable_rule in nullable_certificate["nullable_rules"]:
                self.nullable_rules[nullable_rule["rule_id"]] = {
                    "parser": parser,
                    **nullable_rule,
                }
            self._refresh_nullable_state()
        if selected_instances:
            selected_shape = selected_instances[0]["shape_id"]
            for lexical_context in (
                source_context_id,
                candidate_context_id,
            ):
                if not self._valid_digest(lexical_context):
                    continue
                if (
                    lexical_context not in self.parser_shape_aliases and
                    len(self.parser_shape_aliases) >= 4096
                ):
                    continue
                self.parser_shape_aliases.setdefault(
                    lexical_context, set()).add(selected_shape)
        if fragment_v4:
            self._refresh_synchronized_state()
            self._observe_pcfg_fragment(
                fragment_sha256,
                parser,
                productions,
                normalized_instances,
                packed_edges,
                packed_roots,
            )
        learned = 0
        for cycle in normalized_cycles:
            cycle_id = cycle["cycle_id"]
            if (
                cycle_id not in self.cfg_cycles and
                len(self.cfg_cycles) >= self.HARD_MAX_CFG_CYCLES
            ):
                victim = min(self.cfg_cycles)
                self.cfg_cycles.pop(victim, None)
                for rule_id in tuple(self.recursive_rule_cycles):
                    self.recursive_rule_cycles[rule_id].discard(victim)
                    if not self.recursive_rule_cycles[rule_id]:
                        del self.recursive_rule_cycles[rule_id]
            self.cfg_cycles[cycle_id] = cycle
            recursive_rule = self._learn_rule(
                "recursive",
                bytes.fromhex(cycle["prefix_hex"]),
                b"",
                bytes.fromhex(cycle["suffix_hex"]),
            )
            if recursive_rule is None:
                continue
            self.recursive_rule_cycles.setdefault(
                recursive_rule.rule_id, set()).add(cycle_id)
            recursive_key = self._parser_context_key(
                recursive_rule.rule_id, source_context_id)
            if (
                recursive_key in self.parser_context_aliases or
                len(self.parser_context_aliases) < 4096
            ):
                self.parser_context_aliases[recursive_key] = context_id
            if (
                recursive_key in self.parser_production_aliases or
                len(self.parser_production_aliases) < 4096
            ):
                self.parser_production_aliases[
                    recursive_key] = cycle["production_id"]
            learned += 1
        return learned

    def observe_grammar_validation(
        self,
        rule_id: str,
        *,
        valid: bool,
        parser_valid: bool | None = None,
        context_id: str = "",
        source_context_id: str = "",
        candidate_context_id: str = "",
        production_id: str = "",
        recursion_prefix_hex: str = "",
        recursion_suffix_hex: str = "",
        cfg_fragment_json: str = "",
        cfg_fragment_sha256: str = "",
        telemetry: SolverTelemetry | None = None,
    ) -> bool:
        production_id = str(production_id).lower()
        recursion_prefix_hex = str(recursion_prefix_hex)
        recursion_suffix_hex = str(recursion_suffix_hex)
        rule = self.grammar_rules.get(str(rule_id))
        if rule is None:
            return False
        rule.validations += 1
        if valid:
            rule.verified += 1
        if telemetry is not None:
            if telemetry.data_features:
                rule.data_observations = min(
                    (1 << 31) - 1, rule.data_observations + 1)
                rule.data_quality_sum = min(
                    float(rule.data_observations),
                    rule.data_quality_sum + telemetry.data_quality,
                )
            string_queries = max(
                0, int(telemetry.string_solver_queries))
            string_verified = min(
                string_queries,
                max(0, int(telemetry.string_solver_verified)),
            )
            rule.string_queries = min(
                (1 << 31) - 1,
                rule.string_queries + string_queries,
            )
            rule.string_verified = min(
                rule.string_queries,
                rule.string_verified + string_verified,
            )
        if parser_valid is not None:
            valid_context = (
                len(context_id) == 64 and
                all(character in "0123456789abcdef"
                    for character in context_id)
            )
            valid_source = (
                len(source_context_id) == 64 and
                all(character in "0123456789abcdef"
                    for character in source_context_id)
            )
            if (
                valid_context and valid_source and
                context_id != source_context_id
            ):
                alias_key = self._parser_context_key(
                    str(rule_id), source_context_id)
                if (
                    alias_key in self.parser_context_aliases or
                    len(self.parser_context_aliases) < 4096
                ):
                    self.parser_context_aliases[alias_key] = context_id
            valid_production = self._valid_digest(production_id)
            if valid_context and valid_source and valid_production:
                alias_key = self._parser_context_key(
                    str(rule_id), source_context_id)
                if (
                    alias_key in self.parser_production_aliases or
                    len(self.parser_production_aliases) < 4096
                ):
                    self.parser_production_aliases[
                        alias_key] = production_id
                self._observe_grammar_bitmap(
                    str(rule_id), context_id, production_id)
            rule.parser_validations += 1
            if parser_valid:
                rule.parser_accepted += 1
                if rule.rejected_contexts is not None and context_id:
                    rule.rejected_contexts.pop(context_id, None)
            elif valid_context:
                if rule.rejected_contexts is None:
                    rule.rejected_contexts = {}
                rule.rejected_contexts[context_id] = min(
                    65535,
                    rule.rejected_contexts.get(context_id, 0) + 1,
                )
            cfg_learned = 0
            if parser_valid and valid_context and valid_source:
                cfg_learned = self._ingest_cfg_fragment(
                    cfg_fragment_json,
                    str(cfg_fragment_sha256).lower(),
                    context_id=context_id,
                    source_context_id=source_context_id,
                    candidate_context_id=candidate_context_id,
                )
            if (
                parser_valid and valid_context and valid_source and
                valid_production and not cfg_learned
            ):
                try:
                    recursion_prefix = bytes.fromhex(recursion_prefix_hex)
                    recursion_suffix = bytes.fromhex(recursion_suffix_hex)
                except ValueError:
                    recursion_prefix = b""
                    recursion_suffix = b""
                if (
                    recursion_prefix or recursion_suffix
                ) and len(recursion_prefix) + len(recursion_suffix) <= 128:
                    recursive_rule = self._learn_rule(
                        "recursive", recursion_prefix, b"", recursion_suffix)
                    if recursive_rule is not None:
                        recursive_key = self._parser_context_key(
                            recursive_rule.rule_id, source_context_id)
                        if (
                            recursive_key in self.parser_context_aliases or
                            len(self.parser_context_aliases) < 4096
                        ):
                            self.parser_context_aliases[
                                recursive_key] = context_id
                        if (
                            recursive_key in self.parser_production_aliases or
                            len(self.parser_production_aliases) < 4096
                        ):
                            self.parser_production_aliases[
                                recursive_key] = production_id
        if valid or rule.validations % 8 == 0:
            self.save()
        return True

    @staticmethod
    def _parser_context_key(rule_id: str, source_context_id: str) -> str:
        return f"{rule_id}:{source_context_id}"

    def _resolved_parser_context(
        self,
        rule_id: str,
        source_context_id: str,
    ) -> str:
        return self.parser_context_aliases.get(
            self._parser_context_key(rule_id, source_context_id),
            source_context_id,
        )

    def _resolved_parser_production(
        self,
        rule_id: str,
        source_context_id: str,
    ) -> str:
        return self.parser_production_aliases.get(
            self._parser_context_key(rule_id, source_context_id),
            "",
        )

    def observe_grammar_retention(
        self,
        rule_id: str,
        coverage_features: int,
        *,
        context_id: str = "",
    ) -> bool:
        rule = self.grammar_rules.get(str(rule_id))
        if rule is None:
            return False
        features = max(0, int(coverage_features))
        if features:
            if rule.retained >= rule.verified:
                return False
            rule.retained += 1
            rule.coverage_features += features
            if (
                len(context_id) == 64 and
                all(character in "0123456789abcdef"
                    for character in context_id)
            ):
                if rule.structural_coverage is None:
                    rule.structural_coverage = {}
                if (
                    context_id not in rule.structural_coverage and
                    len(rule.structural_coverage) >= 256
                ):
                    removable = min(
                        rule.structural_coverage,
                        key=lambda key: (
                            rule.structural_coverage[key], key),
                    )
                    del rule.structural_coverage[removable]
                rule.structural_coverage[context_id] = min(
                    (1 << 31) - 1,
                    rule.structural_coverage.get(context_id, 0) + features,
                )
            self.save()
        return True

    def grammar_snapshot(self) -> dict[str, Any]:
        rules = tuple(self.grammar_rules.values())

        def anytime_delay(
            store: dict[str, list[dict[str, Any]]],
        ) -> float:
            delays = [
                max(
                    0,
                    int(certificate["detection_observation_index"]) -
                    int(certificate["cut_observation_index"]),
                )
                for certificates in store.values()
                for certificate in certificates
                if (
                    isinstance(certificate, dict) and
                    isinstance(
                        certificate.get(
                            "detection_observation_index"), int) and
                    not isinstance(
                        certificate.get(
                            "detection_observation_index"), bool) and
                    isinstance(
                        certificate.get(
                            "cut_observation_index"), int) and
                    not isinstance(
                        certificate.get(
                            "cut_observation_index"), bool)
                )
            ]
            return sum(delays) / len(delays) if delays else 0.0

        def calibration_total(
            store: dict[str, dict[str, Any]],
            key: str,
        ) -> float:
            return sum(
                float(stats.get(key, 0.0))
                for stats in store.values()
            )

        def calibration_mean(
            store: dict[str, dict[str, Any]],
            key: str,
        ) -> float:
            observations = calibration_total(store, "observations")
            return (
                calibration_total(store, key) / observations
                if observations else 0.0
            )

        normalized_entropies: list[float] = []
        posterior_probabilities: list[float] = []
        context_nll_bits = 0.0
        context_observations = 0
        circuit_nll_bits = 0.0
        circuit_observations = 0
        for family_id, family in self.pcfg_families.items():
            shape_ids = set(self.pcfg_counts.get(family_id, {}))
            shape_ids.update(
                str(production["shape_id"])
                for production in self.cfg_productions.values()
                if (
                    production["parser"] == family["parser"] and
                    production["lhs"] == family["lhs"] and
                    production["state"] == family["state"]
                )
            )
            probabilities = [
                self._pcfg_probability(family_id, shape_id)
                for shape_id in sorted(shape_ids)
            ]
            posterior_probabilities.extend(probabilities)
            if len(probabilities) <= 1:
                normalized_entropies.append(0.0)
                continue
            entropy = -sum(
                probability * math.log2(probability)
                for probability in probabilities
                if probability > 0.0
            )
            normalized_entropies.append(
                entropy / math.log2(len(probabilities)))
        for context_id, counts in self.pcfg_context_counts.items():
            for shape_id, count in counts.items():
                probability = self._pcfg_context_probability(
                    context_id, shape_id)
                if probability <= 0.0:
                    continue
                context_nll_bits -= count * math.log2(probability)
                context_observations += count
        for context_id, counts in self.pcfg_circuit_counts.items():
            context = self.pcfg_circuit_contexts.get(context_id)
            if context is None:
                continue
            _, parent_context = self._pcfg_context(
                str(context["parser"]),
                str(context["family_id"]),
                str(context["parent_shape_id"]),
                int(context["parent_slot"]),
            )
            for shape_id, count in counts.items():
                probability = self._pcfg_circuit_probability(
                    context_id,
                    shape_id,
                    context=context,
                    parent_context=parent_context,
                )
                if probability <= 0.0:
                    continue
                circuit_nll_bits -= count * math.log2(probability)
                circuit_observations += count
        return {
            "pcfg_context_order": self.pcfg_context_order,
            "pcfg_context_level": self.pcfg_context_level,
            "pcfg_enabled_model_count": self.pcfg_context_order + 1,
            "pcfg_state_order_mismatch_reset": int(
                self.pcfg_state_order_mismatch_reset),
            "pcfg_parent_enabled": int(
                self.pcfg_context_order >= 1),
            "pcfg_circuit_enabled": int(
                self.pcfg_context_order >= 2),
            "pcfg_sibling_enabled": int(
                self.pcfg_context_order >= 3),
            "pcfg_history_enabled": int(
                self.pcfg_context_order >= 4),
            "rules": len(rules),
            "attempted_rules": sum(rule.attempts > 0 for rule in rules),
            "validated_rules": sum(rule.validations > 0 for rule in rules),
            "verified_rules": sum(rule.verified > 0 for rule in rules),
            "retained_rules": sum(rule.retained > 0 for rule in rules),
            "attempts": sum(rule.attempts for rule in rules),
            "validations": sum(rule.validations for rule in rules),
            "verified": sum(rule.verified for rule in rules),
            "retained": sum(rule.retained for rule in rules),
            "coverage_features": sum(
                rule.coverage_features for rule in rules),
            "parser_validations": sum(
                rule.parser_validations for rule in rules),
            "parser_accepted": sum(
                rule.parser_accepted for rule in rules),
            "rejected_contexts": sum(
                len(rule.rejected_contexts or {}) for rule in rules),
            "parser_context_aliases": len(self.parser_context_aliases),
            "parser_production_aliases": len(
                self.parser_production_aliases),
            "structural_rule_contexts": sum(
                len(rule.structural_coverage or {}) for rule in rules),
            "recursive_rules": sum(
                rule.kind == "recursive" for rule in rules),
            "epsilon_rules": sum(
                rule.kind == "epsilon" for rule in rules),
            "grammar_bitmap_slots": len(self.grammar_bitmap),
            "grammar_bitmap_events": self.grammar_bitmap_events,
            "grammar_bitmap_collisions": self.grammar_bitmap_collisions,
            "cfg_productions": len(self.cfg_productions),
            "cfg_ambiguous_productions": sum(
                int(production.get("alternative", 0)) > 0
                for production in self.cfg_productions.values()),
            "cfg_epsilon_productions": sum(
                production.get("epsilon") is True
                for production in self.cfg_productions.values()),
            "cfg_cycles": len(self.cfg_cycles),
            "cfg_mutual_cycles": sum(
                cycle.get("kind") == "mutual"
                for cycle in self.cfg_cycles.values()),
            "cfg_multislot_cycles": sum(
                int(cycle.get("recursive_slots", 0)) > 1
                for cycle in self.cfg_cycles.values()),
            "cfg_derivation_depth": self.max_cfg_derivation_depth,
            "cfg_rule_cycle_links": sum(
                len(cycles) for cycles in self.recursive_rule_cycles.values()),
            "ect_shapes": len(self.ect_shapes),
            "ect_instances": len(self.ect_instances),
            "ect_derivation_paths": len({
                tuple(
                    tuple(item)
                    for item in instance.get("path", ())
                )
                for instance in self.ect_instances.values()
            }),
            "ect_correspondence_classes": sum(
                len({
                    self.ect_instances[instance_id]["yield_sha256"]
                    for instance_id in instances
                    if instance_id in self.ect_instances
                }) > 1
                for instances in self.ect_shapes.values()),
            "ect_subtree_rules": len(self.subtree_rule_instances),
            "ect_epsilon_rules": sum(
                self.grammar_rules.get(rule_id) is not None and
                self.grammar_rules[rule_id].kind == "epsilon"
                for rule_id in self.subtree_rule_instances),
            "packed_nodes": len(self.packed_nodes),
            "packed_edges": len(self.packed_edges),
            "packed_roots": len(self.packed_root_ids),
            "packed_root_evidence": len(self.packed_root_evidence),
            "packed_shared_nodes": sum(
                sum(
                    edge.get("child_id") == node_id
                    for edge in self.packed_edges.values()
                ) > 1
                for node_id in self.packed_nodes),
            "packed_deep_instances": sum(
                len(instance.get("node_path", ())) > 1
                for instance in self.ect_instances.values()
                if instance.get("node_id")),
            "nullable_rules": len(self.nullable_rules),
            "nullable_proofs": len(self.nullable_proofs),
            "nullable_sccs": len(self.nullable_sccs),
            "nullable_shapes": len(self.nullable_shape_ids),
            "nullable_max_depth": max(
                (
                    int(proof.get("depth", 0))
                    for proof in self.nullable_proofs.values()
                ),
                default=0,
            ),
            "synchronized_rules": sum(
                rule.kind == "synchronized" for rule in rules),
            "sync_transactions": len(self.sync_transactions),
            "sync_parent_shapes": len({
                transaction["parent_shape_id"]
                for transaction in self.sync_transactions.values()
            }),
            "sync_changed_slots": sum(
                int(transaction.get("changed_slots", 0))
                for transaction in self.sync_transactions.values()
            ),
            "sync_max_changed_slots": max(
                (
                    int(transaction.get("changed_slots", 0))
                    for transaction in self.sync_transactions.values()
                ),
                default=0,
            ),
            "slot_relations": len(self.slot_relations),
            "slot_relation_support": sum(
                int(relation.get("support", 0))
                for relation in self.slot_relations.values()),
            "slot_relation_kinds": len({
                str(relation.get("kind", ""))
                for relation in self.slot_relations.values()
            }),
            "sync_relation_conforming": sum(
                int(transaction.get("relation_total", 0)) > 0 and
                int(transaction.get("relation_matches", 0)) ==
                int(transaction.get("relation_total", 0))
                for transaction in self.sync_transactions.values()),
            "sync_relation_exploration": sum(
                transaction.get("relation_exploration") is True
                for transaction in self.sync_transactions.values()),
            "pcfg_families": len(self.pcfg_families),
            "pcfg_observations": len(self.pcfg_observations),
            "pcfg_observation_capacity":
                self.HARD_MAX_PCFG_OBSERVATIONS,
            "pcfg_observations_saturated": int(
                len(self.pcfg_observations) >=
                self.HARD_MAX_PCFG_OBSERVATIONS),
            "pcfg_selected_productions": sum(
                sum(counts.values())
                for counts in self.pcfg_counts.values()),
            "pcfg_production_shapes": sum(
                len(counts) for counts in self.pcfg_counts.values()),
            "pcfg_inside_nodes": len(self.packed_inside),
            "pcfg_outside_nodes": len(self.packed_outside),
            "pcfg_packed_alternatives": len(
                self.packed_alternative_probabilities),
            "pcfg_mean_normalized_entropy": (
                sum(normalized_entropies) / len(normalized_entropies)
                if normalized_entropies else 0.0
            ),
            "pcfg_max_posterior": max(
                posterior_probabilities, default=0.0),
            "pcfg_contexts": len(self.pcfg_contexts),
            "pcfg_context_observations": len(
                self.pcfg_context_observations),
            "pcfg_context_selected_productions":
                context_observations,
            "pcfg_context_inside_states": len(
                self.packed_context_inside),
            "pcfg_context_outside_states": len(
                self.packed_context_outside),
            "pcfg_context_alternatives": len(
                self.packed_context_alternative_probabilities),
            "pcfg_context_mean_nll_bits": (
                context_nll_bits / context_observations
                if context_observations else 0.0
            ),
            "pcfg_prequential_receipts": len(
                self.pcfg_prequential_receipts),
            "pcfg_prequential_capacity":
                self.HARD_MAX_PCFG_PREQUENTIAL_RECEIPTS,
            "pcfg_prequential_saturated": int(
                len(self.pcfg_prequential_receipts) >=
                self.HARD_MAX_PCFG_PREQUENTIAL_RECEIPTS),
            "pcfg_prequential_observations": sum(
                int(stats["observations"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_prequential_global_nll_bits": sum(
                float(stats["global_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_prequential_context_nll_bits": sum(
                float(stats["context_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_prequential_context_gain_bits": sum(
                float(stats["gain_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_prequential_mean_global_nll_bits":
                calibration_mean(
                    self.pcfg_context_calibration_stats,
                    "global_nll_bits",
                ),
            "pcfg_prequential_mean_context_nll_bits":
                calibration_mean(
                    self.pcfg_context_calibration_stats,
                    "context_nll_bits",
                ),
            "pcfg_prequential_mean_context_gain_bits":
                calibration_mean(
                    self.pcfg_context_calibration_stats,
                    "gain_bits",
                ),
            "pcfg_prequential_context_wins": sum(
                int(stats["context_wins"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_calibrated_contexts": sum(
                float(stats["weight"]) > 0.0
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_mean_context_weight": (
                sum(
                    float(stats["weight"])
                    for stats in
                    self.pcfg_context_calibration_stats.values()
                ) / len(self.pcfg_context_calibration_stats)
                if self.pcfg_context_calibration_stats else 0.0
            ),
            "pcfg_calibration_recent_fragments":
                self.PCFG_CALIBRATION_RECENT_FRAGMENTS,
            "pcfg_recency_contexts": sum(
                int(stats["recency_mode"]) > 0
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_stale_contexts": sum(
                int(stats["stale"]) > 0
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_recent_observations": sum(
                int(stats["recent_observations"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_recent_global_nll_bits": sum(
                float(stats["recent_global_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_recent_context_nll_bits": sum(
                float(stats["recent_context_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_recent_context_gain_bits": sum(
                float(stats["recent_gain_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_recent_context_wins": sum(
                int(stats["recent_context_wins"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_max_fragments":
                self.PCFG_ADAPTIVE_MAX_FRAGMENTS,
            "pcfg_adaptive_min_fragments":
                self.PCFG_ADAPTIVE_MIN_FRAGMENTS,
            "pcfg_adaptive_clip_bits":
                self.PCFG_ADAPTIVE_CLIP_BITS,
            "pcfg_adaptive_delta": self.PCFG_ADAPTIVE_DELTA,
            "pcfg_anytime_construction":
                "repeated-forward-hoeffding-alpha-spending",
            "pcfg_anytime_guarantee":
                "context-wise-infinite-horizon-pfa",
            "pcfg_anytime_spending_scale":
                self.PCFG_ANYTIME_SPENDING_SCALE,
            "pcfg_anytime_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_anytime_certificates.values()),
            "pcfg_anytime_certified_contexts": sum(
                int(stats["anytime_certified"]) > 0
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_anytime_mean_detection_delay_fragments":
                anytime_delay(self.pcfg_anytime_certificates),
            "pcfg_adaptive_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_adaptive_certificates.values()),
            "pcfg_adaptive_contexts": sum(
                int(stats["recency_mode"]) > 0
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_fragments": sum(
                int(stats["adaptive_fragments"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_receipts": sum(
                int(stats["adaptive_receipts"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_truncated_fragments": sum(
                int(stats["adaptive_truncated_fragments"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_global_nll_bits": sum(
                float(stats["adaptive_global_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_context_nll_bits": sum(
                float(stats["adaptive_context_nll_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_context_gain_bits": sum(
                float(stats["adaptive_gain_bits"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_adaptive_context_wins": sum(
                int(stats["adaptive_wins"])
                for stats in
                self.pcfg_context_calibration_stats.values()),
            "pcfg_circuit_contexts": len(
                self.pcfg_circuit_contexts),
            "pcfg_circuit_observations": len(
                self.pcfg_circuit_observations),
            "pcfg_circuit_selected_productions":
                circuit_observations,
            "pcfg_circuit_mean_nll_bits": (
                circuit_nll_bits / circuit_observations
                if circuit_observations else 0.0
            ),
            "pcfg_circuit_receipts": len(
                self.pcfg_circuit_receipts),
            "pcfg_circuit_receipt_capacity":
                self.HARD_MAX_PCFG_CIRCUIT_RECEIPTS,
            "pcfg_circuit_receipts_saturated": int(
                len(self.pcfg_circuit_receipts) >=
                self.HARD_MAX_PCFG_CIRCUIT_RECEIPTS),
            "pcfg_circuit_calibrated_contexts": sum(
                float(stats["weight"]) > 0.0
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_mean_weight": (
                sum(
                    float(stats["weight"])
                    for stats in
                    self.pcfg_circuit_calibration_stats.values()
                ) / len(self.pcfg_circuit_calibration_stats)
                if self.pcfg_circuit_calibration_stats else 0.0
            ),
            "pcfg_circuit_stale_contexts": sum(
                int(stats["stale"]) > 0
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_circuit_certificates.values()),
            "pcfg_circuit_anytime_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_circuit_anytime_certificates.values()),
            "pcfg_circuit_anytime_certified_contexts": sum(
                int(stats["anytime_certified"]) > 0
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_anytime_mean_detection_delay_fragments":
                anytime_delay(
                    self.pcfg_circuit_anytime_certificates),
            "pcfg_circuit_adaptive_fragments": sum(
                int(stats["adaptive_fragments"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_truncated_fragments": sum(
                int(stats["adaptive_truncated_fragments"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_global_nll_bits": sum(
                float(stats["adaptive_global_nll_bits"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_parent_nll_bits": sum(
                float(stats["adaptive_parent_nll_bits"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_nll_bits": sum(
                float(stats["adaptive_circuit_nll_bits"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_adaptive_robust_gain_bits": sum(
                float(stats["adaptive_robust_gain_bits"])
                for stats in
                self.pcfg_circuit_calibration_stats.values()),
            "pcfg_circuit_prequential_observations": int(
                calibration_total(
                    self.pcfg_circuit_calibration_stats,
                    "observations",
                )),
            "pcfg_circuit_prequential_mean_global_nll_bits":
                calibration_mean(
                    self.pcfg_circuit_calibration_stats,
                    "global_nll_bits",
                ),
            "pcfg_circuit_prequential_mean_parent_nll_bits":
                calibration_mean(
                    self.pcfg_circuit_calibration_stats,
                    "parent_nll_bits",
                ),
            "pcfg_circuit_prequential_mean_nll_bits":
                calibration_mean(
                    self.pcfg_circuit_calibration_stats,
                    "circuit_nll_bits",
                ),
            "pcfg_circuit_prequential_mean_robust_gain_bits":
                calibration_mean(
                    self.pcfg_circuit_calibration_stats,
                    "robust_gain_bits",
                ),
            "pcfg_sibling_contexts": len(
                self.pcfg_sibling_contexts),
            "pcfg_sibling_observations": len(
                self.pcfg_sibling_observations),
            "pcfg_sibling_selected_productions": sum(
                sum(counts.values())
                for counts in self.pcfg_sibling_counts.values()),
            "pcfg_sibling_mean_prequential_nll_bits": (
                sum(
                    float(stats["sibling_nll_bits"])
                    for stats in
                    self.pcfg_sibling_calibration_stats.values()
                ) / sum(
                    int(stats["observations"])
                    for stats in
                    self.pcfg_sibling_calibration_stats.values()
                )
                if self.pcfg_sibling_calibration_stats else 0.0
            ),
            "pcfg_sibling_receipts": len(
                self.pcfg_sibling_receipts),
            "pcfg_sibling_receipt_capacity":
                self.HARD_MAX_PCFG_SIBLING_RECEIPTS,
            "pcfg_sibling_receipts_saturated": int(
                len(self.pcfg_sibling_receipts) >=
                self.HARD_MAX_PCFG_SIBLING_RECEIPTS),
            "pcfg_sibling_calibrated_contexts": sum(
                float(stats["weight"]) > 0.0
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_mean_weight": (
                sum(
                    float(stats["weight"])
                    for stats in
                    self.pcfg_sibling_calibration_stats.values()
                ) / len(self.pcfg_sibling_calibration_stats)
                if self.pcfg_sibling_calibration_stats else 0.0
            ),
            "pcfg_sibling_stale_contexts": sum(
                int(stats["stale"]) > 0
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_sibling_certificates.values()),
            "pcfg_sibling_anytime_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_sibling_anytime_certificates.values()),
            "pcfg_sibling_anytime_certified_contexts": sum(
                int(stats["anytime_certified"]) > 0
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_anytime_mean_detection_delay_fragments":
                anytime_delay(
                    self.pcfg_sibling_anytime_certificates),
            "pcfg_sibling_adaptive_fragments": sum(
                int(stats["adaptive_fragments"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_truncated_fragments": sum(
                int(stats["adaptive_truncated_fragments"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_global_nll_bits": sum(
                float(stats["adaptive_global_nll_bits"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_parent_nll_bits": sum(
                float(stats["adaptive_parent_nll_bits"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_circuit_nll_bits": sum(
                float(stats["adaptive_circuit_nll_bits"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_nll_bits": sum(
                float(stats["adaptive_sibling_nll_bits"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_adaptive_robust_gain_bits": sum(
                float(stats["adaptive_robust_gain_bits"])
                for stats in
                self.pcfg_sibling_calibration_stats.values()),
            "pcfg_sibling_prequential_observations": int(
                calibration_total(
                    self.pcfg_sibling_calibration_stats,
                    "observations",
                )),
            "pcfg_sibling_prequential_mean_global_nll_bits":
                calibration_mean(
                    self.pcfg_sibling_calibration_stats,
                    "global_nll_bits",
                ),
            "pcfg_sibling_prequential_mean_parent_nll_bits":
                calibration_mean(
                    self.pcfg_sibling_calibration_stats,
                    "parent_nll_bits",
                ),
            "pcfg_sibling_prequential_mean_circuit_nll_bits":
                calibration_mean(
                    self.pcfg_sibling_calibration_stats,
                    "circuit_nll_bits",
                ),
            "pcfg_sibling_prequential_mean_nll_bits":
                calibration_mean(
                    self.pcfg_sibling_calibration_stats,
                    "sibling_nll_bits",
                ),
            "pcfg_sibling_prequential_mean_robust_gain_bits":
                calibration_mean(
                    self.pcfg_sibling_calibration_stats,
                    "robust_gain_bits",
                ),
            "pcfg_sibling_factor_steps": sum(
                len(alternative.get("child_factor_steps", ()))
                for alternative in
                self.packed_context_alternative_probabilities.values()),
            "pcfg_sibling_factor_transitions": sum(
                len(step.get("transitions", ()))
                for alternative in
                self.packed_context_alternative_probabilities.values()
                for step in alternative.get(
                    "child_factor_steps", ())
            ),
            "pcfg_history_contexts": len(
                self.pcfg_history_contexts),
            "pcfg_history_observations": len(
                self.pcfg_history_observations),
            "pcfg_history_selected_productions": sum(
                sum(counts.values())
                for counts in self.pcfg_history_counts.values()),
            "pcfg_history_receipts": len(
                self.pcfg_history_receipts),
            "pcfg_history_receipt_capacity":
                self.HARD_MAX_PCFG_HISTORY_RECEIPTS,
            "pcfg_history_receipts_saturated": int(
                len(self.pcfg_history_receipts) >=
                self.HARD_MAX_PCFG_HISTORY_RECEIPTS),
            "pcfg_history_calibrated_contexts": sum(
                float(stats["weight"]) > 0.0
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_mean_weight": (
                sum(
                    float(stats["weight"])
                    for stats in
                    self.pcfg_history_calibration_stats.values()
                ) / len(self.pcfg_history_calibration_stats)
                if self.pcfg_history_calibration_stats else 0.0
            ),
            "pcfg_history_stale_contexts": sum(
                int(stats["stale"]) > 0
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_history_certificates.values()),
            "pcfg_history_anytime_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_history_anytime_certificates.values()),
            "pcfg_history_anytime_certified_contexts": sum(
                int(stats["anytime_certified"]) > 0
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_anytime_mean_detection_delay_fragments":
                anytime_delay(
                    self.pcfg_history_anytime_certificates),
            "pcfg_history_adaptive_fragments": sum(
                int(stats["adaptive_fragments"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_truncated_fragments": sum(
                int(stats["adaptive_truncated_fragments"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_global_nll_bits": sum(
                float(stats["adaptive_global_nll_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_parent_nll_bits": sum(
                float(stats["adaptive_parent_nll_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_circuit_nll_bits": sum(
                float(stats["adaptive_circuit_nll_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_sibling_nll_bits": sum(
                float(stats["adaptive_sibling_nll_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_nll_bits": sum(
                float(stats["adaptive_history_nll_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_adaptive_robust_gain_bits": sum(
                float(stats["adaptive_robust_gain_bits"])
                for stats in
                self.pcfg_history_calibration_stats.values()),
            "pcfg_history_prequential_observations": int(
                calibration_total(
                    self.pcfg_history_calibration_stats,
                    "observations",
                )),
            "pcfg_history_prequential_mean_global_nll_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "global_nll_bits",
                ),
            "pcfg_history_prequential_mean_parent_nll_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "parent_nll_bits",
                ),
            "pcfg_history_prequential_mean_circuit_nll_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "circuit_nll_bits",
                ),
            "pcfg_history_prequential_mean_sibling_nll_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "sibling_nll_bits",
                ),
            "pcfg_history_prequential_mean_nll_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "history_nll_bits",
                ),
            "pcfg_history_prequential_mean_robust_gain_bits":
                calibration_mean(
                    self.pcfg_history_calibration_stats,
                    "robust_gain_bits",
                ),
            "pcfg_anytime_global_guarantee":
                "cross-context-infinite-horizon-fwer",
            "pcfg_anytime_global_delta":
                self.PCFG_GLOBAL_FWER_DELTA,
            "pcfg_anytime_context_allocations": len(
                self.pcfg_anytime_context_allocations),
            "pcfg_anytime_context_allocation_capacity":
                self.HARD_MAX_PCFG_ANYTIME_CONTEXT_ALLOCATIONS,
            "pcfg_anytime_context_allocations_saturated": int(
                len(self.pcfg_anytime_context_allocations) >=
                self.HARD_MAX_PCFG_ANYTIME_CONTEXT_ALLOCATIONS),
            "pcfg_anytime_allocation_ledger_valid": int(
                self.pcfg_anytime_allocation_ledger_valid),
            "pcfg_anytime_allocated_delta": sum(
                float(allocation["context_delta"])
                for allocation in
                self.pcfg_anytime_context_allocations.values()),
            "pcfg_global_anytime_certificates": sum(
                len(certificates)
                for certificates in
                self.pcfg_global_anytime_certificates.values()),
            "pcfg_global_anytime_certified_contexts":
                len(self.pcfg_global_anytime_certificates),
            "pcfg_global_anytime_mean_detection_delay_fragments":
                anytime_delay(
                    self.pcfg_global_anytime_certificates),
            "pcfg_history_factor_states": sum(
                len({
                    tuple(transition.get(
                        "previous_sibling_shape_ids", ()))
                    for transition in step.get("transitions", ())
                    if len(transition.get(
                        "previous_sibling_shape_ids", ())) >= 2
                })
                for alternative in
                self.packed_context_alternative_probabilities.values()
                for step in alternative.get(
                    "child_factor_steps", ())
            ),
            "ect_shape_contexts": len(self.parser_shape_aliases),
            "pareto_enabled": int(self.pareto_scheduling),
            "pareto_rankings": self.pareto_rankings,
            "pareto_frontier_rules": self.pareto_frontier_rules,
            "data_objective_observations": sum(
                rule.data_observations for rule in rules),
            "string_objective_queries": sum(
                rule.string_queries for rule in rules),
            "string_objective_verified": sum(
                rule.string_verified for rule in rules),
            "query_hole_attempts": self.query_hole_attempts,
            "query_hole_verified": self.query_hole_verified,
            "query_hole_rejected": self.query_hole_rejected,
            "history_seeds": len(self.history_seeds),
            "plateau_count": self.plateau_count,
            "history_attempts": self.history_attempts,
            "history_verified": self.history_verified,
            "history_retained": self.history_retained,
        }

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
                continue
            normalized_metadata[key] = str(value)[:1024]
        snapshot = self.grammar_snapshot()
        core = {
            "schema": self.RESEARCH_ARTIFACT_SCHEMA,
            "semantic_state_schema": self.SCHEMA,
            "pcfg_context_order": self.pcfg_context_order,
            "pcfg_context_level": self.pcfg_context_level,
            "metadata": normalized_metadata,
            "metrics": snapshot,
        }
        artifact_sha256 = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        return {"artifact_sha256": artifact_sha256, **core}

    @classmethod
    def verify_research_artifact(
        cls,
        artifact: dict[str, Any],
    ) -> bool:
        if (
            not isinstance(artifact, dict) or
            artifact.get("schema") != cls.RESEARCH_ARTIFACT_SCHEMA or
            artifact.get("semantic_state_schema") != cls.SCHEMA
        ):
            return False
        supplied = str(artifact.get("artifact_sha256", ""))
        core = dict(artifact)
        core.pop("artifact_sha256", None)
        expected = hashlib.sha256(json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        try:
            order = cls.normalize_pcfg_context_order(
                artifact["pcfg_context_order"])
            level = str(artifact["pcfg_context_level"])
            metrics = artifact["metrics"]
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return (
            supplied == expected and
            level == cls.PCFG_CONTEXT_LEVELS[order] and
            isinstance(metrics, dict) and
            metrics.get("pcfg_context_order") == order and
            metrics.get("pcfg_context_level") == level
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
                prefix=".pcfg-research-", dir=directory, text=True)
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

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "observations": self.observations,
            "generated": self.generated,
            "query_hole_attempts": self.query_hole_attempts,
            "query_hole_verified": self.query_hole_verified,
            "query_hole_rejected": self.query_hole_rejected,
            "max_grammar_rules": self.max_grammar_rules,
            "max_grammar_span": self.max_grammar_span,
            "max_cfg_derivation_depth": self.max_cfg_derivation_depth,
            "pcfg_context_order": self.pcfg_context_order,
            "pcfg_context_level": self.pcfg_context_level,
            "pareto_scheduling": self.pareto_scheduling,
            "pareto_rankings": self.pareto_rankings,
            "pareto_frontier_rules": self.pareto_frontier_rules,
            "max_query_holes": self.max_query_holes,
            "plateau_count": self.plateau_count,
            "plateau_observations": self.plateau_observations,
            "history_attempts": self.history_attempts,
            "history_verified": self.history_verified,
            "history_retained": self.history_retained,
            "history_seeds": [
                {
                    "seed_id": seed.seed_id,
                    "content": seed.content.hex(),
                    "coverage_features": seed.coverage_features,
                    "retained": seed.retained,
                    "attempts": seed.attempts,
                    "validations": seed.validations,
                    "verified": seed.verified,
                }
                for seed in sorted(
                    self.history_seeds.values(),
                    key=lambda item: item.seed_id,
                )
            ],
            "exemplars": {
                str(feature): {
                    "matched": exemplar.matched,
                    "width": exemplar.width,
                    "fragment": exemplar.fragment.hex(),
                }
                for feature, exemplar in self.exemplars.items()
            },
            "grammar_fragments": [
                fragment.hex() for fragment in self.grammar_fragments[-512:]
            ],
            "parser_context_aliases": dict(
                sorted(self.parser_context_aliases.items())),
            "parser_production_aliases": dict(
                sorted(self.parser_production_aliases.items())),
            "parser_shape_aliases": {
                context_id: sorted(shape_ids)
                for context_id, shape_ids in sorted(
                    self.parser_shape_aliases.items())
            },
            "grammar_bitmap": {
                str(index): count
                for index, count in sorted(self.grammar_bitmap.items())
            },
            "grammar_bitmap_owners": {
                str(index): owner
                for index, owner in sorted(self.grammar_bitmap_owners.items())
            },
            "grammar_bitmap_events": self.grammar_bitmap_events,
            "grammar_bitmap_collisions": self.grammar_bitmap_collisions,
            "cfg_productions": [
                self.cfg_productions[production_id]
                for production_id in sorted(self.cfg_productions)
            ],
            "cfg_cycles": [
                self.cfg_cycles[cycle_id]
                for cycle_id in sorted(self.cfg_cycles)
            ],
            "recursive_rule_cycles": dict(
                (
                    rule_id,
                    sorted(cycles),
                )
                for rule_id, cycles in sorted(
                    self.recursive_rule_cycles.items())
            ),
            "ect_instances": [
                self.ect_instances[instance_id]
                for instance_id in sorted(self.ect_instances)
            ],
            "packed_nodes": [
                self.packed_nodes[node_id]
                for node_id in sorted(self.packed_nodes)
            ],
            "packed_edges": [
                self.packed_edges[edge_id]
                for edge_id in sorted(self.packed_edges)
            ],
            "packed_roots": sorted(self.packed_root_ids),
            "packed_root_evidence": [
                self.packed_root_evidence[root_id]
                for root_id in sorted(self.packed_root_evidence)
            ],
            "nullable_rules": [
                self.nullable_rules[rule_id]
                for rule_id in sorted(self.nullable_rules)
            ],
            "nullable_proofs": [
                self.nullable_proofs[proof_id]
                for proof_id in sorted(self.nullable_proofs)
            ],
            "nullable_sccs": [
                self.nullable_sccs[scc_id]
                for scc_id in sorted(self.nullable_sccs)
            ],
            "sync_transactions": [
                self.sync_transactions[transaction_id]
                for transaction_id in sorted(self.sync_transactions)
            ],
            "slot_relations": [
                self.slot_relations[relation_id]
                for relation_id in sorted(self.slot_relations)
            ],
            "parent_shape_relations": {
                parent_shape_id: sorted(relation_ids)
                for parent_shape_id, relation_ids in sorted(
                    self.parent_shape_relations.items())
            },
            "pcfg_families": [
                self.pcfg_families[family_id]
                for family_id in sorted(self.pcfg_families)
            ],
            "pcfg_counts": {
                family_id: {
                    shape_id: count
                    for shape_id, count in sorted(counts.items())
                }
                for family_id, counts in sorted(self.pcfg_counts.items())
            },
            "pcfg_observations": {
                fragment_sha256: selections
                for fragment_sha256, selections in sorted(
                    self.pcfg_observations.items())
            },
            "pcfg_contexts": [
                self.pcfg_contexts[context_id]
                for context_id in sorted(self.pcfg_contexts)
            ],
            "pcfg_context_counts": {
                context_id: {
                    shape_id: count
                    for shape_id, count in sorted(counts.items())
                }
                for context_id, counts in sorted(
                    self.pcfg_context_counts.items())
            },
            "pcfg_context_observations": {
                fragment_sha256: selections
                for fragment_sha256, selections in sorted(
                    self.pcfg_context_observations.items())
            },
            "pcfg_prequential_receipts": [
                self.pcfg_prequential_receipts[receipt_id]
                for receipt_id in sorted(
                    self.pcfg_prequential_receipts)
            ],
            "pcfg_adaptive_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_adaptive_certificates)
                for certificate in
                self.pcfg_adaptive_certificates[context_id]
            ],
            "pcfg_anytime_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_anytime_certificates)
                for certificate in
                self.pcfg_anytime_certificates[context_id]
            ],
            "pcfg_circuit_contexts": [
                self.pcfg_circuit_contexts[context_id]
                for context_id in sorted(
                    self.pcfg_circuit_contexts)
            ],
            "pcfg_circuit_counts": {
                context_id: {
                    shape_id: count
                    for shape_id, count in sorted(counts.items())
                }
                for context_id, counts in sorted(
                    self.pcfg_circuit_counts.items())
            },
            "pcfg_circuit_observations": {
                fragment_sha256: selections
                for fragment_sha256, selections in sorted(
                    self.pcfg_circuit_observations.items())
            },
            "pcfg_circuit_receipts": [
                self.pcfg_circuit_receipts[receipt_id]
                for receipt_id in sorted(
                    self.pcfg_circuit_receipts)
            ],
            "pcfg_circuit_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_circuit_certificates)
                for certificate in
                self.pcfg_circuit_certificates[context_id]
            ],
            "pcfg_circuit_anytime_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_circuit_anytime_certificates)
                for certificate in
                self.pcfg_circuit_anytime_certificates[context_id]
            ],
            "pcfg_sibling_contexts": [
                self.pcfg_sibling_contexts[context_id]
                for context_id in sorted(
                    self.pcfg_sibling_contexts)
            ],
            "pcfg_sibling_counts": {
                context_id: {
                    shape_id: count
                    for shape_id, count in sorted(counts.items())
                }
                for context_id, counts in sorted(
                    self.pcfg_sibling_counts.items())
            },
            "pcfg_sibling_observations": {
                fragment_sha256: selections
                for fragment_sha256, selections in sorted(
                    self.pcfg_sibling_observations.items())
            },
            "pcfg_sibling_receipts": [
                self.pcfg_sibling_receipts[receipt_id]
                for receipt_id in sorted(
                    self.pcfg_sibling_receipts)
            ],
            "pcfg_sibling_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_sibling_certificates)
                for certificate in
                self.pcfg_sibling_certificates[context_id]
            ],
            "pcfg_sibling_anytime_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_sibling_anytime_certificates)
                for certificate in
                self.pcfg_sibling_anytime_certificates[context_id]
            ],
            "pcfg_history_contexts": [
                self.pcfg_history_contexts[context_id]
                for context_id in sorted(
                    self.pcfg_history_contexts)
            ],
            "pcfg_history_counts": {
                context_id: {
                    shape_id: count
                    for shape_id, count in sorted(counts.items())
                }
                for context_id, counts in sorted(
                    self.pcfg_history_counts.items())
            },
            "pcfg_history_observations": {
                fragment_sha256: selections
                for fragment_sha256, selections in sorted(
                    self.pcfg_history_observations.items())
            },
            "pcfg_history_receipts": [
                self.pcfg_history_receipts[receipt_id]
                for receipt_id in sorted(
                    self.pcfg_history_receipts)
            ],
            "pcfg_history_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_history_certificates)
                for certificate in
                self.pcfg_history_certificates[context_id]
            ],
            "pcfg_history_anytime_certificates": [
                certificate
                for context_id in sorted(
                    self.pcfg_history_anytime_certificates)
                for certificate in
                self.pcfg_history_anytime_certificates[context_id]
            ],
            "pcfg_anytime_context_allocations": [
                allocation
                for allocation in sorted(
                    self.pcfg_anytime_context_allocations.values(),
                    key=lambda item: int(item["ordinal"]),
                )
            ],
            "pcfg_global_anytime_certificates": [
                certificate
                for allocation_key in sorted(
                    self.pcfg_global_anytime_certificates,
                    key=lambda key: int(
                        self.pcfg_anytime_context_allocations[
                            key]["ordinal"]
                    ),
                )
                for certificate in
                self.pcfg_global_anytime_certificates[
                    allocation_key]
            ],
            "synchronized_rule_transactions": {
                rule_id: sorted(transaction_ids)
                for rule_id, transaction_ids in sorted(
                    self.synchronized_rule_transactions.items())
            },
            "subtree_rule_shapes": {
                rule_id: sorted(shape_ids)
                for rule_id, shape_ids in sorted(
                    self.subtree_rule_shapes.items())
            },
            "subtree_rule_instances": {
                rule_id: sorted(instance_ids)
                for rule_id, instance_ids in sorted(
                    self.subtree_rule_instances.items())
            },
            "grammar_rules": [
                {
                    "rule_id": rule.rule_id,
                    "kind": rule.kind,
                    "prefix": rule.prefix.hex(),
                    "body": rule.body.hex(),
                    "suffix": rule.suffix.hex(),
                    "support": rule.support,
                    "attempts": rule.attempts,
                    "validations": rule.validations,
                    "verified": rule.verified,
                    "retained": rule.retained,
                    "coverage_features": rule.coverage_features,
                    "parser_validations": rule.parser_validations,
                    "parser_accepted": rule.parser_accepted,
                    "data_quality_sum": rule.data_quality_sum,
                    "data_observations": rule.data_observations,
                    "string_queries": rule.string_queries,
                    "string_verified": rule.string_verified,
                    "last_attempt_observation":
                        rule.last_attempt_observation,
                    "rejected_contexts": dict(
                        sorted((rule.rejected_contexts or {}).items())),
                    "structural_coverage": dict(
                        sorted((rule.structural_coverage or {}).items())),
                }
                for rule in sorted(
                    self.grammar_rules.values(),
                    key=lambda item: item.rule_id,
                )
            ],
            "grammar_coverage": self.grammar_snapshot(),
        }

    def save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".semantic-proposals-", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(
                        self.to_mapping(), stream,
                        sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                os.replace(temporary, self.state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if (
            not isinstance(raw, dict) or
            raw.get("schema") not in {
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
                self.SCHEMA,
            }
        ):
            return
        try:
            stored_pcfg_context_order = (
                self.normalize_pcfg_context_order(
                    raw.get("pcfg_context_order", "history"))
            )
        except ValueError:
            stored_pcfg_context_order = -1
        self.pcfg_state_order_mismatch_reset = (
            stored_pcfg_context_order != self.pcfg_context_order
        )
        try:
            self.observations = max(0, int(raw.get("observations", 0)))
            self.generated = max(0, int(raw.get("generated", 0)))
            self.query_hole_attempts = max(
                0, int(raw.get("query_hole_attempts", 0)))
            self.query_hole_verified = max(
                0, int(raw.get("query_hole_verified", 0)))
            self.query_hole_rejected = max(
                0, int(raw.get("query_hole_rejected", 0)))
            self.plateau_count = max(
                0, min(
                    self.plateau_observations * 4,
                    int(raw.get("plateau_count", 0)),
                ))
            self.history_attempts = max(
                0, int(raw.get("history_attempts", 0)))
            self.history_verified = max(
                0, int(raw.get("history_verified", 0)))
            self.history_retained = max(
                0, int(raw.get("history_retained", 0)))
            self.grammar_bitmap_events = max(
                0, min(
                    (1 << 63) - 1,
                    int(raw.get("grammar_bitmap_events", 0)),
                ))
            self.grammar_bitmap_collisions = max(
                0, min(
                    (1 << 63) - 1,
                    int(raw.get("grammar_bitmap_collisions", 0)),
                ))
            self.pareto_rankings = max(
                0, min(
                    (1 << 63) - 1,
                    int(raw.get("pareto_rankings", 0)),
                ))
            self.pareto_frontier_rules = max(
                0, min(
                    (1 << 63) - 1,
                    int(raw.get("pareto_frontier_rules", 0)),
                ))
        except (TypeError, ValueError):
            pass
        aliases = raw.get("parser_context_aliases", {})
        if isinstance(aliases, dict):
            for alias_key, context_id in list(
                    aliases.items())[-4096:]:
                parts = (
                    alias_key.split(":", 1)
                    if isinstance(alias_key, str) else [])
                if (
                    len(parts) == 2 and
                    isinstance(context_id, str) and
                    all(len(part) == 64 for part in parts) and
                    len(context_id) == 64 and
                    all(
                        all(character in "0123456789abcdef"
                            for character in part)
                        for part in parts
                    ) and
                    all(character in "0123456789abcdef"
                        for character in context_id)
                ):
                    self.parser_context_aliases[alias_key] = context_id
        production_aliases = raw.get("parser_production_aliases", {})
        if isinstance(production_aliases, dict):
            for alias_key, production_id in list(
                    production_aliases.items())[-4096:]:
                parts = (
                    alias_key.split(":", 1)
                    if isinstance(alias_key, str) else [])
                if (
                    len(parts) == 2 and
                    isinstance(production_id, str) and
                    all(self._valid_digest(part) for part in parts) and
                    self._valid_digest(production_id)
                ):
                    self.parser_production_aliases[
                        alias_key] = production_id
        shape_aliases = raw.get("parser_shape_aliases", {})
        if isinstance(shape_aliases, dict):
            for context_id, raw_shape_ids in list(
                    shape_aliases.items())[-4096:]:
                if not self._valid_digest(str(context_id)):
                    continue
                shape_ids = (
                    raw_shape_ids
                    if isinstance(raw_shape_ids, list) else [raw_shape_ids]
                )
                valid_shapes = {
                    str(shape_id)
                    for shape_id in shape_ids[:self.HARD_MAX_ECT_SHAPES]
                    if self._valid_digest(str(shape_id))
                }
                if valid_shapes:
                    self.parser_shape_aliases[
                        str(context_id)] = valid_shapes
        bitmap = raw.get("grammar_bitmap", {})
        owners = raw.get("grammar_bitmap_owners", {})
        if isinstance(bitmap, dict):
            for raw_index, raw_count in list(
                    bitmap.items())[:self.GRAMMAR_BITMAP_SIZE]:
                try:
                    index = int(raw_index)
                    count = int(raw_count)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 0 <= index < self.GRAMMAR_BITMAP_SIZE and 0 < count <= 255:
                    self.grammar_bitmap[index] = count
                    owner = (
                        owners.get(raw_index, "")
                        if isinstance(owners, dict) else "")
                    if isinstance(owner, str) and self._valid_digest(owner):
                        self.grammar_bitmap_owners[index] = owner
        stored_packed_nodes = raw.get("packed_nodes", ())
        if isinstance(stored_packed_nodes, list):
            for item in stored_packed_nodes[
                    -self.HARD_MAX_PACKED_NODES:]:
                if not isinstance(item, dict):
                    continue
                parser = item.get("parser")
                try:
                    order = int(item.get("order"))
                    start = int(item.get("start"))
                    end = int(item.get("end"))
                except (TypeError, ValueError, OverflowError):
                    continue
                epsilon = item.get("epsilon")
                yield_sha256 = str(item.get("yield_sha256", ""))
                if (
                    isinstance(item.get("order"), bool) or
                    isinstance(item.get("start"), bool) or
                    isinstance(item.get("end"), bool) or
                    not 0 <= order < 4096 or
                    not 0 <= start <= end or
                    not isinstance(epsilon, bool) or
                    epsilon != (start == end) or
                    self._cfg_label(parser) is None or
                    self._cfg_label(item.get("symbol")) is None or
                    self._cfg_label(
                        item.get("state"), allow_empty=True) is None or
                    not self._valid_digest(yield_sha256) or
                    epsilon and yield_sha256 != hashlib.sha256(b"").hexdigest()
                ):
                    continue
                node_core = {
                    "schema": "symcc-parser-packed-node-v1",
                    "parser": str(parser),
                    "order": order,
                    "symbol": str(item["symbol"]),
                    "state": str(item["state"]),
                    "start": start,
                    "end": end,
                    "epsilon": epsilon,
                    "yield_sha256": yield_sha256,
                }
                node_id = hashlib.sha256(json.dumps(
                    node_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if node_id != item.get("node_id"):
                    continue
                self.packed_nodes[node_id] = {
                    "node_id": node_id,
                    "parser": str(parser),
                    **{
                        key: value
                        for key, value in node_core.items()
                        if key not in {"schema", "parser"}
                    },
                }
        stored_packed_edges = raw.get("packed_edges", ())
        if isinstance(stored_packed_edges, list):
            for item in stored_packed_edges[
                    -self.HARD_MAX_PACKED_EDGES:]:
                if not isinstance(item, dict):
                    continue
                parent = self.packed_nodes.get(
                    str(item.get("parent_id", "")))
                child = self.packed_nodes.get(
                    str(item.get("child_id", "")))
                alternative = item.get("alternative")
                slot = item.get("slot")
                if (
                    parent is None or child is None or
                    parent["parser"] != child["parser"] or
                    not isinstance(alternative, int) or
                    isinstance(alternative, bool) or
                    not 0 <= alternative < 8 or
                    not isinstance(slot, int) or
                    isinstance(slot, bool) or
                    not 0 <= slot < 64 or
                    parent["order"] >= child["order"] or
                    not (
                        parent["start"] <= child["start"] and
                        child["end"] <= parent["end"]
                    ) or
                    parent["epsilon"]
                ):
                    continue
                edge_core = {
                    "schema": "symcc-parser-packed-edge-v1",
                    "parser": parent["parser"],
                    "parent_id": parent["node_id"],
                    "child_id": child["node_id"],
                    "alternative": alternative,
                    "slot": slot,
                }
                edge_id = hashlib.sha256(json.dumps(
                    edge_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if edge_id != item.get("edge_id"):
                    continue
                self.packed_edges[edge_id] = {
                    "edge_id": edge_id,
                    "parser": parent["parser"],
                    "parent_id": parent["node_id"],
                    "child_id": child["node_id"],
                    "alternative": alternative,
                    "slot": slot,
                }
        incoming_node_ids = {
            str(edge["child_id"])
            for edge in self.packed_edges.values()
        }
        # Schema 14 and older did not persist root membership. The graph
        # frontier is the only sound migration available for those states.
        if int(raw.get("schema", 0)) < 15:
            for root_id in sorted(
                    set(self.packed_nodes) - incoming_node_ids):
                node = self.packed_nodes[root_id]
                migration_digest = hashlib.sha256(
                    (
                        "symcc-parser-packed-root-migration-v1:" +
                        str(raw.get("schema", 0)) + ":" + root_id
                    ).encode("ascii")
                ).hexdigest()
                certificate = self._packed_root_certificate(
                    migration_digest,
                    str(node["parser"]),
                    root_id,
                    source="legacy-frontier",
                )
                self.packed_root_ids.add(root_id)
                self.packed_root_evidence[root_id] = certificate
        stored_root_evidence = raw.get("packed_root_evidence", ())
        if isinstance(stored_root_evidence, list):
            for item in stored_root_evidence[
                    -self.HARD_MAX_PACKED_NODES:]:
                if not isinstance(item, dict):
                    continue
                root_id = str(item.get("root_id", ""))
                fragment_sha256 = str(
                    item.get("fragment_sha256", ""))
                source = str(item.get("source", ""))
                node = self.packed_nodes.get(root_id)
                if (
                    node is None or
                    source not in {
                        "accepted-fragment", "legacy-frontier"} or
                    not self._valid_digest(fragment_sha256) or
                    item.get("parser") != node["parser"]
                ):
                    continue
                certificate = self._packed_root_certificate(
                    fragment_sha256,
                    str(node["parser"]),
                    root_id,
                    source=source,
                )
                if item.get("evidence_id") != certificate["evidence_id"]:
                    continue
                self.packed_root_ids.add(root_id)
                self.packed_root_evidence[root_id] = certificate
        stored_nullable_rules = raw.get("nullable_rules", ())
        if isinstance(stored_nullable_rules, list):
            for item in stored_nullable_rules[
                    -self.HARD_MAX_NULLABLE_RULES:]:
                if not isinstance(item, dict):
                    continue
                parser = item.get("parser")
                if self._cfg_label(parser) is None:
                    continue
                certificate = VerifiedProposalManager._nullable_certificate(
                    str(parser), [item])
                if (
                    certificate is None or
                    len(certificate["nullable_rules"]) != 1
                ):
                    continue
                normalized = certificate["nullable_rules"][0]
                if normalized["rule_id"] != item.get("rule_id"):
                    continue
                self.nullable_rules[normalized["rule_id"]] = {
                    "parser": str(parser),
                    **normalized,
                }
        stored_productions = raw.get("cfg_productions", ())
        if isinstance(stored_productions, list):
            for item in stored_productions[
                    -self.HARD_MAX_CFG_PRODUCTIONS:]:
                if not isinstance(item, dict):
                    continue
                parser = item.get("parser")
                lhs = item.get("lhs")
                state = item.get("state")
                rhs = item.get("rhs")
                gaps = item.get("terminal_gaps")
                production_id = str(item.get("id", ""))
                production_schema = str(item.get(
                    "schema", "symcc-parser-production-v1"))
                epsilon = item.get("epsilon", False)
                alternative = item.get("alternative", 0)
                if (
                    production_schema not in {
                        "symcc-parser-production-v1",
                        "symcc-parser-production-v2",
                    } or
                    self._cfg_label(parser) is None or
                    self._cfg_label(lhs) is None or
                    self._cfg_label(state, allow_empty=True) is None or
                    not isinstance(rhs, list) or len(rhs) > 64 or
                    not isinstance(gaps, list) or len(gaps) != len(rhs) + 1 or
                    any(not self._valid_digest(str(gap)) for gap in gaps)
                ):
                    continue
                production_v2 = (
                    production_schema == "symcc-parser-production-v2")
                if (
                    not isinstance(epsilon, bool) or
                    not isinstance(alternative, int) or
                    isinstance(alternative, bool) or
                    not 0 <= alternative < 8 or
                    not production_v2 and (epsilon or alternative)
                ):
                    continue
                normalized_rhs: list[dict[str, Any]] = []
                valid_rhs = True
                for child in rhs:
                    if (
                        not isinstance(child, dict) or
                        self._cfg_label(child.get("symbol")) is None or
                        self._cfg_label(
                            child.get("state"), allow_empty=True) is None or
                        not isinstance(child.get("recursive"), bool)
                    ):
                        valid_rhs = False
                        break
                    normalized_rhs.append({
                        "symbol": str(child["symbol"]),
                        "state": str(child["state"]),
                        "recursive": bool(child["recursive"]),
                    })
                if not valid_rhs:
                    continue
                empty_digest = hashlib.sha256(b"").hexdigest()
                if epsilon and (
                    normalized_rhs or
                    [str(gap) for gap in gaps] != [empty_digest]
                ):
                    continue
                core = {
                    "schema": production_schema,
                    "parser": str(parser),
                    "lhs": str(lhs),
                    "state": str(state),
                    "rhs": normalized_rhs,
                    "terminal_gaps": [str(gap) for gap in gaps],
                }
                if production_v2:
                    core["epsilon"] = epsilon
                expected_id = hashlib.sha256(json.dumps(
                    core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if production_id != expected_id:
                    continue
                shape_core = {
                    "schema": (
                        "symcc-parser-production-shape-v2"
                        if production_v2 else
                        "symcc-parser-production-shape-v1"
                    ),
                    "parser": core["parser"],
                    "lhs": core["lhs"],
                    "state": core["state"],
                    "rhs": normalized_rhs,
                }
                if production_v2:
                    shape_core["epsilon"] = epsilon
                shape_id = hashlib.sha256(json.dumps(
                    shape_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if (
                    item.get("shape_id") is not None and
                    item.get("shape_id") != shape_id
                ):
                    continue
                self.cfg_productions[production_id] = {
                    "id": production_id,
                    "shape_id": shape_id,
                    "parser": core["parser"],
                    "lhs": core["lhs"],
                    "state": core["state"],
                    "rhs": normalized_rhs,
                    "terminal_gaps": core["terminal_gaps"],
                    "schema": production_schema,
                    "epsilon": bool(epsilon),
                    "alternative": int(alternative),
                }
        family_shapes: dict[str, set[str]] = {}
        for production in self.cfg_productions.values():
            family_id, _ = self._pcfg_family(
                str(production["parser"]),
                str(production["lhs"]),
                str(production["state"]),
            )
            family_shapes.setdefault(family_id, set()).add(
                str(production["shape_id"]))
        stored_families = raw.get("pcfg_families", ())
        if isinstance(stored_families, list):
            for item in stored_families[
                    -self.HARD_MAX_CFG_PRODUCTIONS:]:
                if not isinstance(item, dict):
                    continue
                parser = self._cfg_label(item.get("parser"))
                lhs = self._cfg_label(item.get("lhs"))
                state = self._cfg_label(
                    item.get("state"), allow_empty=True)
                if parser is None or lhs is None or state is None:
                    continue
                family_id, family = self._pcfg_family(
                    parser, lhs, state)
                if (
                    item.get("family_id") != family_id or
                    family_id not in family_shapes
                ):
                    continue
                self.pcfg_families[family_id] = family
        stored_counts = raw.get("pcfg_counts", {})
        if isinstance(stored_counts, dict):
            for family_id, raw_counts in list(
                    stored_counts.items())[
                        -self.HARD_MAX_CFG_PRODUCTIONS:]:
                family_id = str(family_id)
                valid_shapes = family_shapes.get(family_id)
                if (
                    family_id not in self.pcfg_families or
                    valid_shapes is None or
                    not isinstance(raw_counts, dict)
                ):
                    continue
                counts: dict[str, int] = {}
                for shape_id, raw_count in list(
                        raw_counts.items())[
                            :self.HARD_MAX_CFG_PRODUCTIONS]:
                    shape_id = str(shape_id)
                    if (
                        shape_id not in valid_shapes or
                        not isinstance(raw_count, int) or
                        isinstance(raw_count, bool) or
                        not 0 < raw_count <= (1 << 31) - 1
                    ):
                        continue
                    counts[shape_id] = raw_count
                if counts:
                    self.pcfg_counts[family_id] = counts
        stored_observations = raw.get("pcfg_observations", {})
        observation_tallies: dict[tuple[str, str], int] = {}
        if isinstance(stored_observations, dict):
            for fragment_sha256, raw_selections in list(
                    stored_observations.items())[
                        -self.HARD_MAX_PCFG_OBSERVATIONS:]:
                fragment_sha256 = str(fragment_sha256)
                if (
                    not self._valid_digest(fragment_sha256) or
                    not isinstance(raw_selections, list) or
                    len(raw_selections) > 32
                ):
                    continue
                selections: list[list[str]] = []
                local_tallies: dict[tuple[str, str], int] = {}
                valid = True
                for selection in raw_selections:
                    if (
                        not isinstance(selection, list) or
                        len(selection) != 2
                    ):
                        valid = False
                        break
                    family_id = str(selection[0])
                    shape_id = str(selection[1])
                    if (
                        family_id not in self.pcfg_families or
                        shape_id not in
                        family_shapes.get(family_id, set()) or
                        shape_id not in self.pcfg_counts.get(family_id, {})
                    ):
                        valid = False
                        break
                    key = (family_id, shape_id)
                    local_tallies[key] = local_tallies.get(key, 0) + 1
                    selections.append([family_id, shape_id])
                if not valid or selections != sorted(selections):
                    continue
                if any(
                    observation_tallies.get(key, 0) + count >
                    self.pcfg_counts[key[0]][key[1]]
                    for key, count in local_tallies.items()
                ):
                    continue
                for key, count in local_tallies.items():
                    observation_tallies[key] = (
                        observation_tallies.get(key, 0) + count)
                self.pcfg_observations[
                    fragment_sha256] = selections
        self.pcfg_counts, self.pcfg_observations = (
            self._reconcile_pcfg_evidence(
                self.pcfg_counts,
                self.pcfg_observations,
            )
        )

        if int(raw.get("schema", 0)) >= 17:
            stored_contexts = raw.get("pcfg_contexts", ())
            if isinstance(stored_contexts, list):
                for item in stored_contexts[
                        -self.HARD_MAX_PCFG_CONTEXTS:]:
                    if not isinstance(item, dict):
                        continue
                    parser = self._cfg_label(item.get("parser"))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    slot = item.get("slot")
                    family = self.pcfg_families.get(family_id)
                    if (
                        parser is None or family is None or
                        parser != family["parser"] or
                        not isinstance(slot, int) or
                        isinstance(slot, bool) or
                        not -1 <= slot < 64 or
                        parent_shape_id and
                        not self._valid_digest(parent_shape_id)
                    ):
                        continue
                    context_id, context = self._pcfg_context(
                        parser,
                        family_id,
                        parent_shape_id,
                        slot,
                    )
                    if (
                        item.get("context_id") != context_id or
                        not self._pcfg_context_is_active(
                            context, family_shapes)
                    ):
                        continue
                    self.pcfg_contexts[context_id] = context

            stored_context_counts = raw.get(
                "pcfg_context_counts", {})
            if isinstance(stored_context_counts, dict):
                for context_id, raw_counts in list(
                        stored_context_counts.items())[
                            -self.HARD_MAX_PCFG_CONTEXTS:]:
                    context_id = str(context_id)
                    context = self.pcfg_contexts.get(context_id)
                    if (
                        context is None or
                        not isinstance(raw_counts, dict)
                    ):
                        continue
                    valid_shapes = family_shapes.get(
                        str(context["family_id"]), set())
                    counts: dict[str, int] = {}
                    for shape_id, raw_count in list(
                            raw_counts.items())[
                                :self.HARD_MAX_CFG_PRODUCTIONS]:
                        shape_id = str(shape_id)
                        if (
                            shape_id not in valid_shapes or
                            not isinstance(raw_count, int) or
                            isinstance(raw_count, bool) or
                            not 0 < raw_count <= (1 << 31) - 1
                        ):
                            continue
                        counts[shape_id] = raw_count
                    if counts:
                        self.pcfg_context_counts[
                            context_id] = counts

            stored_context_observations = raw.get(
                "pcfg_context_observations", {})
            context_tallies: dict[tuple[str, str], int] = {}
            if isinstance(stored_context_observations, dict):
                for fragment_sha256, raw_selections in list(
                        stored_context_observations.items())[
                            -self.HARD_MAX_PCFG_OBSERVATIONS:]:
                    fragment_sha256 = str(fragment_sha256)
                    if (
                        fragment_sha256 not in self.pcfg_observations or
                        not isinstance(raw_selections, list) or
                        len(raw_selections) > 32
                    ):
                        continue
                    selections: list[list[str]] = []
                    local_tallies: dict[
                        tuple[str, str], int
                    ] = {}
                    valid = True
                    for selection in raw_selections:
                        if (
                            not isinstance(selection, list) or
                            len(selection) != 2
                        ):
                            valid = False
                            break
                        context_id = str(selection[0])
                        shape_id = str(selection[1])
                        context = self.pcfg_contexts.get(context_id)
                        if (
                            context is None or
                            shape_id not in
                            self.pcfg_context_counts.get(
                                context_id, {}) or
                            shape_id not in family_shapes.get(
                                str(context["family_id"]), set())
                        ):
                            valid = False
                            break
                        key = (context_id, shape_id)
                        local_tallies[key] = (
                            local_tallies.get(key, 0) + 1)
                        selections.append([context_id, shape_id])
                    if (
                        not valid or selections != sorted(selections) or
                        any(
                            context_tallies.get(key, 0) + count >
                            self.pcfg_context_counts[
                                key[0]][key[1]]
                            for key, count in local_tallies.items()
                        )
                    ):
                        continue
                    for key, count in local_tallies.items():
                        context_tallies[key] = (
                            context_tallies.get(key, 0) + count)
                    self.pcfg_context_observations[
                        fragment_sha256] = selections
            (
                self.pcfg_context_counts,
                self.pcfg_context_observations,
            ) = self._reconcile_pcfg_evidence(
                self.pcfg_context_counts,
                self.pcfg_context_observations,
            )
            for context_id in tuple(self.pcfg_contexts):
                if context_id not in self.pcfg_context_counts:
                    del self.pcfg_contexts[context_id]
            stored_prequential = raw.get(
                "pcfg_prequential_receipts", ())
            seen_prequential_events: set[
                tuple[str, str, str, str]
            ] = set()
            prequential_fragment_indexes: dict[str, int] = {}
            prequential_index_fragments: dict[int, str] = {}
            if isinstance(stored_prequential, list):
                for item in stored_prequential[
                        -self.HARD_MAX_PCFG_PREQUENTIAL_RECEIPTS:]:
                    if not isinstance(item, dict):
                        continue
                    fragment_sha256 = str(
                        item.get("fragment_sha256", ""))
                    node_id = str(item.get("node_id", ""))
                    context_id = str(item.get("context_id", ""))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    shape_id = str(item.get("shape_id", ""))
                    slot = item.get("slot")
                    numeric_fields = (
                        item.get("global_selected_before"),
                        item.get("global_total_before"),
                        item.get("known_shapes"),
                        item.get("context_selected_before"),
                        item.get("context_total_before"),
                    )
                    observation_index_raw = item.get(
                        "observation_index")
                    if observation_index_raw is None:
                        observation_index = None
                    elif (
                        isinstance(observation_index_raw, int) and
                        not isinstance(observation_index_raw, bool) and
                        0 <= observation_index_raw <
                        len(self.pcfg_observations)
                    ):
                        observation_index = observation_index_raw
                    else:
                        continue
                    context = self.pcfg_contexts.get(context_id)
                    event_key = (
                        fragment_sha256,
                        node_id,
                        context_id,
                        shape_id,
                    )
                    if (
                        int(raw.get("schema", 0)) < 18 or
                        context is None or
                        not self._valid_digest(node_id) or
                        [context_id, shape_id] not in
                        self.pcfg_context_observations.get(
                            fragment_sha256, ()) or
                        family_id != context["family_id"] or
                        parent_shape_id !=
                        context["parent_shape_id"] or
                        slot != context["slot"] or
                        any(
                            not isinstance(value, int) or
                            isinstance(value, bool)
                            for value in numeric_fields
                        ) or
                        event_key in seen_prequential_events
                    ):
                        continue
                    if observation_index is not None and (
                        fragment_sha256 in
                        prequential_fragment_indexes and
                        prequential_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in
                        prequential_index_fragments and
                        prequential_index_fragments[
                            observation_index] != fragment_sha256
                    ):
                        continue
                    (
                        global_selected_before,
                        global_total_before,
                        known_shapes,
                        context_selected_before,
                        context_total_before,
                    ) = numeric_fields
                    final_global_counts = self.pcfg_counts.get(
                        family_id, {})
                    final_context_counts = (
                        self.pcfg_context_counts.get(
                            context_id, {})
                    )
                    if (
                        not 0 <= global_selected_before <=
                        global_total_before < sum(
                            final_global_counts.values()) or
                        not 1 <= known_shapes <=
                        self.HARD_MAX_CFG_PRODUCTIONS or
                        not 0 <= context_selected_before <=
                        context_total_before < sum(
                            final_context_counts.values()) or
                        global_selected_before >=
                        final_global_counts.get(shape_id, 0) or
                        context_selected_before >=
                        final_context_counts.get(shape_id, 0)
                    ):
                        continue
                    receipt = self._pcfg_prequential_receipt(
                        fragment_sha256=fragment_sha256,
                        node_id=node_id,
                        context=context,
                        shape_id=shape_id,
                        global_selected_before=
                            global_selected_before,
                        global_total_before=global_total_before,
                        known_shapes=known_shapes,
                        context_selected_before=
                            context_selected_before,
                        context_total_before=
                            context_total_before,
                        observation_index=observation_index,
                    )
                    receipt_id = str(receipt["receipt_id"])
                    if item.get("receipt_id") != receipt_id:
                        continue
                    seen_prequential_events.add(event_key)
                    if observation_index is not None:
                        prequential_fragment_indexes[
                            fragment_sha256] = observation_index
                        prequential_index_fragments[
                            observation_index] = fragment_sha256
                    self.pcfg_prequential_receipts[
                        receipt_id] = receipt
        if int(raw.get("schema", 0)) >= 21:
            stored_circuit_contexts = raw.get(
                "pcfg_circuit_contexts", ())
            if isinstance(stored_circuit_contexts, list):
                for item in stored_circuit_contexts[
                        -self.HARD_MAX_PCFG_CIRCUIT_CONTEXTS:]:
                    if not isinstance(item, dict):
                        continue
                    parser = self._cfg_label(item.get("parser"))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    ancestor_shape_id = str(
                        item.get("ancestor_shape_id", ""))
                    parent_slot = item.get("parent_slot")
                    ancestor_slot = item.get("ancestor_slot")
                    family = self.pcfg_families.get(family_id)
                    if (
                        parser is None or family is None or
                        parser != family["parser"] or
                        not self._valid_digest(parent_shape_id) or
                        not self._valid_digest(ancestor_shape_id) or
                        not isinstance(parent_slot, int) or
                        isinstance(parent_slot, bool) or
                        not 0 <= parent_slot < 64 or
                        not isinstance(ancestor_slot, int) or
                        isinstance(ancestor_slot, bool) or
                        not 0 <= ancestor_slot < 64
                    ):
                        continue
                    context_id, context = self._pcfg_circuit_context(
                        parser,
                        family_id,
                        parent_shape_id,
                        parent_slot,
                        ancestor_shape_id,
                        ancestor_slot,
                    )
                    if (
                        item.get("context_id") != context_id or
                        not self._pcfg_circuit_context_is_active(
                            context, family_shapes)
                    ):
                        continue
                    self.pcfg_circuit_contexts[
                        context_id] = context

            stored_circuit_counts = raw.get(
                "pcfg_circuit_counts", {})
            if isinstance(stored_circuit_counts, dict):
                for context_id, raw_counts in list(
                    stored_circuit_counts.items()
                )[-self.HARD_MAX_PCFG_CIRCUIT_CONTEXTS:]:
                    context_id = str(context_id)
                    context = self.pcfg_circuit_contexts.get(
                        context_id)
                    if (
                        context is None or
                        not isinstance(raw_counts, dict)
                    ):
                        continue
                    valid_shapes = family_shapes.get(
                        str(context["family_id"]), set())
                    counts: dict[str, int] = {}
                    for shape_id, raw_count in list(
                        raw_counts.items()
                    )[:self.HARD_MAX_CFG_PRODUCTIONS]:
                        shape_id = str(shape_id)
                        if (
                            shape_id not in valid_shapes or
                            not isinstance(raw_count, int) or
                            isinstance(raw_count, bool) or
                            not 0 < raw_count <= (1 << 31) - 1
                        ):
                            continue
                        counts[shape_id] = raw_count
                    if counts:
                        self.pcfg_circuit_counts[
                            context_id] = counts

            stored_circuit_observations = raw.get(
                "pcfg_circuit_observations", {})
            circuit_tallies: dict[tuple[str, str], int] = {}
            if isinstance(stored_circuit_observations, dict):
                for fragment_sha256, raw_selections in list(
                    stored_circuit_observations.items()
                )[-self.HARD_MAX_PCFG_OBSERVATIONS:]:
                    fragment_sha256 = str(fragment_sha256)
                    if (
                        fragment_sha256 not in self.pcfg_observations or
                        not isinstance(raw_selections, list) or
                        len(raw_selections) > 32
                    ):
                        continue
                    selections: list[list[str]] = []
                    local_tallies: dict[
                        tuple[str, str], int
                    ] = {}
                    valid = True
                    for selection in raw_selections:
                        if (
                            not isinstance(selection, list) or
                            len(selection) != 2
                        ):
                            valid = False
                            break
                        context_id = str(selection[0])
                        shape_id = str(selection[1])
                        context = self.pcfg_circuit_contexts.get(
                            context_id)
                        if (
                            context is None or
                            shape_id not in
                            self.pcfg_circuit_counts.get(
                                context_id, {}) or
                            shape_id not in family_shapes.get(
                                str(context["family_id"]), set())
                        ):
                            valid = False
                            break
                        key = (context_id, shape_id)
                        local_tallies[key] = (
                            local_tallies.get(key, 0) + 1)
                        selections.append([context_id, shape_id])
                    if (
                        not valid or selections != sorted(selections) or
                        any(
                            circuit_tallies.get(key, 0) + count >
                            self.pcfg_circuit_counts[
                                key[0]][key[1]]
                            for key, count in local_tallies.items()
                        )
                    ):
                        continue
                    for key, count in local_tallies.items():
                        circuit_tallies[key] = (
                            circuit_tallies.get(key, 0) + count)
                    self.pcfg_circuit_observations[
                        fragment_sha256] = selections
            (
                self.pcfg_circuit_counts,
                self.pcfg_circuit_observations,
            ) = self._reconcile_pcfg_evidence(
                self.pcfg_circuit_counts,
                self.pcfg_circuit_observations,
            )
            for context_id in tuple(self.pcfg_circuit_contexts):
                if context_id not in self.pcfg_circuit_counts:
                    del self.pcfg_circuit_contexts[context_id]

            stored_circuit_receipts = raw.get(
                "pcfg_circuit_receipts", ())
            seen_circuit_events: set[
                tuple[str, str, str, str]
            ] = set()
            circuit_fragment_indexes: dict[str, int] = {}
            circuit_index_fragments: dict[int, str] = {}
            if isinstance(stored_circuit_receipts, list):
                for item in stored_circuit_receipts[
                        -self.HARD_MAX_PCFG_CIRCUIT_RECEIPTS:]:
                    if not isinstance(item, dict):
                        continue
                    fragment_sha256 = str(
                        item.get("fragment_sha256", ""))
                    node_id = str(item.get("node_id", ""))
                    context_id = str(item.get("context_id", ""))
                    parent_context_id = str(
                        item.get("parent_context_id", ""))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    ancestor_shape_id = str(
                        item.get("ancestor_shape_id", ""))
                    shape_id = str(item.get("shape_id", ""))
                    parent_slot = item.get("parent_slot")
                    ancestor_slot = item.get("ancestor_slot")
                    observation_index = item.get(
                        "observation_index")
                    numeric_fields = (
                        item.get("global_selected_before"),
                        item.get("global_total_before"),
                        item.get("known_shapes"),
                        item.get("parent_selected_before"),
                        item.get("parent_total_before"),
                        item.get("circuit_selected_before"),
                        item.get("circuit_total_before"),
                    )
                    context = self.pcfg_circuit_contexts.get(
                        context_id)
                    parent_context = self.pcfg_contexts.get(
                        parent_context_id)
                    event_key = (
                        fragment_sha256,
                        node_id,
                        context_id,
                        shape_id,
                    )
                    if (
                        context is None or parent_context is None or
                        not self._valid_digest(node_id) or
                        not isinstance(observation_index, int) or
                        isinstance(observation_index, bool) or
                        not 0 <= observation_index <
                        len(self.pcfg_observations) or
                        [context_id, shape_id] not in
                        self.pcfg_circuit_observations.get(
                            fragment_sha256, ()) or
                        family_id != context["family_id"] or
                        parent_shape_id !=
                        context["parent_shape_id"] or
                        parent_slot != context["parent_slot"] or
                        ancestor_shape_id !=
                        context["ancestor_shape_id"] or
                        ancestor_slot != context["ancestor_slot"] or
                        family_id != parent_context["family_id"] or
                        parent_shape_id !=
                        parent_context["parent_shape_id"] or
                        parent_slot != parent_context["slot"] or
                        any(
                            not isinstance(value, int) or
                            isinstance(value, bool)
                            for value in numeric_fields
                        ) or
                        event_key in seen_circuit_events
                    ):
                        continue
                    if (
                        fragment_sha256 in circuit_fragment_indexes and
                        circuit_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in circuit_index_fragments and
                        circuit_index_fragments[
                            observation_index] != fragment_sha256 or
                        fragment_sha256 in
                        prequential_fragment_indexes and
                        prequential_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in
                        prequential_index_fragments and
                        prequential_index_fragments[
                            observation_index] != fragment_sha256
                    ):
                        continue
                    (
                        global_selected_before,
                        global_total_before,
                        known_shapes,
                        parent_selected_before,
                        parent_total_before,
                        circuit_selected_before,
                        circuit_total_before,
                    ) = numeric_fields
                    final_global_counts = self.pcfg_counts.get(
                        family_id, {})
                    final_parent_counts = (
                        self.pcfg_context_counts.get(
                            parent_context_id, {})
                    )
                    final_circuit_counts = (
                        self.pcfg_circuit_counts.get(
                            context_id, {})
                    )
                    if (
                        not 0 <= global_selected_before <=
                        global_total_before < sum(
                            final_global_counts.values()) or
                        not 1 <= known_shapes <=
                        self.HARD_MAX_CFG_PRODUCTIONS or
                        not 0 <= parent_selected_before <=
                        parent_total_before < sum(
                            final_parent_counts.values()) or
                        not 0 <= circuit_selected_before <=
                        circuit_total_before < sum(
                            final_circuit_counts.values()) or
                        global_selected_before >=
                        final_global_counts.get(shape_id, 0) or
                        parent_selected_before >=
                        final_parent_counts.get(shape_id, 0) or
                        circuit_selected_before >=
                        final_circuit_counts.get(shape_id, 0)
                    ):
                        continue
                    receipt = self._pcfg_circuit_receipt(
                        fragment_sha256=fragment_sha256,
                        node_id=node_id,
                        context=context,
                        parent_context_id=parent_context_id,
                        shape_id=shape_id,
                        global_selected_before=
                            global_selected_before,
                        global_total_before=global_total_before,
                        known_shapes=known_shapes,
                        parent_selected_before=
                            parent_selected_before,
                        parent_total_before=parent_total_before,
                        circuit_selected_before=
                            circuit_selected_before,
                        circuit_total_before=circuit_total_before,
                        observation_index=observation_index,
                    )
                    receipt_id = str(receipt["receipt_id"])
                    if item.get("receipt_id") != receipt_id:
                        continue
                    seen_circuit_events.add(event_key)
                    circuit_fragment_indexes[
                        fragment_sha256] = observation_index
                    circuit_index_fragments[
                        observation_index] = fragment_sha256
                    self.pcfg_circuit_receipts[
                        receipt_id] = receipt
        if int(raw.get("schema", 0)) >= 22:
            stored_sibling_contexts = raw.get(
                "pcfg_sibling_contexts", ())
            if isinstance(stored_sibling_contexts, list):
                for item in stored_sibling_contexts[
                        -self.HARD_MAX_PCFG_SIBLING_CONTEXTS:]:
                    if not isinstance(item, dict):
                        continue
                    parser = self._cfg_label(item.get("parser"))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    left_sibling_shape_id = str(
                        item.get("left_sibling_shape_id", ""))
                    slot = item.get("slot")
                    family = self.pcfg_families.get(family_id)
                    if (
                        parser is None or family is None or
                        parser != family["parser"] or
                        not self._valid_digest(parent_shape_id) or
                        not self._valid_digest(
                            left_sibling_shape_id) or
                        not isinstance(slot, int) or
                        isinstance(slot, bool) or
                        not 1 <= slot < 64
                    ):
                        continue
                    context_id, context = self._pcfg_sibling_context(
                        parser,
                        family_id,
                        parent_shape_id,
                        slot,
                        left_sibling_shape_id,
                    )
                    if (
                        item.get("context_id") != context_id or
                        not self._pcfg_sibling_context_is_active(
                            context, family_shapes)
                    ):
                        continue
                    self.pcfg_sibling_contexts[
                        context_id] = context

            stored_sibling_counts = raw.get(
                "pcfg_sibling_counts", {})
            if isinstance(stored_sibling_counts, dict):
                for context_id, raw_counts in list(
                    stored_sibling_counts.items()
                )[-self.HARD_MAX_PCFG_SIBLING_CONTEXTS:]:
                    context_id = str(context_id)
                    context = self.pcfg_sibling_contexts.get(
                        context_id)
                    if (
                        context is None or
                        not isinstance(raw_counts, dict)
                    ):
                        continue
                    valid_shapes = family_shapes.get(
                        str(context["family_id"]), set())
                    counts: dict[str, int] = {}
                    for shape_id, raw_count in list(
                            raw_counts.items()
                    )[:self.HARD_MAX_CFG_PRODUCTIONS]:
                        shape_id = str(shape_id)
                        if (
                            shape_id not in valid_shapes or
                            not isinstance(raw_count, int) or
                            isinstance(raw_count, bool) or
                            not 0 < raw_count <= (1 << 31) - 1
                        ):
                            continue
                        counts[shape_id] = raw_count
                    if counts:
                        self.pcfg_sibling_counts[
                            context_id] = counts

            stored_sibling_observations = raw.get(
                "pcfg_sibling_observations", {})
            sibling_tallies: dict[tuple[str, str], int] = {}
            if isinstance(stored_sibling_observations, dict):
                for fragment_sha256, raw_selections in list(
                    stored_sibling_observations.items()
                )[-self.HARD_MAX_PCFG_OBSERVATIONS:]:
                    fragment_sha256 = str(fragment_sha256)
                    if (
                        fragment_sha256 not in self.pcfg_observations or
                        not isinstance(raw_selections, list) or
                        len(raw_selections) > 32
                    ):
                        continue
                    selections: list[list[str]] = []
                    local_tallies: dict[
                        tuple[str, str], int
                    ] = {}
                    valid = True
                    for selection in raw_selections:
                        if (
                            not isinstance(selection, list) or
                            len(selection) != 2
                        ):
                            valid = False
                            break
                        context_id = str(selection[0])
                        shape_id = str(selection[1])
                        context = self.pcfg_sibling_contexts.get(
                            context_id)
                        if (
                            context is None or
                            shape_id not in
                            self.pcfg_sibling_counts.get(
                                context_id, {}) or
                            shape_id not in family_shapes.get(
                                str(context["family_id"]), set())
                        ):
                            valid = False
                            break
                        key = (context_id, shape_id)
                        local_tallies[key] = (
                            local_tallies.get(key, 0) + 1)
                        selections.append([context_id, shape_id])
                    if (
                        not valid or selections != sorted(selections) or
                        any(
                            sibling_tallies.get(key, 0) + count >
                            self.pcfg_sibling_counts[
                                key[0]][key[1]]
                            for key, count in local_tallies.items()
                        )
                    ):
                        continue
                    for key, count in local_tallies.items():
                        sibling_tallies[key] = (
                            sibling_tallies.get(key, 0) + count)
                    self.pcfg_sibling_observations[
                        fragment_sha256] = selections
            (
                self.pcfg_sibling_counts,
                self.pcfg_sibling_observations,
            ) = self._reconcile_pcfg_evidence(
                self.pcfg_sibling_counts,
                self.pcfg_sibling_observations,
            )
            for context_id in tuple(self.pcfg_sibling_contexts):
                if context_id not in self.pcfg_sibling_counts:
                    del self.pcfg_sibling_contexts[context_id]

            stored_sibling_receipts = raw.get(
                "pcfg_sibling_receipts", ())
            seen_sibling_events: set[
                tuple[str, str, str, str]
            ] = set()
            sibling_fragment_indexes: dict[str, int] = {}
            sibling_index_fragments: dict[int, str] = {}
            if isinstance(stored_sibling_receipts, list):
                for item in stored_sibling_receipts[
                        -self.HARD_MAX_PCFG_SIBLING_RECEIPTS:]:
                    if not isinstance(item, dict):
                        continue
                    fragment_sha256 = str(
                        item.get("fragment_sha256", ""))
                    node_id = str(item.get("node_id", ""))
                    context_id = str(item.get("context_id", ""))
                    parent_context_id = str(
                        item.get("parent_context_id", ""))
                    circuit_context_id = str(
                        item.get("circuit_context_id", ""))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    left_sibling_shape_id = str(
                        item.get("left_sibling_shape_id", ""))
                    shape_id = str(item.get("shape_id", ""))
                    slot = item.get("slot")
                    observation_index = item.get(
                        "observation_index")
                    numeric_fields = (
                        item.get("global_selected_before"),
                        item.get("global_total_before"),
                        item.get("known_shapes"),
                        item.get("parent_selected_before"),
                        item.get("parent_total_before"),
                        item.get("circuit_selected_before"),
                        item.get("circuit_total_before"),
                        item.get("sibling_selected_before"),
                        item.get("sibling_total_before"),
                    )
                    context = self.pcfg_sibling_contexts.get(
                        context_id)
                    parent_context = self.pcfg_contexts.get(
                        parent_context_id)
                    circuit_context = (
                        self.pcfg_circuit_contexts.get(
                            circuit_context_id)
                        if circuit_context_id else None
                    )
                    event_key = (
                        fragment_sha256,
                        node_id,
                        context_id,
                        shape_id,
                    )
                    if (
                        context is None or parent_context is None or
                        circuit_context_id and
                        circuit_context is None or
                        not self._valid_digest(node_id) or
                        not isinstance(observation_index, int) or
                        isinstance(observation_index, bool) or
                        not 0 <= observation_index <
                        len(self.pcfg_observations) or
                        [context_id, shape_id] not in
                        self.pcfg_sibling_observations.get(
                            fragment_sha256, ()) or
                        [parent_context_id, shape_id] not in
                        self.pcfg_context_observations.get(
                            fragment_sha256, ()) or
                        circuit_context_id and
                        [circuit_context_id, shape_id] not in
                        self.pcfg_circuit_observations.get(
                            fragment_sha256, ()) or
                        family_id != context["family_id"] or
                        parent_shape_id !=
                        context["parent_shape_id"] or
                        slot != context["slot"] or
                        left_sibling_shape_id !=
                        context["left_sibling_shape_id"] or
                        family_id != parent_context["family_id"] or
                        parent_shape_id !=
                        parent_context["parent_shape_id"] or
                        slot != parent_context["slot"] or
                        circuit_context is not None and (
                            family_id !=
                            circuit_context["family_id"] or
                            parent_shape_id !=
                            circuit_context["parent_shape_id"] or
                            slot != circuit_context["parent_slot"]
                        ) or
                        any(
                            not isinstance(value, int) or
                            isinstance(value, bool)
                            for value in numeric_fields
                        ) or
                        event_key in seen_sibling_events
                    ):
                        continue
                    if (
                        fragment_sha256 in sibling_fragment_indexes and
                        sibling_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in sibling_index_fragments and
                        sibling_index_fragments[
                            observation_index] != fragment_sha256 or
                        fragment_sha256 in
                        prequential_fragment_indexes and
                        prequential_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in
                        prequential_index_fragments and
                        prequential_index_fragments[
                            observation_index] != fragment_sha256 or
                        circuit_context_id and
                        fragment_sha256 in
                        circuit_fragment_indexes and
                        circuit_fragment_indexes[
                            fragment_sha256] != observation_index or
                        circuit_context_id and
                        observation_index in
                        circuit_index_fragments and
                        circuit_index_fragments[
                            observation_index] != fragment_sha256
                    ):
                        continue
                    (
                        global_selected_before,
                        global_total_before,
                        known_shapes,
                        parent_selected_before,
                        parent_total_before,
                        circuit_selected_before,
                        circuit_total_before,
                        sibling_selected_before,
                        sibling_total_before,
                    ) = numeric_fields
                    final_global_counts = self.pcfg_counts.get(
                        family_id, {})
                    final_parent_counts = (
                        self.pcfg_context_counts.get(
                            parent_context_id, {})
                    )
                    final_circuit_counts = (
                        self.pcfg_circuit_counts.get(
                            circuit_context_id, {})
                    )
                    final_sibling_counts = (
                        self.pcfg_sibling_counts.get(
                            context_id, {})
                    )
                    circuit_counts_invalid = (
                        (
                            not 0 <= circuit_selected_before <=
                            circuit_total_before < sum(
                                final_circuit_counts.values()) or
                            circuit_selected_before >=
                            final_circuit_counts.get(shape_id, 0)
                        )
                        if circuit_context_id else
                        circuit_selected_before != 0 or
                        circuit_total_before != 0
                    )
                    if (
                        not 0 <= global_selected_before <=
                        global_total_before < sum(
                            final_global_counts.values()) or
                        not 1 <= known_shapes <=
                        self.HARD_MAX_CFG_PRODUCTIONS or
                        not 0 <= parent_selected_before <=
                        parent_total_before < sum(
                            final_parent_counts.values()) or
                        circuit_counts_invalid or
                        not 0 <= sibling_selected_before <=
                        sibling_total_before < sum(
                            final_sibling_counts.values()) or
                        global_selected_before >=
                        final_global_counts.get(shape_id, 0) or
                        parent_selected_before >=
                        final_parent_counts.get(shape_id, 0) or
                        sibling_selected_before >=
                        final_sibling_counts.get(shape_id, 0)
                    ):
                        continue
                    receipt = self._pcfg_sibling_receipt(
                        fragment_sha256=fragment_sha256,
                        node_id=node_id,
                        context=context,
                        parent_context_id=parent_context_id,
                        circuit_context_id=circuit_context_id,
                        shape_id=shape_id,
                        global_selected_before=
                            global_selected_before,
                        global_total_before=global_total_before,
                        known_shapes=known_shapes,
                        parent_selected_before=
                            parent_selected_before,
                        parent_total_before=parent_total_before,
                        circuit_selected_before=
                            circuit_selected_before,
                        circuit_total_before=circuit_total_before,
                        sibling_selected_before=
                            sibling_selected_before,
                        sibling_total_before=sibling_total_before,
                        observation_index=observation_index,
                    )
                    receipt_id = str(receipt["receipt_id"])
                    if item.get("receipt_id") != receipt_id:
                        continue
                    seen_sibling_events.add(event_key)
                    sibling_fragment_indexes[
                        fragment_sha256] = observation_index
                    sibling_index_fragments[
                        observation_index] = fragment_sha256
                    self.pcfg_sibling_receipts[
                        receipt_id] = receipt
        if int(raw.get("schema", 0)) >= 24:
            stored_history_contexts = raw.get(
                "pcfg_history_contexts", ())
            if isinstance(stored_history_contexts, list):
                for item in stored_history_contexts[
                        -self.HARD_MAX_PCFG_HISTORY_CONTEXTS:]:
                    if not isinstance(item, dict):
                        continue
                    parser = self._cfg_label(item.get("parser"))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    older_sibling_shape_id = str(
                        item.get("older_sibling_shape_id", ""))
                    left_sibling_shape_id = str(
                        item.get("left_sibling_shape_id", ""))
                    slot = item.get("slot")
                    family = self.pcfg_families.get(family_id)
                    if (
                        parser is None or family is None or
                        parser != family["parser"] or
                        not self._valid_digest(parent_shape_id) or
                        not self._valid_digest(
                            older_sibling_shape_id) or
                        not self._valid_digest(
                            left_sibling_shape_id) or
                        not isinstance(slot, int) or
                        isinstance(slot, bool) or
                        not 2 <= slot < 64
                    ):
                        continue
                    context_id, context = self._pcfg_history_context(
                        parser,
                        family_id,
                        parent_shape_id,
                        slot,
                        older_sibling_shape_id,
                        left_sibling_shape_id,
                    )
                    if (
                        item.get("context_id") != context_id or
                        not self._pcfg_history_context_is_active(
                            context, family_shapes)
                    ):
                        continue
                    self.pcfg_history_contexts[
                        context_id] = context

            stored_history_counts = raw.get(
                "pcfg_history_counts", {})
            if isinstance(stored_history_counts, dict):
                for context_id, raw_counts in list(
                    stored_history_counts.items()
                )[-self.HARD_MAX_PCFG_HISTORY_CONTEXTS:]:
                    context_id = str(context_id)
                    context = self.pcfg_history_contexts.get(
                        context_id)
                    if (
                        context is None or
                        not isinstance(raw_counts, dict)
                    ):
                        continue
                    valid_shapes = family_shapes.get(
                        str(context["family_id"]), set())
                    counts: dict[str, int] = {}
                    for shape_id, raw_count in list(
                            raw_counts.items()
                    )[:self.HARD_MAX_CFG_PRODUCTIONS]:
                        shape_id = str(shape_id)
                        if (
                            shape_id not in valid_shapes or
                            not isinstance(raw_count, int) or
                            isinstance(raw_count, bool) or
                            not 0 < raw_count <= (1 << 31) - 1
                        ):
                            continue
                        counts[shape_id] = raw_count
                    if counts:
                        self.pcfg_history_counts[
                            context_id] = counts

            stored_history_observations = raw.get(
                "pcfg_history_observations", {})
            history_tallies: dict[tuple[str, str], int] = {}
            if isinstance(stored_history_observations, dict):
                for fragment_sha256, raw_selections in list(
                    stored_history_observations.items()
                )[-self.HARD_MAX_PCFG_OBSERVATIONS:]:
                    fragment_sha256 = str(fragment_sha256)
                    if (
                        fragment_sha256 not in self.pcfg_observations or
                        not isinstance(raw_selections, list) or
                        len(raw_selections) > 32
                    ):
                        continue
                    selections: list[list[str]] = []
                    local_tallies: dict[
                        tuple[str, str], int
                    ] = {}
                    valid = True
                    for selection in raw_selections:
                        if (
                            not isinstance(selection, list) or
                            len(selection) != 2
                        ):
                            valid = False
                            break
                        context_id = str(selection[0])
                        shape_id = str(selection[1])
                        context = self.pcfg_history_contexts.get(
                            context_id)
                        if (
                            context is None or
                            shape_id not in
                            self.pcfg_history_counts.get(
                                context_id, {}) or
                            shape_id not in family_shapes.get(
                                str(context["family_id"]), set())
                        ):
                            valid = False
                            break
                        key = (context_id, shape_id)
                        local_tallies[key] = (
                            local_tallies.get(key, 0) + 1)
                        selections.append([context_id, shape_id])
                    if (
                        not valid or selections != sorted(selections) or
                        any(
                            history_tallies.get(key, 0) + count >
                            self.pcfg_history_counts[
                                key[0]][key[1]]
                            for key, count in local_tallies.items()
                        )
                    ):
                        continue
                    for key, count in local_tallies.items():
                        history_tallies[key] = (
                            history_tallies.get(key, 0) + count)
                    self.pcfg_history_observations[
                        fragment_sha256] = selections
            (
                self.pcfg_history_counts,
                self.pcfg_history_observations,
            ) = self._reconcile_pcfg_evidence(
                self.pcfg_history_counts,
                self.pcfg_history_observations,
            )
            for context_id in tuple(self.pcfg_history_contexts):
                if context_id not in self.pcfg_history_counts:
                    del self.pcfg_history_contexts[context_id]

            stored_history_receipts = raw.get(
                "pcfg_history_receipts", ())
            seen_history_events: set[
                tuple[str, str, str, str]
            ] = set()
            history_fragment_indexes: dict[str, int] = {}
            history_index_fragments: dict[int, str] = {}
            if isinstance(stored_history_receipts, list):
                for item in stored_history_receipts[
                        -self.HARD_MAX_PCFG_HISTORY_RECEIPTS:]:
                    if not isinstance(item, dict):
                        continue
                    fragment_sha256 = str(
                        item.get("fragment_sha256", ""))
                    node_id = str(item.get("node_id", ""))
                    context_id = str(item.get("context_id", ""))
                    parent_context_id = str(
                        item.get("parent_context_id", ""))
                    circuit_context_id = str(
                        item.get("circuit_context_id", ""))
                    sibling_context_id = str(
                        item.get("sibling_context_id", ""))
                    family_id = str(item.get("family_id", ""))
                    parent_shape_id = str(
                        item.get("parent_shape_id", ""))
                    older_sibling_shape_id = str(
                        item.get("older_sibling_shape_id", ""))
                    left_sibling_shape_id = str(
                        item.get("left_sibling_shape_id", ""))
                    shape_id = str(item.get("shape_id", ""))
                    slot = item.get("slot")
                    observation_index = item.get(
                        "observation_index")
                    numeric_fields = (
                        item.get("global_selected_before"),
                        item.get("global_total_before"),
                        item.get("known_shapes"),
                        item.get("parent_selected_before"),
                        item.get("parent_total_before"),
                        item.get("circuit_selected_before"),
                        item.get("circuit_total_before"),
                        item.get("sibling_selected_before"),
                        item.get("sibling_total_before"),
                        item.get("history_selected_before"),
                        item.get("history_total_before"),
                    )
                    context = self.pcfg_history_contexts.get(
                        context_id)
                    parent_context = self.pcfg_contexts.get(
                        parent_context_id)
                    circuit_context = (
                        self.pcfg_circuit_contexts.get(
                            circuit_context_id)
                        if circuit_context_id else None
                    )
                    sibling_context = self.pcfg_sibling_contexts.get(
                        sibling_context_id)
                    event_key = (
                        fragment_sha256,
                        node_id,
                        context_id,
                        shape_id,
                    )
                    if (
                        context is None or parent_context is None or
                        sibling_context is None or
                        circuit_context_id and
                        circuit_context is None or
                        not self._valid_digest(node_id) or
                        not isinstance(observation_index, int) or
                        isinstance(observation_index, bool) or
                        not 0 <= observation_index <
                        len(self.pcfg_observations) or
                        [context_id, shape_id] not in
                        self.pcfg_history_observations.get(
                            fragment_sha256, ()) or
                        [sibling_context_id, shape_id] not in
                        self.pcfg_sibling_observations.get(
                            fragment_sha256, ()) or
                        [parent_context_id, shape_id] not in
                        self.pcfg_context_observations.get(
                            fragment_sha256, ()) or
                        circuit_context_id and
                        [circuit_context_id, shape_id] not in
                        self.pcfg_circuit_observations.get(
                            fragment_sha256, ()) or
                        family_id != context["family_id"] or
                        parent_shape_id !=
                        context["parent_shape_id"] or
                        slot != context["slot"] or
                        older_sibling_shape_id !=
                        context["older_sibling_shape_id"] or
                        left_sibling_shape_id !=
                        context["left_sibling_shape_id"] or
                        family_id != parent_context["family_id"] or
                        parent_shape_id !=
                        parent_context["parent_shape_id"] or
                        slot != parent_context["slot"] or
                        family_id != sibling_context["family_id"] or
                        parent_shape_id !=
                        sibling_context["parent_shape_id"] or
                        slot != sibling_context["slot"] or
                        left_sibling_shape_id !=
                        sibling_context[
                            "left_sibling_shape_id"] or
                        circuit_context is not None and (
                            family_id !=
                            circuit_context["family_id"] or
                            parent_shape_id !=
                            circuit_context["parent_shape_id"] or
                            slot != circuit_context["parent_slot"]
                        ) or
                        any(
                            not isinstance(value, int) or
                            isinstance(value, bool)
                            for value in numeric_fields
                        ) or
                        event_key in seen_history_events
                    ):
                        continue
                    if (
                        fragment_sha256 in history_fragment_indexes and
                        history_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in history_index_fragments and
                        history_index_fragments[
                            observation_index] != fragment_sha256 or
                        fragment_sha256 in
                        prequential_fragment_indexes and
                        prequential_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in
                        prequential_index_fragments and
                        prequential_index_fragments[
                            observation_index] != fragment_sha256 or
                        fragment_sha256 in sibling_fragment_indexes and
                        sibling_fragment_indexes[
                            fragment_sha256] != observation_index or
                        observation_index in sibling_index_fragments and
                        sibling_index_fragments[
                            observation_index] != fragment_sha256 or
                        circuit_context_id and
                        fragment_sha256 in
                        circuit_fragment_indexes and
                        circuit_fragment_indexes[
                            fragment_sha256] != observation_index or
                        circuit_context_id and
                        observation_index in
                        circuit_index_fragments and
                        circuit_index_fragments[
                            observation_index] != fragment_sha256
                    ):
                        continue
                    (
                        global_selected_before,
                        global_total_before,
                        known_shapes,
                        parent_selected_before,
                        parent_total_before,
                        circuit_selected_before,
                        circuit_total_before,
                        sibling_selected_before,
                        sibling_total_before,
                        history_selected_before,
                        history_total_before,
                    ) = numeric_fields
                    final_global_counts = self.pcfg_counts.get(
                        family_id, {})
                    final_parent_counts = (
                        self.pcfg_context_counts.get(
                            parent_context_id, {})
                    )
                    final_circuit_counts = (
                        self.pcfg_circuit_counts.get(
                            circuit_context_id, {})
                    )
                    final_sibling_counts = (
                        self.pcfg_sibling_counts.get(
                            sibling_context_id, {})
                    )
                    final_history_counts = (
                        self.pcfg_history_counts.get(
                            context_id, {})
                    )
                    circuit_counts_invalid = (
                        (
                            not 0 <= circuit_selected_before <=
                            circuit_total_before < sum(
                                final_circuit_counts.values()) or
                            circuit_selected_before >=
                            final_circuit_counts.get(shape_id, 0)
                        )
                        if circuit_context_id else
                        circuit_selected_before != 0 or
                        circuit_total_before != 0
                    )
                    if (
                        not 0 <= global_selected_before <=
                        global_total_before < sum(
                            final_global_counts.values()) or
                        not 1 <= known_shapes <=
                        self.HARD_MAX_CFG_PRODUCTIONS or
                        not 0 <= parent_selected_before <=
                        parent_total_before < sum(
                            final_parent_counts.values()) or
                        circuit_counts_invalid or
                        not 0 <= sibling_selected_before <=
                        sibling_total_before < sum(
                            final_sibling_counts.values()) or
                        not 0 <= history_selected_before <=
                        history_total_before < sum(
                            final_history_counts.values()) or
                        global_selected_before >=
                        final_global_counts.get(shape_id, 0) or
                        parent_selected_before >=
                        final_parent_counts.get(shape_id, 0) or
                        sibling_selected_before >=
                        final_sibling_counts.get(shape_id, 0) or
                        history_selected_before >=
                        final_history_counts.get(shape_id, 0)
                    ):
                        continue
                    receipt = self._pcfg_history_receipt(
                        fragment_sha256=fragment_sha256,
                        node_id=node_id,
                        context=context,
                        parent_context_id=parent_context_id,
                        circuit_context_id=circuit_context_id,
                        sibling_context_id=sibling_context_id,
                        shape_id=shape_id,
                        global_selected_before=
                            global_selected_before,
                        global_total_before=global_total_before,
                        known_shapes=known_shapes,
                        parent_selected_before=
                            parent_selected_before,
                        parent_total_before=parent_total_before,
                        circuit_selected_before=
                            circuit_selected_before,
                        circuit_total_before=circuit_total_before,
                        sibling_selected_before=
                            sibling_selected_before,
                        sibling_total_before=sibling_total_before,
                        history_selected_before=
                            history_selected_before,
                        history_total_before=history_total_before,
                        observation_index=observation_index,
                    )
                    receipt_id = str(receipt["receipt_id"])
                    if item.get("receipt_id") != receipt_id:
                        continue
                    seen_history_events.add(event_key)
                    history_fragment_indexes[
                        fragment_sha256] = observation_index
                    history_index_fragments[
                        observation_index] = fragment_sha256
                    self.pcfg_history_receipts[
                        receipt_id] = receipt
        self._restore_pcfg_anytime_context_allocations(raw)
        stored_cycles = raw.get("cfg_cycles", ())
        if isinstance(stored_cycles, list):
            for item in stored_cycles[-self.HARD_MAX_CFG_CYCLES:]:
                if not isinstance(item, dict):
                    continue
                production = self.cfg_productions.get(
                    str(item.get("production_id", "")))
                path = item.get("path")
                try:
                    prefix = bytes.fromhex(str(item.get("prefix_hex", "")))
                    suffix = bytes.fromhex(str(item.get("suffix_hex", "")))
                    slot = int(item.get("slot"))
                    recursive_slots = int(item.get("recursive_slots"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    production is None or
                    item.get("kind") not in {"direct", "mutual"} or
                    not isinstance(path, list) or not 2 <= len(path) <= 32 or
                    not 0 <= slot < len(production["rhs"]) or
                    not 1 <= recursive_slots <= 64 or
                    not prefix and not suffix or
                    len(prefix) + len(suffix) > 128
                ):
                    continue
                normalized_path: list[list[str]] = []
                valid_path = True
                for path_item in path:
                    if (
                        not isinstance(path_item, list) or
                        len(path_item) != 2 or
                        self._cfg_label(path_item[0]) is None or
                        self._cfg_label(
                            path_item[1], allow_empty=True) is None
                    ):
                        valid_path = False
                        break
                    normalized_path.append([
                        str(path_item[0]), str(path_item[1])])
                if (
                    not valid_path or
                    normalized_path[0][0] != production["lhs"] or
                    normalized_path[-1][0] != production["lhs"]
                ):
                    continue
                cycle_core = {
                    "schema": "symcc-parser-cycle-v1",
                    "parser": production["parser"],
                    "production_id": production["id"],
                    "slot": slot,
                    "path": normalized_path,
                }
                cycle_id = hashlib.sha256(json.dumps(
                    cycle_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if cycle_id != item.get("cycle_id"):
                    continue
                self.cfg_cycles[cycle_id] = {
                    "cycle_id": cycle_id,
                    "production_id": production["id"],
                    "lhs": production["lhs"],
                    "slot": slot,
                    "kind": item["kind"],
                    "path": normalized_path,
                    "recursive_slots": recursive_slots,
                    "prefix_hex": prefix.hex(),
                    "suffix_hex": suffix.hex(),
                }
        stored_instances = raw.get("ect_instances", ())
        if isinstance(stored_instances, list):
            for item in stored_instances[-self.HARD_MAX_ECT_INSTANCES:]:
                if not isinstance(item, dict):
                    continue
                production = self.cfg_productions.get(
                    str(item.get("production_id", "")))
                path = item.get("path")
                gaps_raw = item.get("terminal_gap_hex")
                instance_schema = str(item.get(
                    "schema", "symcc-parser-subtree-instance-v1"))
                packed_instance = (
                    instance_schema ==
                    "symcc-parser-subtree-instance-v2")
                node_id = str(item.get("node_id", ""))
                node_path_raw = item.get("node_path", ())
                instance_alternative = item.get(
                    "alternative",
                    (
                        production.get("alternative", 0)
                        if production is not None else 0
                    ),
                )
                if (
                    production is None or
                    instance_schema not in {
                        "symcc-parser-subtree-instance-v1",
                        "symcc-parser-subtree-instance-v2",
                    } or
                    str(item.get("shape_id", "")) !=
                    production.get("shape_id") or
                    not isinstance(path, list) or
                    not 1 <= len(path) <= 32 or
                    not isinstance(gaps_raw, list) or
                    len(gaps_raw) != len(production["terminal_gaps"]) or
                    not isinstance(item.get("selected"), bool)
                ):
                    continue
                if packed_instance and (
                    node_id not in self.packed_nodes or
                    not isinstance(node_path_raw, list) or
                    not 1 <= len(node_path_raw) <= 32 or
                    any(str(path_node) not in self.packed_nodes
                        for path_node in node_path_raw) or
                    not isinstance(instance_alternative, int) or
                    isinstance(instance_alternative, bool) or
                    not 0 <= instance_alternative < 8 or
                    item.get("selected") is True and
                    instance_alternative != 0
                ):
                    continue
                if not packed_instance and (
                    node_id or node_path_raw
                ):
                    continue
                try:
                    subtree = bytes.fromhex(
                        str(item.get("yield_hex", "")))
                    gaps = [bytes.fromhex(str(gap)) for gap in gaps_raw]
                except ValueError:
                    continue
                yield_sha256 = hashlib.sha256(subtree).hexdigest()
                if (
                    len(subtree) > 256 or
                    production.get("epsilon", False) and bool(subtree) or
                    not production.get("epsilon", False) and not subtree or
                    sum(len(gap) for gap in gaps) > 256 or
                    yield_sha256 != item.get("yield_sha256") or
                    [
                        hashlib.sha256(gap).hexdigest()
                        for gap in gaps
                    ] != production["terminal_gaps"]
                ):
                    continue
                if production.get("epsilon", False) and any(gaps):
                    continue
                normalized_path: list[list[str]] = []
                valid_path = True
                for path_item in path:
                    if (
                        not isinstance(path_item, list) or
                        len(path_item) != 2 or
                        self._cfg_label(path_item[0]) is None or
                        self._cfg_label(
                            path_item[1], allow_empty=True) is None
                    ):
                        valid_path = False
                        break
                    normalized_path.append([
                        str(path_item[0]), str(path_item[1])])
                if (
                    not valid_path or
                    normalized_path[-1] != [
                        production["lhs"], production["state"]]
                ):
                    continue
                normalized_node_path: list[str] = []
                normalized_child_edge_ids: list[str] = []
                if packed_instance:
                    normalized_node_path = [
                        str(path_node) for path_node in node_path_raw]
                    node = self.packed_nodes[node_id]
                    edge_pairs = {
                        (edge["parent_id"], edge["child_id"])
                        for edge in self.packed_edges.values()
                    }
                    if (
                        normalized_node_path[-1] != node_id or
                        len(normalized_node_path) != len(normalized_path) or
                        [
                            [
                                self.packed_nodes[path_node]["symbol"],
                                self.packed_nodes[path_node]["state"],
                            ]
                            for path_node in normalized_node_path
                        ] != normalized_path or
                        any(
                            (parent, child) not in edge_pairs
                            for parent, child in zip(
                                normalized_node_path,
                                normalized_node_path[1:],
                            )
                        ) or
                        node["yield_sha256"] != yield_sha256 or
                        [production["lhs"], production["state"]] !=
                        [node["symbol"], node["state"]]
                    ):
                        continue
                    child_edge_ids_raw = item.get("child_edge_ids")
                    if (
                        int(raw.get("schema", 0)) >= 16 and
                        not isinstance(child_edge_ids_raw, list)
                    ):
                        continue
                    if isinstance(child_edge_ids_raw, list):
                        if (
                            len(child_edge_ids_raw) > 64 or
                            len(set(map(str, child_edge_ids_raw))) !=
                            len(child_edge_ids_raw)
                        ):
                            continue
                        instance_edges = [
                            self.packed_edges.get(str(edge_id))
                            for edge_id in child_edge_ids_raw
                        ]
                        if any(edge is None for edge in instance_edges):
                            continue
                    else:
                        instance_edges = [
                            edge for edge in self.packed_edges.values()
                            if (
                                edge["parent_id"] == node_id and
                                int(edge["alternative"]) ==
                                instance_alternative
                            )
                        ]
                    instance_edges.sort(key=lambda edge: (
                        int(edge["slot"]), str(edge["edge_id"])))
                    instance_slots = [
                        int(edge["slot"])
                        for edge in instance_edges
                    ]
                    if (
                        isinstance(child_edge_ids_raw, list) and
                        [str(edge["edge_id"])
                         for edge in instance_edges] !=
                        [str(edge_id)
                         for edge_id in child_edge_ids_raw] or
                        instance_slots !=
                        list(range(len(production["rhs"]))) or
                        any(
                            edge["parent_id"] != node_id or
                            int(edge["alternative"]) !=
                            instance_alternative or
                            not 0 <= int(edge["slot"]) <
                            len(production["rhs"]) or
                            [
                                self.packed_nodes[
                                    str(edge["child_id"])]["symbol"],
                                self.packed_nodes[
                                    str(edge["child_id"])]["state"],
                            ] != [
                                production["rhs"][
                                    int(edge["slot"])]["symbol"],
                                production["rhs"][
                                    int(edge["slot"])]["state"],
                            ]
                            for edge in instance_edges
                        )
                    ):
                        continue
                    normalized_child_edge_ids = [
                        str(edge["edge_id"])
                        for edge in instance_edges
                    ]
                instance_core = {
                    "schema": instance_schema,
                    "production_id": production["id"],
                    "shape_id": production["shape_id"],
                    "yield_sha256": yield_sha256,
                    "path": normalized_path,
                }
                if packed_instance:
                    instance_core.update({
                        "node_id": node_id,
                        "node_path": normalized_node_path,
                    })
                instance_id = hashlib.sha256(json.dumps(
                    instance_core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if instance_id != item.get("instance_id"):
                    continue
                normalized_instance = {
                    "instance_id": instance_id,
                    "production_id": production["id"],
                    "shape_id": production["shape_id"],
                    "parser": production["parser"],
                    "path": normalized_path,
                    "yield_hex": subtree.hex(),
                    "yield_sha256": yield_sha256,
                    "terminal_gap_hex": [gap.hex() for gap in gaps],
                    "selected": bool(item["selected"]),
                }
                if packed_instance:
                    normalized_instance.update({
                        "schema":
                            "symcc-parser-subtree-instance-v2",
                        "node_id": node_id,
                        "node_path": normalized_node_path,
                        "alternative": instance_alternative,
                        "child_edge_ids": normalized_child_edge_ids,
                    })
                self.ect_instances[instance_id] = normalized_instance
                self.ect_shapes.setdefault(
                    production["shape_id"], set()).add(instance_id)
        history_seeds = raw.get("history_seeds", ())
        if isinstance(history_seeds, list):
            for item in history_seeds[-self.max_history_seeds:]:
                if not isinstance(item, dict):
                    continue
                try:
                    content = bytes.fromhex(str(item.get("content", "")))
                    seed_id = str(item.get("seed_id", ""))
                    if (
                        not content or
                        len(content) > self.max_history_seed_bytes or
                        seed_id != hashlib.sha256(content).hexdigest()
                    ):
                        continue
                    seed = HistorySeed(
                        seed_id=seed_id,
                        content=content,
                        coverage_features=max(
                            0, int(item.get("coverage_features", 0))),
                        retained=max(0, int(item.get("retained", 0))),
                        attempts=max(0, int(item.get("attempts", 0))),
                        validations=max(
                            0, int(item.get("validations", 0))),
                        verified=max(0, int(item.get("verified", 0))),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if seed.verified <= seed.validations:
                    self.history_seeds[seed_id] = seed
        exemplars = raw.get("exemplars", {})
        if not isinstance(exemplars, dict):
            return
        for feature, item in list(exemplars.items())[-8192:]:
            if not isinstance(item, dict):
                continue
            try:
                parsed_feature = int(feature)
                fragment = bytes.fromhex(str(item.get("fragment", "")))
                matched = max(0, int(item.get("matched", 0)))
                width = max(1, int(item.get("width", 1)))
            except (TypeError, ValueError):
                continue
            if parsed_feature >= 0 and 0 < len(fragment) <= 64:
                self.exemplars[parsed_feature] = FeatureExemplar(
                    matched, width, fragment)
        fragments = raw.get("grammar_fragments", ())
        if isinstance(fragments, list):
            for item in fragments[-512:]:
                try:
                    fragment = bytes.fromhex(str(item))
                except ValueError:
                    continue
                if 0 < len(fragment) <= 64 and fragment not in self.grammar_fragments:
                    self.grammar_fragments.append(fragment)
        rules = raw.get("grammar_rules", ())
        if isinstance(rules, list):
            for item in rules[-self.max_grammar_rules:]:
                if not isinstance(item, dict):
                    continue
                try:
                    kind = str(item.get("kind", ""))
                    prefix = bytes.fromhex(str(item.get("prefix", "")))
                    body = bytes.fromhex(str(item.get("body", "")))
                    suffix = bytes.fromhex(str(item.get("suffix", "")))
                    rule_id = str(item.get("rule_id", ""))
                    if (
                        kind not in _GRAMMAR_RULE_KINDS or
                        rule_id != _grammar_rule_id(
                            kind, prefix, body, suffix)
                    ):
                        continue
                    rule = LearnedGrammarRule(
                        rule_id=rule_id,
                        kind=kind,
                        prefix=prefix,
                        body=body,
                        suffix=suffix,
                        support=max(0, int(item.get("support", 0))),
                        attempts=max(0, int(item.get("attempts", 0))),
                        validations=max(0, int(item.get("validations", 0))),
                        verified=max(0, int(item.get("verified", 0))),
                        retained=max(0, int(item.get("retained", 0))),
                        coverage_features=max(
                            0, int(item.get("coverage_features", 0))),
                        parser_validations=max(
                            0, int(item.get("parser_validations", 0))),
                        parser_accepted=max(
                            0, int(item.get("parser_accepted", 0))),
                        data_quality_sum=max(
                            0.0, float(item.get(
                                "data_quality_sum", 0.0))),
                        data_observations=max(
                            0, int(item.get("data_observations", 0))),
                        string_queries=max(
                            0, int(item.get("string_queries", 0))),
                        string_verified=max(
                            0, int(item.get("string_verified", 0))),
                        last_attempt_observation=max(
                            0, int(item.get(
                                "last_attempt_observation", 0))),
                    )
                    rejected_contexts_raw = item.get(
                        "rejected_contexts", {})
                    rejected_contexts: dict[str, int] = {}
                    if isinstance(rejected_contexts_raw, dict):
                        for context_id, count in list(
                                rejected_contexts_raw.items())[:256]:
                            if (
                                isinstance(context_id, str) and
                                len(context_id) == 64 and
                                all(character in "0123456789abcdef"
                                    for character in context_id)
                            ):
                                rejected_contexts[context_id] = max(
                                    0, min(65535, int(count)))
                    rule.rejected_contexts = rejected_contexts
                    structural_coverage_raw = item.get(
                        "structural_coverage", {})
                    structural_coverage: dict[str, int] = {}
                    if isinstance(structural_coverage_raw, dict):
                        for context_id, features in list(
                                structural_coverage_raw.items())[:256]:
                            if (
                                isinstance(context_id, str) and
                                len(context_id) == 64 and
                                all(character in "0123456789abcdef"
                                    for character in context_id)
                            ):
                                structural_coverage[context_id] = max(
                                    0, min(
                                        (1 << 31) - 1, int(features)))
                    rule.structural_coverage = structural_coverage
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    rule.verified <= rule.validations and
                    rule.parser_accepted <= rule.parser_validations and
                    rule.retained <= rule.verified and
                    math.isfinite(rule.data_quality_sum) and
                    rule.data_quality_sum <= rule.data_observations and
                    rule.string_verified <= rule.string_queries and
                    rule.last_attempt_observation <=
                    self.observations + 1 and
                    (body or kind in {
                        "optional", "recursive", "epsilon"}) and
                    (
                        kind != "recursive" or
                        bool(prefix or suffix) and not body
                    ) and
                    (
                        kind != "epsilon" or
                        not prefix and not body and not suffix
                    ) and
                    len(prefix) + len(body) + len(suffix) <= 256
                ):
                    self.grammar_rules[rule_id] = rule
        stored_rule_cycles = raw.get("recursive_rule_cycles", {})
        if isinstance(stored_rule_cycles, dict):
            for rule_id, raw_cycle_ids in list(
                    stored_rule_cycles.items())[-self.max_grammar_rules:]:
                rule = self.grammar_rules.get(str(rule_id))
                cycle_ids = (
                    raw_cycle_ids
                    if isinstance(raw_cycle_ids, list) else [raw_cycle_ids]
                )
                valid_cycles = {
                    str(cycle_id)
                    for cycle_id in cycle_ids[:self.HARD_MAX_CFG_CYCLES]
                    if str(cycle_id) in self.cfg_cycles
                }
                if (
                    rule is not None and rule.kind == "recursive" and
                    valid_cycles
                ):
                    self.recursive_rule_cycles[str(rule_id)] = valid_cycles
        stored_rule_shapes = raw.get("subtree_rule_shapes", {})
        stored_rule_instances = raw.get("subtree_rule_instances", {})
        if isinstance(stored_rule_shapes, dict):
            for rule_id, raw_shape_ids in list(
                    stored_rule_shapes.items())[-self.max_grammar_rules:]:
                rule = self.grammar_rules.get(str(rule_id))
                if (
                    rule is None or
                    rule.kind not in {
                        "subtree", "epsilon", "synchronized"}
                ):
                    continue
                shape_ids = (
                    raw_shape_ids
                    if isinstance(raw_shape_ids, list) else [raw_shape_ids]
                )
                valid_shapes = {
                    str(shape_id)
                    for shape_id in shape_ids[:self.HARD_MAX_ECT_SHAPES]
                    if str(shape_id) in self.ect_shapes
                }
                if valid_shapes:
                    self.subtree_rule_shapes[
                        str(rule_id)] = valid_shapes
        if isinstance(stored_rule_instances, dict):
            for rule_id, raw_instance_ids in list(
                    stored_rule_instances.items())[-self.max_grammar_rules:]:
                rule = self.grammar_rules.get(str(rule_id))
                if (
                    rule is None or
                    rule.kind not in {"subtree", "epsilon"} or
                    str(rule_id) not in self.subtree_rule_shapes
                ):
                    continue
                instance_ids = (
                    raw_instance_ids
                    if isinstance(raw_instance_ids, list)
                    else [raw_instance_ids]
                )
                valid_instances = {
                    str(instance_id)
                    for instance_id in instance_ids[
                        :self.HARD_MAX_ECT_INSTANCES]
                    if (
                        str(instance_id) in self.ect_instances and
                        self.ect_instances[
                            str(instance_id)]["shape_id"] in
                        self.subtree_rule_shapes[str(rule_id)] and
                        bytes.fromhex(
                            self.ect_instances[
                                str(instance_id)]["yield_hex"]) ==
                        rule.body
                    )
                }
                if valid_instances:
                    self.subtree_rule_instances[
                        str(rule_id)] = valid_instances
        if self.nullable_rules:
            # Nullable proofs and their shape authorizations depend on state
            # restored later than the rules themselves. Rebuild the least
            # fixed point instead of trusting persisted derived data.
            self._refresh_nullable_state()
        valid_shape_ids = set(self.ect_shapes) | self.nullable_shape_ids
        for context_id in tuple(self.parser_shape_aliases):
            self.parser_shape_aliases[context_id].intersection_update(
                valid_shape_ids)
            if not self.parser_shape_aliases[context_id]:
                del self.parser_shape_aliases[context_id]
        if self.packed_edges and self.ect_instances:
            # Transactions are derived from independently revalidated graph
            # evidence. Persisted transaction objects are audit data only.
            self._refresh_synchronized_state()
        if self.packed_nodes and self.ect_instances:
            # Posterior masses depend on the fully revalidated forest and
            # counts. Never trust or persist stale inside/outside tables.
            self._refresh_probabilistic_state()
