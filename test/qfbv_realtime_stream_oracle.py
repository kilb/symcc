#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Randomized real-CaDiCaL oracle for checked in-solve clause streaming."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import tempfile
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_realtime_stream import (  # noqa: E402
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
    RealtimeStreamError,
    verify_checked_import_ack,
)


SCHEMA = "symcc-f433-realtime-stream-oracle-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plan(case: int, value: int):
    expressions = {
        "input": {
            "op": "read", "bits": 8, "children": [],
            "attrs": {"index": 0},
        },
        "value": {
            "op": "constant", "bits": 8, "children": [],
            "attrs": {"value_hex": f"{value:02x}"},
        },
        "root": {
            "op": "equal", "bits": 1,
            "children": ["input", "value"], "attrs": {},
        },
    }
    return bitblast_qfbv_query(
        f"f433-oracle-{case}-{value}", ["root"], expressions,
        {"incremental": True},
    )


def _prepare(native, plan) -> None:
    for clause in plan.clauses:
        for literal in clause:
            native.add(literal)
        native.add(0)
    native.observe(plan.max_variable)
    for literal in plan.assumptions:
        native.assume(literal)


def _solve_plain(owner, plan) -> tuple[int, dict[int, int]]:
    native = owner.new_context(
        max_learned_length=0,
        max_imports=0,
        max_import_literals=1,
        max_learned=0,
    )
    try:
        _prepare(native, plan)
        result = native.solve()
        true_variables = {
            abs(literal)
            for _offset, literals in plan.input_literals
            for literal in literals
            if native.val(abs(literal)) > 0
        }
        return result, plan.input_bytes_from_model(true_variables)
    finally:
        native.close()


def run(library: Path, *, cases: int, seed: int) -> dict:
    started = time.monotonic_ns()
    owner = NativeRealtimeCadical(library)
    rng = random.Random(seed)
    mismatches: list[dict[str, object]] = []
    delivered = 0
    checked = 0
    learned = 0
    tamper_rejected = False
    with tempfile.TemporaryDirectory(prefix="symcc-f433-oracle-") as directory:
        store = IncrementalProofStore(Path(directory) / "proofs")
        checker = IncrementalProofChecker(store)
        sequence = 0

        def next_sequence() -> int:
            nonlocal sequence
            current = sequence
            sequence += 1
            return current

        for case in range(cases):
            value = rng.randrange(256)
            plan = _plan(case, value)
            baseline_code, baseline_model = _solve_plain(owner, plan)
            clause = min(plan.clauses, key=lambda item: (len(item), item))
            record = make_rup_clause_record(
                plan,
                clause,
                source_worker="f433-oracle-publisher",
                worker_epoch=seed,
                sequence=next_sequence(),
            )
            store.publish(record)
            expected_clause = tuple(record["shared_clause"])
            native = owner.new_context(
                max_learned_length=8,
                max_imports=8,
                max_import_literals=1024,
                max_learned=8,
            )
            try:
                _prepare(native, plan)
                session = RealtimeClauseExchangeSession(
                    plan,
                    native,
                    store,
                    checker,
                    native_signature=owner.signature,
                    source_worker="f433-oracle-consumer",
                    worker_epoch=seed,
                    next_sequence=next_sequence,
                    stream_ordinal=case + 1,
                    max_imports=8,
                    max_events=64,
                    max_learned=8,
                    max_learned_length=8,
                    poll_interval_ms=1,
                )
                session.start()
                wait_deadline = time.monotonic() + 1.0
                while (
                    native.stats()["imports_enqueued"] == 0
                    and time.monotonic() < wait_deadline
                ):
                    time.sleep(0.0005)
                stream_code = native.solve()
                true_variables = {
                    abs(literal)
                    for _offset, literals in plan.input_literals
                    for literal in literals
                    if native.val(abs(literal)) > 0
                }
                stream_model = plan.input_bytes_from_model(true_variables)
                evidence = session.finish(
                    native.stats()["solve_generation"]
                )
            finally:
                native.close()
            case_delivered = int(
                evidence["backend_realtime_import_delivered"]
            )
            delivered += case_delivered
            learned += int(evidence["backend_realtime_learned_published"])
            delivered_clauses: list[tuple[int, ...]] = []
            for ack in evidence["backend_realtime_import_acks"]:
                authorization = verify_checked_import_ack(
                    plan, ack, checker=checker
                )
                if store.event_at(int(ack["event_sequence"])) != (
                    authorization.formula_sha256,
                    authorization.record_sha256,
                ):
                    raise RuntimeError("ACK/event relation changed")
                checked += 1
                delivered_clauses.append(authorization.clause)
                if not tamper_rejected:
                    tampered = copy.deepcopy(ack)
                    tampered["record_sha256"] = "0" * 64
                    try:
                        verify_checked_import_ack(
                            plan, tampered, checker=checker
                        )
                    except RealtimeStreamError:
                        tamper_rejected = True
            if (
                baseline_code != 10
                or stream_code != baseline_code
                or baseline_model != {0: value}
                or stream_model != baseline_model
                or case_delivered < 1
                or expected_clause not in delivered_clauses
            ):
                mismatches.append({
                    "case": case,
                    "value": value,
                    "baseline_code": baseline_code,
                    "stream_code": stream_code,
                    "baseline_model": baseline_model,
                    "stream_model": stream_model,
                    "delivered": case_delivered,
                })

        cancellation_plan = _plan(cases, 0x42)
        cancellation = owner.new_context(
            max_learned_length=0,
            max_imports=0,
            max_import_literals=1,
            max_learned=0,
        )
        try:
            _prepare(cancellation, cancellation_plan)
            cancellation.terminate()
            cancelled_code = cancellation.solve()
            cancellation.clear_termination()
            resumed_code = cancellation.solve()
        finally:
            cancellation.close()
        proof_stats = store.stats()
    result = {
        "schema": SCHEMA,
        "cases": cases,
        "seed": seed,
        "mismatches": len(mismatches),
        "mismatch_samples": mismatches[:8],
        "checked_imports_delivered": delivered,
        "checked_import_acks_replayed": checked,
        "learned_records_published": learned,
        "tampered_ack_rejected": tamper_rejected,
        "pre_solve_cancel_result": cancelled_code,
        "post_clear_result": resumed_code,
        "proof_records": proof_stats["records"],
        "proof_events": proof_stats["events"],
        "native_signature": owner.signature,
        "library": str(library),
        "library_sha256": _sha256(library),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "claim_boundary": (
            "randomized mechanism oracle; no fuzzing coverage, defect-rate, "
            "or distributed-scaling claim"
        ),
    }
    if (
        mismatches
        or delivered != cases
        or checked != delivered
        or not tamper_rejected
        or cancelled_code != 0
        or resumed_code != 10
    ):
        raise RuntimeError(json.dumps(result, sort_keys=True))
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["artifact_sha256"] = hashlib.sha256(
        encoded.encode("ascii")
    ).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--cases", type=int, default=512)
    parser.add_argument("--seed", type=int, default=0xF433)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(
        args.library.resolve(strict=True),
        cases=max(1, min(args.cases, 4096)),
        seed=args.seed,
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
