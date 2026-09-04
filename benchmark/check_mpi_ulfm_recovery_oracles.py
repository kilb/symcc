#!/usr/bin/env python3
"""Reproducible protocol and runtime oracles for F441 ULFM recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from mpi_ulfm_recovery import (  # noqa: E402
    ULFM_RECOVERY_PROTOCOL,
    EndpointIdentity,
    RecoveryShard,
    UlfmRecoveryController,
    UlfmRecoveryError,
    UlfmRecoveryPolicy,
    build_endpoint_attestation,
    content_digest,
    is_ulfm_failure,
    probe_ulfm_runtime,
    shrink_and_attest,
    verify_recovery_receipt,
    verify_recovery_snapshot,
)


DETERMINISTIC_SCHEMA = "symcc-f441-deterministic-recovery-oracle-v1"
CAPABILITY_SCHEMA = "symcc-f441-mpi-capability-oracle-v1"
LIVE_FAILURE_SCHEMA = "symcc-f441-live-process-failure-oracle-v1"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _endpoint(rank: int) -> EndpointIdentity:
    return EndpointIdentity(
        endpoint_id=f"endpoint-{rank:03d}",
        initial_rank=rank,
        incarnation_sha256=_hash(f"F441-incarnation:{rank}"),
        host_id=f"host-{rank // 4:03d}",
    )


def _policy(endpoints: int, shards: int) -> UlfmRecoveryPolicy:
    return UlfmRecoveryPolicy(
        max_endpoints=max(16, endpoints),
        max_shards=max(128, shards),
        max_recovery_queue=max(128, shards),
        max_repair_attempts=3,
        collective_timeout_seconds=20.0,
        poll_interval_seconds=0.005,
    )


def _new_controller(
    endpoints: int, shards: int, policy: UlfmRecoveryPolicy
) -> UlfmRecoveryController:
    return UlfmRecoveryController(
        "f441-deterministic-oracle",
        [_endpoint(rank) for rank in range(endpoints)],
        [
            RecoveryShard(
                shard_id=f"shard-{index:05d}",
                owner_endpoint=f"endpoint-{index % endpoints:03d}",
                checkpoint_sha256=_hash(f"F441-checkpoint:{index}:0"),
            )
            for index in range(shards)
        ],
        policy,
    )


def _attach_initial_work(
    controller: UlfmRecoveryController, shards: int
) -> tuple[int, int]:
    attached = 0
    completed = 0
    for index in range(shards):
        if index % 3 != 0:
            continue
        shard_id = f"shard-{index:05d}"
        permission = controller.shard_permission(shard_id)
        lease = controller.attach_work(
            shard_id,
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            f"query-{index:05d}",
        )
        attached += 1
        if index % 2 == 0:
            controller.checkpoint_work(
                shard_id,
                permission["owner_endpoint"],
                permission["generation"],
                permission["generation_token"],
                permission["shard_token"],
                lease["lease_token"],
                _hash(f"F441-checkpoint:{index}:1"),
                index + 1,
            )
        if index % 12 == 0:
            controller.finish_work(
                shard_id,
                permission["owner_endpoint"],
                permission["generation"],
                permission["generation_token"],
                permission["shard_token"],
                lease["lease_token"],
                _hash(f"F441-proof:{index}:initial"),
            )
            completed += 1
    return attached, completed


def _execute_deterministic(endpoints: int, shards: int) -> dict[str, Any]:
    if not 4 <= endpoints <= 4096:
        raise UlfmRecoveryError("F441 deterministic endpoint count must be in [4, 4096]")
    if not 1 <= shards <= 262_144:
        raise UlfmRecoveryError("F441 deterministic shard count is invalid")
    policy = _policy(endpoints, shards)
    controller = _new_controller(endpoints, shards, policy)
    attached, completed_before = _attach_initial_work(controller, shards)
    failed_ranks = sorted({endpoints // 2, endpoints - 1})
    failed_endpoints = [f"endpoint-{rank:03d}" for rank in failed_ranks]
    plan = controller.prepare_recovery(
        failed_endpoints,
        generation=controller.generation,
        generation_token=controller.generation_token,
    )
    survivors = [rank for rank in range(endpoints) if rank not in failed_ranks]
    attestations = [
        build_endpoint_attestation(
            run_id=controller.run_id,
            base_generation=plan["base_generation"],
            base_generation_token=plan["base_generation_token"],
            endpoint=_endpoint(old_rank),
            old_rank=old_rank,
            new_rank=new_rank,
        )
        for new_rank, old_rank in enumerate(survivors)
    ]
    receipt = controller.commit_recovery(plan["plan_sha256"], attestations)
    recovery_snapshot = controller.snapshot()
    verify_recovery_receipt(receipt, post_snapshot=recovery_snapshot)

    queued = list(recovery_snapshot["recovery_queue"])
    claims: list[dict[str, Any]] = []
    for item in queued:
        claim = controller.claim_recovery(item["shard_id"])
        permission = controller.shard_permission(item["shard_id"])
        new_lease = claim["new_lease"]
        controller.checkpoint_work(
            item["shard_id"],
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            new_lease["lease_token"],
            _hash(f"F441-replay-checkpoint:{item['shard_id']}"),
            int(item["cursor"]) + 1,
        )
        completion = controller.finish_work(
            item["shard_id"],
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            new_lease["lease_token"],
            _hash(f"F441-replay-proof:{item['work_id']}"),
        )
        claims.append(
            {
                "shard_id": item["shard_id"],
                "old_lease_token": item["lease_token"],
                "new_lease_token": new_lease["lease_token"],
                "completion_sha256": completion["completion_sha256"],
            }
        )
    final_snapshot = verify_recovery_snapshot(controller.snapshot())
    if final_snapshot["recovery_queue"] or any(
        shard["active_work_id"] for shard in final_snapshot["shards"].values()
    ):
        raise UlfmRecoveryError("F441 deterministic replay did not drain all work")
    return {
        "schema": DETERMINISTIC_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "config": {
            "endpoints": endpoints,
            "shards": shards,
            "policy": policy.as_dict(),
            "policy_sha256": policy.sha256,
            "failed_ranks": failed_ranks,
        },
        "plan": plan,
        "receipt": receipt,
        "recovery_snapshot": recovery_snapshot,
        "replay_claims_sha256": content_digest(claims),
        "final_snapshot": final_snapshot,
        "summary": {
            "endpoints_before": endpoints,
            "endpoints_after": endpoints - len(failed_ranks),
            "failed_endpoints": len(failed_ranks),
            "shards": shards,
            "shards_reassigned": len(receipt["reassignments"]),
            "leases_attached_before_failure": attached,
            "leases_completed_before_failure": completed_before,
            "leases_requeued": len(queued),
            "leases_replayed": len(claims),
            "old_lease_tokens_rejected": sum(
                old != new
                for old, new in (
                    (row["old_lease_token"], row["new_lease_token"])
                    for row in claims
                )
            ),
            "final_recovery_queue": len(final_snapshot["recovery_queue"]),
        },
        "claim_boundary": (
            "deterministic protocol oracle; proves stable identity, endpoint/shard/"
            "lease conservation, extra-generation fencing, replay, and strict "
            "artifact verification; it is not a physical process-failure or "
            "multi-node performance result"
        ),
    }


def build_deterministic_oracle(endpoints: int = 12, shards: int = 96) -> dict[str, Any]:
    body = _execute_deterministic(endpoints, shards)
    body["result_sha256"] = content_digest(body)
    return verify_deterministic_oracle(body)


def verify_deterministic_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("F441 deterministic oracle must be an object")
    body = dict(raw)
    supplied = body.pop("result_sha256", "")
    if not isinstance(supplied, str) or content_digest(body) != supplied:
        raise UlfmRecoveryError("F441 deterministic oracle identity changed")
    config = body.get("config")
    if (
        body.get("schema") != DETERMINISTIC_SCHEMA
        or body.get("protocol") != ULFM_RECOVERY_PROTOCOL
        or not isinstance(config, Mapping)
    ):
        raise UlfmRecoveryError("F441 deterministic oracle scope changed")
    expected = _execute_deterministic(config.get("endpoints"), config.get("shards"))
    if body != expected:
        raise UlfmRecoveryError("F441 deterministic oracle replay changed")
    body["result_sha256"] = supplied
    return body


def build_capability_oracle(comm: Any, mpi: Any) -> dict[str, Any] | None:
    local = probe_ulfm_runtime(comm, mpi=mpi)
    gathered = comm.gather(local, root=0)
    if comm.Get_rank() != 0:
        return None
    body = {
        "schema": CAPABILITY_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "world_size": comm.Get_size(),
        "rank_capabilities": gathered,
        "all_ranks_available": all(item["available"] for item in gathered),
        "identical_library": len({item["mpi_library"] for item in gathered}) == 1,
        "distinct_capability_receipts": len(
            {item["capability_sha256"] for item in gathered}
        ),
        "claim_boundary": (
            "live no-failure MPI semantic probe on a duplicated communicator; "
            "exercises ERRORS_RETURN/Get_failed/Agree/Revoke/Shrink but does not "
            "by itself prove recovery after physical process loss"
        ),
    }
    body["result_sha256"] = content_digest(body)
    return verify_capability_oracle(body)


def verify_capability_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("F441 capability oracle must be an object")
    body = dict(raw)
    supplied = body.pop("result_sha256", "")
    if not isinstance(supplied, str) or content_digest(body) != supplied:
        raise UlfmRecoveryError("F441 capability oracle identity changed")
    expected = {
        "schema",
        "protocol",
        "world_size",
        "rank_capabilities",
        "all_ranks_available",
        "identical_library",
        "distinct_capability_receipts",
        "claim_boundary",
    }
    ranks = body.get("rank_capabilities")
    if (
        set(body) != expected
        or body["schema"] != CAPABILITY_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
        or type(body["world_size"]) is not int
        or body["world_size"] < 1
        or not isinstance(ranks, list)
        or len(ranks) != body["world_size"]
    ):
        raise UlfmRecoveryError("F441 capability oracle scope/shape changed")
    libraries: set[str] = set()
    receipts: set[str] = set()
    availability: list[bool] = []
    for capability in ranks:
        if not isinstance(capability, Mapping):
            raise UlfmRecoveryError("F441 rank capability is invalid")
        row = dict(capability)
        digest = row.pop("capability_sha256", "")
        if (
            not isinstance(digest, str)
            or content_digest(row) != digest
            or row.get("protocol") != ULFM_RECOVERY_PROTOCOL
            or type(row.get("available")) is not bool
            or not isinstance(row.get("semantic_checks"), Mapping)
            or set(row["semantic_checks"])
            != {
                "errors_return",
                "get_failed",
                "agree",
                "revoke",
                "shrink",
                "post_shrink_agree",
            }
            or any(type(value) is not bool for value in row["semantic_checks"].values())
        ):
            raise UlfmRecoveryError("F441 rank capability identity/shape changed")
        if row["available"] != all(row["semantic_checks"].values()):
            raise UlfmRecoveryError("F441 rank capability result is inconsistent")
        if row["available"] != (row.get("error") == ""):
            raise UlfmRecoveryError("F441 rank capability error is inconsistent")
        libraries.add(str(row.get("mpi_library")))
        receipts.add(digest)
        availability.append(row["available"])
    if (
        body["all_ranks_available"] != all(availability)
        or body["identical_library"] != (len(libraries) == 1)
        or body["distinct_capability_receipts"] != len(receipts)
    ):
        raise UlfmRecoveryError("F441 capability aggregate changed")
    body["result_sha256"] = supplied
    return body


def run_live_failure_oracle(comm: Any, mpi: Any) -> dict[str, Any] | None:
    size = comm.Get_size()
    rank = comm.Get_rank()
    if size < 3:
        raise UlfmRecoveryError("F441 live failure oracle requires at least 3 ranks")
    policy = _policy(size, size * 4)
    controller = _new_controller(size, size * 4, policy)
    _attach_initial_work(controller, size * 4)
    failed_rank = size - 1
    plan = controller.prepare_recovery(
        [f"endpoint-{failed_rank:03d}"],
        generation=controller.generation,
        generation_token=controller.generation_token,
    )
    trace = os.environ.get("F441_LIVE_TRACE") == "1"

    def emit(stage: str) -> None:
        if trace:
            print(f"F441 rank={rank} stage={stage}", file=sys.stderr, flush=True)

    comm.Set_errhandler(mpi.ERRORS_RETURN)
    emit("initial-barrier-enter")
    comm.Barrier()
    emit("initial-barrier-exit")
    if rank == failed_rank:
        emit("intentional-exit")
        os._exit(86)
    # The production supervisor/heartbeat path initiates revoke once a stable
    # endpoint is suspected.  Do the same explicitly here: a blocking Barrier
    # is not itself a portable failure detector and may wait indefinitely.
    time.sleep(2.0)
    emit("revoke-enter")
    comm.Revoke()
    emit("revoke-exit")
    observed_error = ""
    observed_error_class = -1
    try:
        emit("revoked-barrier-enter")
        comm.Barrier()
    except Exception as error:
        if not is_ulfm_failure(error, mpi):
            raise
        observed_error_class = int(error.Get_error_class())
        observed_error = str(mpi.Get_error_string(observed_error_class))
        emit("revoked-barrier-error")
    emit("repair-enter")
    result = shrink_and_attest(
        comm,
        local_endpoint=_endpoint(rank),
        recovery_plan=plan,
        policy=policy,
        mpi=mpi,
    )
    emit("repair-exit")
    receipt = controller.commit_recovery(plan["plan_sha256"], result.attestations)
    emit("commit-exit")
    snapshot = controller.snapshot()
    verify_recovery_receipt(receipt, post_snapshot=snapshot)
    hashes = result.communicator.allgather(receipt["receipt_sha256"])
    if len(set(hashes)) != 1:
        raise UlfmRecoveryError("F441 survivor receipts disagree")
    body = None
    if result.communicator.Get_rank() == 0:
        body = {
            "schema": LIVE_FAILURE_SCHEMA,
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "world_size_before": size,
            "world_size_after": result.communicator.Get_size(),
            "failed_rank": failed_rank,
            "failed_endpoints": list(result.failed_endpoints),
            "repair_attempts": result.attempts,
            "trigger_error": observed_error,
            "trigger_error_class": observed_error_class,
            "receipt_sha256": receipt["receipt_sha256"],
            "post_state_sha256": snapshot["snapshot_sha256"],
            "survivor_receipts_identical": True,
            "claim_boundary": (
                "physical local MPI process exit followed by survivor communicator "
                "repair and identical sealed state commit"
            ),
        }
        body["result_sha256"] = content_digest(body)
    result.communicator.Free()
    return None if body is None else verify_live_failure_oracle(body)


def verify_live_failure_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("F441 live-failure oracle must be an object")
    body = dict(raw)
    supplied = body.pop("result_sha256", "")
    if not isinstance(supplied, str) or content_digest(body) != supplied:
        raise UlfmRecoveryError("F441 live-failure oracle identity changed")
    expected = {
        "schema",
        "protocol",
        "world_size_before",
        "world_size_after",
        "failed_rank",
        "failed_endpoints",
        "repair_attempts",
        "trigger_error",
        "trigger_error_class",
        "receipt_sha256",
        "post_state_sha256",
        "survivor_receipts_identical",
        "claim_boundary",
    }
    before = body.get("world_size_before")
    after = body.get("world_size_after")
    failed_rank = body.get("failed_rank")
    failures = body.get("failed_endpoints")
    if (
        set(body) != expected
        or body["schema"] != LIVE_FAILURE_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
        or type(before) is not int
        or before < 3
        or type(after) is not int
        or after != before - 1
        or type(failed_rank) is not int
        or failed_rank != before - 1
        or failures != [f"endpoint-{failed_rank:03d}"]
        or type(body["repair_attempts"]) is not int
        or not 1 <= body["repair_attempts"] <= 64
        or type(body["trigger_error_class"]) is not int
        or body["trigger_error_class"] < 0
        or not isinstance(body["trigger_error"], str)
        or not body["trigger_error"]
        or body["survivor_receipts_identical"] is not True
    ):
        raise UlfmRecoveryError("F441 live-failure oracle scope/result changed")
    for field in ("receipt_sha256", "post_state_sha256"):
        value = body[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise UlfmRecoveryError(f"F441 live-failure {field} changed")
    body["result_sha256"] = supplied
    return body


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("deterministic", "capability", "live-failure"),
        default="deterministic",
    )
    parser.add_argument("--endpoints", type=int, default=12)
    parser.add_argument("--shards", type=int, default=96)
    parser.add_argument("--output")
    parser.add_argument("--verify")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.verify:
        raw = json.loads(Path(args.verify).read_text(encoding="ascii"))
        schema = raw.get("schema") if isinstance(raw, Mapping) else None
        if schema == DETERMINISTIC_SCHEMA:
            result = verify_deterministic_oracle(raw)
        elif schema == CAPABILITY_SCHEMA:
            result = verify_capability_oracle(raw)
        elif schema == LIVE_FAILURE_SCHEMA:
            result = verify_live_failure_oracle(raw)
        else:
            raise UlfmRecoveryError("unknown F441 oracle schema")
        print(result["result_sha256"])
        return 0
    if args.mode == "deterministic":
        result = build_deterministic_oracle(args.endpoints, args.shards)
    else:
        from mpi4py import MPI

        result = (
            build_capability_oracle(MPI.COMM_WORLD, MPI)
            if args.mode == "capability"
            else run_live_failure_oracle(MPI.COMM_WORLD, MPI)
        )
        if result is None:
            return 0
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="ascii")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
