#!/usr/bin/env python3
"""Replay oracle and deterministic multi-rank scenarios for F438."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Mapping

from qfbv_malleable_workers import (
    MALLEABLE_WORKER_PROTOCOL,
    MalleableJobSignal,
    MalleableWorkerController,
    MalleableWorkerError,
    MalleableWorkerPolicy,
    verify_malleable_snapshot,
)


MALLEABLE_EVALUATION_PROTOCOL = "symcc-f438-malleable-multirank-evaluation-v1"
MALLEABLE_EVALUATION_SCHEMA = "symcc-f438-malleable-multirank-result-v1"
MALLEABLE_MPI_PROTOCOL = "symcc-f438-malleable-physical-mpi-attestation-v1"
MALLEABLE_MPI_SCHEMA = "symcc-f438-malleable-physical-mpi-result-v1"
MALLEABLE_MPI_RANK_SCHEMA = "symcc-f438-malleable-physical-rank-report-v1"


class MalleableEvaluationError(ValueError):
    """A malleable multi-rank trace failed independent replay."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise MalleableEvaluationError(f"{name} must be in [{lower}, {upper}]")
    return value


@dataclass(frozen=True)
class MalleableEvaluationConfig:
    world_size: int = 5
    epochs: int = 8
    seed: int = 0xF438
    jobs: int = 3
    backlog_per_slot: int = 2

    def __post_init__(self) -> None:
        _integer(self.world_size, "F438 world size", 3, 4097)
        _integer(self.epochs, "F438 epochs", 3, 4096)
        _integer(self.seed, "F438 seed", 0, (1 << 63) - 1)
        _integer(self.jobs, "F438 jobs", 2, min(256, self.world_size - 1))
        _integer(self.backlog_per_slot, "F438 backlog per slot", 1, 1_000_000)

    @property
    def worker_ids(self) -> tuple[str, ...]:
        return tuple(f"rank-{rank}" for rank in range(1, self.world_size))

    @property
    def policy(self) -> MalleableWorkerPolicy:
        return MalleableWorkerPolicy(
            total_slots=self.world_size - 1,
            backlog_per_slot=self.backlog_per_slot,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": MALLEABLE_EVALUATION_PROTOCOL,
            "world_size": self.world_size,
            "worker_ids": list(self.worker_ids),
            "epochs": self.epochs,
            "seed": self.seed,
            "jobs": self.jobs,
            "backlog_per_slot": self.backlog_per_slot,
            "policy": self.policy.as_dict(),
            "policy_sha256": self.policy.sha256,
        }


def _family(job: str) -> str:
    return hashlib.sha256(f"F438-family:{job}".encode("ascii")).hexdigest()


def _proof(lease_id: str) -> str:
    return hashlib.sha256(f"F438-proof:{lease_id}".encode("ascii")).hexdigest()


def _record(
    operations: list[dict[str, Any]],
    op: str,
    arguments: Mapping[str, Any],
    result: Any,
) -> None:
    operations.append(
        {
            "ordinal": len(operations) + 1,
            "op": op,
            "arguments": dict(arguments),
            "result": result,
        }
    )


def _signals(
    config: MalleableEvaluationConfig,
    rng: random.Random,
    epoch: int,
) -> list[MalleableJobSignal]:
    signals = []
    for job_index in range(config.jobs):
        job = f"job-{job_index}"
        phase = (epoch + job_index) % config.jobs
        backlog = (
            0
            if phase == config.jobs - 1
            else rng.randrange(1, (config.world_size - 1) * config.backlog_per_slot + 1)
        )
        outcomes = 2 + epoch
        delivered = max(1, outcomes - (job_index % 2))
        activated = min(delivered, 1 + ((epoch + job_index) % delivered))
        signals.append(
            MalleableJobSignal(
                job_id=job,
                formula_family_sha256=_family(job),
                backlog=backlog,
                reward_total=(activated * 5000) + ((delivered - activated) * 500),
                outcomes=outcomes,
                delivered=delivered,
                activated=activated,
                checker_total_us=(epoch + job_index + 1) * 100,
                event_lag_total=(epoch + job_index) * 3,
            )
        )
    return signals


def _drain_and_commit(
    controller: MalleableWorkerController,
    operations: list[dict[str, Any]],
) -> tuple[int, int]:
    snapshot = controller.snapshot()
    pending = snapshot["pending_transition"]
    if pending is None:
        return 0, 0
    leases = 0
    proofs = 0
    transition = pending["transition"]
    for worker in transition["drain_workers"]:
        permission = controller.permission(worker)
        expected = list(transition["expected_leases"][worker])
        for lease_id in expected:
            proof = _proof(lease_id)
            arguments = {
                "worker_id": worker,
                "generation": permission["assignment_generation"],
                "token": permission["assignment_token"],
                "lease_id": lease_id,
                "durable_proofs": [proof],
            }
            result = controller.finish_lease(
                worker,
                arguments["generation"],
                arguments["token"],
                lease_id,
                durable_proofs=[proof],
            )
            _record(operations, "finish", arguments, result)
            leases += 1
            proofs += 1
        permission = controller.permission(worker)
        arguments = {
            "worker_id": worker,
            "generation": permission["assignment_generation"],
            "token": permission["assignment_token"],
            "returned_leases": expected,
            "durable_proofs": permission["durable_proofs"],
            "proof_cursor": permission["proof_cursor"],
        }
        result = controller.acknowledge_drain(
            worker,
            arguments["generation"],
            arguments["token"],
            returned_leases=arguments["returned_leases"],
            durable_proofs=arguments["durable_proofs"],
            proof_cursor=arguments["proof_cursor"],
        )
        _record(operations, "ack", arguments, result)
    result = controller.commit()
    _record(operations, "commit", {}, result)
    return leases, proofs


def build_malleable_trial(config: MalleableEvaluationConfig) -> dict[str, Any]:
    """Build a sealed grow/drain/migrate/shrink trace over logical MPI ranks."""
    controller = MalleableWorkerController(
        "f438-multirank-pool", config.worker_ids, config.policy
    )
    rng = random.Random(config.seed)
    operations: list[dict[str, Any]] = []
    stale_permissions: list[dict[str, Any]] = []
    attached = 0
    retired = 0
    durable = 0
    allocations: list[dict[str, int]] = []
    for epoch in range(config.epochs):
        signals = _signals(config, rng, epoch)
        before = {worker: controller.permission(worker) for worker in config.worker_ids}
        result = controller.prepare(signals)
        _record(
            operations,
            "prepare",
            {"signals": [signal.as_dict() for signal in signals]},
            result,
        )
        if result is not None:
            stale_permissions.extend(
                before[worker] for worker in result["drain_workers"]
            )
            drained, proofs = _drain_and_commit(controller, operations)
            retired += drained
            durable += proofs
        snapshot = controller.snapshot()
        allocation: dict[str, int] = {}
        for assignment in snapshot["assignments"].values():
            if assignment["job_id"]:
                allocation[assignment["job_id"]] = (
                    allocation.get(assignment["job_id"], 0) + 1
                )
        allocations.append(dict(sorted(allocation.items())))
        for worker, assignment in sorted(snapshot["assignments"].items()):
            if assignment["state"] != "active" or assignment["leases"]:
                continue
            permission = controller.permission(worker)
            lease_id = f"epoch-{epoch}:{worker}:{assignment['job_id']}"
            arguments = {
                "worker_id": worker,
                "generation": permission["assignment_generation"],
                "token": permission["assignment_token"],
                "lease_id": lease_id,
            }
            event = controller.attach_lease(
                worker,
                arguments["generation"],
                arguments["token"],
                lease_id,
            )
            _record(operations, "attach", arguments, event)
            attached += 1
        _record(
            operations,
            "epoch",
            {"epoch": epoch},
            {"allocation": allocations[-1]},
        )
        if epoch == config.epochs // 2:
            checkpoint = controller.snapshot()
            _record(operations, "restart", {"snapshot": checkpoint}, checkpoint)
            controller = MalleableWorkerController.from_snapshot(
                config.policy, checkpoint
            )

    final_signals = [
        MalleableJobSignal(
            job_id=f"job-{index}",
            formula_family_sha256=_family(f"job-{index}"),
            backlog=0,
        )
        for index in range(config.jobs)
    ]
    result = controller.prepare(final_signals)
    _record(
        operations,
        "prepare",
        {"signals": [signal.as_dict() for signal in final_signals]},
        result,
    )
    drained, proofs = _drain_and_commit(controller, operations)
    retired += drained
    durable += proofs

    stale_rejected = 0
    for index, permission in enumerate(stale_permissions):
        if (
            controller.permission(permission["worker_id"])["assignment_token"]
            == permission["assignment_token"]
        ):
            continue
        arguments = {
            "worker_id": permission["worker_id"],
            "generation": permission["assignment_generation"],
            "token": permission["assignment_token"],
            "lease_id": f"stale-{index}",
        }
        try:
            controller.attach_lease(
                arguments["worker_id"],
                arguments["generation"],
                arguments["token"],
                arguments["lease_id"],
            )
        except MalleableWorkerError:
            rejected = True
            stale_rejected += 1
        else:
            rejected = False
        _record(operations, "stale-probe", arguments, {"rejected": rejected})

    final_snapshot = controller.snapshot()
    body: dict[str, Any] = {
        "schema": MALLEABLE_EVALUATION_SCHEMA,
        "protocol": MALLEABLE_EVALUATION_PROTOCOL,
        "malleability_protocol": MALLEABLE_WORKER_PROTOCOL,
        "config": config.as_dict(),
        "operations": operations,
        "allocations": allocations,
        "attached_leases": attached,
        "retired_leases": retired,
        "durable_proofs": durable,
        "stale_fences_rejected": stale_rejected,
        "final_snapshot": final_snapshot,
        "claim_boundary": (
            "deterministic logical multi-rank protocol oracle; validates resource, "
            "lease, proof, restart, and fencing conservation without throughput, "
            "coverage, defect-yield, MPI rank-spawn, or multi-node speedup claims"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    return verify_malleable_trial(body)


def verify_malleable_trial(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Replay every operation and reject even re-sealed trace tampering."""
    if not isinstance(raw, Mapping):
        raise MalleableEvaluationError("F438 trial must be an object")
    body = dict(raw)
    supplied = body.pop("artifact_sha256", "")
    if not isinstance(supplied, str) or content_digest(body) != supplied:
        raise MalleableEvaluationError("F438 trial identity changed")
    if (
        body.get("schema") != MALLEABLE_EVALUATION_SCHEMA
        or body.get("protocol") != MALLEABLE_EVALUATION_PROTOCOL
        or body.get("malleability_protocol") != MALLEABLE_WORKER_PROTOCOL
    ):
        raise MalleableEvaluationError("F438 trial scope changed")
    config_raw = body.get("config")
    if not isinstance(config_raw, Mapping):
        raise MalleableEvaluationError("F438 configuration is missing")
    config = MalleableEvaluationConfig(
        world_size=config_raw.get("world_size"),
        epochs=config_raw.get("epochs"),
        seed=config_raw.get("seed"),
        jobs=config_raw.get("jobs"),
        backlog_per_slot=config_raw.get("backlog_per_slot"),
    )
    if config.as_dict() != dict(config_raw):
        raise MalleableEvaluationError("F438 configuration changed")
    controller = MalleableWorkerController(
        "f438-multirank-pool", config.worker_ids, config.policy
    )
    operations = body.get("operations")
    if not isinstance(operations, list) or not operations:
        raise MalleableEvaluationError("F438 operation trace is missing")
    attached = 0
    retired = 0
    proofs: set[str] = set()
    stale_rejected = 0
    replay_allocations: list[dict[str, int]] = []
    for expected_ordinal, row in enumerate(operations, 1):
        if (
            not isinstance(row, Mapping)
            or row.get("ordinal") != expected_ordinal
            or not isinstance(row.get("arguments"), Mapping)
        ):
            raise MalleableEvaluationError("F438 operation trace order changed")
        op = row.get("op")
        arguments = row["arguments"]
        try:
            if op == "prepare":
                signals = [
                    MalleableJobSignal.from_mapping(item)
                    for item in arguments.get("signals", [])
                ]
                result = controller.prepare(signals)
            elif op == "attach":
                result = controller.attach_lease(
                    arguments["worker_id"],
                    arguments["generation"],
                    arguments["token"],
                    arguments["lease_id"],
                )
                attached += 1
            elif op == "finish":
                result = controller.finish_lease(
                    arguments["worker_id"],
                    arguments["generation"],
                    arguments["token"],
                    arguments["lease_id"],
                    durable_proofs=arguments["durable_proofs"],
                )
                retired += 1
                proofs.update(arguments["durable_proofs"])
            elif op == "ack":
                result = controller.acknowledge_drain(
                    arguments["worker_id"],
                    arguments["generation"],
                    arguments["token"],
                    returned_leases=arguments["returned_leases"],
                    durable_proofs=arguments["durable_proofs"],
                    proof_cursor=arguments["proof_cursor"],
                )
            elif op == "commit":
                result = controller.commit()
            elif op == "restart":
                checkpoint = verify_malleable_snapshot(
                    arguments["snapshot"], policy=config.policy
                )
                if checkpoint != controller.snapshot():
                    raise MalleableEvaluationError("F438 restart prefix changed")
                controller = MalleableWorkerController.from_snapshot(
                    config.policy, checkpoint
                )
                result = controller.snapshot()
            elif op == "epoch":
                if arguments.get("epoch") != len(replay_allocations):
                    raise MalleableEvaluationError("F438 epoch order changed")
                allocation: dict[str, int] = {}
                for assignment in controller.snapshot()["assignments"].values():
                    job = str(assignment["job_id"])
                    if job:
                        allocation[job] = allocation.get(job, 0) + 1
                allocation = dict(sorted(allocation.items()))
                replay_allocations.append(allocation)
                result = {"allocation": allocation}
            elif op == "stale-probe":
                try:
                    controller.attach_lease(
                        arguments["worker_id"],
                        arguments["generation"],
                        arguments["token"],
                        arguments["lease_id"],
                    )
                except MalleableWorkerError:
                    result = {"rejected": True}
                    stale_rejected += 1
                else:
                    result = {"rejected": False}
            else:
                raise MalleableEvaluationError("F438 operation kind changed")
        except (KeyError, TypeError, MalleableWorkerError) as error:
            raise MalleableEvaluationError(
                f"F438 operation {expected_ordinal} failed replay"
            ) from error
        if result != row.get("result"):
            raise MalleableEvaluationError(
                f"F438 operation {expected_ordinal} disagrees with replay"
            )
    final_snapshot = verify_malleable_snapshot(
        body.get("final_snapshot"), policy=config.policy
    )
    if final_snapshot != controller.snapshot():
        raise MalleableEvaluationError("F438 final state disagrees with replay")
    if any(
        assignment["state"] != "standby"
        or assignment["leases"]
        or assignment["durable_proofs"]
        for assignment in final_snapshot["assignments"].values()
    ):
        raise MalleableEvaluationError("F438 final shrink did not quiesce")
    allocations = body.get("allocations")
    if (
        not isinstance(allocations, list)
        or len(allocations) != config.epochs
        or allocations != replay_allocations
        or any(
            not isinstance(row, Mapping)
            or sum(row.values()) > config.world_size - 1
            or any(type(value) is not int or value < 0 for value in row.values())
            for row in allocations
        )
    ):
        raise MalleableEvaluationError("F438 allocation conservation changed")
    if (
        body.get("attached_leases") != attached
        or body.get("retired_leases") != retired
        or body.get("durable_proofs") != len(proofs)
        or body.get("stale_fences_rejected") != stale_rejected
        or attached != retired
        or stale_rejected <= 0
    ):
        raise MalleableEvaluationError("F438 lease/proof/fence conservation changed")
    return {**body, "artifact_sha256": supplied}


def malleable_rank_report(
    trial: Mapping[str, Any], rank: int, processor: str
) -> dict[str, Any]:
    """Build one exact physical-rank attestation after local full replay."""
    verified = verify_malleable_trial(trial)
    config = verified["config"]
    world_size = int(config["world_size"])
    normalized_rank = _integer(rank, "F438 MPI rank", 0, world_size - 1)
    if not isinstance(processor, str) or not processor or len(processor) > 256:
        raise MalleableEvaluationError("F438 processor identity is invalid")
    worker = "" if normalized_rank == 0 else f"rank-{normalized_rank}"
    operation_ids = [
        content_digest(row)
        for row in verified["operations"]
        if worker and row["arguments"].get("worker_id") == worker
    ]
    body = {
        "schema": MALLEABLE_MPI_RANK_SCHEMA,
        "protocol": MALLEABLE_MPI_PROTOCOL,
        "rank": normalized_rank,
        "world_size": world_size,
        "role": "coordinator" if normalized_rank == 0 else "worker",
        "worker_id": worker,
        "processor": processor,
        "trial_sha256": verified["artifact_sha256"],
        "full_replay_verified": True,
        "worker_operation_sha256": operation_ids,
    }
    body["report_sha256"] = content_digest(body)
    return body


def verify_malleable_mpi_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Verify exact rank membership and command-stream ownership."""
    if not isinstance(raw, Mapping):
        raise MalleableEvaluationError("F438 MPI result must be an object")
    body = dict(raw)
    supplied = body.pop("artifact_sha256", "")
    if not isinstance(supplied, str) or content_digest(body) != supplied:
        raise MalleableEvaluationError("F438 MPI result identity changed")
    if (
        body.get("schema") != MALLEABLE_MPI_SCHEMA
        or body.get("protocol") != MALLEABLE_MPI_PROTOCOL
    ):
        raise MalleableEvaluationError("F438 MPI result scope changed")
    if set(body) != {
        "schema",
        "protocol",
        "physical_world_size",
        "mpi_library_version",
        "trial",
        "rank_reports",
        "worker_operations_attested",
        "claim_boundary",
    } or any(
        not isinstance(body.get(name), str)
        or not body[name]
        or len(body[name]) > maximum
        for name, maximum in (
            ("mpi_library_version", 4096),
            ("claim_boundary", 2048),
        )
    ):
        raise MalleableEvaluationError("F438 MPI result shape changed")
    trial = verify_malleable_trial(body.get("trial"))
    world_size = int(trial["config"]["world_size"])
    reports = body.get("rank_reports")
    if not isinstance(reports, list) or len(reports) != world_size:
        raise MalleableEvaluationError("F438 MPI rank inventory changed")
    normalized: list[dict[str, Any]] = []
    observed: set[str] = set()
    for expected_rank, raw_report in enumerate(reports):
        if not isinstance(raw_report, Mapping):
            raise MalleableEvaluationError("F438 MPI rank report is invalid")
        report = dict(raw_report)
        report_sha = report.pop("report_sha256", "")
        if not isinstance(report_sha, str) or content_digest(report) != report_sha:
            raise MalleableEvaluationError("F438 MPI rank report identity changed")
        worker = "" if expected_rank == 0 else f"rank-{expected_rank}"
        expected_ids = [
            content_digest(row)
            for row in trial["operations"]
            if worker and row["arguments"].get("worker_id") == worker
        ]
        if set(report) != {
            "schema",
            "protocol",
            "rank",
            "world_size",
            "role",
            "worker_id",
            "processor",
            "trial_sha256",
            "full_replay_verified",
            "worker_operation_sha256",
        } or (
            report.get("schema") != MALLEABLE_MPI_RANK_SCHEMA
            or report.get("protocol") != MALLEABLE_MPI_PROTOCOL
            or report.get("rank") != expected_rank
            or report.get("world_size") != world_size
            or report.get("role") != ("coordinator" if expected_rank == 0 else "worker")
            or report.get("worker_id") != worker
            or report.get("trial_sha256") != trial["artifact_sha256"]
            or report.get("full_replay_verified") is not True
            or report.get("worker_operation_sha256") != expected_ids
            or not isinstance(report.get("processor"), str)
            or not report["processor"]
            or len(report["processor"]) > 256
        ):
            raise MalleableEvaluationError("F438 MPI rank report changed")
        observed.update(expected_ids)
        normalized.append({**report, "report_sha256": report_sha})
    expected_worker_operations = {
        content_digest(row)
        for row in trial["operations"]
        if "worker_id" in row["arguments"]
    }
    if observed != expected_worker_operations:
        raise MalleableEvaluationError("F438 MPI operation ownership changed")
    if (
        body.get("rank_reports") != normalized
        or body.get("physical_world_size") != world_size
        or body.get("worker_operations_attested") != len(observed)
    ):
        raise MalleableEvaluationError("F438 MPI aggregate changed")
    return {
        **body,
        "trial": trial,
        "rank_reports": normalized,
        "artifact_sha256": supplied,
    }
