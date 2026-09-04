from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import copy
import hashlib
import json
import random
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_malleable_workers import (  # noqa: E402
    MALLEABLE_WORKER_PROTOCOL,
    MalleableJobSignal,
    MalleableWorkerController,
    MalleableWorkerError,
    MalleableWorkerPolicy,
    job_signals_from_pairing_snapshots,
    recommend_job_slots,
    verify_malleable_snapshot,
)
from qfbv_malleable_evaluation import (  # noqa: E402
    MALLEABLE_MPI_PROTOCOL,
    MALLEABLE_MPI_SCHEMA,
    MalleableEvaluationConfig,
    MalleableEvaluationError,
    build_malleable_trial,
    content_digest,
    malleable_rank_report,
    verify_malleable_mpi_result,
    verify_malleable_trial,
)
from qfbv_utility_pairing import (  # noqa: E402
    UtilityPairingCandidate,
    UtilityPairingController,
    UtilityPairingPolicy,
)
from query_store import QueryStore  # noqa: E402
from symcc_query_service import (  # noqa: E402
    _MALLEABLE_QUERY_FAMILY,
    _MALLEABLE_QUERY_JOB,
    _MalleableQueryWorkerPool,
)


def _family(name: str) -> str:
    return hashlib.sha256(name.encode("ascii")).hexdigest()


def _proof(name: str) -> str:
    return hashlib.sha256(("proof:" + name).encode("ascii")).hexdigest()


def _reseal(value: dict, field: str) -> None:
    body = {key: item for key, item in value.items() if key != field}
    value[field] = hashlib.sha256(
        json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _signal(job: str, backlog: int, **changes: int | str) -> MalleableJobSignal:
    values: dict[str, int | str] = {
        "job_id": job,
        "formula_family_sha256": _family(job),
        "backlog": backlog,
    }
    values.update(changes)
    return MalleableJobSignal(**values)


def _query_envelope(value: int = 66) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "malleable-test",
        "nodes": [
            {"id": 0, "op": "read", "bits": 8, "children": [], "attrs": {"index": 0}},
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "41"},
            },
            {"id": 2, "op": "equal", "bits": 1, "children": [0, 1], "attrs": {}},
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{value:02x}"},
            },
            {"id": 4, "op": "equal", "bits": 1, "children": [0, 3], "attrs": {}},
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "input_hex": "41",
        "timeout_ms": 1000,
        "metadata": {"source": "malleable-test", "site": value},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            f"(assert (= |0| #x{value:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": (
            f"(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x{value:02x}))\n"
        ),
    }


def _controller(slots: int = 4, **policy_changes: int) -> MalleableWorkerController:
    policy = MalleableWorkerPolicy(total_slots=slots, **policy_changes)
    return MalleableWorkerController(
        "pool-a", [f"worker-{index}" for index in range(slots)], policy
    )


def _commit_ready(controller: MalleableWorkerController) -> dict:
    transition = controller.snapshot()["pending_transition"]
    assert transition is not None
    for worker in transition["transition"]["drain_workers"]:
        if worker in transition["receipts"]:
            continue
        permission = controller.permission(worker)
        expected = transition["transition"]["expected_leases"][worker]
        for lease in expected:
            controller.finish_lease(
                worker,
                permission["assignment_generation"],
                permission["assignment_token"],
                lease,
            )
        latest = controller.permission(worker)
        controller.acknowledge_drain(
            worker,
            latest["assignment_generation"],
            latest["assignment_token"],
            returned_leases=expected,
            durable_proofs=latest["durable_proofs"],
            proof_cursor=latest["proof_cursor"],
        )
    return controller.commit()


def test_policy_signal_and_inventory_validation_are_strict() -> None:
    policy = MalleableWorkerPolicy.from_mapping(
        {
            "total_slots": 3,
            "backlog_per_slot": 2,
        }
    )
    assert policy.sha256 == MalleableWorkerPolicy.from_sealed(policy.as_dict()).sha256
    assert len(policy.sha256) == 64
    assert MalleableJobSignal.from_mapping(_signal("a", 2).as_dict()).job_id == "a"

    with pytest.raises(MalleableWorkerError):
        MalleableWorkerPolicy.from_mapping({"total_slots": True})
    with pytest.raises(MalleableWorkerError):
        MalleableWorkerPolicy.from_mapping({"total_slots": 2, "unknown": 1})
    with pytest.raises(MalleableWorkerError):
        _signal("a", 1, outcomes=1, delivered=2)
    with pytest.raises(MalleableWorkerError):
        MalleableWorkerController("pool", ["same", "same"], MalleableWorkerPolicy(2))


def test_allocation_is_deterministic_bounded_and_utility_aware() -> None:
    policy = MalleableWorkerPolicy(total_slots=5, backlog_per_slot=2)
    signals = [
        _signal("cold", 8, reward_total=-1000, outcomes=2),
        _signal(
            "hot",
            8,
            reward_total=9000,
            outcomes=2,
            delivered=2,
            activated=2,
        ),
        _signal("idle", 0),
    ]
    forward = recommend_job_slots(policy, signals)
    backward = recommend_job_slots(policy, list(reversed(signals)))
    assert forward == backward
    assert sum(forward.values()) == 5
    assert forward["hot"] > forward["cold"]
    assert forward["idle"] == 0


def test_initial_grow_and_idle_shrink_are_atomic() -> None:
    controller = _controller(4)
    transition = controller.prepare([_signal("job-a", 2)])
    assert transition is not None
    assert transition["drain_workers"] == []
    commit = controller.commit()
    assert commit["allocation"] == {"job-a": 2}
    assert len(commit["changed_workers"]) == 2
    snapshot = verify_malleable_snapshot(
        controller.snapshot(), policy=controller.policy
    )
    assert (
        sum(item["state"] == "active" for item in snapshot["assignments"].values()) == 2
    )
    assert (
        sum(item["state"] == "standby" for item in snapshot["assignments"].values())
        == 2
    )

    assert controller.prepare([_signal("job-a", 0)]) is not None
    _commit_ready(controller)
    assert all(
        item["state"] == "standby"
        for item in controller.snapshot()["assignments"].values()
    )


def test_shrink_retains_busy_worker_and_drains_idle_assignment() -> None:
    controller = _controller(3)
    controller.prepare([_signal("job-a", 2)])
    controller.commit()
    busy = controller.permission("worker-1")
    controller.attach_lease(
        "worker-1",
        busy["assignment_generation"],
        busy["assignment_token"],
        "busy-lease",
    )
    transition = controller.prepare([_signal("job-a", 1)])
    assert transition is not None
    assert transition["desired_jobs"]["worker-1"] == "job-a"
    assert transition["drain_workers"] == ["worker-0"]
    controller.acknowledge_drain(
        "worker-0",
        controller.permission("worker-0")["assignment_generation"],
        controller.permission("worker-0")["assignment_token"],
        returned_leases=[],
        durable_proofs=[],
        proof_cursor=0,
    )
    controller.commit()
    assert controller.permission("worker-1")["state"] == "active"
    controller.finish_lease(
        "worker-1",
        busy["assignment_generation"],
        busy["assignment_token"],
        "busy-lease",
    )


def test_migration_drains_exact_leases_and_fences_old_assignment() -> None:
    controller = _controller(2)
    assert controller.prepare([_signal("old", 2), _signal("new", 0)])
    controller.commit()
    old_permission = controller.permission("worker-0")
    controller.attach_lease(
        "worker-0",
        old_permission["assignment_generation"],
        old_permission["assignment_token"],
        "query-a:lease-1",
    )
    transition = controller.prepare([_signal("old", 0), _signal("new", 2)])
    assert transition is not None
    assert "worker-0" in transition["drain_workers"]
    with pytest.raises(MalleableWorkerError, match="not admitting"):
        controller.attach_lease(
            "worker-0",
            old_permission["assignment_generation"],
            old_permission["assignment_token"],
            "late-lease",
        )
    with pytest.raises(MalleableWorkerError, match="incomplete"):
        controller.commit()

    proof = _proof("query-a")
    controller.finish_lease(
        "worker-0",
        old_permission["assignment_generation"],
        old_permission["assignment_token"],
        "query-a:lease-1",
        durable_proofs=[proof],
    )
    with pytest.raises(MalleableWorkerError, match="exact lease"):
        controller.acknowledge_drain(
            "worker-0",
            old_permission["assignment_generation"],
            old_permission["assignment_token"],
            returned_leases=[],
            durable_proofs=[proof],
            proof_cursor=1,
        )
    controller.acknowledge_drain(
        "worker-0",
        old_permission["assignment_generation"],
        old_permission["assignment_token"],
        returned_leases=["query-a:lease-1"],
        durable_proofs=[proof],
        proof_cursor=1,
    )
    _commit_ready(controller)
    new_permission = controller.permission("worker-0")
    assert new_permission["job_id"] == "new"
    assert new_permission["assignment_token"] != old_permission["assignment_token"]
    with pytest.raises(MalleableWorkerError, match="stale"):
        controller.attach_lease(
            "worker-0",
            old_permission["assignment_generation"],
            old_permission["assignment_token"],
            "stale-result",
        )


def test_pending_transition_survives_snapshot_replay() -> None:
    controller = _controller(2)
    controller.prepare([_signal("a", 2), _signal("b", 0)])
    controller.commit()
    permission = controller.permission("worker-0")
    controller.attach_lease(
        "worker-0",
        permission["assignment_generation"],
        permission["assignment_token"],
        "lease-a",
    )
    controller.prepare([_signal("a", 0), _signal("b", 2)])
    controller.finish_lease(
        "worker-0",
        permission["assignment_generation"],
        permission["assignment_token"],
        "lease-a",
        durable_proofs=[_proof("a")],
    )
    restored = MalleableWorkerController.from_snapshot(
        controller.policy, controller.snapshot()
    )
    latest = restored.permission("worker-0")
    restored.acknowledge_drain(
        "worker-0",
        latest["assignment_generation"],
        latest["assignment_token"],
        returned_leases=["lease-a"],
        durable_proofs=[_proof("a")],
        proof_cursor=1,
    )
    _commit_ready(restored)
    assert restored.permission("worker-0")["job_id"] == "b"
    assert (
        verify_malleable_snapshot(restored.snapshot(), policy=restored.policy)[
            "pending_transition"
        ]
        is None
    )


def test_query_store_checkpoint_is_monotonic_and_fork_detecting(
    tmp_path: Path,
) -> None:
    store = QueryStore(tmp_path / "queries")
    controller = _controller(2)
    initial = controller.snapshot()
    assert (
        store.commit_qfbv_malleable_worker_snapshot(
            controller.pool_id, controller.policy, initial
        )
        == "advanced"
    )
    assert (
        store.commit_qfbv_malleable_worker_snapshot(
            controller.pool_id, controller.policy, initial
        )
        == "idempotent"
    )

    controller.prepare([_signal("a", 2)])
    controller.commit()
    advanced = controller.snapshot()
    assert (
        store.commit_qfbv_malleable_worker_snapshot(
            controller.pool_id, controller.policy, advanced
        )
        == "advanced"
    )
    assert (
        store.commit_qfbv_malleable_worker_snapshot(
            controller.pool_id, controller.policy, initial
        )
        == "stale"
    )
    loaded = store.load_qfbv_malleable_worker_snapshot(
        controller.pool_id, controller.policy
    )
    assert loaded == advanced
    stats = store.stats()
    assert stats["malleable_worker_pools"] == 1
    assert stats["malleable_worker_allocation_generations"] == 1

    fork_controller = _controller(2)
    fork_controller.prepare([_signal("a", 2)])
    fork_controller.abort()
    fork = fork_controller.snapshot()
    with pytest.raises(ValueError, match="forked"):
        store.commit_qfbv_malleable_worker_snapshot(
            controller.pool_id, controller.policy, fork
        )


def test_query_service_pool_grows_claims_and_safely_shrinks(tmp_path: Path) -> None:
    store = QueryStore(tmp_path / "service-store")
    store.ingest(_query_envelope())
    pool = _MalleableQueryWorkerPool(
        store,
        pool_id="service-pool",
        slots=2,
        backlog_per_slot=1,
        hysteresis_slots=1,
    )
    grown = pool.rebalance()
    active = sorted(
        worker
        for worker, assignment in grown["assignments"].items()
        if assignment["state"] == "active"
    )
    assert active == ["slot-0"]
    claimed = pool.claim(
        0,
        store,
        "test-owner",
        lease_seconds=30.0,
        traversal="structural",
    )
    assert claimed is not None
    permission, lease, lease_id = claimed
    assert store.query_lease_is_active(lease_id)
    assert store.complete(
        lease,
        "test-owner",
        {
            "status": "unknown",
            "assignments": {},
            "solver": "malleable-test",
            "elapsed_us": 1,
        },
    )
    pool.finish(0, permission, lease_id)
    assert not store.query_lease_is_active(lease_id)
    shrunk = pool.rebalance()
    assert all(
        assignment["state"] == "standby"
        for assignment in shrunk["assignments"].values()
    )
    with pytest.raises(RuntimeError, match="already has a coordinator"):
        _MalleableQueryWorkerPool(
            store,
            pool_id="service-pool",
            slots=2,
            backlog_per_slot=1,
            hysteresis_slots=1,
        )
    pool.close()
    restored = _MalleableQueryWorkerPool(
        store,
        pool_id="service-pool",
        slots=2,
        backlog_per_slot=1,
        hysteresis_slots=1,
    )
    assert restored.snapshot() == shrunk
    restored.close()


def test_query_service_restart_waits_for_live_lease_then_recovers(
    tmp_path: Path,
) -> None:
    store = QueryStore(tmp_path / "restart-store")
    store.ingest(_query_envelope())
    pool = _MalleableQueryWorkerPool(
        store,
        pool_id="restart-pool",
        slots=1,
        backlog_per_slot=1,
        hysteresis_slots=1,
    )
    pool.rebalance()
    claimed = pool.claim(
        0,
        store,
        "old-owner",
        lease_seconds=30.0,
        traversal="structural",
    )
    assert claimed is not None
    _permission, lease, lease_id = claimed
    transition = pool.controller.prepare(
        [
            MalleableJobSignal(
                job_id=_MALLEABLE_QUERY_JOB,
                formula_family_sha256=_MALLEABLE_QUERY_FAMILY,
                backlog=0,
            )
        ]
    )
    assert transition is not None
    pool._persist()
    pool.close()

    recovered = _MalleableQueryWorkerPool(
        store,
        pool_id="restart-pool",
        slots=1,
        backlog_per_slot=1,
        hysteresis_slots=1,
    )
    waiting = recovered.rebalance()
    assert waiting["pending_transition"] is not None
    assert waiting["assignments"]["slot-0"]["state"] == "draining"
    assert store.query_lease_is_active(lease_id)

    assert store.complete(
        lease,
        "old-owner",
        {
            "status": "unknown",
            "assignments": {},
            "solver": "restart-test",
            "elapsed_us": 1,
        },
    )
    quiescent = recovered.rebalance()
    assert quiescent["pending_transition"] is None
    assert quiescent["assignments"]["slot-0"]["state"] == "standby"
    assert not store.query_lease_is_active(lease_id)
    recovered.close()


def test_query_service_drain_waits_for_process_local_expired_lease(
    tmp_path: Path,
) -> None:
    store = QueryStore(tmp_path / "expired-live-store")
    store.ingest(_query_envelope())
    pool = _MalleableQueryWorkerPool(
        store,
        pool_id="expired-live-pool",
        slots=1,
        backlog_per_slot=1,
        hysteresis_slots=1,
    )
    pool.rebalance()
    claimed = pool.claim(
        0,
        store,
        "live-owner",
        lease_seconds=30.0,
        traversal="structural",
    )
    assert claimed is not None
    permission, lease, lease_id = claimed
    transition = pool.controller.prepare(
        [
            MalleableJobSignal(
                job_id=_MALLEABLE_QUERY_JOB,
                formula_family_sha256=_MALLEABLE_QUERY_FAMILY,
                backlog=0,
            )
        ]
    )
    assert transition is not None
    pool._persist()
    with store._connect() as db:
        db.execute(
            "UPDATE queries SET lease_until = 0 WHERE query_id = ?",
            (lease.query_id,),
        )
        db.commit()
    assert not store.query_lease_is_active(lease_id)

    waiting = pool.rebalance()
    assert waiting["pending_transition"] is not None
    assert waiting["assignments"]["slot-0"]["leases"] == [lease_id]

    assert not store.complete(
        lease,
        "live-owner",
        {
            "status": "unknown",
            "assignments": {},
            "solver": "expired-live-test",
            "elapsed_us": 1,
        },
    )
    # Global expiry rejects the result, while the process-local assignment is
    # still drained explicitly so the pending resize can commit.
    pool.finish(0, permission, lease_id)
    quiescent = pool.snapshot()
    assert quiescent["pending_transition"] is None
    assert quiescent["assignments"]["slot-0"]["state"] == "standby"
    pool.close()


def test_abort_reopens_old_assignment_without_reusing_a_new_token() -> None:
    controller = _controller(1)
    controller.prepare([_signal("a", 1), _signal("b", 0)])
    controller.commit()
    before = controller.permission("worker-0")
    controller.prepare([_signal("a", 0), _signal("b", 1)])
    controller.abort()
    after = controller.permission("worker-0")
    assert after["state"] == "active"
    assert after["job_id"] == "a"
    assert after["assignment_token"] == before["assignment_token"]


def test_snapshot_tampering_fails_closed() -> None:
    controller = _controller(2)
    controller.prepare([_signal("a", 2)])
    controller.commit()
    snapshot = controller.snapshot()
    for mutate in (
        lambda value: value["assignments"]["worker-0"].__setitem__("job_id", "b"),
        lambda value: value.__setitem__("allocation_generation", 99),
        lambda value: value["totals"].__setitem__("committed", 99),
    ):
        changed = copy.deepcopy(snapshot)
        mutate(changed)
        with pytest.raises(MalleableWorkerError):
            verify_malleable_snapshot(changed, policy=controller.policy)

    changed = copy.deepcopy(snapshot)
    changed["assignments"]["worker-0"]["job_id"] = "b"
    changed.pop("snapshot_sha256")
    changed["snapshot_sha256"] = hashlib.sha256(
        json.dumps(
            changed, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()
    with pytest.raises(MalleableWorkerError, match="token"):
        verify_malleable_snapshot(changed, policy=controller.policy)


def test_resealed_nondeterministic_worker_mapping_fails_replay() -> None:
    controller = _controller(2)
    controller.prepare([_signal("a", 1), _signal("b", 1), _signal("c", 0)])
    controller.commit()
    controller.prepare([_signal("a", 0), _signal("b", 1), _signal("c", 1)])
    snapshot = controller.snapshot()
    pending = snapshot["pending_transition"]
    assert pending is not None
    desired = pending["transition"]["desired_jobs"]
    workers = sorted(desired)
    desired[workers[0]], desired[workers[1]] = desired[workers[1]], desired[workers[0]]
    _reseal(pending["transition"], "transition_sha256")
    _reseal(snapshot, "snapshot_sha256")
    with pytest.raises(MalleableWorkerError, match="mapping"):
        verify_malleable_snapshot(snapshot, policy=controller.policy)


def test_verified_pairing_feedback_drives_formula_family_signal() -> None:
    pairing_policy = UtilityPairingPolicy()
    pairing = UtilityPairingController(pairing_policy)
    family = _family("family-a")
    candidate = UtilityPairingCandidate(
        record_sha256=_family("record"),
        stream_id=_family("stream"),
        publisher_worker="publisher",
        consumer_worker="consumer",
        formula_family_sha256=family,
        event_sequence=1,
        event_lag=3,
        checker_elapsed_us=17,
    )
    decision = pairing.consider(candidate)
    pairing.observe(decision["decision_sha256"], outcome="unit")
    signals = job_signals_from_pairing_snapshots(
        {"job-a": (family, 7), "job-b": (_family("family-b"), 1)},
        [pairing.snapshot()],
        pairing_policy=pairing_policy,
    )
    first = {signal.job_id: signal for signal in signals}["job-a"]
    assert first.backlog == 7
    assert first.activated == first.delivered == first.outcomes == 1
    assert first.reward_total == pairing_policy.unit_reward
    with pytest.raises(MalleableWorkerError, match="duplicate"):
        job_signals_from_pairing_snapshots(
            {"job-a": (family, 1)},
            [pairing.snapshot(), pairing.snapshot()],
            pairing_policy=pairing_policy,
        )


def test_seeded_grow_drain_migrate_shrink_preserves_conservation() -> None:
    rng = random.Random(0xF438)
    controller = _controller(8, backlog_per_slot=2)
    jobs = ["a", "b", "c"]
    stale_permissions: list[dict] = []
    for round_index in range(80):
        signals = [
            _signal(
                job,
                rng.randrange(0, 13),
                reward_total=rng.randrange(-2000, 10_001),
                outcomes=2,
                delivered=1,
                activated=rng.randrange(0, 2),
                checker_total_us=rng.randrange(0, 20_001),
                event_lag_total=rng.randrange(0, 65),
            )
            for job in jobs
        ]
        transition = controller.prepare(signals)
        if transition is not None:
            stale_permissions.extend(
                controller.permission(worker) for worker in transition["drain_workers"]
            )
            _commit_ready(controller)
        snapshot = verify_malleable_snapshot(
            controller.snapshot(), policy=controller.policy
        )
        assert len(snapshot["assignments"]) == controller.policy.total_slots
        assert (
            sum(
                item["state"] in {"active", "draining"}
                for item in snapshot["assignments"].values()
            )
            <= controller.policy.total_slots
        )
        if round_index % 11 == 0:
            controller = MalleableWorkerController.from_snapshot(
                controller.policy, snapshot
            )
    assert stale_permissions
    rejected = 0
    for permission in stale_permissions:
        if (
            controller.permission(permission["worker_id"])["assignment_token"]
            == permission["assignment_token"]
        ):
            continue
        with pytest.raises(MalleableWorkerError, match="stale"):
            controller.attach_lease(
                permission["worker_id"],
                permission["assignment_generation"],
                permission["assignment_token"],
                f"stale-{rejected}",
            )
        rejected += 1
    assert rejected > 0
    assert controller.snapshot()["protocol"] == MALLEABLE_WORKER_PROTOCOL


def test_multirank_trace_replays_and_rejects_resealed_tampering() -> None:
    trial = build_malleable_trial(
        MalleableEvaluationConfig(
            world_size=5,
            epochs=8,
            seed=0xF438,
            jobs=3,
        )
    )
    assert verify_malleable_trial(trial) == trial
    assert trial["attached_leases"] == trial["retired_leases"]
    assert trial["durable_proofs"] == trial["retired_leases"]
    assert trial["stale_fences_rejected"] > 0
    assert len(trial["allocations"]) == 8

    changed = copy.deepcopy(trial)
    changed["allocations"][0] = {"job-0": 4}
    _reseal(changed, "artifact_sha256")
    with pytest.raises(MalleableEvaluationError, match="replay|conservation"):
        verify_malleable_trial(changed)

    changed = copy.deepcopy(trial)
    attach = next(row for row in changed["operations"] if row["op"] == "attach")
    attach["arguments"]["lease_id"] += "-changed"
    _reseal(changed, "artifact_sha256")
    with pytest.raises(MalleableEvaluationError, match="disagrees"):
        verify_malleable_trial(changed)


def test_physical_rank_attestation_has_exact_operation_ownership() -> None:
    trial = build_malleable_trial(MalleableEvaluationConfig())
    reports = [
        malleable_rank_report(trial, rank, f"node-{rank % 2}")
        for rank in range(trial["config"]["world_size"])
    ]
    body = {
        "schema": MALLEABLE_MPI_SCHEMA,
        "protocol": MALLEABLE_MPI_PROTOCOL,
        "physical_world_size": len(reports),
        "mpi_library_version": "test-mpi-v1",
        "trial": trial,
        "rank_reports": reports,
        "worker_operations_attested": sum(
            len(report["worker_operation_sha256"]) for report in reports
        ),
        "claim_boundary": "test-only physical rank attestation",
    }
    body["artifact_sha256"] = content_digest(body)
    assert verify_malleable_mpi_result(body) == body

    changed = copy.deepcopy(body)
    changed["rank_reports"][1]["worker_operation_sha256"].pop()
    _reseal(changed["rank_reports"][1], "report_sha256")
    _reseal(changed, "artifact_sha256")
    with pytest.raises(MalleableEvaluationError, match="rank report"):
        verify_malleable_mpi_result(changed)
