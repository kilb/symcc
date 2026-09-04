#!/usr/bin/env python3
"""Physical two-host SSH transport oracle for F451 cube recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
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


SCHEMA = "symcc-f451-distributed-crosshost-oracle-v1"
REQUEST_SCHEMA = "symcc-f451-remote-worker-request-v1"
RESPONSE_SCHEMA = "symcc-f451-remote-worker-response-v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("cross-host response has duplicate members")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RuntimeError(f"cross-host response contains {value}")


def _run(
    command: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        cwd=ROOT,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _ssh(remote: str, *command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        remote,
        *command,
    ]


def _remote_identity(remote: str) -> tuple[str, str]:
    result = _run(_ssh(remote, "hostname", "&&", "python3", "--version"))
    if result.returncode != 0:
        raise RuntimeError(
            f"remote qualification failed: {result.stderr.decode(errors='replace')}"
        )
    lines = result.stdout.decode("utf-8").splitlines()
    if len(lines) != 2:
        raise RuntimeError("remote qualification output changed")
    return lines[0].strip(), lines[1].strip()


def _plan(query_id: str):
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
    return bitblast_qfbv_query(query_id, ["root"], expressions)


def _base_result(plan, checker) -> dict[str, Any]:
    return {
        "status": "unknown",
        "assignments": {},
        "solver": "f451-crosshost-remote-worker",
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


def _request(lease: Mapping[str, Any], result: Mapping[str, Any], assignment: int) -> bytes:
    body = {
        "schema": REQUEST_SCHEMA,
        "lease": dict(lease),
        "result_template": dict(result),
        "assignment": assignment,
    }
    body["request_sha256"] = _digest(body)
    return _canonical_json(body)


def _verify_response(
    raw: bytes,
    lease: Mapping[str, Any],
    host: str,
    request_sha256: str,
) -> dict[str, Any]:
    try:
        response = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("remote worker response is not JSON") from error
    if not isinstance(response, Mapping):
        raise RuntimeError("remote worker response is not an object")
    body = dict(response)
    supplied = body.pop("response_sha256", None)
    if supplied != _digest(body):
        raise RuntimeError("remote worker response identity changed")
    if (
        body.get("schema") != RESPONSE_SCHEMA
        or body.get("lease_sha256") != lease["lease_sha256"]
        or body.get("request_sha256") != request_sha256
        or body.get("host") != host
        or not isinstance(body.get("result"), Mapping)
    ):
        raise RuntimeError("remote worker response scope changed")
    return dict(response)


def _endpoints(local_host: str, remote_host: str, round_index: int) -> list[EndpointIdentity]:
    hosts = [local_host, remote_host, remote_host]
    names = ["local-survivor", "remote-failed", "remote-survivor"]
    return [
        EndpointIdentity(
            endpoint_id=names[rank],
            initial_rank=rank,
            incarnation_sha256=hashlib.sha256(
                f"f451-crosshost:{round_index}:{rank}".encode("ascii")
            ).hexdigest(),
            host_id=hosts[rank],
        )
        for rank in range(3)
    ]


def _one_round(
    root: Path,
    *,
    remote: str,
    remote_script: str,
    remote_host: str,
    local_host: str,
    round_index: int,
) -> dict[str, Any]:
    plan = _plan(f"f451-crosshost-{round_index}")
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=4, max_depth=2)
    )
    policy = PartitionExecutionPolicy(
        parallelism=3,
        max_attempts=3,
        cube_timeout_ms=10_000,
        task_lease_ms=60_000,
    )
    partition_store = PartitionExecutionStore(root / "executions")
    proof_store = IncrementalProofStore(root / "proofs")
    checker = IncrementalProofChecker(proof_store)
    execution = partition_store.create(plan, certificate, policy, checker=checker)
    endpoints = _endpoints(local_host, remote_host, round_index)
    controller = UlfmRecoveryController(
        distributed_run_id(execution),
        endpoints,
        [
            RecoveryShard(
                shard_id=f"cube-slot-{rank}",
                owner_endpoint=endpoints[rank].endpoint_id,
                checkpoint_sha256=hashlib.sha256(
                    f"f451-crosshost:{round_index}:shard:{rank}".encode("ascii")
                ).hexdigest(),
            )
            for rank in range(3)
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
        candidate_validator=lambda candidate: candidate == b"\x00",
        input_hex="00",
    )
    # Claim ordinal zero on the surviving remote endpoint. For this fixed
    # certificate it is the input==0 cube and therefore a valid SAT witness.
    survivor_lease = coordinator.claim("remote-survivor")
    failed_lease = coordinator.claim("remote-failed")
    local_lease = coordinator.claim("local-survivor")
    if any(
        lease is None for lease in (survivor_lease, failed_lease, local_lease)
    ):
        raise RuntimeError("cross-host oracle did not fill its three endpoints")
    assert survivor_lease is not None
    assert failed_lease is not None
    assert local_lease is not None
    if survivor_lease["cube"]["ordinal"] != 0:
        raise RuntimeError("cross-host SAT fixture cube ordering changed")

    failed_plan = extend_bitblast_assumptions(
        plan, failed_lease["cube"]["literals"]
    )
    failure = _run(
        _ssh(remote, "python3", remote_script, "--mode", "fail"),
        input_bytes=_request(failed_lease, _base_result(failed_plan, checker), 0),
    )
    if failure.returncode != 86:
        raise RuntimeError(
            f"remote failure injection returned {failure.returncode}: "
            f"{failure.stderr.decode(errors='replace')}"
        )

    before = coordinator.durable.controller.snapshot()
    attestations = [
        build_endpoint_attestation(
            run_id=controller.run_id,
            base_generation=before["generation"],
            base_generation_token=before["generation_token"],
            endpoint=endpoints[old_rank],
            old_rank=old_rank,
            new_rank=new_rank,
        )
        for new_rank, old_rank in enumerate((0, 2))
    ]
    recovery = coordinator.recover(
        ["remote-failed"], attestations
    )
    recovered = {
        lease["cube"]["ordinal"]: lease
        for lease in recovery["recovered_leases"]
    }
    if set(recovered) != {0, 1, 2}:
        raise RuntimeError("cross-host recovered cube inventory changed")
    remote_lease = recovered[0]
    if remote_lease["ulfm_fence"]["endpoint_id"] != "remote-survivor":
        raise RuntimeError("surviving remote endpoint lost its stable shard")
    derived = extend_bitblast_assumptions(
        plan, remote_lease["cube"]["literals"]
    )
    success_request = _request(
        remote_lease, _base_result(derived, checker), 0
    )
    request_sha256 = json.loads(success_request)["request_sha256"]
    success = _run(
        _ssh(remote, "python3", remote_script, "--mode", "solve"),
        input_bytes=success_request,
    )
    if success.returncode != 0:
        raise RuntimeError(
            f"remote solve failed: {success.stderr.decode(errors='replace')}"
        )
    response = _verify_response(
        success.stdout, remote_lease, remote_host, request_sha256
    )
    if response["result"].get(
        "backend_f451_remote_request_sha256"
    ) != request_sha256:
        raise RuntimeError("remote result is not bound to its request")
    if not coordinator.complete(remote_lease, response["result"]):
        raise RuntimeError("remote SAT result failed dual-fence completion")
    result = coordinator.finalize_if_ready()
    if result is None or result["status"] != "sat":
        raise RuntimeError("cross-host oracle did not reach SAT")
    stats = coordinator.stats()
    return {
        "round": round_index,
        "local_host": local_host,
        "remote_host": remote_host,
        "physical_hosts": len({local_host, remote_host}),
        "remote_failure_exit": failure.returncode,
        "target_generation": recovery["recovery_receipt"]["target_generation"],
        "failed_endpoints": recovery["recovery_receipt"]["failed_endpoints"],
        "surviving_endpoints": sorted(
            recovery["recovery_receipt"]["new_members"]
        ),
        "requeued_cubes": len(recovery["recovery_receipt"]["requeued_work"]),
        "recovered_cubes": len(recovered),
        "remote_response_sha256": response["response_sha256"],
        "remote_lease_sha256": remote_lease["lease_sha256"],
        "result": result["status"],
        "completed_cubes": result["backend_partition_completed_cubes"],
        "cancelled_peers": stats["bindings"]["states"].get("cancelled", 0),
        "active_work_after": stats["ulfm_active_work"],
        "recovery_queue_after": stats["ulfm_recovery_queue"],
        "attempts": partition_store.stats()["attempts"],
    }


def verify_oracle(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    supplied = body.pop("oracle_sha256", None)
    if supplied != _digest(body):
        raise ValueError("cross-host oracle identity changed")
    if body.get("schema") != SCHEMA:
        raise ValueError("cross-host oracle schema changed")
    rounds = body.get("rounds")
    trials = body.get("trials")
    if not isinstance(rounds, int) or rounds < 1:
        raise ValueError("cross-host oracle rounds changed")
    if not isinstance(trials, list) or len(trials) != rounds:
        raise ValueError("cross-host trial inventory changed")
    for trial in trials:
        if (
            trial["physical_hosts"] != 2
            or trial["remote_failure_exit"] != 86
            or trial["target_generation"] != 1
            or trial["failed_endpoints"] != ["remote-failed"]
            or trial["surviving_endpoints"]
            != ["local-survivor", "remote-survivor"]
            or trial["requeued_cubes"] != 3
            or trial["recovered_cubes"] != 3
            or trial["result"] != "sat"
            or trial["completed_cubes"] != 1
            or trial["cancelled_peers"] != 2
            or trial["active_work_after"] != 0
            or trial["recovery_queue_after"] != 0
            or trial["attempts"] != 3
        ):
            raise ValueError("cross-host recovery/cancellation invariant changed")
    return dict(payload)


def build_oracle(remote: str, rounds: int) -> dict[str, Any]:
    if not 1 <= rounds <= 20:
        raise ValueError("cross-host rounds must be in [1, 20]")
    local_host = socket.gethostname()
    remote_host, remote_python = _remote_identity(remote)
    if remote_host == local_host:
        raise ValueError("cross-host oracle requires two distinct hostnames")
    worker = ROOT / "benchmark/qfbv_distributed_remote_worker.py"
    worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
    remote_script = f"/tmp/symcc-f451-worker-{worker_sha256[:16]}.py"
    copied = _run(
        [
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            str(worker),
            f"{remote}:{remote_script}",
        ]
    )
    if copied.returncode != 0:
        raise RuntimeError(
            f"remote worker copy failed: {copied.stderr.decode(errors='replace')}"
        )
    try:
        remote_hash = _run(_ssh(remote, "sha256sum", remote_script))
        if remote_hash.returncode != 0 or remote_hash.stdout.decode().split()[0] != worker_sha256:
            raise RuntimeError("remote worker content identity changed")
        started = time.monotonic_ns()
        with tempfile.TemporaryDirectory(prefix="symcc-f451-crosshost-") as directory:
            trials = [
                _one_round(
                    Path(directory) / f"round-{round_index}",
                    remote=remote,
                    remote_script=remote_script,
                    remote_host=remote_host,
                    local_host=local_host,
                    round_index=round_index,
                )
                for round_index in range(rounds)
            ]
        elapsed_ms = max(1, (time.monotonic_ns() - started) // 1_000_000)
    finally:
        _run(_ssh(remote, "rm", "-f", remote_script))
    payload = {
        "schema": SCHEMA,
        "evidence_level": "E-crosshost-application-transport",
        "remote": remote,
        "local_host": local_host,
        "remote_host": remote_host,
        "remote_python": remote_python,
        "remote_worker_sha256": worker_sha256,
        "rounds": rounds,
        "elapsed_ms": elapsed_ms,
        "trials": trials,
        "claim_boundary": (
            "Two physical hosts exchange canonical F451 leases/results over SSH. "
            "A remote process exits with 86, the durable application controller "
            "re-fences all in-flight cubes, and a surviving remote process wins SAT. "
            "This is not a new MPI/ULFM latency, solver-speedup or coverage result; "
            "physical ULFM communicator repair remains supported by F447 evidence."
        ),
    }
    payload["oracle_sha256"] = _digest(payload)
    return verify_oracle(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="root@down.kew.ac")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = build_oracle(args.remote, args.rounds)
    encoded = _canonical_json(payload) + b"\n"
    if args.output is None:
        sys.stdout.buffer.write(encoded)
    else:
        args.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
