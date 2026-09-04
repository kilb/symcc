#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Mechanism benchmark for F433 real-time checked clause exchange."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_conformance import build_operator_matrix_envelope  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_realtime_stream import (  # noqa: E402
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
)
from query_store import QueryStore  # noqa: E402


SCHEMA = "symcc-f433-realtime-stream-benchmark-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(samples: list[int]) -> dict[str, int]:
    ordered = sorted(samples)
    return {
        "rounds": len(ordered),
        "median_us": int(statistics.median(ordered)),
        "p95_us": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "minimum_us": ordered[0],
        "maximum_us": ordered[-1],
        "total_us": sum(ordered),
    }


def _context(owner, plan, *, imports: int):
    native = owner.new_context(
        max_learned_length=0,
        max_imports=imports,
        max_import_literals=4096,
        max_learned=0,
    )
    for clause in plan.clauses:
        for literal in clause:
            native.add(literal)
        native.add(0)
    native.observe(plan.max_variable)
    return native


def _assume_and_check(native, plan) -> None:
    for literal in plan.assumptions:
        native.assume(literal)
    if native.solve() != 10:
        raise RuntimeError("benchmark formula did not remain SAT")
    true_variables = {
        abs(literal)
        for _offset, literals in plan.input_literals
        for literal in literals
        if native.val(abs(literal)) > 0
    }
    if plan.input_bytes_from_model(true_variables) != {0: 0x42, 1: 0x03}:
        raise RuntimeError("benchmark model changed")


def run(library: Path, *, rounds: int) -> dict:
    owner = NativeRealtimeCadical(library)
    with tempfile.TemporaryDirectory(prefix="symcc-f433-benchmark-") as directory:
        root = Path(directory)
        query_store = QueryStore(root / "queries")
        query_id, _ = query_store.ingest(
            build_operator_matrix_envelope(timeout_ms=30_000)
        )
        roots, expressions = query_store.load_query_ir(query_id)
        plan = bitblast_qfbv_query(
            query_id, roots, expressions, {"incremental": True}
        )
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        clause = min(plan.clauses, key=lambda item: (len(item), item))
        proof_store.publish(make_rup_clause_record(
            plan,
            clause,
            source_worker="f433-benchmark-publisher",
            worker_epoch=0,
            sequence=0,
        ))

        cold_samples = []
        for _ in range(rounds):
            started = time.monotonic_ns()
            native = _context(owner, plan, imports=0)
            try:
                _assume_and_check(native, plan)
            finally:
                native.close()
            cold_samples.append((time.monotonic_ns() - started) // 1000)

        native = _context(owner, plan, imports=0)
        try:
            _assume_and_check(native, plan)
            reuse_samples = []
            for _ in range(rounds):
                started = time.monotonic_ns()
                _assume_and_check(native, plan)
                reuse_samples.append((time.monotonic_ns() - started) // 1000)
        finally:
            native.close()

        sequence = 1

        def next_sequence() -> int:
            nonlocal sequence
            current = sequence
            sequence += 1
            return current

        native = _context(owner, plan, imports=0)
        try:
            idle_samples = []
            for ordinal in range(1, rounds + 1):
                for literal in plan.assumptions:
                    native.assume(literal)
                started = time.monotonic_ns()
                session = RealtimeClauseExchangeSession(
                    plan,
                    native,
                    proof_store,
                    checker,
                    native_signature=owner.signature,
                    source_worker="f433-benchmark-idle",
                    worker_epoch=0,
                    next_sequence=next_sequence,
                    stream_ordinal=ordinal,
                    seen_records=proof_store.records_for_formula(
                        plan.formula_sha256, limit=64
                    ),
                    max_imports=0,
                    max_events=64,
                    max_learned=0,
                    poll_interval_ms=1,
                )
                session.start()
                if native.solve() != 10:
                    raise RuntimeError("idle stream solve failed")
                evidence = session.finish(
                    native.stats()["solve_generation"]
                )
                if evidence["backend_realtime_import_delivered"] != 0:
                    raise RuntimeError("idle stream unexpectedly imported a clause")
                idle_samples.append((time.monotonic_ns() - started) // 1000)
        finally:
            native.close()

        loaded_samples = []
        delivered = 0
        for ordinal in range(1, rounds + 1):
            started = time.monotonic_ns()
            native = _context(owner, plan, imports=8)
            try:
                for literal in plan.assumptions:
                    native.assume(literal)
                before = native.stats()["imports_enqueued"]
                session = RealtimeClauseExchangeSession(
                    plan,
                    native,
                    proof_store,
                    checker,
                    native_signature=owner.signature,
                    source_worker="f433-benchmark-loaded",
                    worker_epoch=0,
                    next_sequence=next_sequence,
                    stream_ordinal=ordinal,
                    max_imports=1,
                    max_events=64,
                    max_learned=0,
                    poll_interval_ms=1,
                )
                session.start()
                wait_deadline = time.monotonic() + 1.0
                while (
                    native.stats()["imports_enqueued"] == before
                    and time.monotonic() < wait_deadline
                ):
                    time.sleep(0.0005)
                if native.solve() != 10:
                    raise RuntimeError("loaded stream solve failed")
                evidence = session.finish(
                    native.stats()["solve_generation"]
                )
                delivered += int(
                    evidence["backend_realtime_import_delivered"]
                )
                loaded_samples.append((time.monotonic_ns() - started) // 1000)
            finally:
                native.close()
    cold = _summary(cold_samples)
    reuse = _summary(reuse_samples)
    idle = _summary(idle_samples)
    loaded = _summary(loaded_samples)
    result = {
        "schema": SCHEMA,
        "rounds": rounds,
        "formula": "qfbv-38-operator-matrix-sat",
        "native_cold": cold,
        "native_reuse": reuse,
        "realtime_idle": idle,
        "realtime_checked_import": loaded,
        "idle_total_overhead_ratio": idle["total_us"] / max(1, reuse["total_us"]),
        "checked_import_cold_total_overhead_ratio": (
            loaded["total_us"] / max(1, cold["total_us"])
        ),
        "checked_imports_delivered": delivered,
        "native_signature": owner.signature,
        "library": str(library),
        "library_sha256": _sha256(library),
        "claim_boundary": (
            "same-process mechanism-cost benchmark; no coverage, defect-rate, "
            "network-latency, or distributed-scaling claim"
        ),
    }
    if delivered != rounds:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["artifact_sha256"] = hashlib.sha256(
        encoded.encode("ascii")
    ).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=64)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        args.library.resolve(strict=True),
        rounds=max(4, min(args.rounds, 4096)),
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
