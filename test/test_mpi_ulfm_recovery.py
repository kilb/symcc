from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mpi4py import MPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from mpi_ulfm_recovery import (  # noqa: E402
    ULFM_CAPABILITY_SCHEMA,
    DurableUlfmCoordinator,
    EndpointIdentity,
    RecoveryShard,
    UlfmRecoveryController,
    UlfmRecoveryError,
    UlfmRecoveryPolicy,
    UlfmCollectiveTimeout,
    UlfmRuntimeError,
    build_endpoint_attestation,
    build_work_envelope,
    build_work_fence,
    content_digest,
    deadline_shrink,
    is_ulfm_failure,
    observe_failed_endpoints,
    probe_ulfm_runtime,
    shrink_and_attest,
    verify_endpoint_attestation,
    verify_recovery_receipt,
    verify_recovery_snapshot,
    verify_work_envelope,
    verify_work_fence,
)
from query_store import QueryStore  # noqa: E402
from ulfm_snapshot_store import AtomicUlfmSnapshotStore  # noqa: E402
from check_mpi_ulfm_recovery_oracles import (  # noqa: E402
    build_deterministic_oracle,
    verify_deterministic_oracle,
)
from check_f443_ulfm_hot_path_oracles import (  # noqa: E402
    SCHEMA as F443_SCHEMA,
    verify_campaign_oracle,
)
from check_f446_elastic_ulfm_oracles import (  # noqa: E402
    SCHEMA as F446_SCHEMA,
    verify_campaign_oracle as verify_f446_campaign_oracle,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _endpoint(rank: int) -> EndpointIdentity:
    return EndpointIdentity(
        endpoint_id=f"worker-{rank}",
        initial_rank=rank,
        incarnation_sha256=_hash(f"incarnation-{rank}"),
        host_id=f"node-{rank // 2}",
    )


def _controller(
    endpoints: int = 4,
    shards: int = 8,
    *,
    policy: UlfmRecoveryPolicy | None = None,
) -> UlfmRecoveryController:
    identities = [_endpoint(rank) for rank in range(endpoints)]
    return UlfmRecoveryController(
        "f441-test-run",
        identities,
        [
            RecoveryShard(
                shard_id=f"shard-{index}",
                owner_endpoint=f"worker-{index % endpoints}",
                checkpoint_sha256=_hash(f"checkpoint-{index}"),
            )
            for index in range(shards)
        ],
        policy,
    )


def _attach(controller: UlfmRecoveryController, shard_id: str, work_id: str) -> dict:
    permission = controller.shard_permission(shard_id)
    return controller.attach_work(
        shard_id,
        permission["owner_endpoint"],
        permission["generation"],
        permission["generation_token"],
        permission["shard_token"],
        work_id,
    )


def _plan(
    controller: UlfmRecoveryController, failures: list[str]
) -> dict[str, Any]:
    return controller.prepare_recovery(
        failures,
        generation=controller.generation,
        generation_token=controller.generation_token,
    )


def _attestations(
    controller: UlfmRecoveryController,
    plan: dict[str, Any],
    survivors: list[int],
) -> list[dict[str, Any]]:
    return [
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


def test_policy_inventory_and_attestation_are_strict() -> None:
    policy = UlfmRecoveryPolicy.from_mapping(
        {"max_endpoints": 8, "max_shards": 16, "max_recovery_queue": 16}
    )
    assert UlfmRecoveryPolicy.from_sealed(policy.as_dict()).sha256 == policy.sha256
    controller = _controller(policy=policy)
    plan = _plan(controller, ["worker-3"])
    attestation = _attestations(controller, plan, [0, 1, 2])[0]
    assert verify_endpoint_attestation(attestation) == attestation

    with pytest.raises(UlfmRecoveryError):
        UlfmRecoveryPolicy.from_mapping({"max_endpoints": True})
    with pytest.raises(UlfmRecoveryError):
        UlfmRecoveryPolicy.from_mapping({"unknown": 1})
    with pytest.raises(UlfmRecoveryError, match="dense"):
        UlfmRecoveryController(
            "bad",
            [_endpoint(0), EndpointIdentity("worker-2", 2, _hash("i"), "n")],
            [RecoveryShard("s", "worker-0", _hash("c"))],
        )
    tampered = copy.deepcopy(attestation)
    tampered["new_rank"] = 2
    with pytest.raises(UlfmRecoveryError, match="identity"):
        verify_endpoint_attestation(tampered)


def test_work_checkpoint_completion_and_lease_fences_are_monotonic() -> None:
    controller = _controller()
    lease = _attach(controller, "shard-0", "query-0")
    permission = controller.shard_permission("shard-0")
    checkpoint = controller.checkpoint_work(
        "shard-0",
        permission["owner_endpoint"],
        permission["generation"],
        permission["generation_token"],
        permission["shard_token"],
        lease["lease_token"],
        _hash("checkpoint-new"),
        7,
    )
    assert checkpoint["cursor"] == 7
    with pytest.raises(UlfmRecoveryError, match="not monotonic"):
        controller.checkpoint_work(
            "shard-0",
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            lease["lease_token"],
            _hash("checkpoint-stale"),
            7,
        )
    completion = controller.finish_work(
        "shard-0",
        permission["owner_endpoint"],
        permission["generation"],
        permission["generation_token"],
        permission["shard_token"],
        lease["lease_token"],
        _hash("proof-0"),
    )
    assert completion["completed"] == 1
    with pytest.raises(UlfmRecoveryError, match="stale ULFM work lease"):
        controller.finish_work(
            "shard-0",
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            lease["lease_token"],
            _hash("proof-duplicate"),
        )


def test_exact_work_fence_envelope_and_cancellation_are_fail_closed() -> None:
    controller = _controller()
    lease = _attach(controller, "shard-0", "query-0")
    fence = build_work_fence(lease)
    assert verify_work_fence(fence) == fence
    envelope = build_work_envelope({"input_hash": _hash("input")}, lease)
    assert verify_work_envelope(envelope) == envelope
    assert controller.classify_work_message(fence) == "current"

    stale = copy.deepcopy(fence)
    stale["work_id"] = "another-query"
    stale.pop("fence_sha256")
    stale["fence_sha256"] = content_digest(stale)
    assert controller.classify_work_message(stale) == "stale"

    tampered = copy.deepcopy(envelope)
    tampered["payload"]["input_hash"] = _hash("other")
    with pytest.raises(UlfmRecoveryError, match="identity"):
        verify_work_envelope(tampered)

    permission = controller.shard_permission("shard-0")
    cancellation = controller.cancel_work(
        "shard-0",
        permission["owner_endpoint"],
        permission["generation"],
        permission["generation_token"],
        permission["shard_token"],
        lease["lease_token"],
        "local-result-rejected",
    )
    assert cancellation["work_id"] == "query-0"
    assert controller.classify_work_message(fence) == "stale"
    with pytest.raises(UlfmRecoveryError, match="stale"):
        controller.cancel_work(
            "shard-0",
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            lease["lease_token"],
            "duplicate-cancel",
        )


def test_query_store_persists_prepare_commit_and_rejects_recovery_forks(
    tmp_path: Path,
) -> None:
    store = QueryStore(tmp_path / "query-store")
    controller = _controller()
    durable = DurableUlfmCoordinator(controller, store)
    initial = store.load_ulfm_recovery_snapshot(
        controller.run_id, controller.policy
    )
    assert initial == controller.snapshot()

    lease = durable.dispatch("shard-3", "replay-me")
    assert durable.classify_result(build_work_fence(lease)) == "current"
    fork_controller = UlfmRecoveryController.from_snapshot(controller.snapshot())
    fork_fence = build_work_fence(lease)
    fork_controller.cancel_work(
        fork_fence["shard_id"],
        fork_fence["endpoint_id"],
        fork_fence["generation"],
        fork_fence["generation_token"],
        fork_fence["shard_token"],
        fork_fence["lease_token"],
        "unsequenced-fork",
    )
    with pytest.raises(ValueError, match="explicit state ordinal"):
        store.commit_ulfm_recovery_snapshot(
            controller.run_id, controller.policy, fork_controller.snapshot()
        )

    plan = durable.prepare_recovery(["worker-3"])
    prepared = store.load_ulfm_recovery_snapshot(
        controller.run_id, controller.policy
    )
    assert prepared is not None
    assert prepared["pending_recovery"]["plan_sha256"] == plan["plan_sha256"]
    assert store.commit_ulfm_recovery_snapshot(
        controller.run_id, controller.policy, prepared
    ) == "idempotent"

    restored = DurableUlfmCoordinator.restore(
        controller.run_id, controller.policy, store
    )
    receipt = restored.commit_recovery(
        plan["plan_sha256"],
        _attestations(restored.controller, plan, [0, 1, 2]),
    )
    committed = store.load_ulfm_recovery_snapshot(
        controller.run_id, controller.policy
    )
    assert committed is not None
    assert committed["generation"] == 1
    assert committed["recovery_count"] == 1
    assert receipt["post_state_sha256"] == committed["snapshot_sha256"]
    assert DurableUlfmCoordinator.restore(
        controller.run_id, controller.policy, store
    ).controller.snapshot() == committed

    fork = copy.deepcopy(committed)
    fork["shards"]["shard-0"]["completed"] += 1
    fork.pop("snapshot_sha256")
    fork["snapshot_sha256"] = content_digest(fork)
    committed_state = store.load_ulfm_recovery_state(
        controller.run_id, controller.policy
    )
    assert committed_state is not None
    with pytest.raises(ValueError, match="forked at one state ordinal"):
        store.commit_ulfm_recovery_snapshot(
            controller.run_id,
            controller.policy,
            fork,
            state_ordinal=committed_state[1],
        )


def test_durable_recovery_reconciles_or_rolls_back_failed_persistence(
    tmp_path: Path,
) -> None:
    class FaultStore:
        def __init__(self, delegate: QueryStore) -> None:
            self.delegate = delegate
            self.mode = "pass"

        def load_ulfm_recovery_snapshot(self, *args: Any, **kwargs: Any) -> Any:
            return self.delegate.load_ulfm_recovery_snapshot(*args, **kwargs)

        def load_ulfm_recovery_state(self, *args: Any, **kwargs: Any) -> Any:
            return self.delegate.load_ulfm_recovery_state(*args, **kwargs)

        def commit_ulfm_recovery_snapshot(
            self, *args: Any, **kwargs: Any
        ) -> str:
            if self.mode == "before":
                raise OSError("injected pre-commit failure")
            result = self.delegate.commit_ulfm_recovery_snapshot(*args, **kwargs)
            if self.mode == "after":
                raise OSError("injected ambiguous post-commit failure")
            return result

    store = FaultStore(QueryStore(tmp_path / "query-store"))
    durable = DurableUlfmCoordinator(_controller(), store)
    initial = durable.controller.snapshot()

    store.mode = "before"
    with pytest.raises(OSError, match="pre-commit"):
        durable.prepare_recovery(["worker-3"])
    assert durable.controller.snapshot() == initial

    store.mode = "after"
    plan = durable.prepare_recovery(["worker-3"])
    assert durable.controller.pending
    assert (
        store.load_ulfm_recovery_snapshot(
            durable.controller.run_id, durable.controller.policy
        )["pending_recovery"]["plan_sha256"]
        == plan["plan_sha256"]
    )


def test_atomic_snapshot_store_restores_and_rejects_same_ordinal_forks(
    tmp_path: Path,
) -> None:
    store = AtomicUlfmSnapshotStore(tmp_path / "ulfm-atomic")
    controller = _controller()
    durable = DurableUlfmCoordinator(controller, store)
    plan = durable.prepare_recovery(["worker-3"])

    replica = AtomicUlfmSnapshotStore(tmp_path / "ulfm-atomic")
    restored = DurableUlfmCoordinator.restore(
        controller.run_id, controller.policy, replica
    )
    receipt = restored.commit_recovery(
        plan["plan_sha256"],
        _attestations(restored.controller, plan, [0, 1, 2]),
    )
    state = replica.load_ulfm_recovery_state(
        controller.run_id, controller.policy
    )
    assert state is not None
    snapshot, ordinal = state
    assert snapshot["generation"] == 1
    assert receipt["post_state_sha256"] == snapshot["snapshot_sha256"]

    fork = copy.deepcopy(snapshot)
    fork["shards"]["shard-0"]["completed"] += 1
    fork.pop("snapshot_sha256")
    fork["snapshot_sha256"] = content_digest(fork)
    with pytest.raises(ValueError, match="forked at one state ordinal"):
        replica.commit_ulfm_recovery_snapshot(
            controller.run_id,
            controller.policy,
            fork,
            state_ordinal=ordinal,
        )


def test_atomic_snapshot_store_rejects_corruption(tmp_path: Path) -> None:
    root = tmp_path / "ulfm-atomic"
    store = AtomicUlfmSnapshotStore(root)
    controller = _controller()
    DurableUlfmCoordinator(controller, store)
    path = root / "recovery-state.json"
    payload = bytearray(path.read_bytes())
    payload[len(payload) // 2] ^= 1
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        store.load_ulfm_recovery_state(controller.run_id, controller.policy)


def test_atomic_snapshot_store_file_mode_is_explicit_and_strict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ulfm-shared-mode"
    store = AtomicUlfmSnapshotStore(root, file_mode=0o666)
    controller = _controller()
    DurableUlfmCoordinator(controller, store)
    assert (root / "recovery-state.json").stat().st_mode & 0o777 == 0o666
    assert (root / "recovery-state.lock").stat().st_mode & 0o777 == 0o666

    for invalid in (True, 0o400, 0o700, 0o1666):
        with pytest.raises(ValueError, match="file mode"):
            AtomicUlfmSnapshotStore(
                tmp_path / f"invalid-{invalid}", file_mode=invalid
            )


def test_recovery_requeues_all_inflight_work_and_conserves_shards() -> None:
    controller = _controller()
    old_permissions = {
        shard: controller.shard_permission(shard) for shard in ("shard-0", "shard-3")
    }
    _attach(controller, "shard-0", "survivor-work")
    _attach(controller, "shard-3", "failed-work")
    plan = _plan(controller, ["worker-3"])
    receipt = controller.commit_recovery(
        plan["plan_sha256"], _attestations(controller, plan, [0, 1, 2])
    )

    assert controller.generation == 1
    assert receipt["failed_endpoints"] == ["worker-3"]
    assert verify_recovery_receipt(
        receipt, post_snapshot=controller.snapshot()
    ) == receipt
    assert receipt["conservation"] == {
        "old_endpoints": 4,
        "survivors": 3,
        "failed": 1,
        "shards_before": 8,
        "shards_after": 8,
        "in_flight_before": 2,
        "recovery_queue_before": 0,
        "recovery_queue_after": 2,
    }
    assert {item["work_id"] for item in receipt["requeued_work"]} == {
        "survivor-work",
        "failed-work",
    }
    assert set(receipt["reassignments"]) == {"shard-3", "shard-7"}
    assert all(
        state["owner_endpoint"] != "worker-3"
        for state in controller.snapshot()["shards"].values()
    )

    stale = {
        "run_id": controller.run_id,
        "generation": old_permissions["shard-0"]["generation"],
        "generation_token": old_permissions["shard-0"]["generation_token"],
        "endpoint_id": old_permissions["shard-0"]["owner_endpoint"],
        "shard_id": "shard-0",
        "shard_token": old_permissions["shard-0"]["shard_token"],
    }
    assert controller.classify_message(stale) == "stale"
    current = controller.shard_permission("shard-0")
    assert controller.classify_message(
        {
            "run_id": controller.run_id,
            "generation": current["generation"],
            "generation_token": current["generation_token"],
            "endpoint_id": current["owner_endpoint"],
            "shard_id": "shard-0",
            "shard_token": current["shard_token"],
        }
    ) == "current"


def test_recovery_accounts_for_additional_failure_detected_during_shrink() -> None:
    controller = _controller()
    _attach(controller, "shard-2", "late-failure-work")
    plan = _plan(controller, ["worker-3"])
    receipt = controller.commit_recovery(
        plan["plan_sha256"], _attestations(controller, plan, [0, 1])
    )
    assert receipt["failed_endpoints"] == ["worker-2", "worker-3"]
    assert receipt["conservation"]["old_endpoints"] == (
        receipt["conservation"]["survivors"]
        + receipt["conservation"]["failed"]
    )
    assert set(controller.snapshot()["members"]) == {"worker-0", "worker-1"}


def test_recovery_rejects_incomplete_duplicate_and_reappearing_membership() -> None:
    controller = _controller()
    plan = _plan(controller, ["worker-3"])
    valid = _attestations(controller, plan, [0, 1, 2])
    duplicate = [valid[0], valid[0], valid[2]]
    with pytest.raises(UlfmRecoveryError, match="duplicate"):
        controller.commit_recovery(plan["plan_sha256"], duplicate)

    reappearing = _attestations(controller, plan, [0, 1, 2, 3])
    with pytest.raises(UlfmRecoveryError, match="reappeared"):
        controller.commit_recovery(plan["plan_sha256"], reappearing)

    wrong_rank = copy.deepcopy(valid)
    wrong_rank[1]["new_rank"] = 0
    wrong_rank[1].pop("attestation_sha256")
    wrong_rank[1]["attestation_sha256"] = content_digest(wrong_rank[1])
    with pytest.raises(UlfmRecoveryError, match="duplicate repaired"):
        controller.commit_recovery(plan["plan_sha256"], wrong_rank)


def test_pending_and_committed_recovery_survive_strict_snapshot_replay() -> None:
    controller = _controller()
    _attach(controller, "shard-3", "replay-me")
    plan = _plan(controller, ["worker-3"])
    pending_snapshot = verify_recovery_snapshot(controller.snapshot())
    restored = UlfmRecoveryController.from_snapshot(pending_snapshot)
    receipt = restored.commit_recovery(
        plan["plan_sha256"], _attestations(controller, plan, [0, 1, 2])
    )
    committed = verify_recovery_snapshot(restored.snapshot())
    assert receipt["post_state_sha256"] == committed["snapshot_sha256"]

    replayed = UlfmRecoveryController.from_snapshot(committed)
    queued_permission = replayed.shard_permission("shard-3")
    with pytest.raises(UlfmRecoveryError, match="priority recovery"):
        replayed.attach_work(
            "shard-3",
            queued_permission["owner_endpoint"],
            queued_permission["generation"],
            queued_permission["generation_token"],
            queued_permission["shard_token"],
            "new-work-must-wait",
        )
    claim = replayed.claim_recovery("shard-3")
    assert claim["replayed_from"]["work_id"] == "replay-me"
    assert claim["new_lease"]["lease_token"] != plan["in_flight"][0]["lease_token"]
    assert verify_recovery_snapshot(replayed.snapshot())["recovery_queue"] == []

    tampered = copy.deepcopy(committed)
    tampered["shards"]["shard-0"]["owner_endpoint"] = "worker-3"
    with pytest.raises(UlfmRecoveryError, match="identity"):
        verify_recovery_snapshot(tampered)

    tampered_receipt = copy.deepcopy(receipt)
    tampered_receipt["conservation"]["shards_after"] -= 1
    tampered_receipt.pop("receipt_sha256")
    tampered_receipt["receipt_sha256"] = content_digest(tampered_receipt)
    with pytest.raises(UlfmRecoveryError, match="conservation"):
        verify_recovery_receipt(tampered_receipt, post_snapshot=committed)


class _ImmediateRequest:
    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        self.freed = False

    def Test(self) -> Any:
        return True if self.payload is None else (True, self.payload)

    def Free(self) -> None:
        self.freed = True


class _FakeMPI:
    ERRORS_RETURN = object()
    ERR_PROC_FAILED = 75
    ERR_PROC_FAILED_PENDING = 76
    ERR_REVOKED = 77
    UNDEFINED = -32766
    UNSIGNED_LONG_LONG = object()
    BYTE = object()


class _FakeComm:
    def __init__(
        self,
        *,
        rank: int,
        size: int,
        gathered: list[dict[str, Any]] | None = None,
        shrink_to: "_FakeComm | None" = None,
    ) -> None:
        self.rank = rank
        self.size = size
        self.gathered = gathered
        self.shrink_to = shrink_to
        self.revoked = 0
        self.agreements = 0
        self.freed = False

    def Set_errhandler(self, _handler: Any) -> None:
        return None

    def Revoke(self) -> None:
        self.revoked += 1

    def Shrink(self) -> "_FakeComm":
        if self.shrink_to is None:
            raise RuntimeError("no repaired communicator")
        return self.shrink_to

    def Ishrink(self) -> tuple["_FakeComm", _ImmediateRequest]:
        return self.Shrink(), _ImmediateRequest()

    def Get_rank(self) -> int:
        return self.rank

    def Get_size(self) -> int:
        return self.size

    def Iallgather(self, send: list[Any], receive: list[Any]) -> _ImmediateRequest:
        send_buffer = send[0]
        receive_buffer = receive[0]
        remotes = [] if self.gathered is None else self.gathered
        if isinstance(send_buffer, bytearray):
            receive_buffer[: len(send_buffer)] = send_buffer
            for rank, attestation in enumerate(remotes, 1):
                encoded = json.dumps(
                    attestation,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                offset = rank * len(send_buffer)
                receive_buffer[offset : offset + len(encoded)] = encoded
        else:
            receive_buffer[0] = send_buffer[0]
            for rank, attestation in enumerate(remotes, 1):
                receive_buffer[rank] = len(
                    json.dumps(
                        attestation,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                )
        return _ImmediateRequest()

    def Iagree(self, flag: Any) -> _ImmediateRequest:
        self.agreements += 1
        return _ImmediateRequest()

    def Free(self) -> None:
        self.freed = True


class _FakeGroup:
    def __init__(self, ranks: list[int]) -> None:
        self.ranks = ranks
        self.freed = False

    def Get_size(self) -> int:
        return len(self.ranks)

    def Translate_ranks(
        self, ranks: list[int], _other: "_FakeGroup"
    ) -> list[int]:
        return [self.ranks[index] for index in ranks]

    def Free(self) -> None:
        self.freed = True


class _FailureObservationComm:
    def __init__(self, size: int, failed: list[int]) -> None:
        self.size = size
        self.failed = _FakeGroup(failed)
        self.group = _FakeGroup(list(range(size)))

    def Get_size(self) -> int:
        return self.size

    def Get_failed(self) -> _FakeGroup:
        return self.failed

    def Get_group(self) -> _FakeGroup:
        return self.group


def test_live_adapter_executes_revoke_shrink_attest_and_agree() -> None:
    policy = UlfmRecoveryPolicy(
        max_endpoints=8,
        max_shards=16,
        max_recovery_queue=16,
        collective_timeout_seconds=1.0,
    )
    controller = _controller(policy=policy)
    plan = _plan(controller, ["worker-3"])
    remotes = _attestations(controller, plan, [0, 1, 2])[1:]
    repaired = _FakeComm(rank=0, size=3, gathered=remotes)
    old = _FakeComm(rank=0, size=4, shrink_to=repaired)
    result = shrink_and_attest(
        old,
        local_endpoint=_endpoint(0),
        recovery_plan=plan,
        policy=policy,
        mpi=_FakeMPI,
    )
    assert result.communicator is repaired
    assert result.failed_endpoints == ("worker-3",)
    assert result.attempts == 1
    assert old.revoked == 1
    assert repaired.agreements == 1


def test_deadline_shrink_times_out_pollable_request() -> None:
    class PendingRequest:
        freed = False

        def Test(self) -> bool:
            return False

        def Free(self) -> None:
            self.freed = True

    request = PendingRequest()
    candidate = _FakeComm(rank=0, size=1)

    class PendingComm:
        def Ishrink(self):
            return candidate, request

    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    policy = UlfmRecoveryPolicy(
        max_endpoints=2,
        max_shards=1,
        max_recovery_queue=1,
        collective_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(UlfmCollectiveTimeout, match="collective deadline"):
        deadline_shrink(
            PendingComm(),
            policy=policy,
            monotonic=monotonic,
            sleep=sleep,
        )
    assert not request.freed

    controller = _controller(
        policy=UlfmRecoveryPolicy(
            max_endpoints=8,
            max_shards=16,
            max_recovery_queue=16,
            max_repair_attempts=3,
            collective_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )
    )

    class PendingRepairComm(PendingComm):
        calls = 0

        def Set_errhandler(self, _handler: Any) -> None:
            return None

        def Revoke(self) -> None:
            return None

        def Ishrink(self):
            self.calls += 1
            return candidate, request

    repair_comm = PendingRepairComm()
    clock[0] = 0.0
    with pytest.raises(UlfmCollectiveTimeout, match="collective deadline"):
        shrink_and_attest(
            repair_comm,
            local_endpoint=_endpoint(0),
            recovery_plan=_plan(controller, ["worker-3"]),
            policy=controller.policy,
            mpi=_FakeMPI,
            monotonic=monotonic,
            sleep=sleep,
        )
    assert repair_comm.calls == 1
    assert not request.freed


def test_deadline_shrink_rejects_blocking_only_runtime() -> None:
    class BlockingOnlyComm:
        def Shrink(self):
            raise AssertionError("blocking Shrink must not be called")

    with pytest.raises(UlfmRuntimeError, match="Ishrink"):
        deadline_shrink(BlockingOnlyComm(), policy=UlfmRecoveryPolicy())


def test_failed_rank_observation_maps_to_stable_endpoint_identity() -> None:
    members = _controller().snapshot()["members"]
    comm = _FailureObservationComm(4, [3, 1])
    assert observe_failed_endpoints(comm, members, mpi=_FakeMPI) == (
        "worker-1",
        "worker-3",
    )
    assert comm.failed.freed and comm.group.freed


def test_live_adapter_fails_closed_on_malformed_membership() -> None:
    policy = UlfmRecoveryPolicy(
        max_endpoints=8,
        max_shards=16,
        max_recovery_queue=16,
        max_repair_attempts=1,
        collective_timeout_seconds=1.0,
    )
    controller = _controller(policy=policy)
    plan = _plan(controller, ["worker-3"])
    remotes = _attestations(controller, plan, [0, 1, 2])[1:]
    remotes[0] = copy.deepcopy(remotes[0])
    remotes[0]["new_rank"] = 0
    remotes[0].pop("attestation_sha256")
    remotes[0]["attestation_sha256"] = content_digest(remotes[0])
    repaired = _FakeComm(rank=0, size=3, gathered=remotes)
    old = _FakeComm(rank=0, size=4, shrink_to=repaired)
    with pytest.raises(UlfmRuntimeError, match="duplicate repaired"):
        shrink_and_attest(
            old,
            local_endpoint=_endpoint(0),
            recovery_plan=plan,
            policy=policy,
            mpi=_FakeMPI,
        )
    assert repaired.freed


def test_real_runtime_capability_probe_is_semantically_self_consistent() -> None:
    result = probe_ulfm_runtime(MPI.COMM_SELF)
    assert result["schema"] == ULFM_CAPABILITY_SCHEMA
    supplied = result["capability_sha256"]
    assert supplied == content_digest(
        {key: value for key, value in result.items() if key != "capability_sha256"}
    )
    if result["available"]:
        assert all(result["semantic_checks"].values())
        assert result["error"] == ""
    else:
        assert result["error"]

    class Failure(Exception):
        def Get_error_class(self) -> int:
            return _FakeMPI.ERR_PROC_FAILED

    assert is_ulfm_failure(Failure(), _FakeMPI)
    assert not is_ulfm_failure(ValueError("not MPI"), _FakeMPI)


def test_deterministic_recovery_oracle_replays_and_rejects_tampering() -> None:
    result = build_deterministic_oracle(endpoints=8, shards=32)
    assert verify_deterministic_oracle(result) == result
    assert result["summary"] == {
        "endpoints_after": 6,
        "endpoints_before": 8,
        "failed_endpoints": 2,
        "final_recovery_queue": 0,
        "leases_attached_before_failure": 11,
        "leases_completed_before_failure": 3,
        "leases_replayed": 8,
        "leases_requeued": 8,
        "old_lease_tokens_rejected": 8,
        "shards": 32,
        "shards_reassigned": 8,
    }
    tampered = copy.deepcopy(result)
    tampered["summary"]["leases_replayed"] -= 1
    tampered.pop("result_sha256")
    tampered["result_sha256"] = content_digest(tampered)
    with pytest.raises(UlfmRecoveryError, match="replay changed"):
        verify_deterministic_oracle(tampered)


def test_f443_hot_path_oracle_is_content_addressed_and_fail_closed() -> None:
    body = {
        "schema": F443_SCHEMA,
        "expected_failed_endpoint": "rank-1",
        "generation": 1,
        "recovery_count": 1,
        "state_ordinal": 8,
        "survivor_endpoints": ["rank-0", "rank-2"],
        "requeued_at_repair": 1,
        "final_recovery_queue": 0,
        "final_active_work": 0,
        "completed_work": 3,
        "public_objects": 16,
        "snapshot_sha256": _hash("snapshot"),
        "receipt_sha256": _hash("receipt"),
        "database_sha256": _hash("database"),
        "log_sha256": _hash("log"),
        "claim_boundary": "same-host mechanism evidence",
    }
    body["result_sha256"] = content_digest(body)
    assert verify_campaign_oracle(body) == body

    tampered = copy.deepcopy(body)
    tampered["final_active_work"] = 1
    tampered["result_sha256"] = content_digest(
        {key: value for key, value in tampered.items() if key != "result_sha256"}
    )
    with pytest.raises(UlfmRecoveryError, match="invariants"):
        verify_campaign_oracle(tampered)


def test_f446_elastic_oracle_is_content_addressed_and_fail_closed() -> None:
    body = {
        "schema": F446_SCHEMA,
        "failed_endpoints": ["rank-2", "rank-3"],
        "warm_spares": 2,
        "final_generation": 2,
        "recovery_count": 2,
        "surviving_hosts": 2,
        "promoted_by_generation": [[], [8], [8, 9]],
        "final_active_endpoints": [0, 1, 4, 5, 6, 7, 8, 9],
        "final_standby_endpoints": [],
        "final_group_controllers": 2,
        "final_group_completed": 49,
        "wal_analyzed": 51,
        "wal_generated": 255,
        "wal_leased_at_deadline": 206,
        "public_objects": 257,
        "global_snapshot_sha256": _hash("global-snapshot"),
        "global_database_sha256": _hash("global-database"),
        "log_sha256": _hash("campaign-log"),
        "claim_boundary": "physical process-failure mechanism evidence",
    }
    body["result_sha256"] = content_digest(body)
    assert verify_f446_campaign_oracle(body) == body

    tampered = copy.deepcopy(body)
    tampered["recovery_count"] = 1
    tampered["result_sha256"] = content_digest(
        {key: value for key, value in tampered.items() if key != "result_sha256"}
    )
    with pytest.raises(UlfmRecoveryError, match="invariants"):
        verify_f446_campaign_oracle(tampered)
