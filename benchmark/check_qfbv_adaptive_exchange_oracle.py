#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Compare static drop-on-pressure with adaptive deferred proof admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_adaptive_exchange import (  # noqa: E402
    ADAPTIVE_EXCHANGE_PROTOCOL,
    AdaptiveProofController,
    AdaptiveProofPolicy,
    verify_adaptive_stream_result,
)
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_multirank_evaluation import (  # noqa: E402
    MultirankConfig,
    content_digest,
    build_random_3sat_plan,
    exchange_clauses,
)
from qfbv_realtime_stream import (  # noqa: E402
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
    verify_checked_import_ack,
)


SCHEMA = "symcc-f435-adaptive-proof-oracle-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _wait_until(predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _run_mode(
    *,
    mode: str,
    library: Path,
    root: Path,
    seed: int,
    variables: int,
    clauses: int,
    records: int,
    queue_capacity: int,
    timeout_ms: int,
) -> dict[str, Any]:
    config = MultirankConfig(
        world_size=3,
        publishers=1,
        rounds=1,
        seed=seed,
        variables=variables,
        clauses=clauses,
        mode="active",
        solve_timeout_ms=timeout_ms,
        poll_interval_ms=1,
    )
    plan = build_random_3sat_plan(config, 0)
    store = IncrementalProofStore(root / mode / "proofs")
    checker = IncrementalProofChecker(store)
    published: list[str] = []
    published_clauses: set[tuple[int, ...]] = set()
    candidate_clauses = exchange_clauses(
        plan, min(len(plan.clauses), max(records * 8, records))
    )
    for index, clause in enumerate(candidate_clauses, start=1):
        record = make_rup_clause_record(
            plan,
            clause,
            source_worker=f"f435-publisher-{index}",
            worker_epoch=1,
            sequence=index,
        )
        normalized_clause = tuple(int(value) for value in record["shared_clause"])
        if normalized_clause in published_clauses:
            continue
        published_clauses.add(normalized_clause)
        digest, created = store.publish(record)
        if not created:
            raise RuntimeError("F435 oracle record was not freshly published")
        published.append(digest)
        if len(published) == records:
            break
    if len(published) != records:
        raise RuntimeError(
            "F435 oracle could not construct enough distinct normalized clauses"
        )

    owner = NativeRealtimeCadical(library)
    native = owner.new_context(
        max_learned_length=0,
        max_imports=queue_capacity,
        max_import_literals=max(64, records * 32),
        max_learned=0,
    )
    try:
        for clause in plan.clauses:
            for literal in clause:
                native.add(int(literal))
            native.add(0)
        native.observe(plan.max_variable)
        for literal in plan.assumptions:
            native.assume(int(literal))
        controller = None
        if mode == "adaptive":
            controller = AdaptiveProofController(AdaptiveProofPolicy(
                queue_capacity=queue_capacity,
                high_watermark_permille=1000,
                max_deferred=records,
                max_retries=32,
                max_decisions=max(256, records * 32),
                minimum_remaining_ms=0,
            ))
        session = RealtimeClauseExchangeSession(
            plan,
            native,
            store,
            checker,
            native_signature=owner.signature,
            source_worker=f"f435-{mode}-consumer",
            worker_epoch=2,
            next_sequence=iter(range(1_000_000, 2_000_000)).__next__,
            stream_ordinal=1,
            max_imports=records,
            max_events=records,
            max_learned=0,
            poll_interval_ms=1,
            checker_budget_ms=max(100, timeout_ms // 2),
            adaptive_controller=controller,
            solve_budget_ms=timeout_ms,
        )
        session.start()
        prescan = _wait_until(
            lambda: session.progress()["settled_candidates"] == records,
            timeout=max(2.0, timeout_ms / 1000),
        )
        if not prescan:
            session.abort()
            raise RuntimeError("F435 oracle did not prescan every proof record")
        pre_solve = session.progress()
        solve_result: dict[str, Any] = {}

        def solve() -> None:
            started = time.monotonic_ns()
            try:
                solve_result["code"] = native.solve()
            except Exception as error:  # pragma: no cover - native boundary
                solve_result["error"] = f"{type(error).__name__}: {error}"
            solve_result["elapsed_us"] = (
                time.monotonic_ns() - started
            ) // 1000

        thread = threading.Thread(target=solve, name=f"f435-{mode}-solve")
        thread.start()
        thread.join(timeout=timeout_ms / 1000)
        timed_out = thread.is_alive()
        if timed_out:
            native.terminate()
            thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("F435 native solve survived bounded termination")
        evidence = session.finish(native.stats()["solve_generation"])
        for ack in evidence["backend_realtime_import_acks"]:
            verify_checked_import_ack(plan, ack, checker=checker)
        if mode == "adaptive":
            verify_adaptive_stream_result(
                evidence,
                stream_id=evidence["backend_realtime_stream_id"],
                delivered_records=tuple(
                    evidence["backend_realtime_import_record_sha256"]
                ),
                authorized=int(evidence["backend_realtime_import_authorized"]),
                backpressure=int(
                    evidence["backend_realtime_import_backpressure"]
                ),
            )
        return {
            "mode": mode,
            "formula_sha256": plan.formula_sha256,
            "cnf_sha256": str(plan.certificate["cnf_sha256"]),
            "published": len(published),
            "published_record_sha256": published,
            "pre_solve": pre_solve,
            "solve_result": int(solve_result.get("code", 0)),
            "solve_error": str(solve_result.get("error", "")),
            "solve_elapsed_us": int(solve_result.get("elapsed_us", 0)),
            "timed_out": timed_out,
            "events": int(evidence["backend_realtime_events_observed"]),
            "candidates": int(evidence["backend_realtime_import_candidates"]),
            "settled_candidates": int(
                evidence["backend_realtime_import_settled_candidates"]
            ),
            "duplicate_clauses": int(
                evidence["backend_realtime_import_duplicate_clauses"]
            ),
            "authorized": int(evidence["backend_realtime_import_authorized"]),
            "delivered": int(evidence["backend_realtime_import_delivered"]),
            "pending": int(evidence["backend_realtime_import_pending"]),
            "backpressure": int(
                evidence["backend_realtime_import_backpressure"]
            ),
            "delivered_record_sha256": list(
                evidence["backend_realtime_import_record_sha256"]
            ),
            "adaptive": (
                {
                    key: value for key, value in evidence.items()
                    if key.startswith("backend_realtime_adaptive_")
                }
                if mode == "adaptive"
                else None
            ),
        }
    finally:
        native.close()


def verify_oracle_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("schema") != SCHEMA or raw.get("status") != "pass":
        raise ValueError("F435 oracle did not pass")
    body = dict(raw)
    artifact = str(body.pop("artifact_sha256", ""))
    if len(artifact) != 64 or content_digest(body) != artifact:
        raise ValueError("F435 oracle artifact identity changed")
    static = raw.get("static")
    adaptive = raw.get("adaptive")
    if not isinstance(static, Mapping) or not isinstance(adaptive, Mapping):
        raise ValueError("F435 oracle modes are incomplete")
    records = int(raw.get("records", 0))
    queue_capacity = int(raw.get("queue_capacity", 0))
    if (
        records <= queue_capacity
        or static.get("published") != records
        or adaptive.get("published") != records
        or static.get("events") != records
        or adaptive.get("events") != records
        or static.get("settled_candidates") != records
        or adaptive.get("settled_candidates") != records
        or static.get("duplicate_clauses") != 0
        or adaptive.get("duplicate_clauses") != 0
        or int(static.get("delivered", -1)) != queue_capacity
        or int(static.get("backpressure", -1)) != records - queue_capacity
        or adaptive.get("delivered") != records
        or adaptive.get("pending") != 0
        or adaptive.get("backpressure") != 0
        or adaptive.get("timed_out") is not False
        or adaptive.get("solve_error") != ""
        or adaptive.get("adaptive", {}).get(
            "backend_realtime_adaptive_protocol"
        ) != ADAPTIVE_EXCHANGE_PROTOCOL
        or raw.get("delivery_gain")
        != int(adaptive.get("delivered", 0)) - int(static.get("delivered", 0))
        or int(raw.get("delivery_gain", 0)) <= 0
    ):
        raise ValueError("F435 adaptive conservation oracle changed")
    return dict(raw)


def run(args: argparse.Namespace) -> dict[str, Any]:
    library = args.library.resolve(strict=True)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic_ns()
    static = _run_mode(
        mode="static",
        library=library,
        root=root,
        seed=args.seed,
        variables=args.variables,
        clauses=args.clauses,
        records=args.records,
        queue_capacity=args.queue_capacity,
        timeout_ms=args.timeout_ms,
    )
    adaptive = _run_mode(
        mode="adaptive",
        library=library,
        root=root,
        seed=args.seed,
        variables=args.variables,
        clauses=args.clauses,
        records=args.records,
        queue_capacity=args.queue_capacity,
        timeout_ms=args.timeout_ms,
    )
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass",
        "library": str(library),
        "library_sha256": _sha256(library),
        "native_signature": NativeRealtimeCadical(library).signature,
        "seed": args.seed,
        "variables": args.variables,
        "clauses": args.clauses,
        "records": args.records,
        "queue_capacity": args.queue_capacity,
        "static": static,
        "adaptive": adaptive,
        "delivery_gain": adaptive["delivered"] - static["delivered"],
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "claim_boundary": (
            "synchronized local burst mechanism oracle; no coverage, solver "
            "speedup, multi-node scaling, LBD, or defect-yield claim"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    verified = verify_oracle_result(body)
    _atomic_json(root / "f435-adaptive-proof-oracle.json", verified)
    return verified


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=0xF435)
    parser.add_argument("--variables", type=int, default=200)
    parser.add_argument("--clauses", type=int, default=860)
    parser.add_argument("--records", type=int, default=8)
    parser.add_argument("--queue-capacity", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    args = parser.parse_args()
    if (
        args.records < 2
        or args.records > 64
        or args.queue_capacity < 1
        or args.queue_capacity >= args.records
        or args.timeout_ms < 100
        or args.timeout_ms > 3_600_000
    ):
        parser.error("invalid F435 oracle bounds")
    return args


def main() -> int:
    result = run(parse_args())
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
