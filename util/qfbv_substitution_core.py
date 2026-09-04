#!/usr/bin/env python3
"""Proof-carrying QF_BV UNSAT-core reuse modulo input-byte renaming."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from qfbv_proof_receipt import (
    ProofAuthorization,
    ProofVerificationError,
    QfbvProofVerifier,
    normalize_proof_receipt,
)
from qfbv_artifact_lifecycle import (
    LIFECYCLE_PROTOCOL as ARTIFACT_LIFECYCLE_PROTOCOL,
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)


CORE_PROTOCOL = "symcc-qfbv-variable-substitution-unsat-core-v1"
CORE_RECORD_SCHEMA = "symcc-qfbv-substitution-core-record-v1"
CORE_STORE_SCHEMA = "symcc-qfbv-substitution-core-store-v1"
CORE_POLICY_SCHEMA = "symcc-qfbv-substitution-core-policy-v1"
CORE_QUERY_SCHEMA = "symcc-qfbv-substitution-core-query-v1"
NODE_SCHEMA = "symcc-expr-node-v1"
BLOOM_BITS = 1024
BLOOM_HASHES = 7
MAX_CORE_CLAUSES = 64
MAX_TARGET_CLAUSES = 4096
MAX_CORE_NODES = 250_000
MAX_CORE_DEPTH = 512
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_EXTRACTOR_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_EXTRACTOR_DIAGNOSTIC_BYTES = 64 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CORE_NAME = re.compile(r"symcc_core_([0-9]+)")

ProcessRegister = Callable[[subprocess.Popen[Any]], Any]
ProcessUnregister = Callable[[Any], None]


class SubstitutionCoreError(ValueError):
    """A core artifact, substitution, proof, or policy failed closed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex_digest(value: Any, name: str) -> str:
    parsed = str(value)
    if _HEX64.fullmatch(parsed) is None:
        raise SubstitutionCoreError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return parsed


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise SubstitutionCoreError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise SubstitutionCoreError(f"{name} must be an integer") from error
    if not lower <= parsed <= upper:
        raise SubstitutionCoreError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def _normalize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise SubstitutionCoreError("JSON nesting exceeds its bound")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if -(1 << 63) <= value <= (1 << 64) - 1:
            return value
        raise SubstitutionCoreError("JSON integer exceeds its bound")
    if isinstance(value, list):
        if len(value) > MAX_CORE_NODES:
            raise SubstitutionCoreError("JSON list exceeds its bound")
        return [_normalize_json(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise SubstitutionCoreError("JSON object exceeds its bound")
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key or "\x00" in raw_key:
                raise SubstitutionCoreError("JSON object has an invalid key")
            normalized[raw_key] = _normalize_json(raw_value, depth=depth + 1)
        return normalized
    raise SubstitutionCoreError("JSON value has an unsupported type")


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SubstitutionCoreError("core JSON contains a duplicate key")
        value[key] = item
    return value


def _normalize_node(raw: Mapping[str, Any], digest: str) -> dict[str, Any]:
    if set(raw) != {"schema", "op", "bits", "children", "attrs"}:
        raise SubstitutionCoreError("core expression has unexpected fields")
    if raw.get("schema") != NODE_SCHEMA:
        raise SubstitutionCoreError("core expression has an invalid schema")
    op = raw.get("op")
    if not isinstance(op, str) or not op or len(op) > 64:
        raise SubstitutionCoreError("core expression has an invalid operator")
    bits = _bounded_int(raw.get("bits"), "expression bits", 1, 1 << 20)
    raw_children = raw.get("children")
    if not isinstance(raw_children, list) or len(raw_children) > 3:
        raise SubstitutionCoreError("core expression children are invalid")
    children = [_hex_digest(child, "child digest") for child in raw_children]
    raw_attrs = raw.get("attrs")
    if not isinstance(raw_attrs, Mapping):
        raise SubstitutionCoreError("core expression attrs are invalid")
    attrs = _normalize_json(raw_attrs)
    if op == "read":
        if bits != 8 or children:
            raise SubstitutionCoreError("substitutable reads must be input bytes")
        _bounded_int(attrs.get("index"), "read index", 0, (1 << 32) - 1)
    node = {
        "schema": NODE_SCHEMA,
        "op": op,
        "bits": bits,
        "children": children,
        "attrs": attrs,
    }
    if _digest(_canonical_json(node)) != digest:
        raise SubstitutionCoreError("core expression digest mismatch")
    return node


def _normalize_expression_graph(
    roots: Sequence[Any],
    expressions: Mapping[str, Mapping[str, Any]],
    *,
    require_exact: bool,
    deadline: float | None = None,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    if (
        not isinstance(roots, Sequence)
        or isinstance(roots, (str, bytes))
        or not roots
        or len(roots) > MAX_TARGET_CLAUSES
    ):
        raise SubstitutionCoreError("formula roots exceed their bound")
    normalized_roots = tuple(_hex_digest(root, "root digest") for root in roots)
    if len(expressions) > MAX_CORE_NODES:
        raise SubstitutionCoreError("expression graph exceeds its node bound")
    normalized: dict[str, dict[str, Any]] = {}
    pending = list(normalized_roots)
    while pending:
        if len(normalized) % 1024 == 0 and (
            deadline is not None and time.monotonic() > deadline
        ):
            raise TimeoutError("expression normalization deadline expired")
        digest = pending.pop()
        if digest in normalized:
            continue
        raw = expressions.get(digest)
        if not isinstance(raw, Mapping):
            raise SubstitutionCoreError("expression graph is incomplete")
        node = _normalize_node(raw, digest)
        normalized[digest] = node
        pending.extend(child for child in node["children"] if child not in normalized)
        if len(normalized) > MAX_CORE_NODES:
            raise SubstitutionCoreError("reachable expression graph is too large")
    # Content hashes make cycles computationally infeasible, but rejecting them
    # explicitly keeps recursion and proof reconstruction bounded by contract.
    colors: dict[str, int] = {}
    for root in normalized_roots:
        if colors.get(root) == 2:
            continue
        stack: list[tuple[str, int, bool]] = [(root, 1, False)]
        while stack:
            if len(colors) % 1024 == 0 and (
                deadline is not None and time.monotonic() > deadline
            ):
                raise TimeoutError("expression normalization deadline expired")
            digest, depth, leaving = stack.pop()
            if depth > MAX_CORE_DEPTH:
                raise SubstitutionCoreError("expression graph depth exceeds its bound")
            if leaving:
                colors[digest] = 2
                continue
            color = colors.get(digest, 0)
            if color == 1:
                raise SubstitutionCoreError("expression graph contains a cycle")
            if color == 2:
                continue
            colors[digest] = 1
            stack.append((digest, depth, True))
            for child in reversed(normalized[digest]["children"]):
                if colors.get(child) == 1:
                    raise SubstitutionCoreError("expression graph contains a cycle")
                if colors.get(child) != 2:
                    stack.append((child, depth + 1, False))
    if require_exact and set(expressions) != set(normalized):
        raise SubstitutionCoreError("core record contains unreachable expressions")
    return normalized_roots, normalized


def structural_fingerprint(
    root: str,
    expressions: Mapping[str, Mapping[str, Any]],
    *,
    memo: dict[str, str] | None = None,
    deadline: float | None = None,
) -> str:
    """Hash one clause while alpha-renaming input-byte offsets."""
    cache = {} if memo is None else memo

    def fingerprint(digest: str, depth: int) -> str:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("clause fingerprint deadline expired")
        if depth > MAX_CORE_DEPTH:
            raise SubstitutionCoreError("fingerprint recursion exceeds its bound")
        cached = cache.get(digest)
        if cached is not None:
            return cached
        node = expressions.get(digest)
        if not isinstance(node, Mapping):
            raise SubstitutionCoreError("fingerprint expression is missing")
        attrs = _normalize_json(node.get("attrs"))
        if node.get("op") == "read":
            attrs = dict(attrs)
            attrs.pop("index", None)
        children = node.get("children")
        if not isinstance(children, list):
            raise SubstitutionCoreError("fingerprint children are invalid")
        body = {
            "schema": "symcc-qfbv-alpha-clause-v1",
            "op": str(node.get("op")),
            "bits": _bounded_int(node.get("bits"), "fingerprint bits", 1, 1 << 20),
            "attrs": attrs,
            "children": [fingerprint(str(child), depth + 1) for child in children],
        }
        value = _digest(_canonical_json(body))
        cache[digest] = value
        return value

    return fingerprint(_hex_digest(root, "fingerprint root"), 1)


def clause_footprints(
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    memo: dict[str, str] = {}
    return tuple(
        structural_fingerprint(
            root, expressions, memo=memo, deadline=deadline
        )
        for root in roots
    )


def bloom_for_footprints(footprints: Sequence[str]) -> str:
    bits = 0
    for raw in set(footprints):
        digest = bytes.fromhex(_hex_digest(raw, "clause footprint"))
        for index in range(BLOOM_HASHES):
            start = (index * 4) % (len(digest) - 3)
            position = int.from_bytes(digest[start : start + 4], "big") % BLOOM_BITS
            bits |= 1 << position
    return bits.to_bytes(BLOOM_BITS // 8, "big").hex()


def bloom_maybe_subset(core_bloom_hex: str, target_bloom_hex: str) -> bool:
    expected = BLOOM_BITS // 4
    if (
        not isinstance(core_bloom_hex, str)
        or not isinstance(target_bloom_hex, str)
        or len(core_bloom_hex) != expected
        or len(target_bloom_hex) != expected
    ):
        raise SubstitutionCoreError("Bloom filter has an invalid width")
    try:
        core = int(core_bloom_hex, 16)
        target = int(target_bloom_hex, 16)
    except ValueError as error:
        raise SubstitutionCoreError("Bloom filter is not hexadecimal") from error
    return core & ~target == 0


def _unify_clause(
    source_root: str,
    target_root: str,
    source: Mapping[str, Mapping[str, Any]],
    target: Mapping[str, Mapping[str, Any]],
    *,
    pair_budget: list[int],
    deadline: float,
) -> dict[int, int] | None:
    mapping: dict[int, int] = {}
    visited: set[tuple[str, str]] = set()

    def visit(source_digest: str, target_digest: str, depth: int) -> bool:
        if time.monotonic() > deadline:
            raise TimeoutError("substitution unification deadline expired")
        pair_budget[0] -= 1
        if pair_budget[0] < 0:
            raise SubstitutionCoreError("substitution unification budget exhausted")
        if depth > MAX_CORE_DEPTH:
            raise SubstitutionCoreError("substitution unification is too deep")
        pair = (source_digest, target_digest)
        if pair in visited:
            return True
        visited.add(pair)
        source_node = source[source_digest]
        target_node = target[target_digest]
        if (
            source_node["op"] != target_node["op"]
            or source_node["bits"] != target_node["bits"]
            or len(source_node["children"]) != len(target_node["children"])
        ):
            return False
        source_attrs = dict(source_node["attrs"])
        target_attrs = dict(target_node["attrs"])
        if source_node["op"] == "read":
            source_index = _bounded_int(
                source_attrs.pop("index", None), "source read index", 0, (1 << 32) - 1
            )
            target_index = _bounded_int(
                target_attrs.pop("index", None), "target read index", 0, (1 << 32) - 1
            )
            if source_attrs != target_attrs:
                return False
            previous = mapping.get(source_index)
            if previous is not None and previous != target_index:
                return False
            mapping[source_index] = target_index
            return True
        if source_attrs != target_attrs:
            return False
        return all(
            visit(str(source_child), str(target_child), depth + 1)
            for source_child, target_child in zip(
                source_node["children"], target_node["children"], strict=True
            )
        )

    return mapping if visit(source_root, target_root, 1) else None


def substituted_root_digests(
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    mapping: Mapping[int, int],
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    memo: dict[str, str] = {}

    def substitute(digest: str, depth: int) -> str:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("content substitution deadline expired")
        if depth > MAX_CORE_DEPTH:
            raise SubstitutionCoreError("substitution recursion exceeds its bound")
        cached = memo.get(digest)
        if cached is not None:
            return cached
        node = expressions[digest]
        attrs = dict(node["attrs"])
        if node["op"] == "read":
            source_index = _bounded_int(
                attrs.get("index"), "source read index", 0, (1 << 32) - 1
            )
            if source_index not in mapping:
                raise SubstitutionCoreError("substitution does not cover a source read")
            attrs["index"] = _bounded_int(
                mapping[source_index], "target read index", 0, (1 << 32) - 1
            )
        substituted = {
            "schema": NODE_SCHEMA,
            "op": node["op"],
            "bits": node["bits"],
            "children": [substitute(child, depth + 1) for child in node["children"]],
            "attrs": attrs,
        }
        result = _digest(_canonical_json(substituted))
        memo[digest] = result
        return result

    return tuple(substitute(str(root), 1) for root in roots)


@dataclass(frozen=True)
class SubstitutionMatch:
    mapping: dict[int, int]
    substituted_roots: tuple[str, ...]
    clause_pairs_considered: int
    candidate_rows: int
    join_states: int
    elapsed_us: int


def exact_substitution_match(
    source_roots: Sequence[str],
    source_expressions: Mapping[str, Mapping[str, Any]],
    target_roots: Sequence[str],
    target_expressions: Mapping[str, Mapping[str, Any]],
    *,
    timeout_ms: int = 5_000,
    max_unification_pairs: int = 1_000_000,
    max_join_states: int = 65_536,
) -> SubstitutionMatch | None:
    """Find one sort-preserving sigma with sigma(source) contained in target."""
    started = time.monotonic_ns()
    timeout = _bounded_int(timeout_ms, "substitution timeout_ms", 1, 3_600_000)
    pair_limit = _bounded_int(
        max_unification_pairs, "max_unification_pairs", 1, 100_000_000
    )
    join_limit = _bounded_int(max_join_states, "max_join_states", 1, 100_000_000)
    deadline = time.monotonic() + timeout / 1000.0
    normalized_source_roots, normalized_source = _normalize_expression_graph(
        source_roots,
        source_expressions,
        require_exact=False,
        deadline=deadline,
    )
    normalized_target_roots, normalized_target = _normalize_expression_graph(
        target_roots,
        target_expressions,
        require_exact=False,
        deadline=deadline,
    )
    if len(normalized_source_roots) > MAX_CORE_CLAUSES:
        raise SubstitutionCoreError("source core has too many clauses")
    source_fp = clause_footprints(
        normalized_source_roots, normalized_source, deadline=deadline
    )
    target_fp = clause_footprints(
        normalized_target_roots, normalized_target, deadline=deadline
    )
    targets_by_fp: dict[str, list[str]] = {}
    for root, fingerprint in zip(normalized_target_roots, target_fp, strict=True):
        targets_by_fp.setdefault(fingerprint, []).append(root)
    pair_budget = [pair_limit]
    tables: list[list[dict[int, int]]] = []
    clause_pairs = 0
    candidate_rows = 0
    for source_root, fingerprint in zip(
        normalized_source_roots, source_fp, strict=True
    ):
        rows: list[dict[int, int]] = []
        seen_rows: set[tuple[tuple[int, int], ...]] = set()
        for target_root in targets_by_fp.get(fingerprint, ()):
            clause_pairs += 1
            row = _unify_clause(
                source_root,
                target_root,
                normalized_source,
                normalized_target,
                pair_budget=pair_budget,
                deadline=deadline,
            )
            if row is not None:
                identity = tuple(sorted(row.items()))
                if identity not in seen_rows:
                    seen_rows.add(identity)
                    rows.append(row)
        if not rows:
            return None
        candidate_rows += len(rows)
        tables.append(rows)

    domains: dict[int, set[int]] = {}
    for table in tables:
        variables = {source for row in table for source in row}
        for source in variables:
            values = {row[source] for row in table if source in row}
            if source in domains:
                domains[source].intersection_update(values)
            else:
                domains[source] = values
            if not domains[source]:
                return None
    for table in tables:
        table[:] = [
            row
            for row in table
            if all(target in domains[source] for source, target in row.items())
        ]
        if not table:
            return None

    # Natural join the most selective clause tables first. Non-injective
    # substitutions are intentionally allowed by the underlying theorem.
    tables.sort(key=len)
    join_states = 0
    answer: dict[int, int] | None = None

    def join(index: int, mapping: dict[int, int]) -> bool:
        nonlocal join_states, answer
        if time.monotonic() > deadline:
            raise TimeoutError("substitution join deadline expired")
        join_states += 1
        if join_states > join_limit:
            raise SubstitutionCoreError("substitution join budget exhausted")
        if index == len(tables):
            answer = dict(mapping)
            return True
        for row in tables[index]:
            if all(mapping.get(source, target) == target for source, target in row.items()):
                added = [source for source in row if source not in mapping]
                mapping.update(row)
                if join(index + 1, mapping):
                    return True
                for source in added:
                    mapping.pop(source, None)
        return False

    if not join(0, {}) or answer is None:
        return None
    substituted = substituted_root_digests(
        normalized_source_roots,
        normalized_source,
        answer,
        deadline=deadline,
    )
    target_set = set(normalized_target_roots)
    if any(root not in target_set for root in substituted):
        raise SubstitutionCoreError(
            "joined substitution failed exact content-addressed subset verification"
        )
    return SubstitutionMatch(
        mapping=answer,
        substituted_roots=substituted,
        clause_pairs_considered=clause_pairs,
        candidate_rows=candidate_rows,
        join_states=join_states,
        elapsed_us=(time.monotonic_ns() - started) // 1000,
    )


def _core_query_id(roots: Sequence[str], capability_sha256: str) -> str:
    return _digest(
        _canonical_json(
            {
                "schema": CORE_QUERY_SCHEMA,
                "protocol": CORE_PROTOCOL,
                "roots": list(roots),
                "capability_sha256": capability_sha256,
            }
        )
    )


def _reachable_expressions(
    roots: Sequence[str], expressions: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    _roots, normalized = _normalize_expression_graph(
        roots, expressions, require_exact=False
    )
    return normalized


def build_core_record(
    *,
    source_query_id: str,
    source_roots: Sequence[str],
    source_expressions: Mapping[str, Mapping[str, Any]],
    capability_sha256: str,
    authorization: ProofAuthorization,
    exchange_policy_sha256: str,
    extractor_sha256: str,
) -> dict[str, Any]:
    source_id = _hex_digest(source_query_id, "source_query_id")
    capability = _hex_digest(capability_sha256, "capability_sha256")
    policy = _hex_digest(exchange_policy_sha256, "exchange_policy_sha256")
    extractor = _hex_digest(extractor_sha256, "extractor_sha256")
    roots, expressions = _normalize_expression_graph(
        source_roots, source_expressions, require_exact=False
    )
    if len(roots) > MAX_CORE_CLAUSES:
        raise SubstitutionCoreError("source core has too many clauses")
    footprints = clause_footprints(roots, expressions)
    offsets = sorted(
        {
            int(node["attrs"]["index"])
            for node in expressions.values()
            if node["op"] == "read"
        }
    )
    receipt = normalize_proof_receipt(authorization.receipt)
    core_query_id = _core_query_id(roots, capability)
    if (
        receipt["query_id"] != core_query_id
        or receipt["capability_sha256"] != capability
    ):
        raise SubstitutionCoreError("core proof receipt identity mismatch")
    body = {
        "schema": CORE_RECORD_SCHEMA,
        "protocol": CORE_PROTOCOL,
        "logic": "QF_BV",
        "source_query_id": source_id,
        "source_core_query_id": core_query_id,
        "source_roots": list(roots),
        "source_expressions": [
            {"digest": digest, "node": expressions[digest]}
            for digest in sorted(expressions)
        ],
        "source_clause_count": len(roots),
        "source_node_count": len(expressions),
        "source_variable_offsets": offsets,
        "source_clause_footprints": list(footprints),
        "source_bloom_hex": bloom_for_footprints(footprints),
        "capability_sha256": capability,
        "proof_receipt": receipt,
        "proof_receipt_sha256": receipt["receipt_sha256"],
        "checker_policy_sha256": receipt["checker_policy_sha256"],
        "exchange_policy_sha256": policy,
        "extractor_sha256": extractor,
    }
    body["record_sha256"] = _digest(_canonical_json(body))
    return normalize_core_record(body)


def normalize_core_record(
    raw: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    fields = {
        "schema",
        "protocol",
        "logic",
        "source_query_id",
        "source_core_query_id",
        "source_roots",
        "source_expressions",
        "source_clause_count",
        "source_node_count",
        "source_variable_offsets",
        "source_clause_footprints",
        "source_bloom_hex",
        "capability_sha256",
        "proof_receipt",
        "proof_receipt_sha256",
        "checker_policy_sha256",
        "exchange_policy_sha256",
        "extractor_sha256",
        "record_sha256",
    }
    if set(raw) != fields:
        raise SubstitutionCoreError("core record has unexpected fields")
    if (
        raw.get("schema") != CORE_RECORD_SCHEMA
        or raw.get("protocol") != CORE_PROTOCOL
        or raw.get("logic") != "QF_BV"
    ):
        raise SubstitutionCoreError("core record schema or protocol is invalid")
    roots_raw = raw.get("source_roots")
    if (
        not isinstance(roots_raw, list)
        or not roots_raw
        or len(roots_raw) > MAX_CORE_CLAUSES
    ):
        raise SubstitutionCoreError("core roots exceed their bound")
    expressions_raw = raw.get("source_expressions")
    if not isinstance(expressions_raw, list) or not expressions_raw:
        raise SubstitutionCoreError("core expressions must be a non-empty list")
    if len(expressions_raw) > MAX_CORE_NODES:
        raise SubstitutionCoreError("core expressions exceed their bound")
    expressions: dict[str, dict[str, Any]] = {}
    for entry in expressions_raw:
        if len(expressions) % 1024 == 0 and (
            deadline is not None and time.monotonic() > deadline
        ):
            raise TimeoutError("core record normalization deadline expired")
        if not isinstance(entry, Mapping) or set(entry) != {"digest", "node"}:
            raise SubstitutionCoreError("core expression entry is invalid")
        digest = _hex_digest(entry.get("digest"), "expression digest")
        if digest in expressions or not isinstance(entry.get("node"), Mapping):
            raise SubstitutionCoreError("core expression entry is duplicated")
        expressions[digest] = _normalize_node(entry["node"], digest)
    roots, reachable = _normalize_expression_graph(
        roots_raw,
        expressions,
        require_exact=True,
        deadline=deadline,
    )
    clause_count = _bounded_int(
        raw.get("source_clause_count"), "source_clause_count", 1, MAX_CORE_CLAUSES
    )
    node_count = _bounded_int(
        raw.get("source_node_count"), "source_node_count", 1, MAX_CORE_NODES
    )
    if clause_count != len(roots) or node_count != len(reachable):
        raise SubstitutionCoreError("core record counts are inconsistent")
    offsets_raw = raw.get("source_variable_offsets")
    if not isinstance(offsets_raw, list):
        raise SubstitutionCoreError("source variable offsets must be a list")
    offsets = [
        _bounded_int(value, "source variable offset", 0, (1 << 32) - 1)
        for value in offsets_raw
    ]
    expected_offsets = sorted(
        {
            int(node["attrs"]["index"])
            for node in reachable.values()
            if node["op"] == "read"
        }
    )
    if offsets != expected_offsets:
        raise SubstitutionCoreError("source variable offsets are not canonical")
    footprints_raw = raw.get("source_clause_footprints")
    if not isinstance(footprints_raw, list):
        raise SubstitutionCoreError("clause footprints must be a list")
    footprints = [_hex_digest(value, "clause footprint") for value in footprints_raw]
    if tuple(footprints) != clause_footprints(
        roots, reachable, deadline=deadline
    ):
        raise SubstitutionCoreError("clause footprints do not match the core")
    bloom = str(raw.get("source_bloom_hex", ""))
    if bloom != bloom_for_footprints(footprints):
        raise SubstitutionCoreError("core Bloom filter does not match its footprints")
    capability = _hex_digest(raw.get("capability_sha256"), "capability_sha256")
    core_query_id = _core_query_id(roots, capability)
    if _hex_digest(raw.get("source_core_query_id"), "source_core_query_id") != core_query_id:
        raise SubstitutionCoreError("source core query identity mismatch")
    proof_raw = raw.get("proof_receipt")
    if not isinstance(proof_raw, Mapping):
        raise SubstitutionCoreError("core proof receipt must be an object")
    try:
        receipt = normalize_proof_receipt(proof_raw)
    except ProofVerificationError as error:
        raise SubstitutionCoreError("core proof receipt is invalid") from error
    if (
        receipt["query_id"] != core_query_id
        or receipt["capability_sha256"] != capability
        or receipt["receipt_sha256"]
        != _hex_digest(raw.get("proof_receipt_sha256"), "proof_receipt_sha256")
        or receipt["checker_policy_sha256"]
        != _hex_digest(raw.get("checker_policy_sha256"), "checker_policy_sha256")
    ):
        raise SubstitutionCoreError("core proof receipt binding mismatch")
    normalized = {
        "schema": CORE_RECORD_SCHEMA,
        "protocol": CORE_PROTOCOL,
        "logic": "QF_BV",
        "source_query_id": _hex_digest(raw.get("source_query_id"), "source_query_id"),
        "source_core_query_id": core_query_id,
        "source_roots": list(roots),
        "source_expressions": [
            {"digest": digest, "node": reachable[digest]}
            for digest in sorted(reachable)
        ],
        "source_clause_count": clause_count,
        "source_node_count": node_count,
        "source_variable_offsets": offsets,
        "source_clause_footprints": footprints,
        "source_bloom_hex": bloom,
        "capability_sha256": capability,
        "proof_receipt": receipt,
        "proof_receipt_sha256": receipt["receipt_sha256"],
        "checker_policy_sha256": receipt["checker_policy_sha256"],
        "exchange_policy_sha256": _hex_digest(
            raw.get("exchange_policy_sha256"), "exchange_policy_sha256"
        ),
        "extractor_sha256": _hex_digest(raw.get("extractor_sha256"), "extractor_sha256"),
    }
    expected_record = _digest(_canonical_json(normalized))
    record_sha256 = _hex_digest(raw.get("record_sha256"), "record_sha256")
    if record_sha256 != expected_record:
        raise SubstitutionCoreError("core record digest mismatch")
    normalized["record_sha256"] = record_sha256
    if len(_canonical_json(normalized)) > MAX_RECORD_BYTES:
        raise SubstitutionCoreError("core record exceeds its byte contract")
    return normalized


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing core artifact")
        view = view[written:]


def _read_regular(path: Path, max_bytes: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for core artifact reads")
    descriptor = os.open(path, os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise SubstitutionCoreError("core artifact is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        path_state = os.stat(path, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            len(content) > max_bytes
            or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (before.st_dev, before.st_ino) != (path_state.st_dev, path_state.st_ino)
            or not stat.S_ISREG(path_state.st_mode)
        ):
            raise SubstitutionCoreError("core artifact changed during stable read")
        return content
    finally:
        os.close(descriptor)


class QfbvSubstitutionCoreStore:
    """Bounded SQLite index over immutable content-addressed core records."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_records: int = 10_000,
        max_bytes: int = 1 << 30,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ):
        root_path = Path(root)
        if root_path.is_symlink():
            raise SubstitutionCoreError(
                "substitution-core store root must not be a symlink"
            )
        self.root = root_path.resolve()
        self.object_dir = self.root / "objects"
        self.db_path = self.root / "index.sqlite3"
        self.lock_path = self.root / ".publish.lock"
        self.max_records = _bounded_int(max_records, "max_records", 1, 10_000_000)
        self.max_bytes = _bounded_int(max_bytes, "max_bytes", 4096, 1 << 50)
        if lifecycle_lease is not None and lifecycle is None:
            raise SubstitutionCoreError(
                "substitution-core lifecycle lease requires a registry"
            )
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self.root.mkdir(parents=True, exist_ok=True)
        if self.object_dir.is_symlink() or self.db_path.is_symlink():
            raise SubstitutionCoreError(
                "substitution-core store paths must not be symlinks"
            )
        self.object_dir.mkdir(parents=True, exist_ok=True)
        if not self.object_dir.is_dir():
            raise SubstitutionCoreError(
                "substitution-core object path is not a directory"
            )
        if self.lifecycle is None:
            self._initialize()
        else:
            with self.lifecycle.maintenance():
                self._initialize()

    def _record_lifecycle_record(
        self,
        record: Mapping[str, Any],
        encoded_bytes: int,
        *,
        now: float | None = None,
    ) -> None:
        if self.lifecycle is None:
            return
        self.lifecycle.record_artifact(
            ArtifactRef("core", str(record["record_sha256"])),
            encoded_bytes=encoded_bytes,
            edges=(
                ArtifactRef("receipt", str(record["proof_receipt_sha256"])),
            ),
            lease=self.lifecycle_lease,
            now=now,
        )

    def _assert_lifecycle_mode(self) -> None:
        if self.lifecycle is not None:
            return
        with self._connect() as db:
            managed = db.execute(
                "SELECT value FROM metadata WHERE key = 'lifecycle_protocol'"
            ).fetchone()
        if managed is not None:
            raise SubstitutionCoreError(
                "managed substitution-core store requires its artifact lifecycle"
            )

    @contextmanager
    def _operation(self, *, timeout_ms: int = 30_000):
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            yield
            return
        with self.lifecycle.operation(timeout_ms=timeout_ms):
            yield

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    record_sha256 TEXT PRIMARY KEY,
                    capability_sha256 TEXT NOT NULL,
                    exchange_policy_sha256 TEXT NOT NULL,
                    source_clause_count INTEGER NOT NULL,
                    source_bloom_hex TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_used REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS record_candidates
                    ON records(capability_sha256, last_used DESC);
                """
            )
            expected = {
                "schema": CORE_STORE_SCHEMA,
                "protocol": CORE_PROTOCOL,
            }
            if self.lifecycle is not None:
                expected.update(
                    {
                        "lifecycle_protocol": ARTIFACT_LIFECYCLE_PROTOCOL,
                        "lifecycle_identity_sha256": self.lifecycle.identity_sha256,
                    }
                )
            found = {
                str(row["key"]): str(row["value"])
                for row in db.execute("SELECT key, value FROM metadata")
            }
            for key, value in expected.items():
                if key in found and found[key] != value:
                    raise SubstitutionCoreError("core store metadata mismatch")
                db.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES(?, ?)",
                    (key, value),
                )

    def _record_path(self, record_sha256: str) -> Path:
        digest = _hex_digest(record_sha256, "record_sha256")
        return self.object_dir / digest[:2] / f"{digest}.json"

    def _ensure_record_parent(self, path: Path, *, create: bool) -> None:
        parent = path.parent
        if parent.is_symlink():
            raise SubstitutionCoreError(
                "substitution-core shard directory must not be a symlink"
            )
        if create:
            parent.mkdir(parents=False, exist_ok=True)
        try:
            metadata = parent.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise SubstitutionCoreError(
                "substitution-core shard directory is missing"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise SubstitutionCoreError(
                "substitution-core shard path is not a directory"
            )

    def publish(self, raw: Mapping[str, Any], *, lock_timeout_ms: int = 30_000) -> bool:
        with self._operation(timeout_ms=lock_timeout_ms):
            return self._publish(raw, lock_timeout_ms=lock_timeout_ms)

    def _publish(self, raw: Mapping[str, Any], *, lock_timeout_ms: int) -> bool:
        record = normalize_core_record(raw)
        content = _canonical_json(record) + b"\n"
        digest = record["record_sha256"]
        path = self._record_path(digest)
        self._ensure_record_parent(path, create=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for core store locking")
        lock = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(lock)
            path_state = os.stat(self.lock_path, follow_symlinks=False)
        except BaseException:
            os.close(lock)
            raise
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(path_state.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (path_state.st_dev, path_state.st_ino)
        ):
            os.close(lock)
            raise SubstitutionCoreError(
                "core publication lock is not a stable regular file"
            )
        deadline = time.monotonic() + _bounded_int(
            lock_timeout_ms, "lock_timeout_ms", 1, 3_600_000
        ) / 1000.0
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("core publication lock timeout")
                    time.sleep(0.005)
            locked = os.stat(self.lock_path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (locked.st_dev, locked.st_ino):
                raise SubstitutionCoreError(
                    "core publication lock changed while waiting"
                )
            with self._connect() as db:
                existing = db.execute(
                    "SELECT relative_path FROM records WHERE record_sha256 = ?",
                    (digest,),
                ).fetchone()
                if existing is not None:
                    loaded = self._load(digest)
                    if loaded != record:
                        raise SubstitutionCoreError("core CAS identity collision")
                    db.execute(
                        "UPDATE records SET last_used = ? WHERE record_sha256 = ?",
                        (time.time(), digest),
                    )
                    return False
                count, used = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(encoded_bytes), 0) FROM records"
                ).fetchone()
                if int(count) >= self.max_records or int(used) + len(content) > self.max_bytes:
                    raise SubstitutionCoreError("core store quota exceeded")
            if path.exists() or path.is_symlink():
                if _read_regular(path, MAX_RECORD_BYTES + 1) != content:
                    raise SubstitutionCoreError("core CAS file collision")
            else:
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{digest}.", suffix=".tmp", dir=path.parent
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    _write_all(descriptor, content)
                    os.fsync(descriptor)
                    os.close(descriptor)
                    descriptor = -1
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        pass
                    if _read_regular(path, MAX_RECORD_BYTES + 1) != content:
                        raise SubstitutionCoreError("core CAS pathname collision")
                    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
            now = time.time()
            self._record_lifecycle_record(record, len(content), now=now)
            relative = str(path.relative_to(self.root))
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO records("
                    "record_sha256, capability_sha256, exchange_policy_sha256, "
                    "source_clause_count, source_bloom_hex, encoded_bytes, "
                    "relative_path, created, last_used) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        record["capability_sha256"],
                        record["exchange_policy_sha256"],
                        record["source_clause_count"],
                        record["source_bloom_hex"],
                        len(content),
                        relative,
                        now,
                        now,
                    ),
                )
            return True
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                os.close(lock)

    def load(self, record_sha256: str) -> dict[str, Any]:
        with self._operation():
            return self._load(record_sha256)

    def _load(self, record_sha256: str) -> dict[str, Any]:
        digest = _hex_digest(record_sha256, "record_sha256")
        with self._connect() as db:
            row = db.execute(
                "SELECT relative_path, encoded_bytes FROM records WHERE record_sha256 = ?",
                (digest,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(digest)
        relative = Path(str(row["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise SubstitutionCoreError("core record path escapes its store")
        path = self._record_path(digest)
        self._ensure_record_parent(path, create=False)
        if relative != path.relative_to(self.root):
            raise SubstitutionCoreError("core record index path mismatch")
        content = _read_regular(path, MAX_RECORD_BYTES + 1)
        if len(content) != int(row["encoded_bytes"]):
            raise SubstitutionCoreError("core record size mismatch")
        try:
            raw = json.loads(
                content.decode("ascii"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise SubstitutionCoreError("core record JSON is invalid") from error
        if not isinstance(raw, Mapping):
            raise SubstitutionCoreError("core record JSON is not an object")
        record = normalize_core_record(raw)
        if record["record_sha256"] != digest:
            raise SubstitutionCoreError("loaded core identity mismatch")
        with self._connect() as db:
            db.execute(
                "UPDATE records SET last_used = ? WHERE record_sha256 = ?",
                (time.time(), digest),
            )
        self._record_lifecycle_record(record, len(content))
        return record

    def candidate_digests(
        self,
        *,
        capability_sha256: str,
        exchange_policy_sha256: str,
        target_clause_count: int,
        target_bloom_hex: str,
        limit: int = 64,
        scan_limit: int = 4096,
    ) -> tuple[list[str], int]:
        with self._operation():
            return self._candidate_digests(
                capability_sha256=capability_sha256,
                exchange_policy_sha256=exchange_policy_sha256,
                target_clause_count=target_clause_count,
                target_bloom_hex=target_bloom_hex,
                limit=limit,
                scan_limit=scan_limit,
            )

    def _candidate_digests(
        self,
        *,
        capability_sha256: str,
        exchange_policy_sha256: str,
        target_clause_count: int,
        target_bloom_hex: str,
        limit: int,
        scan_limit: int,
    ) -> tuple[list[str], int]:
        capability = _hex_digest(capability_sha256, "capability_sha256")
        policy = _hex_digest(exchange_policy_sha256, "exchange_policy_sha256")
        _bounded_int(
            target_clause_count, "target_clause_count", 1, MAX_TARGET_CLAUSES
        )
        bounded_limit = _bounded_int(limit, "candidate limit", 1, 4096)
        bounded_scan = _bounded_int(
            scan_limit, "candidate scan limit", bounded_limit, 1_000_000
        )
        with self._connect() as db:
            rows = db.execute(
                "SELECT record_sha256, source_bloom_hex FROM records "
                "WHERE capability_sha256 = ? AND exchange_policy_sha256 = ? "
                "ORDER BY last_used DESC, record_sha256 LIMIT ?",
                (capability, policy, bounded_scan),
            ).fetchall()
        digests: list[str] = []
        for row in rows:
            if bloom_maybe_subset(str(row["source_bloom_hex"]), target_bloom_hex):
                digests.append(str(row["record_sha256"]))
                if len(digests) >= bounded_limit:
                    break
        return digests, len(rows)

    def delete_lifecycle_artifact(
        self,
        kind: str,
        digest: str,
        expected_bytes: int,
    ) -> int:
        """Idempotently delete one unreachable record under the GC lock."""
        if kind != "core":
            raise SubstitutionCoreError(
                "substitution-core store cannot delete another artifact kind"
            )
        value = _hex_digest(digest, "record_sha256")
        size = _bounded_int(
            expected_bytes, "expected core bytes", 0, MAX_RECORD_BYTES + 1
        )
        path = self._record_path(value)
        self._ensure_record_parent(path, create=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT encoded_bytes, relative_path FROM records "
                "WHERE record_sha256 = ?",
                (value,),
            ).fetchone()
            if row is not None:
                if (
                    int(row["encoded_bytes"]) != size
                    or str(row["relative_path"])
                    != str(path.relative_to(self.root))
                ):
                    raise SubstitutionCoreError(
                        "core lifecycle identity disagrees with its index"
                    )
                db.execute(
                    "DELETE FROM records WHERE record_sha256 = ?", (value,)
                )
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise SubstitutionCoreError(
                "core lifecycle object is not the expected regular file"
            )
        encoded = _read_regular(path, MAX_RECORD_BYTES + 1)
        try:
            parsed = json.loads(
                encoded.decode("ascii"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise SubstitutionCoreError(
                "core lifecycle object is not valid JSON"
            ) from error
        record = normalize_core_record(parsed)
        if record["record_sha256"] != value:
            raise SubstitutionCoreError("core lifecycle digest mismatch")
        path.unlink()
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return len(encoded)

    def synchronize_lifecycle(self, *, max_entries: int) -> dict[str, int | bool]:
        """Validate and import every indexed core before collection."""
        if self.lifecycle is None:
            raise SubstitutionCoreError(
                "core lifecycle synchronization requires a registry"
            )
        limit = _bounded_int(
            max_entries, "core lifecycle scan limit", 1, 10_000_000
        )
        with self.lifecycle.operation():
            with self._connect() as db:
                count = int(
                    db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                )
                if count > limit:
                    return {"complete": False, "scanned": 0, "total": count}
                rows = db.execute(
                    "SELECT record_sha256, capability_sha256, "
                    "exchange_policy_sha256, source_clause_count, "
                    "source_bloom_hex, encoded_bytes, relative_path, last_used "
                    "FROM records ORDER BY record_sha256"
                ).fetchall()
            for row in rows:
                digest = _hex_digest(row["record_sha256"], "record_sha256")
                path = self._record_path(digest)
                self._ensure_record_parent(path, create=False)
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise SubstitutionCoreError(
                        "core lifecycle inventory path mismatch"
                    )
                encoded = _read_regular(path, MAX_RECORD_BYTES + 1)
                if len(encoded) != int(row["encoded_bytes"]):
                    raise SubstitutionCoreError(
                        "core lifecycle inventory size mismatch"
                    )
                try:
                    parsed = json.loads(
                        encoded.decode("ascii"),
                        object_pairs_hook=_object_without_duplicate_keys,
                    )
                except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
                    raise SubstitutionCoreError(
                        "core lifecycle inventory is not valid JSON"
                    ) from error
                record = normalize_core_record(parsed)
                if (
                    record["record_sha256"] != digest
                    or record["capability_sha256"]
                    != row["capability_sha256"]
                    or record["exchange_policy_sha256"]
                    != row["exchange_policy_sha256"]
                    or record["source_clause_count"]
                    != int(row["source_clause_count"])
                    or record["source_bloom_hex"] != row["source_bloom_hex"]
                ):
                    raise SubstitutionCoreError(
                        "core lifecycle inventory disagrees with its index"
                    )
                self._record_lifecycle_record(
                    record,
                    len(encoded),
                    now=float(row["last_used"]),
                )
        return {"complete": True, "scanned": count, "total": count}

    def stats(self) -> dict[str, Any]:
        with self._operation():
            return self._stats()

    def _stats(self) -> dict[str, Any]:
        with self._connect() as db:
            count, used = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(encoded_bytes), 0) FROM records"
            ).fetchone()
        return {
            "schema": CORE_STORE_SCHEMA,
            "protocol": CORE_PROTOCOL,
            "records": int(count),
            "encoded_bytes": int(used),
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
        }


def _parse_core_response(text: str, clause_count: int) -> tuple[int, ...]:
    if len(text.encode("utf-8", errors="replace")) > MAX_EXTRACTOR_OUTPUT_BYTES:
        raise SubstitutionCoreError("core extractor response exceeds its bound")
    tokens = re.findall(r"\(|\)|[^\s()]+", text)
    stack: list[list[Any]] = []
    forms: list[Any] = []
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            if not stack:
                raise SubstitutionCoreError("core extractor response is unbalanced")
            form = stack.pop()
            if stack:
                stack[-1].append(form)
            else:
                forms.append(form)
        elif stack:
            stack[-1].append(token)
        else:
            forms.append(token)
    if stack:
        raise SubstitutionCoreError("core extractor response is unbalanced")
    forms = [form for form in forms if form != "success"]
    if len(forms) != 2 or forms[0] != "unsat" or not isinstance(forms[1], list):
        raise SubstitutionCoreError(
            "core extractor must return exactly UNSAT and one core list"
        )
    indices: list[int] = []
    seen: set[int] = set()
    for name in forms[1]:
        if not isinstance(name, str):
            raise SubstitutionCoreError("nested core names are invalid")
        match = _CORE_NAME.fullmatch(name)
        if match is None:
            raise SubstitutionCoreError("core extractor returned an unknown name")
        index = int(match.group(1))
        if index >= clause_count or index in seen:
            raise SubstitutionCoreError("core extractor returned a duplicate or invalid name")
        seen.add(index)
        indices.append(index)
    if not indices or len(indices) > MAX_CORE_CLAUSES:
        raise SubstitutionCoreError("extracted core exceeds its clause bound")
    return tuple(sorted(indices))


def _interrupt_process(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _communicate_extractor_bounded(
    process: subprocess.Popen[Any],
    command: Sequence[str],
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise SubstitutionCoreError("core extractor pipes are unavailable")
    deadline = time.monotonic() + max(0.001, timeout_seconds)
    outputs: dict[int, tuple[bytearray, int, str]] = {
        process.stdout.fileno(): (
            bytearray(),
            MAX_EXTRACTOR_OUTPUT_BYTES,
            "output",
        ),
        process.stderr.fileno(): (
            bytearray(),
            MAX_EXTRACTOR_DIAGNOSTIC_BYTES,
            "diagnostic",
        ),
    }
    with selectors.DefaultSelector() as selector:
        for descriptor in outputs:
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                ready = selector.select(remaining)
            except InterruptedError:
                continue
            if not ready:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _events in ready:
                descriptor = int(key.fd)
                output, limit, label = outputs[descriptor]
                try:
                    chunk = os.read(
                        descriptor,
                        min(65536, limit + 1 - len(output)),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    raise SubstitutionCoreError(
                        f"core extractor {label} exceeds its bound"
                    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    process.wait(timeout=remaining)
    return bytes(outputs[process.stdout.fileno()][0]), bytes(
        outputs[process.stderr.fileno()][0]
    )


@dataclass(frozen=True)
class CoreReuseAuthorization:
    record: dict[str, Any]
    match: SubstitutionMatch
    checker_elapsed_us: int
    candidates: int
    candidate_scan: int
    proof_reused: bool


class QfbvSubstitutionCoreExchange:
    """Extract, prove, publish, and consume alpha-equivalent UNSAT cores."""

    def __init__(
        self,
        store: QfbvSubstitutionCoreStore,
        proof_verifier: QfbvProofVerifier,
        extractor_command: Sequence[str],
        *,
        max_candidates: int = 64,
        max_candidate_scan: int = 4096,
        max_join_states: int = 65_536,
        max_unification_pairs: int = 1_000_000,
        verified_core_cache_entries: int = 1024,
        lookup_timeout_ms: int = 5_000,
        publish_timeout_ms: int = 30_000,
    ):
        if not extractor_command or any(not str(item) for item in extractor_command):
            raise SubstitutionCoreError("core extractor command must not be empty")
        self.store = store
        self.proof_verifier = proof_verifier
        self.extractor_command = tuple(str(item) for item in extractor_command)
        self.max_candidates = _bounded_int(max_candidates, "max_candidates", 1, 4096)
        self.max_candidate_scan = _bounded_int(
            max_candidate_scan,
            "max_candidate_scan",
            self.max_candidates,
            1_000_000,
        )
        self.max_join_states = _bounded_int(
            max_join_states, "max_join_states", 1, 100_000_000
        )
        self.max_unification_pairs = _bounded_int(
            max_unification_pairs, "max_unification_pairs", 1, 100_000_000
        )
        self.verified_core_cache_entries = _bounded_int(
            verified_core_cache_entries,
            "verified_core_cache_entries",
            1,
            65_536,
        )
        self.lookup_timeout_ms = _bounded_int(
            lookup_timeout_ms, "lookup_timeout_ms", 1, 3_600_000
        )
        self.publish_timeout_ms = _bounded_int(
            publish_timeout_ms, "publish_timeout_ms", 1, 3_600_000
        )
        policy = {
            "schema": CORE_POLICY_SCHEMA,
            "protocol": CORE_PROTOCOL,
            "checker_policy_sha256": proof_verifier.policy_sha256,
            "extractor_command": list(self.extractor_command),
            "bloom_bits": BLOOM_BITS,
            "bloom_hashes": BLOOM_HASHES,
            "max_core_clauses": MAX_CORE_CLAUSES,
            "max_candidates": self.max_candidates,
            "max_candidate_scan": self.max_candidate_scan,
            "max_join_states": self.max_join_states,
            "max_unification_pairs": self.max_unification_pairs,
            "verified_core_cache_entries": self.verified_core_cache_entries,
            "lookup_timeout_ms": self.lookup_timeout_ms,
            "publish_timeout_ms": self.publish_timeout_ms,
        }
        self.policy_sha256 = _digest(_canonical_json(policy))
        self.extractor_sha256 = _digest(_canonical_json(list(self.extractor_command)))
        self._verified_core_lock = threading.Lock()
        self._verified_core_cache: OrderedDict[tuple[str, str], None] = (
            OrderedDict()
        )

    @staticmethod
    def _verification_domain(value: str) -> str:
        domain = str(value)
        if (
            not domain
            or len(domain) > 64
            or re.fullmatch(r"[a-z][a-z0-9-]*", domain) is None
        ):
            raise SubstitutionCoreError("core verification domain is invalid")
        return domain

    def _remember_verified_core(self, record_sha256: str, domain: str) -> None:
        key = (self._verification_domain(domain), record_sha256)
        with self._verified_core_lock:
            self._verified_core_cache[key] = None
            self._verified_core_cache.move_to_end(key)
            while len(self._verified_core_cache) > self.verified_core_cache_entries:
                self._verified_core_cache.popitem(last=False)

    def _verify_source_core_proof(
        self,
        record: Mapping[str, Any],
        proof_inputs: Mapping[str, Any],
        *,
        domain: str,
        deadline: float,
    ) -> tuple[int, bool]:
        key = (
            self._verification_domain(domain),
            str(record["record_sha256"]),
        )
        # Serialize the first verification to prevent a concurrent cache stampede.
        with self._verified_core_lock:
            if key in self._verified_core_cache:
                self._verified_core_cache.move_to_end(key)
                return 0, True
            remaining_ms = int((deadline - time.monotonic()) * 1000.0)
            if remaining_ms <= 0:
                raise TimeoutError("core proof verification deadline expired")
            try:
                checker_elapsed = self.proof_verifier.verify_receipt(
                    record["proof_receipt"],
                    **proof_inputs,
                    timeout_ms=remaining_ms,
                )
            except (OSError, sqlite3.Error, ProofVerificationError) as error:
                raise SubstitutionCoreError(
                    "source core proof verification failed"
                ) from error
            self._verified_core_cache[key] = None
            self._verified_core_cache.move_to_end(key)
            while len(self._verified_core_cache) > self.verified_core_cache_entries:
                self._verified_core_cache.popitem(last=False)
            return checker_elapsed, False

    @staticmethod
    def _record_expressions(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(entry["digest"]): dict(entry["node"])
            for entry in record["source_expressions"]
        }

    def _proof_inputs(
        self,
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        # Delayed import avoids a module cycle with the backend adapter.
        from qf_bv_backend import lower_qfbv_proof_problem

        capability = _hex_digest(
            capabilities.get("capability_sha256"), "capability_sha256"
        )
        query_id = _core_query_id(roots, capability)
        (
            smt2,
            proof_query_smt2,
            reference_smt2,
            certificate,
            offsets,
            _terms,
            context,
        ) = lower_qfbv_proof_problem(query_id, roots, expressions, capabilities)
        return {
            "query_id": query_id,
            "smt2": smt2.encode("ascii"),
            "proof_query_smt2": proof_query_smt2.encode("ascii"),
            "reference_smt2": reference_smt2.encode("ascii"),
            "offsets": offsets,
            "lowering_certificate_sha256": certificate["certificate_sha256"],
            "capability_sha256": capability,
            "context": context,
        }

    def verify_record_for_formula(
        self,
        raw: Mapping[str, Any],
        target_roots: Sequence[str],
        target_expressions: Mapping[str, Mapping[str, Any]],
        capabilities: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
        expected_mapping: Mapping[int, int] | None = None,
        verification_domain: str = "worker",
    ) -> CoreReuseAuthorization:
        bounded_timeout = self.lookup_timeout_ms if timeout_ms is None else _bounded_int(
            timeout_ms, "timeout_ms", 1, 3_600_000
        )
        deadline = time.monotonic() + bounded_timeout / 1000.0
        record = normalize_core_record(raw, deadline=deadline)
        capability = _hex_digest(
            capabilities.get("capability_sha256"), "capability_sha256"
        )
        if (
            record["exchange_policy_sha256"] != self.policy_sha256
            or record["checker_policy_sha256"] != self.proof_verifier.policy_sha256
            or record["capability_sha256"] != capability
        ):
            raise SubstitutionCoreError("core record policy identity mismatch")
        expressions = self._record_expressions(record)
        proof_inputs = self._proof_inputs(record["source_roots"], expressions, capabilities)
        if time.monotonic() >= deadline:
            raise TimeoutError("core proof reconstruction deadline expired")
        checker_elapsed, proof_reused = self._verify_source_core_proof(
            record,
            proof_inputs,
            domain=verification_domain,
            deadline=deadline,
        )
        remaining_ms = int((deadline - time.monotonic()) * 1000.0)
        if remaining_ms <= 0:
            raise TimeoutError("core verification deadline expired")
        match = exact_substitution_match(
            record["source_roots"],
            expressions,
            target_roots,
            target_expressions,
            timeout_ms=remaining_ms,
            max_unification_pairs=self.max_unification_pairs,
            max_join_states=self.max_join_states,
        )
        if match is None:
            raise SubstitutionCoreError("core has no exact variable substitution")
        if expected_mapping is not None:
            expected = {int(source): int(target) for source, target in expected_mapping.items()}
            if match.mapping != expected:
                raise SubstitutionCoreError("recomputed substitution mapping differs")
        return CoreReuseAuthorization(
            record=record,
            match=match,
            checker_elapsed_us=checker_elapsed,
            candidates=1,
            candidate_scan=1,
            proof_reused=proof_reused,
        )

    def lookup(
        self,
        target_roots: Sequence[str],
        target_expressions: Mapping[str, Mapping[str, Any]],
        capabilities: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> CoreReuseAuthorization | None:
        bounded_timeout = self.lookup_timeout_ms if timeout_ms is None else _bounded_int(
            timeout_ms, "timeout_ms", 1, 3_600_000
        )
        deadline = time.monotonic() + bounded_timeout / 1000.0
        roots, expressions = _normalize_expression_graph(
            target_roots,
            target_expressions,
            require_exact=False,
            deadline=deadline,
        )
        footprints = clause_footprints(
            roots, expressions, deadline=deadline
        )
        candidate_digests, candidate_scan = self.store.candidate_digests(
            capability_sha256=str(capabilities.get("capability_sha256", "")),
            exchange_policy_sha256=self.policy_sha256,
            target_clause_count=len(roots),
            target_bloom_hex=bloom_for_footprints(footprints),
            limit=self.max_candidates,
            scan_limit=self.max_candidate_scan,
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("core candidate lookup deadline expired")
        checked = 0
        for record_sha256 in candidate_digests:
            remaining_ms = int((deadline - time.monotonic()) * 1000.0)
            if remaining_ms <= 0:
                break
            checked += 1
            try:
                record = self.store.load(record_sha256)
                remaining_ms = int((deadline - time.monotonic()) * 1000.0)
                if remaining_ms <= 0:
                    break
                authorization = self.verify_record_for_formula(
                    record,
                    roots,
                    expressions,
                    capabilities,
                    timeout_ms=remaining_ms,
                )
            except (FileNotFoundError, OSError, sqlite3.Error, TimeoutError, SubstitutionCoreError):
                continue
            return CoreReuseAuthorization(
                record=authorization.record,
                match=authorization.match,
                checker_elapsed_us=authorization.checker_elapsed_us,
                candidates=checked,
                candidate_scan=candidate_scan,
                proof_reused=authorization.proof_reused,
            )
        return None

    def _extract_indices(
        self,
        root_terms: Sequence[str],
        offsets: Sequence[int],
        *,
        timeout_ms: int,
        register_process: ProcessRegister | None,
        unregister_process: ProcessUnregister | None,
    ) -> tuple[tuple[int, ...], int]:
        if not root_terms or len(root_terms) > MAX_TARGET_CLAUSES:
            raise SubstitutionCoreError("extractor formula roots exceed their bound")
        script_rows = [
            "(set-logic QF_BV)",
            "(set-option :produce-unsat-cores true)",
        ]
        script_rows.extend(
            f"(declare-const symcc_input_{int(offset)} (_ BitVec 8))"
            for offset in offsets
        )
        script_rows.extend(
            f"(assert (! {term} :named symcc_core_{index}))"
            for index, term in enumerate(root_terms)
        )
        script_rows.extend(("(check-sat)", "(get-unsat-core)", "(exit)"))
        script = "\n".join(script_rows) + "\n"
        started = time.monotonic_ns()
        query_path = ""
        process: subprocess.Popen[Any] | None = None
        token: Any = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", suffix=".smt2", delete=False
            ) as query:
                query.write(script)
                query_path = query.name
            command = [
                item.replace("{query}", query_path).replace("{timeout_ms}", str(timeout_ms))
                for item in self.extractor_command
            ]
            if not any("{query}" in item for item in self.extractor_command):
                command.append(query_path)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            cancelled = None
            if register_process is not None:
                registration = register_process(process)
                if (
                    not isinstance(registration, tuple)
                    or len(registration) != 2
                ):
                    raise SubstitutionCoreError(
                        "core process registration returned an invalid token"
                    )
                token, cancelled = registration
            try:
                stdout, stderr = _communicate_extractor_bounded(
                    process,
                    command,
                    timeout_ms / 1000.0,
                )
            except subprocess.TimeoutExpired as error:
                _interrupt_process(process)
                raise TimeoutError("core extractor timeout") from error
            finally:
                if token is not None and unregister_process is not None:
                    unregister_process(token)
            if cancelled is not None and cancelled.is_set():
                raise SubstitutionCoreError("core extraction was cancelled")
            if process.returncode not in {0, 10, 20} or stderr.strip():
                diagnostic = (stderr + b"\n" + stdout)[-512:].decode(
                    "utf-8", errors="replace"
                )
                raise SubstitutionCoreError(f"core extractor failed: {diagnostic}")
            try:
                text = stdout.decode("ascii")
            except UnicodeDecodeError as error:
                raise SubstitutionCoreError("core extractor output is not ASCII") from error
            return (
                _parse_core_response(text, len(root_terms)),
                (time.monotonic_ns() - started) // 1000,
            )
        except (TimeoutError, SubstitutionCoreError):
            raise
        except OSError as error:
            raise SubstitutionCoreError("cannot execute core extractor") from error
        finally:
            if process is not None and process.poll() is None:
                _interrupt_process(process)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            if query_path:
                try:
                    os.unlink(query_path)
                except FileNotFoundError:
                    pass

    def publish_from_unsat(
        self,
        *,
        source_query_id: str,
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        root_terms: Sequence[str],
        offsets: Sequence[int],
        capabilities: Mapping[str, Any],
        timeout_ms: int | None = None,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> tuple[dict[str, Any], bool, int, int]:
        if (register_process is None) != (unregister_process is None):
            raise SubstitutionCoreError(
                "core process registration callbacks must be paired"
            )
        bounded_timeout = self.publish_timeout_ms if timeout_ms is None else _bounded_int(
            timeout_ms, "timeout_ms", 1, 3_600_000
        )
        deadline = time.monotonic() + bounded_timeout / 1000.0
        indices, extractor_elapsed = self._extract_indices(
            root_terms,
            offsets,
            timeout_ms=bounded_timeout,
            register_process=register_process,
            unregister_process=unregister_process,
        )
        core_roots = tuple(str(roots[index]) for index in indices)
        core_expressions = _reachable_expressions(core_roots, expressions)
        proof_inputs = self._proof_inputs(core_roots, core_expressions, capabilities)
        remaining_ms = int((deadline - time.monotonic()) * 1000.0)
        if remaining_ms <= 0:
            raise TimeoutError("core publication deadline expired")
        try:
            authorization = self.proof_verifier.authorize(
                **proof_inputs,
                timeout_ms=remaining_ms,
                register_process=register_process,
                unregister_process=unregister_process,
            )
        except (OSError, sqlite3.Error, ProofVerificationError) as error:
            raise SubstitutionCoreError("extracted core failed independent proof") from error
        if authorization is None:
            raise SubstitutionCoreError("extracted core proof was not authorized")
        record = build_core_record(
            source_query_id=source_query_id,
            source_roots=core_roots,
            source_expressions=core_expressions,
            capability_sha256=str(capabilities.get("capability_sha256", "")),
            authorization=authorization,
            exchange_policy_sha256=self.policy_sha256,
            extractor_sha256=self.extractor_sha256,
        )
        self._remember_verified_core(record["record_sha256"], "worker")
        created = self.store.publish(
            record,
            lock_timeout_ms=max(1, int((deadline - time.monotonic()) * 1000.0)),
        )
        return (
            record,
            created,
            extractor_elapsed,
            authorization.generator_elapsed_us + authorization.checker_elapsed_us,
        )
