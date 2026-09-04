#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from mpi_ulfm_recovery import (  # noqa: E402
    DurableUlfmCoordinator,
    EndpointIdentity,
    RecoveryShard,
    UlfmRecoveryController,
    UlfmRecoveryError,
    build_endpoint_attestation,
)
from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from qfbv_distributed_partition_execution import (  # noqa: E402
    DISTRIBUTED_PARTITION_PROTOCOL,
    DistributedCubeBindingStore,
    DistributedPartitionCoordinator,
    DistributedPartitionError,
    distributed_run_id,
    verify_distributed_cube_lease,
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
from symcc_qfbv_distributed_partition import main as distributed_cli_main  # noqa: E402


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _plan(*, contradiction: bool = False):
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
    return bitblast_qfbv_query("f451-query", roots, expressions)


def _partition(plan, cubes: int):
    return build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(cube_count=cubes, max_depth=8),
    )


def _base_result(plan, checker, status: str) -> dict:
    return {
        "status": status,
        "assignments": {},
        "solver": "f451-test-backend",
        "elapsed_us": 10,
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


def _unsat_result(plan, proof_store, checker, sequence: int) -> dict:
    record = make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker="f451-leaf",
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


def _value_satisfying_cube(plan, literals) -> int:
    value = 0
    required = {abs(literal): literal > 0 for literal in literals}
    for _offset, input_literals in plan.input_literals:
        for bit, input_literal in enumerate(input_literals):
            variable = abs(input_literal)
            if variable not in required:
                continue
            semantic = required[variable]
            if input_literal < 0:
                semantic = not semantic
            if semantic:
                value |= 1 << bit
    return value


def _sat_result(plan, checker, literals) -> dict:
    result = _base_result(plan, checker, "sat")
    result["assignments"] = {0: _value_satisfying_cube(plan, literals)}
    result["backend_model_verified"] = True
    return result


def _endpoint(rank: int) -> EndpointIdentity:
    return EndpointIdentity(
        endpoint_id=f"worker-{rank}",
        initial_rank=rank,
        incarnation_sha256=_hash(f"incarnation-{rank}"),
        host_id=f"node-{rank % 2}",
    )


def _query_envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f451-cli-test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "00"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [3],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {"source": "f451-cli", "output_dir": "", "site": 451},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x00))\n"
        ),
        "prefix_smt2": "(declare-fun |0| () (_ BitVec 8))\n(assert true)\n",
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x00))\n"
        ),
    }


def _runtime(
    tmp_path: Path,
    *,
    cubes: int = 4,
    endpoints: int = 3,
    contradiction: bool = False,
):
    plan = _plan(contradiction=contradiction)
    certificate = _partition(plan, cubes)
    policy = PartitionExecutionPolicy(
        parallelism=min(cubes, endpoints),
        max_attempts=3,
        cube_timeout_ms=100,
        task_lease_ms=10_000,
    )
    partition_store = PartitionExecutionStore(tmp_path / "partition-execution")
    proof_store = IncrementalProofStore(tmp_path / "proofs")
    checker = IncrementalProofChecker(proof_store)
    execution = partition_store.create(
        plan, certificate, policy, checker=checker, now=100.0
    )
    identities = [_endpoint(rank) for rank in range(endpoints)]
    controller = UlfmRecoveryController(
        distributed_run_id(execution),
        identities,
        [
            RecoveryShard(
                shard_id=f"cube-slot-{index}",
                owner_endpoint=f"worker-{index % endpoints}",
                checkpoint_sha256=_hash(f"checkpoint-{index}"),
            )
            for index in range(endpoints)
        ],
    )
    query_store = QueryStore(tmp_path / "query-store")
    durable = DurableUlfmCoordinator(controller, query_store)
    bindings = DistributedCubeBindingStore(tmp_path / "bindings")
    coordinator = DistributedPartitionCoordinator(
        plan,
        certificate,
        policy,
        partition_store,
        bindings,
        durable,
        proof_store,
        checker,
        input_hex="00",
    )
    return {
        "plan": plan,
        "certificate": certificate,
        "policy": policy,
        "partition_store": partition_store,
        "proof_store": proof_store,
        "checker": checker,
        "execution": execution,
        "identities": identities,
        "query_store": query_store,
        "bindings": bindings,
        "coordinator": coordinator,
    }


def _attest_survivors(runtime, survivor_ranks: list[int]) -> list[dict]:
    controller = runtime["coordinator"].durable.controller
    snapshot = controller.snapshot()
    return [
        build_endpoint_attestation(
            run_id=controller.run_id,
            base_generation=snapshot["generation"],
            base_generation_token=snapshot["generation_token"],
            endpoint=runtime["identities"][old_rank],
            old_rank=snapshot["members"][f"worker-{old_rank}"]["rank"],
            new_rank=new_rank,
        )
        for new_rank, old_rank in enumerate(survivor_ranks)
    ]


def test_distributed_lease_is_exact_canonical_and_endpoint_bound(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    lease = runtime["coordinator"].claim("worker-0", now=101.0)
    assert lease is not None
    normalized, cube, fence = verify_distributed_cube_lease(lease)
    assert normalized == lease
    assert normalized["protocol"] == DISTRIBUTED_PARTITION_PROTOCOL
    assert fence["endpoint_id"] == "worker-0"
    assert cube.owner.startswith("dist-cube:0:")

    for field in ("generation", "work_id", "lease_token"):
        tampered = copy.deepcopy(lease)
        if field == "generation":
            tampered["ulfm_fence"][field] += 1
        else:
            tampered["ulfm_fence"][field] = "changed"
        with pytest.raises(DistributedPartitionError):
            verify_distributed_cube_lease(tampered)


def test_parallel_endpoint_claims_are_unique_and_persisted(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, cubes=6, endpoints=3)
    coordinator = runtime["coordinator"]
    leases: list[dict] = []

    def claim(rank: int) -> None:
        lease = coordinator.claim(f"worker-{rank}", now=101.0)
        assert lease is not None
        leases.append(lease)

    threads = [threading.Thread(target=claim, args=(rank,)) for rank in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({lease["cube"]["ordinal"] for lease in leases}) == 3
    assert {lease["ulfm_fence"]["endpoint_id"] for lease in leases} == {
        "worker-0",
        "worker-1",
        "worker-2",
    }
    assert runtime["bindings"].stats()["states"] == {"active": 3}
    assert all(coordinator.heartbeat(lease, now=102.0) for lease in leases)


def test_generation_recovery_refences_every_inflight_cube_without_retry_cost(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=4, endpoints=3)
    coordinator = runtime["coordinator"]
    old = [
        coordinator.claim("worker-0", now=101.0),
        coordinator.claim("worker-1", now=101.0),
    ]
    assert all(lease is not None for lease in old)
    result = coordinator.recover(
        ["worker-0"], _attest_survivors(runtime, [1, 2]), now=102.0
    )
    recovered = result["recovered_leases"]
    assert len(recovered) == 2
    assert result["recovery_receipt"]["target_generation"] == 1
    assert runtime["partition_store"].stats()["attempts"] == 2
    assert all(lease["ulfm_fence"]["generation"] == 1 for lease in recovered)
    old_by_ordinal = {lease["cube"]["ordinal"]: lease for lease in old}
    for lease in recovered:
        prior = old_by_ordinal[lease["cube"]["ordinal"]]
        assert lease["cube"]["token"] == prior["cube"]["token"] + 1
        assert not coordinator.heartbeat(prior, now=103.0)
        assert coordinator.heartbeat(lease, now=103.0)


def test_recovered_unsat_leaves_aggregate_without_loss(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path, cubes=2, endpoints=2, contradiction=True
    )
    coordinator = runtime["coordinator"]
    assert coordinator.claim("worker-0", now=101.0) is not None
    assert coordinator.claim("worker-1", now=101.0) is not None
    recovery = coordinator.recover(
        ["worker-0"], _attest_survivors(runtime, [1]), now=102.0
    )
    assert len(recovery["recovered_leases"]) == 2
    for sequence, lease in enumerate(recovery["recovered_leases"]):
        derived = extend_bitblast_assumptions(
            runtime["plan"], lease["cube"]["literals"]
        )
        assert coordinator.complete(
            lease,
            _unsat_result(
                derived,
                runtime["proof_store"],
                runtime["checker"],
                sequence,
            ),
            now=103.0 + sequence,
        )
    result = coordinator.finalize_if_ready()
    assert result is not None
    assert result["status"] == "unsat"
    assert result["backend_partition_completed_cubes"] == 2
    runtime["checker"].verify_result_receipt(
        runtime["plan"], result["backend_incremental_result_receipt"]
    )
    assert coordinator.stats()["ulfm_active_work"] == 0


def test_sat_winner_cancels_every_remote_peer_and_old_completion(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, cubes=4, endpoints=3)
    coordinator = runtime["coordinator"]
    winner = coordinator.claim("worker-0", now=101.0)
    peers = [
        coordinator.claim("worker-1", now=101.0),
        coordinator.claim("worker-2", now=101.0),
    ]
    assert winner is not None and all(peer is not None for peer in peers)
    derived = extend_bitblast_assumptions(
        runtime["plan"], winner["cube"]["literals"]
    )
    assert coordinator.complete(
        winner,
        _sat_result(derived, runtime["checker"], winner["cube"]["literals"]),
        now=102.0,
    )
    assert runtime["bindings"].stats()["states"] == {
        "cancelled": 2,
        "completed": 1,
    }
    assert coordinator.stats()["ulfm_active_work"] == 0
    assert not coordinator.heartbeat(peers[0], now=103.0)
    assert not coordinator.complete(
        peers[0], {"status": "unknown", "assignments": {}}, now=103.0
    )
    result = coordinator.finalize_if_ready()
    assert result is not None and result["status"] == "sat"
    assert result["backend_partition_completed_cubes"] == 1


def test_reconcile_closes_inner_commit_outer_finish_crash_window(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path, cubes=2, endpoints=2, contradiction=True
    )
    coordinator = runtime["coordinator"]
    lease = coordinator.claim("worker-0", now=101.0)
    assert lease is not None
    _, cube, _ = verify_distributed_cube_lease(lease)
    derived = extend_bitblast_assumptions(
        runtime["plan"], lease["cube"]["literals"]
    )
    assert runtime["partition_store"].complete(
        runtime["plan"],
        runtime["certificate"],
        cube,
        _unsat_result(
            derived, runtime["proof_store"], runtime["checker"], 0
        ),
        checker=runtime["checker"],
        now=102.0,
    )
    assert coordinator.stats()["ulfm_active_work"] == 1
    assert coordinator.reconcile(now=103.0) == {
        "kept": 0,
        "completed": 1,
        "cancelled": 0,
        "stale": 0,
        "reconstructed": 0,
    }
    assert coordinator.stats()["ulfm_active_work"] == 0
    assert runtime["bindings"].stats()["states"] == {"completed": 1}


def test_binding_and_durable_state_reopen_without_losing_current_lease(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=2, endpoints=2)
    lease = runtime["coordinator"].claim("worker-0", now=101.0)
    assert lease is not None
    restored = DurableUlfmCoordinator.restore(
        runtime["coordinator"].durable.controller.run_id,
        runtime["coordinator"].durable.controller.policy,
        runtime["query_store"],
    )
    coordinator = DistributedPartitionCoordinator(
        runtime["plan"],
        runtime["certificate"],
        runtime["policy"],
        PartitionExecutionStore(tmp_path / "partition-execution"),
        DistributedCubeBindingStore(tmp_path / "bindings"),
        restored,
        runtime["proof_store"],
        runtime["checker"],
        input_hex="00",
    )
    assert coordinator.reconcile(now=102.0)["kept"] == 1
    assert coordinator.heartbeat(lease, now=102.0)


def test_reconcile_reconstructs_dispatch_binding_crash_window(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=2, endpoints=2)
    coordinator = runtime["coordinator"]
    permission = coordinator.durable.controller.shard_permission("cube-slot-0")
    cube = runtime["partition_store"].claim(
        runtime["execution"],
        coordinator._owner_for_permission(permission),
        now=101.0,
    )
    assert cube is not None
    coordinator.durable.dispatch(
        "cube-slot-0", f"cube:{runtime['execution']}:{cube.ordinal}"
    )
    assert runtime["bindings"].stats()["bindings"] == 0
    reconciled = coordinator.reconcile(now=102.0)
    assert reconciled == {
        "kept": 1,
        "completed": 0,
        "cancelled": 0,
        "stale": 0,
        "reconstructed": 1,
    }
    active = runtime["bindings"].active(
        coordinator.durable.controller.run_id
    )
    assert len(active) == 1
    assert coordinator.heartbeat(active[0], now=103.0)


def test_committed_recovery_queue_resumes_without_advancing_again(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=4, endpoints=3)
    coordinator = runtime["coordinator"]
    old = [
        coordinator.claim("worker-0", now=101.0),
        coordinator.claim("worker-1", now=101.0),
    ]
    assert all(lease is not None for lease in old)
    attestations = _attest_survivors(runtime, [1, 2])
    plan = coordinator.durable.prepare_recovery(["worker-0"])
    coordinator.durable.commit_recovery(plan["plan_sha256"], attestations)
    assert len(coordinator.durable.controller.snapshot()["recovery_queue"]) == 2
    resumed = coordinator.resume_recovery_queue(now=102.0)
    assert len(resumed["recovered_leases"]) == 2
    snapshot = coordinator.durable.controller.snapshot()
    assert snapshot["generation"] == 1
    assert snapshot["recovery_count"] == 1
    assert snapshot["recovery_queue"] == []
    assert all(
        coordinator.heartbeat(lease, now=103.0)
        for lease in resumed["recovered_leases"]
    )


def test_two_stale_masters_cannot_fork_one_durable_dispatch_ordinal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=4, endpoints=3)
    original = runtime["coordinator"]
    restored = DurableUlfmCoordinator.restore(
        original.durable.controller.run_id,
        original.durable.controller.policy,
        runtime["query_store"],
    )
    replica = DistributedPartitionCoordinator(
        runtime["plan"],
        runtime["certificate"],
        runtime["policy"],
        PartitionExecutionStore(tmp_path / "partition-execution"),
        DistributedCubeBindingStore(tmp_path / "bindings"),
        restored,
        runtime["proof_store"],
        runtime["checker"],
        input_hex="00",
    )
    leases: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def claim(coordinator, endpoint: str) -> None:
        barrier.wait(timeout=5)
        try:
            lease = coordinator.claim(endpoint, now=101.0)
            if lease is not None:
                leases.append(lease)
        except Exception as error:
            errors.append(error)

    threads = [
        threading.Thread(target=claim, args=(original, "worker-0")),
        threading.Thread(target=claim, args=(replica, "worker-1")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(leases) == 1
    assert len(errors) == 1
    durable = DurableUlfmCoordinator.restore(
        original.durable.controller.run_id,
        original.durable.controller.policy,
        runtime["query_store"],
    )
    assert sum(
        bool(shard["active_work_id"])
        for shard in durable.controller.snapshot()["shards"].values()
    ) == 1
    assert runtime["bindings"].stats()["states"] == {"active": 1}
    ledger = runtime["partition_store"].stats()
    assert ledger["attempts"] == 2
    assert ledger["task_states"] == {"leased": 1, "pending": 3}


def test_invalid_recovery_attestation_fails_closed_with_pending_plan(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, cubes=2, endpoints=2)
    coordinator = runtime["coordinator"]
    assert coordinator.claim("worker-0", now=101.0) is not None
    attestations = _attest_survivors(runtime, [1])
    attestations[0]["new_rank"] = 1
    with pytest.raises(UlfmRecoveryError):
        coordinator.recover(["worker-0"], attestations, now=102.0)
    snapshot = coordinator.durable.controller.snapshot()
    assert snapshot["generation"] == 0
    assert snapshot["pending_recovery"] is not None
    assert runtime["partition_store"].stats()["task_states"] == {"leased": 1, "pending": 1}


def test_binding_database_tamper_is_rejected_before_heartbeat(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, cubes=2, endpoints=2)
    lease = runtime["coordinator"].claim("worker-0", now=101.0)
    assert lease is not None
    database = runtime["bindings"].database
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE distributed_cube_bindings SET lease_json='{}' "
            "WHERE lease_sha256=?",
            (lease["lease_sha256"],),
        )
    with pytest.raises(DistributedPartitionError):
        runtime["coordinator"].heartbeat(lease, now=102.0)


def test_stateless_cli_rebuilds_and_completes_one_remote_cube(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query_root = tmp_path / "query-store"
    query_store = QueryStore(query_root)
    query_id, _ = query_store.ingest(_query_envelope())
    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(
        json.dumps([_endpoint(0).as_dict(), _endpoint(1).as_dict()]),
        encoding="utf-8",
    )
    common = [
        "--query-store",
        str(query_root),
        "--query-id",
        query_id,
        "--proof-store",
        str(tmp_path / "proofs"),
        "--partition-store",
        str(tmp_path / "partitions"),
        "--execution-store",
        str(tmp_path / "executions"),
        "--binding-store",
        str(tmp_path / "bindings"),
        "--endpoints",
        str(endpoints),
        "--cubes",
        "2",
        "--parallelism",
        "2",
        "--cube-timeout-ms",
        "100",
        "--task-lease-ms",
        "10000",
        "--input-hex",
        "00",
    ]
    assert distributed_cli_main(["init", *common]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["value"]["ulfm_members"] == 2

    assert distributed_cli_main(
        ["claim", *common, "--endpoint", "worker-0"]
    ) == 0
    lease = json.loads(capsys.readouterr().out)["value"]
    lease_path = tmp_path / "lease.json"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    assert distributed_cli_main(
        ["heartbeat", *common, "--lease", str(lease_path)]
    ) == 0
    assert json.loads(capsys.readouterr().out)["value"] is True

    loaded = query_store.load_query_ir(query_id)
    assert loaded is not None
    plan = bitblast_qfbv_query(query_id, loaded[0], loaded[1])
    derived = extend_bitblast_assumptions(plan, lease["cube"]["literals"])
    checker = IncrementalProofChecker(IncrementalProofStore(tmp_path / "proofs"))
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(_sat_result(derived, checker, lease["cube"]["literals"])),
        encoding="utf-8",
    )
    assert distributed_cli_main(
        [
            "complete",
            *common,
            "--lease",
            str(lease_path),
            "--result",
            str(result_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["value"] is True
    assert distributed_cli_main(["finalize", *common]) == 0
    final = json.loads(capsys.readouterr().out)["value"]
    assert final["status"] == "sat"
    assert final["backend_partition_completed_cubes"] == 1


def test_executable_same_host_and_dual_host_fault_oracle() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "benchmark/check_qfbv_distributed_partition_oracles.py"
            ),
            "--rounds",
            "1",
            "--cubes",
            "4",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["summary"] == {
        "lost_cubes": 0,
        "median_recovery_case_us": payload["summary"][
            "median_recovery_case_us"
        ],
        "recovered_cubes": 8,
        "recovery_cases": 2,
        "sat_results": 1,
        "unsat_results": 2,
    }
    assert payload["summary"]["median_recovery_case_us"] > 0
    assert [case["initial_hosts"] for case in payload["cases"]] == [1, 2]


def test_remote_worker_protocol_is_digest_bound_and_strict(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, cubes=2, endpoints=2)
    lease = runtime["coordinator"].claim("worker-0", now=101.0)
    assert lease is not None
    derived = extend_bitblast_assumptions(
        runtime["plan"], lease["cube"]["literals"]
    )
    body = {
        "schema": "symcc-f451-remote-worker-request-v1",
        "lease": lease,
        "result_template": _base_result(
            derived, runtime["checker"], "unknown"
        ),
        "assignment": 0,
    }
    encoded_body = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    body["request_sha256"] = hashlib.sha256(encoded_body).hexdigest()
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    command = [
        sys.executable,
        str(ROOT / "benchmark/qfbv_distributed_remote_worker.py"),
        "--mode",
        "solve",
    ]
    accepted = subprocess.run(
        command,
        input=encoded,
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert accepted.returncode == 0, accepted.stderr.decode()
    response = json.loads(accepted.stdout)
    assert response["lease_sha256"] == lease["lease_sha256"]
    assert response["request_sha256"] == body["request_sha256"]
    assert response["result"]["backend_f451_remote_request_sha256"] == body[
        "request_sha256"
    ]

    tampered = copy.deepcopy(body)
    tampered["assignment"] = 1
    rejected = subprocess.run(
        command,
        input=json.dumps(tampered).encode("ascii"),
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 2
    duplicate = encoded.replace(b'"assignment":0', b'"assignment":0,"assignment":0')
    rejected = subprocess.run(
        command,
        input=duplicate,
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 2
