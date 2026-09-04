#!/usr/bin/env python3
"""Executable oracle for plan-scoped bounded QF_BV proof replay caching."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    REPLAY_CACHE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofStore,
    make_imported_lrup_clause_record,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402


def _plan(depth: int):
    expressions = {
        "true": {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": True},
        },
        "false": {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": False},
        },
    }
    return bitblast_qfbv_query(
        f"f450-proof-cache-depth-{depth}",
        ["true", "false"],
        expressions,
    )


def _build_chain(plan, store: IncrementalProofStore, depth: int) -> str:
    checker = IncrementalProofChecker(store)
    record = make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker="f450-leaf",
        worker_epoch=0,
        sequence=0,
    )
    authorization = checker.verify_clause_record(plan, record)
    digest, _created = store.publish(record)
    if authorization.record_sha256 != digest:
        raise RuntimeError("leaf publication changed proof identity")
    imported_id = len(plan.clauses) + 1
    for sequence in range(1, depth):
        record = make_imported_lrup_clause_record(
            plan,
            [authorization],
            [(authorization.clause, [imported_id])],
            dependency_assumptions=plan.assumptions,
            source_worker="f450-parent",
            worker_epoch=0,
            sequence=sequence,
        )
        authorization = checker.verify_clause_record(plan, record)
        digest, _created = store.publish(record)
        if authorization.record_sha256 != digest:
            raise RuntimeError("parent publication changed proof identity")
    return digest


def _replay(checker, plan, store, digest: str) -> tuple[int, Any]:
    started = time.monotonic_ns()
    authorization = checker.verify_clause_record(plan, store.load(digest))
    return (time.monotonic_ns() - started) // 1000, authorization


def _case(depth: int, rounds: int) -> dict[str, Any]:
    plan = _plan(depth)
    with tempfile.TemporaryDirectory() as directory:
        store = IncrementalProofStore(directory)
        digest = _build_chain(plan, store, depth)
        disabled = IncrementalProofChecker(
            store,
            replay_cache_entries=0,
            replay_cache_bytes=0,
        )
        enabled = IncrementalProofChecker(
            store,
            replay_cache_entries=max(64, depth * 2),
            replay_cache_bytes=max(1024 * 1024, depth * 2048),
        )
        cold_enabled_us, cold = _replay(enabled, plan, store, digest)
        disabled_us: list[int] = []
        enabled_us: list[int] = []
        for _round in range(rounds):
            elapsed, baseline = _replay(disabled, plan, store, digest)
            disabled_us.append(elapsed)
            elapsed, cached = _replay(enabled, plan, store, digest)
            enabled_us.append(elapsed)
            if (
                baseline.record_sha256 != cached.record_sha256
                or baseline.clause != cached.clause
                or baseline.propagation_count != cached.propagation_count
            ):
                raise RuntimeError("cached and uncached authorizations diverged")
        enabled_stats = enabled.replay_cache_stats()
        disabled_stats = disabled.replay_cache_stats()
        if (
            cold.replay_cache_hit
            or enabled_stats["entries"] != depth
            or enabled_stats["hits"] != rounds
            or enabled_stats["misses"] != depth
            or enabled_stats["attestation_failures"] != 0
            or enabled_stats["attested_objects"] != (depth - 1) * rounds
            or (depth > 1 and enabled_stats["attested_bytes"] <= 0)
            or disabled_stats["bypassed"] != depth * rounds
        ):
            raise RuntimeError("replay cache accounting is not conservative")
        baseline_median = int(statistics.median(disabled_us))
        cached_median = int(statistics.median(enabled_us))
        return {
            "depth": depth,
            "rounds": rounds,
            "record_sha256": digest,
            "cold_enabled_us": cold_enabled_us,
            "disabled_median_us": baseline_median,
            "enabled_hot_median_us": cached_median,
            "mechanism_replay_ratio": round(baseline_median / max(1, cached_median), 6),
            "disabled_samples_us": disabled_us,
            "enabled_hot_samples_us": enabled_us,
            "disabled_cache": disabled_stats,
            "enabled_cache": enabled_stats,
            "proof_records": store.stats()["records"],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--depths", default="8,32,64")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= 100:
        raise ValueError("--rounds must be in [1, 100]")
    depths = [int(value) for value in args.depths.split(",")]
    if (
        not depths
        or len(set(depths)) != len(depths)
        or any(not 1 <= value <= 256 for value in depths)
    ):
        raise ValueError("depths must be unique values in [1, 256]")
    payload = {
        "schema": "symcc-f450-proof-replay-cache-oracle-v1",
        "status": "pass",
        "rounds": args.rounds,
        "cases": [_case(depth, args.rounds) for depth in depths],
        "cache_protocol": REPLAY_CACHE_PROTOCOL,
        "claim": (
            "proof-DAG replay mechanism only; ratios are not SAT solving, "
            "fuzzing coverage, or defect-yield measurements"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
