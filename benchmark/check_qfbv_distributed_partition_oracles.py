#!/usr/bin/env python3
"""Executable mechanism oracle for F451 distributed certified cubes."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from mpi_ulfm_recovery import (  # noqa: E402
    DurableUlfmCoordinator,
    EndpointIdentity,
    RecoveryShard,
    UlfmRecoveryController,
    build_endpoint_attestation,
)
from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from qfbv_distributed_partition_execution import (  # noqa: E402
    DistributedCubeBindingStore,
    DistributedPartitionCoordinator,
    distributed_run_id,
)
from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (  # noqa: E402
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
)
from qfbv_partition_execution import (  # noqa: E402
    PartitionExecutionPolicy,
    PartitionExecutionStore,
)
from qfbv_proof_prefix_partition import (  # noqa: E402
    ProofPrefixPartitionPolicy,
    build_proof_prefix_partition,
)
from query_store import QueryStore  # noqa: E402


SCHEMA = "symcc-f451-distributed-partition-oracle-v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plan(query_id: str, *, contradiction: bool):
    expressions = {
        "input": {
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 0},
        },
        "zero": {
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": "00"},
        },
        "root": {
            "op": "equal",
            "bits": 1,
            "children": ["input", "zero"],
            "attrs": {},
        },
    }
    roots = ["root"]
    if contradiction:
        expressions["false"] = {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": False},
        }
        roots.append("false")
    return bitblast_qfbv_query(query_id, roots, expressions)


def _base_result(plan, checker, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "assignments": {},
        "solver": "f451-oracle",
        "elapsed_us": 1,
        "backend_kind": "bitblast-cadical-qfbv",
        "backend_capabilities": normalize_qfbv_capabilities(
            {"incremental": True}
        ),
        "backend_model_verified": False,
        "backend_unsat_authorized": False,
        "capability_status": "supported",
        "bitblast_certificate": dict(plan.certificate),
        "backend_incremental_proof_protocol": CLAUSE_PROTOCOL,
        "backend_incremental_proof_policy_sha256": checker.policy_sha256,
        "backend_incremental_import_candidates": 0,
        "backend_incremental_imported_clauses": 0,
        "backend_incremental_import_checker_elapsed_us": 0,
        "backend_incremental_import_record_sha256": [],
    }


def _unsat_result(plan, proof_store, checker, sequence: int) -> dict[str, Any]:
    record = make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker="f451-oracle-leaf",
        worker_epoch=1,
        sequence=sequence,
    )
    authorization = checker.verify_clause_record(plan, record)
    digest, created = proof_store.publish(record)
    receipt = make_unsat_result_receipt(plan, digest, plan.assumptions)
    checker.verify_result_receipt(plan, receipt)
    result = _base_result(plan, checker, "unsat")
    result.update(
        {
            "backend_unsat_authorized": True,
            "backend_incremental_proof_verified": True,
            "backend_incremental_proof_created": created,
            "backend_incremental_proof_record_sha256": digest,
            "backend_incremental_proof_steps": authorization.proof_steps,
            "backend_incremental_proof_propagations": (
                authorization.propagation_count
            ),
            "backend_incremental_proof_checker_elapsed_us": max(
                1, authorization.checker_elapsed_us
            ),
            "backend_incremental_result_receipt": receipt,
        }
    )
    return result


def _value_satisfying_cube(plan, literals: Sequence[int]) -> int:
    required = {abs(literal): literal > 0 for literal in literals}
    value = 0
    for _offset, input_literals in plan.input_literals:
        for bit, input_literal in enumerate(input_literals):
            if abs(input_literal) not in required:
                continue
            semantic = required[abs(input_literal)]
            if input_literal < 0:
                semantic = not semantic
            if semantic:
                value |= 1 << bit
    return value


def _sat_result(plan, checker, literals: Sequence[int]) -> dict[str, Any]:
    result = _base_result(plan, checker, "sat")
    result["assignments"] = {0: _value_satisfying_cube(plan, literals)}
    result["backend_model_verified"] = True
    return result


def _runtime(
    root: Path,
    *,
    query_id: str,
    cubes: int,
    host_by_rank: Sequence[str],
    contradiction: bool,
) -> tuple[DistributedPartitionCoordinator, list[EndpointIdentity], Any, Any, Any]:
    plan = _plan(query_id, contradiction=contradiction)
    certificate = build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(
            cube_count=cubes, max_depth=max(1, (cubes - 1).bit_length())
        ),
    )
    policy = PartitionExecutionPolicy(
        parallelism=len(host_by_rank),
        max_attempts=3,
        cube_timeout_ms=100,
        task_lease_ms=60_000,
    )
    partition_store = PartitionExecutionStore(root / "executions")
    proof_store = IncrementalProofStore(root / "proofs")
    checker = IncrementalProofChecker(proof_store)
    execution = partition_store.create(plan, certificate, policy, checker=checker)
    endpoints = [
        EndpointIdentity(
            endpoint_id=f"worker-{rank}",
            initial_rank=rank,
            incarnation_sha256=hashlib.sha256(
                f"{query_id}:incarnation:{rank}".encode("ascii")
            ).hexdigest(),
            host_id=host,
        )
        for rank, host in enumerate(host_by_rank)
    ]
    controller = UlfmRecoveryController(
        distributed_run_id(execution),
        endpoints,
        [
            RecoveryShard(
                shard_id=f"cube-slot-{rank}",
                owner_endpoint=f"worker-{rank}",
                checkpoint_sha256=hashlib.sha256(
                    f"{query_id}:checkpoint:{rank}".encode("ascii")
                ).hexdigest(),
            )
            for rank in range(len(endpoints))
        ],
    )
    coordinator = DistributedPartitionCoordinator(
        plan,
        certificate,
        policy,
        partition_store,
        DistributedCubeBindingStore(root / "bindings"),
        DurableUlfmCoordinator(controller, QueryStore(root / "query-store")),
        proof_store,
        checker,
        input_hex="00",
    )
    return coordinator, endpoints, plan, proof_store, checker


def _attest(
    coordinator: DistributedPartitionCoordinator,
    endpoints: Sequence[EndpointIdentity],
    survivors: Sequence[int],
) -> list[dict[str, Any]]:
    snapshot = coordinator.durable.controller.snapshot()
    return [
        build_endpoint_attestation(
            run_id=coordinator.durable.controller.run_id,
            base_generation=snapshot["generation"],
            base_generation_token=snapshot["generation_token"],
            endpoint=endpoints[old_rank],
            old_rank=snapshot["members"][f"worker-{old_rank}"]["rank"],
            new_rank=new_rank,
        )
        for new_rank, old_rank in enumerate(survivors)
    ]


def _fill_unsat(
    coordinator: DistributedPartitionCoordinator,
    plan: Any,
    proof_store: IncrementalProofStore,
    checker: IncrementalProofChecker,
    initial: Sequence[Mapping[str, Any]],
    *,
    sequence_base: int,
) -> dict[str, Any]:
    pending = list(initial)
    sequence = sequence_base
    while True:
        while pending:
            lease = pending.pop(0)
            derived = extend_bitblast_assumptions(
                plan, lease["cube"]["literals"]
            )
            if not coordinator.complete(
                lease,
                _unsat_result(derived, proof_store, checker, sequence),
            ):
                raise AssertionError("oracle cube result was not accepted")
            sequence += 1
        result = coordinator.finalize_if_ready()
        if result is not None:
            return result
        snapshot = coordinator.durable.controller.snapshot()
        for endpoint in sorted(snapshot["members"]):
            while True:
                lease = coordinator.claim(endpoint)
                if lease is None:
                    break
                pending.append(lease)
        if not pending:
            raise AssertionError("oracle execution stalled before a result")


def _recovery_case(
    root: Path,
    *,
    name: str,
    cubes: int,
    host_by_rank: Sequence[str],
    failed_ranks: Sequence[int],
    sequence_base: int,
) -> dict[str, Any]:
    coordinator, endpoints, plan, proof_store, checker = _runtime(
        root,
        query_id=f"f451-{name}",
        cubes=cubes,
        host_by_rank=host_by_rank,
        contradiction=True,
    )
    started = time.monotonic_ns()
    initial = [
        coordinator.claim(f"worker-{rank}") for rank in range(len(endpoints))
    ]
    if any(lease is None for lease in initial):
        raise AssertionError("oracle did not fill every initial endpoint")
    survivors = [rank for rank in range(len(endpoints)) if rank not in failed_ranks]
    recovery = coordinator.recover(
        [f"worker-{rank}" for rank in failed_ranks],
        _attest(coordinator, endpoints, survivors),
    )
    old = [lease for lease in initial if lease is not None]
    recovered = recovery["recovered_leases"]
    old_by_ordinal = {lease["cube"]["ordinal"]: lease for lease in old}
    if set(old_by_ordinal) != {
        lease["cube"]["ordinal"] for lease in recovered
    }:
        raise AssertionError("recovered cube inventory changed")
    if any(
        lease["cube"]["token"]
        != old_by_ordinal[lease["cube"]["ordinal"]]["cube"]["token"] + 1
        for lease in recovered
    ):
        raise AssertionError("recovered cube token did not advance once")
    result = _fill_unsat(
        coordinator,
        plan,
        proof_store,
        checker,
        recovered,
        sequence_base=sequence_base,
    )
    checker.verify_result_receipt(plan, result["backend_incremental_result_receipt"])
    elapsed_us = max(1, (time.monotonic_ns() - started) // 1000)
    stats = coordinator.stats()
    return {
        "name": name,
        "initial_hosts": len(set(host_by_rank)),
        "surviving_hosts": len({host_by_rank[rank] for rank in survivors}),
        "failed_endpoints": len(failed_ranks),
        "target_generation": recovery["recovery_receipt"]["target_generation"],
        "requeued_cubes": len(recovery["recovery_receipt"]["requeued_work"]),
        "recovered_cubes": len(recovered),
        "cube_count": cubes,
        "attempts": coordinator.partition_store.stats()["attempts"],
        "completed_cubes": result["backend_partition_completed_cubes"],
        "result": result["status"],
        "aggregate_proof_sha256": result[
            "backend_partition_aggregate_proof_sha256"
        ],
        "active_work_after": stats["ulfm_active_work"],
        "recovery_queue_after": stats["ulfm_recovery_queue"],
        "binding_states": stats["bindings"]["states"],
        "elapsed_us": elapsed_us,
    }


def _sat_case(root: Path, *, endpoints_count: int) -> dict[str, Any]:
    coordinator, _endpoints, plan, _proof_store, checker = _runtime(
        root,
        query_id="f451-sat-cancel",
        cubes=endpoints_count,
        host_by_rank=[f"node-{rank % 2}" for rank in range(endpoints_count)],
        contradiction=False,
    )
    leases = [
        coordinator.claim(f"worker-{rank}") for rank in range(endpoints_count)
    ]
    if any(lease is None for lease in leases):
        raise AssertionError("SAT oracle did not fill every endpoint")
    winner = leases[0]
    assert winner is not None
    derived = extend_bitblast_assumptions(
        plan, winner["cube"]["literals"]
    )
    if not coordinator.complete(
        winner,
        _sat_result(derived, checker, winner["cube"]["literals"]),
    ):
        raise AssertionError("SAT oracle winner was rejected")
    result = coordinator.finalize_if_ready()
    if result is None:
        raise AssertionError("SAT oracle did not terminate")
    stats = coordinator.stats()
    return {
        "cube_count": endpoints_count,
        "completed_cubes": result["backend_partition_completed_cubes"],
        "cancelled_cubes": stats["bindings"]["states"].get("cancelled", 0),
        "result": result["status"],
        "active_work_after": stats["ulfm_active_work"],
        "binding_states": stats["bindings"]["states"],
    }


def verify_oracle(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("F451 oracle must be an object")
    normalized = dict(payload)
    supplied = normalized.pop("oracle_sha256", None)
    if supplied != _digest(normalized):
        raise ValueError("F451 oracle identity changed")
    if normalized.get("schema") != SCHEMA:
        raise ValueError("F451 oracle schema changed")
    rounds = normalized.get("rounds")
    cases = normalized.get("cases")
    sat_cases = normalized.get("sat_cases")
    if not isinstance(rounds, int) or rounds < 1:
        raise ValueError("F451 oracle rounds are invalid")
    if not isinstance(cases, list) or len(cases) != rounds * 2:
        raise ValueError("F451 recovery case inventory changed")
    if not isinstance(sat_cases, list) or len(sat_cases) != rounds:
        raise ValueError("F451 SAT case inventory changed")
    for case in cases:
        if (
            case["result"] != "unsat"
            or case["target_generation"] != 1
            or case["requeued_cubes"] != case["recovered_cubes"]
            or case["attempts"] != case["cube_count"]
            or case["completed_cubes"] != case["cube_count"]
            or case["active_work_after"] != 0
            or case["recovery_queue_after"] != 0
            or len(case["aggregate_proof_sha256"]) != 64
        ):
            raise ValueError("F451 recovery conservation changed")
        if case["name"].startswith("same-host") and (
            case["initial_hosts"], case["surviving_hosts"], case["failed_endpoints"]
        ) != (1, 1, 1):
            raise ValueError("F451 same-host topology changed")
        if case["name"].startswith("dual-host") and (
            case["initial_hosts"], case["surviving_hosts"], case["failed_endpoints"]
        ) != (2, 1, 2):
            raise ValueError("F451 dual-host topology changed")
    for case in sat_cases:
        if (
            case["result"] != "sat"
            or case["completed_cubes"] != 1
            or case["cancelled_cubes"] != case["cube_count"] - 1
            or case["active_work_after"] != 0
        ):
            raise ValueError("F451 SAT cancellation changed")
    return dict(payload)


def build_oracle(rounds: int, cubes: int) -> dict[str, Any]:
    if not 1 <= rounds <= 100 or not 4 <= cubes <= 128:
        raise ValueError("oracle rounds/cubes are outside the bounded contract")
    cases: list[dict[str, Any]] = []
    sat_cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f451-oracle-") as directory:
        root = Path(directory)
        for round_index in range(rounds):
            cases.append(
                _recovery_case(
                    root / f"same-{round_index}",
                    name=f"same-host-r{round_index}",
                    cubes=cubes,
                    host_by_rank=["node-local"] * 4,
                    failed_ranks=[0],
                    sequence_base=round_index * 10_000,
                )
            )
            cases.append(
                _recovery_case(
                    root / f"dual-{round_index}",
                    name=f"dual-host-r{round_index}",
                    cubes=cubes,
                    host_by_rank=["node-a", "node-a", "node-b", "node-b"],
                    failed_ranks=[0, 1],
                    sequence_base=round_index * 10_000 + 1_000,
                )
            )
            sat_cases.append(
                _sat_case(root / f"sat-{round_index}", endpoints_count=4)
            )
    elapsed = [case["elapsed_us"] for case in cases]
    payload = {
        "schema": SCHEMA,
        "protocol": "symcc-qfbv-generation-fenced-partition-execution-v1",
        "evidence_level": "I/T/E-mechanism-logical-topology",
        "rounds": rounds,
        "cube_count": cubes,
        "cases": cases,
        "sat_cases": sat_cases,
        "summary": {
            "recovery_cases": len(cases),
            "unsat_results": sum(case["result"] == "unsat" for case in cases),
            "sat_results": sum(case["result"] == "sat" for case in sat_cases),
            "recovered_cubes": sum(case["recovered_cubes"] for case in cases),
            "lost_cubes": sum(
                case["cube_count"] - case["completed_cubes"] for case in cases
            ),
            "median_recovery_case_us": int(statistics.median(elapsed)),
        },
        "claim_boundary": (
            "The oracle proves durable dual-fence, replay, proof aggregation and "
            "global cancellation on one machine with one-host/two-host logical "
            "membership. It is not a physical multi-node latency or solver speedup result."
        ),
    }
    payload["oracle_sha256"] = _digest(payload)
    return verify_oracle(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--cubes", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = build_oracle(args.rounds, args.cubes)
    encoded = _canonical_json(payload) + b"\n"
    if args.output is None:
        sys.stdout.buffer.write(encoded)
    else:
        args.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
