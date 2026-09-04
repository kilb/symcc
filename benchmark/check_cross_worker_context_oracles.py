#!/usr/bin/env python3
"""Independent finite and live oracles for cross-worker QF_BV contexts."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from cross_worker_context import (  # noqa: E402
    CONTEXT_PROTOCOL,
    CONTEXT_SCHEMA,
    LOWERING_PROTOCOL,
    CrossWorkerContextStore,
    build_context_manifests,
)
from qf_bv_backend import (  # noqa: E402
    PersistentSmtLibQfbvSolver,
)
from query_store import QueryStore  # noqa: E402


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reference_chain(
    roots: Sequence[str],
    terms: Sequence[str],
    capability: str,
) -> tuple[dict[str, Any], ...]:
    """Small independent encoder used only by this oracle."""
    parent = ""
    offsets: set[int] = set()
    formula_rows: list[tuple[str, str]] = []
    manifests: list[dict[str, Any]] = []
    for depth, (root, term) in enumerate(zip(roots, terms), start=1):
        term_hash = _digest(term.encode("ascii"))
        previous = set(offsets)
        for token in term.replace("(", " ").replace(")", " ").split():
            if token.startswith("symcc_input_"):
                suffix = token[len("symcc_input_"):]
                if suffix.isdigit():
                    offsets.add(int(suffix))
        formula_rows.append((root, term_hash))
        body: dict[str, Any] = {
            "schema": CONTEXT_SCHEMA,
            "protocol": CONTEXT_PROTOCOL,
            "lowering_protocol": LOWERING_PROTOCOL,
            "logic": "QF_BV",
            "parent_context_sha256": parent,
            "capability_sha256": capability,
            "root_hash": root,
            "term": term,
            "term_sha256": term_hash,
            "delta_offsets": sorted(offsets - previous),
            "offsets": sorted(offsets),
            "offsets_sha256": _digest(_canonical_json(tuple(sorted(offsets)))),
            "depth": depth,
            "formula_sha256": _digest(_canonical_json(formula_rows)),
        }
        parent = _digest(_canonical_json(body))
        body["context_sha256"] = parent
        manifests.append(body)
    return tuple(manifests)


def _finite_oracle() -> dict[str, Any]:
    cases = 0
    for depth, offset_base, operator in itertools.product(
        range(1, 5), range(4), ("=", "bvuge")
    ):
        roots = tuple(
            _digest(f"root:{depth}:{offset_base}:{operator}:{index}".encode())
            for index in range(depth)
        )
        terms = tuple(
            f"({operator} symcc_input_{offset_base + index} (_ bv{index + 1} 8))"
            for index in range(depth)
        )
        capability = _digest(f"capability:{operator}".encode())
        production = build_context_manifests(
            roots,
            terms,
            capability_sha256=capability,
        )
        reference = _reference_chain(roots, terms, capability)
        assert production == reference
        cases += 1
    return {
        "differential_cases": cases,
        "false_identity": 0,
        "maximum_depth": 4,
    }


def _envelope(prefix_depth: int, target_value: int) -> dict[str, Any]:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "cross-worker-context-oracle",
        "nodes": [
            {"id": 0, "op": "read", "bits": 8, "children": [], "attrs": {"index": 0}},
            {"id": 1, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": "40"}},
            {"id": 2, "op": "uge", "bits": 1, "children": [0, 1], "attrs": {}},
            {"id": 3, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": "50"}},
            {"id": 4, "op": "ule", "bits": 1, "children": [0, 3], "attrs": {}},
            {"id": 5, "op": "constant", "bits": 8, "children": [], "attrs": {"value_hex": f"{target_value:02x}"}},
            {"id": 6, "op": "equal", "bits": 1, "children": [0, 5], "attrs": {}},
        ],
        "prefix_roots": [2, 4][:prefix_depth],
        "target_root": 6,
        "input_hex": "00",
        "timeout_ms": 5000,
        "metadata": {"source": "cross-worker-context-oracle"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def _live_oracle() -> dict[str, Any] | None:
    cvc5 = shutil.which("cvc5")
    if cvc5 is None:
        return None
    with tempfile.TemporaryDirectory(prefix="symcc-cross-context-oracle-") as tmp:
        root = Path(tmp)
        query_store = QueryStore(root / "queries")
        context_store = CrossWorkerContextStore(root / "contexts")
        command = [
            cvc5,
            "--lang",
            "smt2",
            "--incremental",
            "--produce-models",
        ]

        def lease(prefix_depth: int, target: int, owner: str):
            query_store.ingest(_envelope(prefix_depth, target))
            claimed = query_store.claim(owner)
            assert claimed is not None
            return claimed

        backend_a = PersistentSmtLibQfbvSolver(
            query_store,
            command,
            name="cvc5-worker-a",
            capabilities={"incremental": True},
            prefix_cache_entries=2,
            shared_context_store=context_store,
            context_owner="worker-a",
        )
        try:
            first_lease = lease(1, 0x42, "worker-a-1")
            first = dict(backend_a(first_lease))
            assert query_store.complete(first_lease, "worker-a-1", first)
            second_lease = lease(2, 0x43, "worker-a-2")
            second = dict(backend_a(second_lease))
            assert query_store.complete(second_lease, "worker-a-2", second)
        finally:
            backend_a.close()

        backend_b = PersistentSmtLibQfbvSolver(
            query_store,
            command,
            name="cvc5-worker-b",
            capabilities={"incremental": True},
            prefix_cache_entries=2,
            shared_context_store=CrossWorkerContextStore(root / "contexts"),
            context_owner="worker-b",
        )
        try:
            third_lease = lease(2, 0x44, "worker-b")
            third = dict(backend_b(third_lease))
            assert query_store.complete(third_lease, "worker-b", third)
        finally:
            backend_b.close()

        assert first["assignments"] == {"0": 0x42}
        assert second["assignments"] == {"0": 0x43}
        assert third["assignments"] == {"0": 0x44}
        assert second["backend_parent_context_reused"] is True
        assert third["backend_shared_context_exact_hit"] is True
        assert third["prefix_cache_hit"] is False
        return {
            "solver": "cvc5",
            "statuses": [first["status"], second["status"], third["status"]],
            "assignments": [
                first["assignments"],
                second["assignments"],
                third["assignments"],
            ],
            "parent_reuse": bool(second["backend_parent_context_reused"]),
            "cross_worker_exact_hit": bool(
                third["backend_shared_context_exact_hit"]
            ),
            "cross_worker_local_hit": bool(third["prefix_cache_hit"]),
            "context_objects": context_store.stats()["contexts"],
            "query_store_stats": {
                key: query_store.stats()[key]
                for key in (
                    "cross_worker_context_results",
                    "cross_worker_context_exact_hits",
                    "cross_worker_context_parent_reuses",
                    "cross_worker_context_quota_timeouts",
                )
            },
        }


def _mechanism_cost(iterations: int) -> dict[str, Any]:
    roots = tuple(_digest(f"cost-root-{index}".encode()) for index in range(8))
    terms = tuple(
        f"(= symcc_input_{index} (_ bv{index} 8))" for index in range(8)
    )
    capability = _digest(b"cost-capability")
    samples: list[int] = []
    with tempfile.TemporaryDirectory(prefix="symcc-context-cost-") as tmp:
        store = CrossWorkerContextStore(tmp)
        for _ in range(iterations):
            started = time.perf_counter_ns()
            publication = store.publish_chain(
                roots,
                terms,
                capability_sha256=capability,
            )
            assert publication is not None
            store.resolve(
                publication.context_sha256,
                expected_capability_sha256=capability,
            )
            samples.append(time.perf_counter_ns() - started)
    return {
        "iterations": iterations,
        "median_ns": int(statistics.median(samples)),
        "minimum_ns": min(samples),
        "maximum_ns": max(samples),
        "operation": "eight-level exact publish plus verified resolve",
        "claim": "mechanism cost only; not an end-to-end speedup result",
    }


def run_oracle(iterations: int) -> dict[str, Any]:
    finite = _finite_oracle()
    live = _live_oracle()
    payload = {
        "schema": "symcc-cross-worker-qfbv-context-oracle-v1",
        "all_passed": live is not None,
        "finite": finite,
        "live": live,
        "mechanism_cost": _mechanism_cost(iterations),
        "claim_boundary": (
            "Content-addressed QF_BV prefix-plan transport, fenced "
            "materialization, local parent extension, and store-verified SAT; "
            "not serialized solver internals, learned-clause transport, a "
            "checkable SMT UNSAT proof, or a public-target speedup result"
        ),
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10_000:
        parser.error("--iterations must be in [1, 10000]")
    payload = run_oracle(args.iterations)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="ascii")
    print(encoded)
    return 0 if payload["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
