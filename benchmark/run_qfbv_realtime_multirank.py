#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Run a qualified MPI trial of checked proof-stream delivery and pairing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from mpi4py import MPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import probe_shared_state_filesystem  # noqa: E402
from mpi_filesystem_qualification import (  # noqa: E402
    observe_cluster_lock_qualification_inputs,
    qualify_mpi_cluster_advisory_lock,
)
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_multirank_evaluation import (  # noqa: E402
    PROTOCOL,
    RANK_REPORT_SCHEMA,
    MultirankConfig,
    MultirankEvaluationError,
    aggregate_rank_reports,
    build_random_3sat_plan,
    content_digest,
    exchange_clauses,
)
from qfbv_realtime_stream import (  # noqa: E402
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
    verify_checked_import_ack,
    verify_clause_activity_receipt,
)
from qfbv_utility_pairing import (  # noqa: E402
    UtilityPairingController,
    UtilityPairingPolicy,
)


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


def _qualification_dict(result: Any) -> dict[str, Any]:
    return {
        "clean": bool(result.clean),
        "verified": bool(result.verified),
        "members": [
            {"rank": int(rank), "processor": str(processor)}
            for rank, processor in result.members
        ],
        "representatives": list(map(int, result.representatives)),
        "rounds": int(result.rounds),
        "contention_checks": int(result.contention_checks),
        "release_checks": int(result.release_checks),
        "identity_checks": int(result.identity_checks),
        "qualification_generation": int(result.qualification_generation),
        "proof_transcript": str(result.proof_transcript),
        "error": str(result.error),
        "capability": (
            result.capability.snapshot() if result.capability is not None else None
        ),
    }


def _native_context(owner: NativeRealtimeCadical, plan: Any, imports: int) -> Any:
    native = owner.new_context(
        max_learned_length=0,
        max_imports=imports,
        max_import_literals=min(1 << 24, max(1, imports * 65_536)),
        max_learned=0,
    )
    for clause in plan.clauses:
        for literal in clause:
            native.add(literal)
        native.add(0)
    native.observe(plan.max_variable)
    for literal in plan.assumptions:
        native.assume(literal)
    return native


def _run_solve(native: Any, result: dict[str, Any]) -> None:
    started = time.monotonic_ns()
    try:
        result["code"] = native.solve()
    except Exception as error:  # retained in rank evidence before job rejection
        result["error"] = f"{type(error).__name__}: {str(error)[:384]}"
    finally:
        result["elapsed_us"] = (time.monotonic_ns() - started) // 1000


def _wait_for(predicate: Any, deadline: float, interval: float) -> bool:
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def run(args: argparse.Namespace) -> dict[str, Any] | None:
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()
    processor = MPI.Get_processor_name() or socket.gethostname()
    config = MultirankConfig(
        world_size=size,
        publishers=args.publishers,
        rounds=args.rounds,
        seed=args.seed,
        variables=args.variables,
        clauses=args.clauses,
        mode=args.mode,
        solve_timeout_ms=args.solve_timeout_ms,
        poll_interval_ms=args.poll_interval_ms,
        track_clause_activity=args.track_clause_activity,
        utility_pairing=args.utility_pairing,
    )

    library = args.library.resolve(strict=True)
    library_sha256 = _sha256(library)
    library_identities = comm.allgather((str(library), library_sha256))
    if len(set(library_identities)) != 1:
        raise MultirankEvaluationError(
            "all ranks must load the same absolute native library and digest"
        )
    owner = NativeRealtimeCadical(library)
    signatures = comm.allgather(owner.signature)
    if len(set(signatures)) != 1:
        raise MultirankEvaluationError("native solver signatures differ across ranks")

    base_root: str | None
    if rank == 0:
        if args.proof_root is None:
            base_root = tempfile.mkdtemp(prefix="symcc-f434-mpi-")
        else:
            base_root = str(args.proof_root.resolve())
            Path(base_root).mkdir(parents=True, exist_ok=True)
        run_nonce = os.urandom(16).hex()
    else:
        base_root = None
        run_nonce = None
    base_root, run_nonce = comm.bcast((base_root, run_nonce), root=0)
    assert base_root is not None and run_nonce is not None
    proof_root = Path(base_root) / f"run-{run_nonce}" / "proofs"
    proof_root.mkdir(parents=True, exist_ok=True)
    comm.Barrier()

    local_capability = None
    local_error = ""
    try:
        local_capability = probe_shared_state_filesystem(
            str(proof_root.parent), timeout=min(30.0, args.qualification_timeout)
        )
    except Exception as error:
        local_error = f"local filesystem probe failed: {type(error).__name__}: {str(error)[:384]}"
    observation = observe_cluster_lock_qualification_inputs(
        local_capability,
        processor_name_probe=lambda: processor,
        local_error=local_error,
    )
    qualification = qualify_mpi_cluster_advisory_lock(
        comm,
        observation.capability,
        root=str(proof_root.parent),
        epoch=content_digest({
            "protocol": PROTOCOL,
            "config_sha256": config.sha256,
            "run_nonce": run_nonce,
        }),
        global_rank=rank,
        expected_master_ranks=range(size),
        processor_name=observation.processor_name,
        timeout=args.qualification_timeout,
        local_error=observation.error,
    )
    qualification_rows = comm.allgather(_qualification_dict(qualification))
    shared_comm = comm.Split_type(MPI.COMM_TYPE_SHARED, key=rank)
    physically_colocated = shared_comm.Get_size() == size
    shared_comm.Free()
    same_host_accepted = (
        physically_colocated
        and all(row["clean"] and row["capability"] is not None
                for row in qualification_rows)
    )
    for row in qualification_rows:
        row["accepted"] = bool(row["verified"] or same_host_accepted)
        row["scope"] = (
            "cross-host-mpi-lock-v2"
            if row["verified"]
            else "same-host-subprocess-v1"
            if same_host_accepted
            else "unqualified"
        )
    transcript_ids = {
        (row["clean"], row["verified"], row["proof_transcript"])
        for row in qualification_rows
    }
    if (
        len(transcript_ids) != 1
        or not qualification.clean
        or not all(row["accepted"] for row in qualification_rows)
    ):
        raise MultirankEvaluationError(
            "shared filesystem qualification did not reach clean consensus: "
            + json.dumps(qualification_rows, sort_keys=True)[:2048]
        )

    store = IncrementalProofStore(proof_root)
    checker = IncrementalProofChecker(store)
    role = config.role(rank)
    pairing_controller = (
        UtilityPairingController(UtilityPairingPolicy())
        if role == "consumer" and config.utility_pairing
        else None
    )
    report: dict[str, Any] = {
        "schema": RANK_REPORT_SCHEMA,
        "protocol": PROTOCOL,
        "config_sha256": config.sha256,
        "rank": rank,
        "world_size": size,
        "role": role,
        "processor": processor,
        "pid": os.getpid(),
        "library_sha256": library_sha256,
        "native_signature": owner.signature,
        "proof_store_identity_sha256": store.identity_sha256,
        "rounds": [],
        "error": "",
    }
    sequence = rank * config.rounds

    def next_sequence() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    for ordinal in range(config.rounds):
        plan = build_random_3sat_plan(config, ordinal)
        plan_identity = (
            plan.formula_sha256,
            str(plan.certificate["cnf_sha256"]),
            plan.max_variable,
            len(plan.clauses),
        )
        if len(set(comm.allgather(plan_identity))) != 1:
            raise MultirankEvaluationError("bit-blast plan differs across ranks")
        selected = exchange_clauses(plan, config.publishers)
        prepared_record = None
        if role == "publisher":
            publisher_index = config.publisher_ranks.index(rank)
            prepared_record = make_rup_clause_record(
                plan,
                selected[publisher_index],
                source_worker=f"f434-publisher-rank-{rank}",
                worker_epoch=config.seed,
                sequence=ordinal + 1,
                deadline_ns=time.monotonic_ns() + 30_000_000_000,
            )

        native = None
        session = None
        solve_thread = None
        solve_result: dict[str, Any] = {}
        if role == "consumer":
            native = _native_context(owner, plan, config.publishers)
            session = RealtimeClauseExchangeSession(
                plan,
                native,
                store,
                checker,
                native_signature=owner.signature,
                source_worker=f"f434-consumer-rank-{rank}",
                worker_epoch=config.seed,
                next_sequence=next_sequence,
                stream_ordinal=ordinal + 1,
                max_imports=config.publishers,
                max_events=max(64, config.publishers * 8),
                max_learned=0,
                poll_interval_ms=config.poll_interval_ms,
                checker_budget_ms=min(60_000, config.solve_timeout_ms),
                track_clause_activity=config.track_clause_activity,
                pairing_controller=pairing_controller,
            )
            session.start()

        comm.Barrier()
        epoch_started = time.monotonic_ns()
        ready = True
        if role == "consumer" and config.mode == "active":
            assert native is not None
            solve_thread = threading.Thread(
                target=_run_solve,
                args=(native, solve_result),
                name=f"f434-native-solve-rank-{rank}",
                daemon=True,
            )
            solve_thread.start()
            ready = _wait_for(
                lambda: (
                    native.stats()["solve_generation"] == 1
                    and native.stats()["solving"] == 1
                ),
                time.monotonic() + min(5.0, config.solve_timeout_ms / 1000),
                config.poll_interval_ms / 1000,
            )
        readiness = comm.allgather({"rank": rank, "ready": ready})
        all_ready = all(bool(item["ready"]) for item in readiness)

        publication: dict[str, Any] = {}
        if role == "publisher" and all_ready:
            assert prepared_record is not None
            publish_started = time.monotonic_ns()
            digest, created = store.publish(prepared_record)
            event = store.event_for_record(digest)
            publish_elapsed = (time.monotonic_ns() - publish_started) // 1000
            if event is None or event[1] != plan.formula_sha256:
                raise MultirankEvaluationError("published proof event is unavailable")
            publication = {
                "rank": rank,
                "record_sha256": digest,
                "event_sequence": event[0],
                "created": created,
                "publish_elapsed_us": publish_elapsed,
            }
        publications = comm.allgather(publication)
        notification_ns = time.monotonic_ns()
        published = [item for item in publications if item]
        if not all_ready or len(published) != config.publishers:
            raise MultirankEvaluationError(
                "consumer readiness or publisher cardinality failed"
            )

        consumer_row: dict[str, Any] | None = None
        if role == "consumer":
            assert native is not None and session is not None
            active_at_publication = bool(ready and all_ready)
            enqueue_deadline = time.monotonic() + config.solve_timeout_ms / 1000
            enqueued = _wait_for(
                lambda: (
                    session.progress()["settled_candidates"]
                    == config.publishers
                    if config.utility_pairing
                    else native.stats()["imports_enqueued"]
                    == config.publishers
                ),
                enqueue_deadline,
                config.poll_interval_ms / 1000,
            )
            if config.mode == "preloaded":
                solve_thread = threading.Thread(
                    target=_run_solve,
                    args=(native, solve_result),
                    name=f"f434-native-solve-rank-{rank}",
                    daemon=True,
                )
                solve_thread.start()
            assert solve_thread is not None
            solve_thread.join(timeout=config.solve_timeout_ms / 1000)
            timed_out = solve_thread.is_alive()
            if timed_out:
                native.terminate()
                solve_thread.join(timeout=5.0)
            if solve_thread.is_alive():
                raise MultirankEvaluationError(
                    "native solve did not stop after bounded termination"
                )
            generation = native.stats()["solve_generation"]
            evidence = session.finish(generation)
            delivered = list(evidence["backend_realtime_import_record_sha256"])
            pairing_fields = {}
            imports_expected = config.publishers
            if config.utility_pairing:
                imports_expected = int(
                    evidence["backend_realtime_pairing_action_counts"]["admit"]
                )
                pairing_fields = {
                    "stream_id": evidence["backend_realtime_stream_id"],
                    "pairing_evidence": {
                        key: value
                        for key, value in evidence.items()
                        if key.startswith("backend_realtime_pairing_")
                    },
                }
            consumer_row = {
                "round": ordinal,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": str(plan.certificate["cnf_sha256"]),
                "solve_result": int(solve_result.get("code", 0)),
                "solve_elapsed_us": int(solve_result.get("elapsed_us", 0)),
                "notification_to_finish_us": (
                    time.monotonic_ns() - notification_ns
                ) // 1000,
                "imports_enqueued_before_wait_deadline": enqueued,
                "imports_expected": imports_expected,
                "imports_delivered": len(delivered),
                "delivered_record_sha256": delivered,
                "acks": evidence["backend_realtime_import_acks"],
                "activity_protocol": evidence[
                    "backend_realtime_clause_activity_protocol"
                ],
                "activity_unit": int(
                    evidence["backend_realtime_clause_activity_unit"]
                ),
                "activity_conflict": int(
                    evidence["backend_realtime_clause_activity_conflict"]
                ),
                "activity_unactivated": int(
                    evidence["backend_realtime_clause_activity_unactivated"]
                ),
                "activity_receipts": evidence[
                    "backend_realtime_clause_activity_receipts"
                ],
                "checker_elapsed_us": int(
                    evidence["backend_realtime_import_checker_elapsed_us"]
                ),
                "events_observed": int(
                    evidence["backend_realtime_events_observed"]
                ),
                "backpressure": int(
                    evidence["backend_realtime_import_backpressure"]
                ),
                "active_at_publication": active_at_publication,
                "timed_out": timed_out,
                "stream_error": str(evidence["backend_realtime_stream_error"]),
                "solve_error": str(solve_result.get("error", "")),
                **pairing_fields,
            }
            native.close()

        comm.Barrier()
        epoch_elapsed_us = (time.monotonic_ns() - epoch_started) // 1000
        if role == "coordinator":
            report["rounds"].append({
                "round": ordinal,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": str(plan.certificate["cnf_sha256"]),
                "epoch_makespan_us": epoch_elapsed_us,
                "publisher_records": sorted(
                    item["record_sha256"] for item in published
                ),
            })
        elif role == "publisher":
            row = next(item for item in published if item["rank"] == rank)
            report["rounds"].append({
                "round": ordinal,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": str(plan.certificate["cnf_sha256"]),
                "record_sha256": row["record_sha256"],
                "event_sequence": row["event_sequence"],
                "created": row["created"],
                "publish_elapsed_us": row["publish_elapsed_us"],
            })
        else:
            assert consumer_row is not None
            report["rounds"].append(consumer_row)

    reports = comm.gather(report, root=0)
    if rank != 0:
        return None
    assert reports is not None

    # Rank 0 independently replays every native ACK against the durable CAS.
    replayed = 0
    activity_replayed = 0
    for consumer_rank in config.consumer_ranks:
        consumer_report = reports[consumer_rank]
        for ordinal, row in enumerate(consumer_report["rounds"]):
            plan = build_random_3sat_plan(config, ordinal)
            for ack in row["acks"]:
                authorization = verify_checked_import_ack(
                    plan, ack, checker=checker
                )
                event = store.event_at(int(ack["event_sequence"]))
                if event != (
                    authorization.formula_sha256,
                    authorization.record_sha256,
                ):
                    raise MultirankEvaluationError(
                        "independent ACK replay found an event identity mismatch"
                    )
                replayed += 1
            acks_by_digest = {
                str(ack["ack_sha256"]): ack for ack in row["acks"]
            }
            for receipt in row.get("activity_receipts", []):
                ack = acks_by_digest.get(str(receipt.get("ack_sha256", "")))
                if ack is None:
                    raise MultirankEvaluationError(
                        "independent activity replay lacks its ACK"
                    )
                authorization = verify_clause_activity_receipt(
                    plan, receipt, ack=ack, checker=checker
                )
                event = store.event_at(int(ack["event_sequence"]))
                if event != (
                    authorization.formula_sha256,
                    authorization.record_sha256,
                ):
                    raise MultirankEvaluationError(
                        "independent activity replay found an event mismatch"
                    )
                activity_replayed += 1
    result = aggregate_rank_reports(
        config,
        reports,
        filesystem_qualification=qualification_rows[0],
        library_sha256=library_sha256,
    )
    result["run_nonce"] = run_nonce
    result["proof_root"] = str(proof_root)
    result["native_signature"] = owner.signature
    result["acks_independently_replayed"] = replayed
    result["activity_receipts_independently_replayed"] = activity_replayed
    result["artifact_sha256"] = content_digest({
        key: value for key, value in result.items() if key != "artifact_sha256"
    })
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--proof-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publishers", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0xF434)
    parser.add_argument("--variables", type=int, default=200)
    parser.add_argument("--clauses", type=int, default=860)
    parser.add_argument("--mode", choices=("active", "preloaded"), default="active")
    parser.add_argument("--solve-timeout-ms", type=int, default=30_000)
    parser.add_argument("--poll-interval-ms", type=int, default=1)
    parser.add_argument("--qualification-timeout", type=float, default=30.0)
    parser.add_argument("--track-clause-activity", action="store_true")
    parser.add_argument("--utility-pairing", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args)
    if MPI.COMM_WORLD.Get_rank() == 0:
        assert result is not None
        encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
        if args.output is not None:
            _atomic_json(args.output.resolve(), result)
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
