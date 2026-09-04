#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from cadical_qfbv_backend import PersistentCadicalQfbvSolver  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (  # noqa: E402
    QfbvBitBlastError,
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
    parse_dimacs_assignment,
)
from qfbv_artifact_lifecycle import ArtifactLifecycleRegistry  # noqa: E402
from qfbv_partition_execution import (  # noqa: E402
    PARTITION_EXECUTION_PROTOCOL,
    PartitionExecutionError,
    PartitionExecutionPolicy,
    PartitionExecutionStore,
    ProofAwarePartitionExecutor,
    execution_identity,
    verify_cube_result,
)
from qfbv_proof_prefix_partition import (  # noqa: E402
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
)
from query_store import QueryStore  # noqa: E402
from symcc_query_service import main as query_service_main  # noqa: E402


def _plan(*, contradiction: bool = False, query_id: str = "f449-query"):
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


def _partition(plan, cubes: int = 2):
    return build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(cube_count=cubes, max_depth=4),
    )


def _base_result(plan, checker, *, status: str) -> dict:
    return {
        "status": status,
        "assignments": {},
        "solver": "f449-test-backend",
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


def _unsat_result(plan, proof_store, checker, *, sequence: int = 0):
    record = make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker="f449-leaf",
        worker_epoch=0,
        sequence=sequence,
    )
    authorization = checker.verify_clause_record(plan, record)
    digest, created = proof_store.publish(record)
    receipt = make_unsat_result_receipt(plan, digest, plan.assumptions)
    result_authorization = checker.verify_result_receipt(plan, receipt)
    assert result_authorization.clause_receipt_sha256 == digest
    result = _base_result(plan, checker, status="unsat")
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


def _sat_result(plan, checker, value: int = 0):
    result = _base_result(plan, checker, status="sat")
    result.update(
        {
            "assignments": {0: value},
            "backend_model_verified": True,
        }
    )
    return result


def test_assumption_plan_and_dimacs_assignment_are_strict() -> None:
    plan = _plan()
    variable = abs(plan.input_literals[0][1][0])
    negative = extend_bitblast_assumptions(plan, [-variable])
    assert negative.assumptions == plan.assumptions + (-variable,)
    assert negative.formula_sha256 == plan.formula_sha256
    assert negative.assumption_sha256 != plan.assumption_sha256
    assert negative.certificate["certificate_sha256"] != plan.certificate[
        "certificate_sha256"
    ]
    assert extend_bitblast_assumptions(plan, ()) is plan
    with pytest.raises(QfbvBitBlastError, match="unique"):
        extend_bitblast_assumptions(plan, [variable, -variable])
    with pytest.raises(QfbvBitBlastError, match="integer"):
        extend_bitblast_assumptions(plan, [True])
    status, assignment = parse_dimacs_assignment(
        "s SATISFIABLE\nv 1 -2 3 0\n"
    )
    assert status == "sat"
    assert assignment == {1: True, 2: False, 3: True}
    with pytest.raises(QfbvBitBlastError, match="inconsistently"):
        parse_dimacs_assignment("s SATISFIABLE\nv 1 -1 0\n")
    with pytest.raises(QfbvBitBlastError, match="after"):
        parse_dimacs_assignment("s SATISFIABLE\nv 1 0 2\n")


def test_policy_and_execution_identity_are_sealed() -> None:
    plan = _plan()
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=3,
        max_attempts=2,
        cube_timeout_ms=100,
        task_lease_ms=500,
    )
    first, verified = execution_identity(plan, certificate, policy)
    second, _ = execution_identity(plan, copy.deepcopy(certificate), policy)
    assert first == second
    assert verified["partition_sha256"] == certificate["partition_sha256"]
    assert policy.as_dict()["protocol"] == PARTITION_EXECUTION_PROTOCOL
    with pytest.raises(PartitionExecutionError):
        PartitionExecutionPolicy(parallelism=True)
    with pytest.raises(PartitionExecutionError, match="cover"):
        PartitionExecutionPolicy(cube_timeout_ms=1000, task_lease_ms=999)


def test_claims_are_unique_persistent_and_token_fenced() -> None:
    plan = _plan()
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=4,
        max_attempts=2,
        cube_timeout_ms=100,
        task_lease_ms=500,
    )
    with tempfile.TemporaryDirectory() as directory:
        store = PartitionExecutionStore(directory)
        execution = store.create(plan, certificate, policy, now=100.0)
        leases = []

        def claim(index: int) -> None:
            lease = store.claim(execution, f"worker-{index}", now=100.0)
            assert lease is not None
            leases.append(lease)

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len({lease.ordinal for lease in leases}) == 4
        reopened = PartitionExecutionStore(directory)
        assert reopened.snapshot(execution)["task_counts"] == {"leased": 4}

        stale = leases[0]
        reclaimed = reopened.claim(execution, "replacement", now=101.0)
        assert reclaimed is not None
        assert reclaimed.ordinal == stale.ordinal
        assert reclaimed.token == stale.token + 1
        with pytest.raises(PartitionExecutionError, match="finite"):
            reopened.heartbeat(reclaimed, now=float("nan"))
        with pytest.raises(PartitionExecutionError, match="finite"):
            reopened.complete(
                plan,
                certificate,
                reclaimed,
                {"status": "unknown", "assignments": {}},
                checker=IncrementalProofChecker(
                    IncrementalProofStore(Path(directory) / "p")
                ),
                now=float("inf"),
            )
        assert not reopened.complete(
            plan,
            certificate,
            stale,
            {"status": "unknown", "assignments": {}},
            checker=IncrementalProofChecker(IncrementalProofStore(Path(directory) / "p")),
            now=101.0,
        )


def test_execution_store_rejects_symlinks_metadata_and_cube_drift() -> None:
    plan = _plan()
    certificate = _partition(plan)
    policy = PartitionExecutionPolicy(
        parallelism=1,
        cube_timeout_ms=100,
        task_lease_ms=500,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        real = root / "real"
        real.mkdir()
        linked = root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with pytest.raises(PartitionExecutionError, match="symlink"):
            PartitionExecutionStore(linked)

        database_root = root / "database-link"
        database_root.mkdir()
        target = root / "foreign.sqlite3"
        target.write_bytes(b"")
        (database_root / "partition-execution.sqlite3").symlink_to(target)
        with pytest.raises(PartitionExecutionError, match="symlink"):
            PartitionExecutionStore(database_root)

        store = PartitionExecutionStore(root / "store")
        execution = store.create(plan, certificate, policy)
        with store._connect() as db:
            db.execute(
                "UPDATE partition_cube_tasks SET cube_sha256=? "
                "WHERE execution_sha256=? AND ordinal=0",
                ("0" * 64, execution),
            )
        with pytest.raises(PartitionExecutionError, match="inventory"):
            store.create(plan, certificate, policy)

        metadata = PartitionExecutionStore(root / "metadata")
        with metadata._connect() as db:
            db.execute(
                "UPDATE partition_execution_metadata SET value='foreign' "
                "WHERE key='protocol'"
            )
        with pytest.raises(PartitionExecutionError, match="metadata"):
            PartitionExecutionStore(root / "metadata")

        policy_store = PartitionExecutionStore(root / "policy")
        policy_execution = policy_store.create(plan, certificate, policy)
        with policy_store._connect() as db:
            db.execute(
                "UPDATE partition_executions SET policy_json=? "
                "WHERE execution_sha256=?",
                ('{"parallelism":1,"parallelism":1}', policy_execution),
            )
        with pytest.raises(PartitionExecutionError, match="policy"):
            policy_store.claim(policy_execution, "worker")


def test_query_store_parent_lease_renewal_is_owner_and_token_fenced() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = QueryStore(directory)
        query_id, _created = store.ingest(_query_envelope(unsat=False))
        timestamp = time.time()
        lease = store.claim("owner", lease_seconds=1.0)
        assert lease is not None and lease.query_id == query_id
        assert store.renew(lease, "owner", 2.0, now=timestamp + 0.5)
        assert not store.renew(lease, "other", 2.0, now=timestamp + 0.6)
        assert not store.renew(lease, "owner", 2.0, now=timestamp + 3.0)
        with pytest.raises(ValueError, match="duration"):
            store.renew(lease, "owner", float("nan"))


def test_signed_cube_unsat_receipt_replays() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan)
    negative_cube = certificate["cubes"][0]
    assert negative_cube["literals"][0] < 0
    derived = extend_bitblast_assumptions(plan, negative_cube["literals"])
    with tempfile.TemporaryDirectory() as directory:
        proof_store = IncrementalProofStore(Path(directory) / "proofs")
        checker = IncrementalProofChecker(proof_store)
        result = _unsat_result(derived, proof_store, checker)
        status, _ = verify_cube_result(
            plan, certificate, 0, result, checker=checker
        )
        assert status == "unsat"
        authorization = checker.verify_result_receipt(
            derived, result["backend_incremental_result_receipt"]
        )
        assert min(authorization.failed_assumptions) < 0
        tampered = copy.deepcopy(result)
        tampered["backend_incremental_result_receipt"]["failed_assumptions"] = list(
            plan.assumptions
        )
        tampered["backend_incremental_result_receipt"].pop("receipt_sha256")
        with pytest.raises(PartitionExecutionError, match="receipt"):
            verify_cube_result(plan, certificate, 0, tampered, checker=checker)
        with pytest.raises(IncrementalProofError, match="integer"):
            make_unsat_result_receipt(
                derived,
                result["backend_incremental_proof_record_sha256"],
                [str(derived.assumptions[0])],
            )
        noncanonical_digest = copy.deepcopy(
            result["backend_incremental_result_receipt"]
        )
        noncanonical_digest["clause_receipt_sha256"] = int(
            noncanonical_digest["clause_receipt_sha256"], 16
        )
        noncanonical_digest.pop("receipt_sha256")
        with pytest.raises(IncrementalProofError, match="SHA-256"):
            checker.verify_result_receipt(derived, noncanonical_digest)
        duplicate = copy.deepcopy(result["backend_incremental_result_receipt"])
        duplicate["failed_assumptions"].append(
            duplicate["failed_assumptions"][0]
        )
        with pytest.raises(IncrementalProofError, match="unique"):
            checker.verify_result_receipt(derived, duplicate)


def test_sat_witness_must_match_cube_and_query_replay() -> None:
    plan = _plan()
    certificate = _partition(plan)
    negative_cube = certificate["cubes"][0]
    assert negative_cube["literals"][0] < 0
    derived = extend_bitblast_assumptions(plan, negative_cube["literals"])
    with tempfile.TemporaryDirectory() as directory:
        checker = IncrementalProofChecker(
            IncrementalProofStore(Path(directory) / "proofs")
        )
        status, _ = verify_cube_result(
            plan,
            certificate,
            0,
            _sat_result(derived, checker, 0),
            checker=checker,
            candidate_validator=lambda candidate: candidate == b"\x00",
            input_hex="00",
        )
        assert status == "sat"
        with pytest.raises(PartitionExecutionError, match="cube"):
            verify_cube_result(
                plan,
                certificate,
                0,
                _sat_result(derived, checker, 1),
                checker=checker,
            )
        changed = _sat_result(derived, checker, 0)
        changed["bitblast_certificate"] = dict(plan.certificate)
        with pytest.raises(PartitionExecutionError, match="scope"):
            verify_cube_result(plan, certificate, 0, changed, checker=checker)


def test_all_unsat_cubes_resolve_to_base_query_receipt() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=4,
        max_attempts=2,
        cube_timeout_ms=100,
        task_lease_ms=1000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        store = PartitionExecutionStore(root / "executions")
        execution = store.create(plan, certificate, policy)
        for sequence in range(4):
            lease = store.claim(execution, f"worker-{sequence}")
            assert lease is not None
            derived = extend_bitblast_assumptions(plan, lease.literals)
            assert store.complete(
                plan,
                certificate,
                lease,
                _unsat_result(derived, proof_store, checker, sequence=sequence),
                checker=checker,
            )
        assert store.snapshot(execution)["task_counts"] == {"unsat": 4}
        aggregate = store.finalize_unsat(
            plan,
            certificate,
            execution,
            checker=checker,
            proof_store=proof_store,
        )
        assert aggregate["status"] == "unsat"
        assert aggregate["backend_partition_completed_cubes"] == 4
        assert aggregate["backend_partition_execution_result"] == "unsat"
        receipt = checker.verify_result_receipt(
            plan, aggregate["backend_incremental_result_receipt"]
        )
        record = checker.verify_clause_record(
            plan, proof_store.load(receipt.clause_receipt_sha256)
        )
        assert record.import_count == 4
        assert record.proof_steps == 3
        assert record.clause == tuple(-literal for literal in plan.assumptions)
        replay = store.finalize_unsat(
            plan,
            certificate,
            execution,
            checker=checker,
            proof_store=proof_store,
        )
        assert replay["backend_incremental_proof_record_sha256"] == (
            aggregate["backend_incremental_proof_record_sha256"]
        )


def test_concurrent_unsat_aggregation_converges_to_one_proof() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=4,
        cube_timeout_ms=100,
        task_lease_ms=1000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        store = PartitionExecutionStore(root / "executions")
        execution = store.create(plan, certificate, policy)
        for sequence in range(4):
            lease = store.claim(execution, f"worker-{sequence}")
            assert lease is not None
            derived = extend_bitblast_assumptions(plan, lease.literals)
            assert store.complete(
                plan,
                certificate,
                lease,
                _unsat_result(derived, proof_store, checker, sequence=sequence),
                checker=checker,
            )

        publish_barrier = threading.Barrier(2)
        original_publish = proof_store.publish

        def synchronized_publish(record):
            if str(record.get("source_worker", "")).startswith(
                "partition-aggregate:"
            ):
                publish_barrier.wait(timeout=5)
            return original_publish(record)

        proof_store.publish = synchronized_publish  # type: ignore[method-assign]
        results = []
        errors = []

        def finalize() -> None:
            try:
                results.append(
                    store.finalize_unsat(
                        plan,
                        certificate,
                        execution,
                        checker=checker,
                        proof_store=proof_store,
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=finalize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        proof_store.publish = original_publish  # type: ignore[method-assign]
        assert not errors
        assert len(results) == 2
        assert len(
            {
                result["backend_incremental_proof_record_sha256"]
                for result in results
            }
        ) == 1
        assert proof_store.stats()["records"] == 5


@pytest.mark.parametrize("tamper", ["inventory", "duplicate-json", "proof-digest"])
def test_unsat_aggregation_revalidates_persisted_cube_results(tamper: str) -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 2)
    policy = PartitionExecutionPolicy(
        parallelism=2,
        cube_timeout_ms=100,
        task_lease_ms=1000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        store = PartitionExecutionStore(root / "executions")
        execution = store.create(plan, certificate, policy)
        for sequence in range(2):
            lease = store.claim(execution, f"worker-{sequence}")
            assert lease is not None
            derived = extend_bitblast_assumptions(plan, lease.literals)
            assert store.complete(
                plan,
                certificate,
                lease,
                _unsat_result(derived, proof_store, checker, sequence=sequence),
                checker=checker,
            )
        with store._connect() as db:
            if tamper == "inventory":
                db.execute(
                    "UPDATE partition_cube_tasks SET cube_sha256=? "
                    "WHERE execution_sha256=? AND ordinal=0",
                    ("0" * 64, execution),
                )
            else:
                row = db.execute(
                    "SELECT result_json FROM partition_cube_tasks "
                    "WHERE execution_sha256=? AND ordinal=0",
                    (execution,),
                ).fetchone()
                assert row is not None
                raw = str(row["result_json"])
                if tamper == "duplicate-json":
                    changed = '{"status":"unsat",' + raw[1:]
                else:
                    decoded = json.loads(raw)
                    decoded["backend_incremental_proof_record_sha256"] = "0" * 64
                    changed = json.dumps(
                        decoded,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                db.execute(
                    "UPDATE partition_cube_tasks SET result_json=? "
                    "WHERE execution_sha256=? AND ordinal=0",
                    (changed, execution),
                )
        expected = "inventory" if tamper == "inventory" else (
            "duplicate" if tamper == "duplicate-json" else "proof"
        )
        with pytest.raises(PartitionExecutionError, match=expected):
            store.finalize_unsat(
                plan,
                certificate,
                execution,
                checker=checker,
                proof_store=proof_store,
            )


class _UnsatBackend:
    def __init__(self, plan, proof_store, checker, sequence) -> None:
        self.plan = plan
        self.proof_store = proof_store
        self.checker = checker
        self.sequence = sequence

    def solve_with_assumptions(self, _lease, literals):
        derived = extend_bitblast_assumptions(self.plan, literals)
        return _unsat_result(
            derived, self.proof_store, self.checker, sequence=self.sequence
        )


def _value_satisfying_cube(plan, literals) -> int:
    value = 0
    required = {abs(literal): literal > 0 for literal in literals}
    for _offset, input_literals in plan.input_literals:
        for bit, input_literal in enumerate(input_literals):
            variable = abs(input_literal)
            if variable not in required:
                continue
            semantic = (
                required[variable]
                if input_literal > 0
                else not required[variable]
            )
            if semantic:
                value |= 1 << bit
    return value


class _CooperativeSatBackend:
    def __init__(self, index, plan, checker, barrier) -> None:
        self.index = index
        self.plan = plan
        self.checker = checker
        self.barrier = barrier
        self.cancelled = threading.Event()
        self.cancel_calls = 0

    def solve_with_assumptions(self, _lease, literals):
        self.barrier.wait(timeout=5)
        derived = extend_bitblast_assumptions(self.plan, literals)
        if self.index == 0:
            return _sat_result(
                derived,
                self.checker,
                _value_satisfying_cube(self.plan, literals),
            )
        assert self.cancelled.wait(timeout=5)
        return {"status": "unknown", "assignments": {}, "cancelled": True}

    def cancel(self, _lease) -> bool:
        self.cancel_calls += 1
        self.cancelled.set()
        return True


def test_executor_runs_parallel_workers_and_returns_replayable_result() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=3,
        max_attempts=2,
        cube_timeout_ms=100,
        task_lease_ms=1000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        sequence_lock = threading.Lock()
        sequence = 0

        def backend_factory(_index):
            nonlocal sequence
            with sequence_lock:
                current = sequence
                sequence += 1
            return _UnsatBackend(plan, proof_store, checker, current)

        executor = ProofAwarePartitionExecutor(
            PartitionExecutionStore(root / "execution"),
            proof_store,
            checker,
            backend_factory,
        )

        class Parent:
            input_hex = "00"

        result = executor.execute(plan, certificate, Parent(), policy)
        assert result["status"] == "unsat"
        assert result["backend_partition_cube_count"] == 4
        assert result["backend_partition_completed_cubes"] == 4
        checker.verify_result_receipt(
            plan, result["backend_incremental_result_receipt"]
        )


def test_sat_winner_cooperatively_cancels_every_active_peer() -> None:
    plan = _plan()
    certificate = _partition(plan, 4)
    policy = PartitionExecutionPolicy(
        parallelism=4,
        cube_timeout_ms=1000,
        task_lease_ms=2000,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        barrier = threading.Barrier(4)
        backends = []

        def factory(index):
            backend = _CooperativeSatBackend(index, plan, checker, barrier)
            backends.append(backend)
            return backend

        executor = ProofAwarePartitionExecutor(
            PartitionExecutionStore(root / "execution"),
            proof_store,
            checker,
            factory,
        )

        class Parent:
            input_hex = "00"

        result = executor.execute(plan, certificate, Parent(), policy)
        assert result["status"] == "sat"
        assert result["backend_partition_completed_cubes"] == 1
        assert sum(backend.cancel_calls for backend in backends) == 3
        assert all(
            backend.cancelled.is_set() for backend in backends if backend.index != 0
        )


def test_executor_waits_for_and_reclaims_a_crash_lease() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 2)
    policy = PartitionExecutionPolicy(
        parallelism=2,
        max_attempts=2,
        cube_timeout_ms=100,
        task_lease_ms=300,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        store = PartitionExecutionStore(root / "execution")
        execution = store.create(plan, certificate, policy)
        abandoned = store.claim(execution, "crashed-worker")
        assert abandoned is not None

        executor = ProofAwarePartitionExecutor(
            store,
            proof_store,
            checker,
            lambda index: _UnsatBackend(plan, proof_store, checker, index),
        )

        class Parent:
            input_hex = "00"

        started = time.monotonic()
        result = executor.execute(plan, certificate, Parent(), policy)
        assert time.monotonic() - started >= 0.20
        assert result["status"] == "unsat"
        assert store.stats()["attempts"] == 3


def test_execution_lifecycle_is_dependency_closed_and_active_fails_closed() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan, 2)
    policy = PartitionExecutionPolicy(
        parallelism=2,
        cube_timeout_ms=100,
        task_lease_ms=500,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lifecycle = ArtifactLifecycleRegistry(root / "lifecycle")
        lease = lifecycle.start_job("f449-test", "test-owner", lease_seconds=10)
        proof_store = IncrementalProofStore(
            root / "proofs", lifecycle=lifecycle, lifecycle_lease=lease
        )
        checker = IncrementalProofChecker(proof_store)
        partition_store = ProofPrefixPartitionStore(
            root / "partitions", lifecycle=lifecycle, lifecycle_lease=lease
        )
        partition_store.publish(plan, certificate, checker=checker)
        store = PartitionExecutionStore(
            root / "executions", lifecycle=lifecycle, lifecycle_lease=lease
        )
        execution = store.create(plan, certificate, policy)
        active_inventory = store.synchronize_lifecycle(max_entries=16)
        assert active_inventory == {"indexed": 0, "active": 1, "complete": False}
        for sequence in range(2):
            cube_lease = store.claim(execution, f"worker-{sequence}")
            assert cube_lease is not None
            derived = extend_bitblast_assumptions(plan, cube_lease.literals)
            assert store.complete(
                plan,
                certificate,
                cube_lease,
                _unsat_result(derived, proof_store, checker, sequence=sequence),
                checker=checker,
            )
        store.finalize_unsat(
            plan,
            certificate,
            execution,
            checker=checker,
            proof_store=proof_store,
        )
        assert store.synchronize_lifecycle(max_entries=16) == {
            "indexed": 1,
            "active": 0,
            "complete": True,
        }
        lifecycle.release_job(lease)

        def delete(kind: str, digest: str, size: int) -> int:
            if kind == "partition-execution":
                return store.delete_lifecycle_artifact(kind, digest, size)
            if kind == "partition":
                return partition_store.delete_lifecycle_artifact(kind, digest, size)
            if kind == "sat-proof":
                return proof_store.delete_lifecycle_artifact(kind, digest, size)
            raise AssertionError(kind)

        collected = lifecycle.collect(
            delete,
            grace_seconds=0,
            max_objects=16,
            max_bytes=1 << 24,
            time_budget_ms=10_000,
            now=time.time() + 10,
        )
        deleted = list(collected.deleted)
        assert len(deleted) == 5
        execution_index = next(
            index for index, item in enumerate(deleted)
            if item.kind == "partition-execution"
        )
        assert all(
            execution_index < index
            for index, item in enumerate(deleted)
            if item.kind in {"partition", "sat-proof"}
        )
        assert store.stats()["executions"] == 0


def test_imported_resolution_rejects_a_tampered_leaf() -> None:
    plan = _plan(contradiction=True)
    certificate = _partition(plan)
    with tempfile.TemporaryDirectory() as directory:
        proof_store = IncrementalProofStore(Path(directory) / "proofs")
        checker = IncrementalProofChecker(proof_store)
        derived = extend_bitblast_assumptions(
            plan, certificate["cubes"][0]["literals"]
        )
        result = _unsat_result(derived, proof_store, checker)
        record = proof_store.load(
            result["backend_incremental_proof_record_sha256"]
        )
        record["proof_steps"][0]["clause"] = [1]
        record.pop("record_sha256")
        with pytest.raises(IncrementalProofError):
            checker.verify_clause_record(plan, record)


def _query_envelope(*, unsat: bool) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f449-service-test",
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
                "attrs": {"value": not unsat},
            },
        ],
        "prefix_roots": [3],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 2000,
        "metadata": {"source": "f449-service-test"},
        "smt2": f"(assert {'false' if unsat else 'true'})\n",
        "prefix_smt2": f"(assert {'false' if unsat else 'true'})\n",
        "target_smt2": "(assert (= input0 #x00))\n",
    }


def _write_cnf_solver(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import itertools,pathlib,sys
cnf=pathlib.Path(sys.argv[-2])
proof=pathlib.Path(sys.argv[-1])
lines=[line.strip() for line in cnf.read_text(encoding='ascii').splitlines()
       if line.strip() and not line.startswith('c')]
header=lines.pop(0).split()
maximum=int(header[2])
clauses=[tuple(map(int,line.split()[:-1])) for line in lines]
model=None
for bits in itertools.product((False,True),repeat=maximum):
    if all(any(bits[abs(lit)-1] == (lit>0) for lit in clause) for clause in clauses):
        model=bits
        break
if model is not None:
    values=[str(index if value else -index) for index,value in enumerate(model,1)]
    print('s SATISFIABLE')
    print('v '+' '.join(values)+' 0')
    raise SystemExit(10)
assignment={}
hints=[]
changed=True
while changed:
    changed=False
    for clause_id,clause in enumerate(clauses,1):
        unassigned=[]
        satisfied=False
        for literal in clause:
            value=assignment.get(abs(literal))
            if value is None:
                unassigned.append(literal)
            elif value == (literal>0):
                satisfied=True
                break
        if satisfied or len(unassigned)>1:
            continue
        hints.append(clause_id)
        if not unassigned:
            proof.write_text(
                f'{len(clauses)+1} 0 '+ ' '.join(map(str,hints))+' 0\\n',
                encoding='ascii')
            print('s UNSATISFIABLE')
            raise SystemExit(20)
        literal=unassigned[0]
        previous=assignment.setdefault(abs(literal),literal>0)
        if previous != (literal>0):
            proof.write_text(
                f'{len(clauses)+1} 0 '+ ' '.join(map(str,hints))+' 0\\n',
                encoding='ascii')
            print('s UNSATISFIABLE')
            raise SystemExit(20)
        changed=True
print('s UNKNOWN')
raise SystemExit(0)
""",
        encoding="ascii",
    )
    path.chmod(0o755)


@pytest.mark.parametrize("unsat,expected", [(False, "sat"), (True, "unsat")])
def test_query_service_executes_certified_cubes_end_to_end(
    unsat: bool, expected: str, capsys
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        query_store = QueryStore(root / "queries")
        query_store.ingest(_query_envelope(unsat=unsat))
        solver = root / "cnf_solver.py"
        _write_cnf_solver(solver)
        portfolio = {
            "solvers": [
                {
                    "name": "f449-cnf-test",
                    "kind": "bitblast-cadical-qfbv",
                    "persistent": False,
                    "capabilities": {"incremental": True},
                    "command": [
                        sys.executable,
                        str(solver),
                        "--plain",
                        "--lrat",
                        "--no-binary",
                        "{cnf}",
                        "{proof}",
                    ],
                }
            ]
        }
        assert query_service_main(
            [
                "--store",
                str(root / "queries"),
                "--portfolio",
                json.dumps(portfolio),
                "--qfbv-partition-cubes",
                "2",
                "--qfbv-partition-parallelism",
                "2",
                "--qfbv-partition-cube-timeout-ms",
                "2000",
                "--qfbv-partition-task-lease-ms",
                "5000",
                "--lease-seconds",
                "5",
                "--jobs",
                "1",
                "--once",
            ]
        ) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["worker"][expected] == 1, summary
        assert summary["qfbv_partition_store"]["partitions"] == 1
        execution = summary["qfbv_partition_execution_store"]
        assert execution["executions"] == 1
        assert execution["execution_states"] == {expected: 1}
        stats = query_store.stats()
        assert stats["results"] == 1
        assert stats["partition_execution_results"] == 1
        assert stats[f"partition_execution_{expected}"] == 1
        assert stats["partition_execution_cubes"] == 2


def test_query_service_online_cubing_falls_back_without_activity(
    capsys,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        query_store = QueryStore(root / "queries")
        query_store.ingest(_query_envelope(unsat=False))
        solver = root / "cnf_solver.py"
        _write_cnf_solver(solver)
        portfolio = {
            "solvers": [
                {
                    "name": "f453-static-fallback",
                    "kind": "bitblast-cadical-qfbv",
                    "persistent": False,
                    "capabilities": {"incremental": True},
                    "command": [
                        sys.executable,
                        str(solver),
                        "--plain",
                        "--lrat",
                        "--no-binary",
                        "{cnf}",
                        "{proof}",
                    ],
                }
            ]
        }
        assert query_service_main(
            [
                "--store",
                str(root / "queries"),
                "--portfolio",
                json.dumps(portfolio),
                "--qfbv-partition-cubes",
                "2",
                "--qfbv-partition-parallelism",
                "2",
                "--qfbv-partition-cube-timeout-ms",
                "2000",
                "--qfbv-partition-task-lease-ms",
                "5000",
                "--qfbv-online-cubing",
                "--qfbv-online-cubing-candidates",
                "2,4",
                "--lease-seconds",
                "5",
                "--jobs",
                "1",
                "--once",
            ]
        ) == 0
        summary = json.loads(capsys.readouterr().out)
        online = summary["qfbv_online_cubing_store"]
        assert online["decisions"] == 1
        assert online["outcomes"] == 1
        assert online["arms"]["static"]["decisions"] == 1
        stats = query_store.stats()
        assert stats["online_cubing_results"] == 1
        assert stats["online_cubing_static"] == 1
        assert stats["online_cubing_configured_cpu_budget_ms"] == 12_000
        assert stats["online_cubing_effective_cpu_budget_ms"] == 12_000


def test_query_service_online_cubing_configuration_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="requires proof-aware"):
            query_service_main(
                [
                    "--store",
                    str(Path(directory) / "queries"),
                    "--qfbv-online-cubing",
                    "--once",
                ]
            )


def test_persistent_native_backend_reuses_formula_not_cube_assumptions() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "cadical_stub.c"
        library = root / "libcadical.so"
        source.write_text(
            "#include <stdlib.h>\n"
            "typedef struct { int terminated; } Solver;\n"
            "const char* ccadical_signature(void){return \"cadical-3.0.1-f449\";}\n"
            "void* ccadical_init(void){return calloc(1,sizeof(Solver));}\n"
            "void ccadical_release(void*p){free(p);}\n"
            "void ccadical_add(void*p,int l){(void)p;(void)l;}\n"
            "void ccadical_assume(void*p,int l){(void)p;(void)l;}\n"
            "int ccadical_solve(void*p){return ((Solver*)p)->terminated?0:20;}\n"
            "int ccadical_val(void*p,int l){(void)p;return l;}\n"
            "int ccadical_failed(void*p,int l){(void)p;(void)l;return 1;}\n"
            "void ccadical_terminate(void*p){((Solver*)p)->terminated=1;}\n"
            "void ccadical_set_terminate(void*p,void*s,void*f){"
            "(void)p;(void)s;(void)f;}\n",
            encoding="ascii",
        )
        subprocess.run(
            ["cc", "-shared", "-fPIC", str(source), "-o", str(library)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        solver = root / "cnf_solver.py"
        _write_cnf_solver(solver)
        query_store = QueryStore(root / "queries")
        query_store.ingest(_query_envelope(unsat=True))
        lease = query_store.claim("native-owner", lease_seconds=10)
        assert lease is not None
        loaded = query_store.load_query_ir(lease.query_id)
        assert loaded is not None
        capabilities = normalize_qfbv_capabilities({"incremental": True})
        plan = bitblast_qfbv_query(
            lease.query_id, loaded[0], loaded[1], capabilities
        )
        variable = abs(plan.input_literals[0][1][0])
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        command = [
            sys.executable,
            str(solver),
            "--plain",
            "--lrat",
            "--no-binary",
            "{cnf}",
            "{proof}",
        ]
        with PersistentCadicalQfbvSolver(
            query_store,
            library,
            command,
            name="f449-native",
            proof_store=proof_store,
            proof_checker=checker,
            capabilities=capabilities,
        ) as backend:
            negative = dict(backend.solve_with_assumptions(lease, [-variable]))
            positive = dict(backend.solve_with_assumptions(lease, [variable]))
        assert negative["status"] == positive["status"] == "unsat"
        assert not negative["backend_native_context_cache_hit"]
        assert positive["backend_native_context_cache_hit"]
        assert negative["bitblast_certificate"]["assumption_sha256"] != (
            positive["bitblast_certificate"]["assumption_sha256"]
        )
        for literal, result in ((-variable, negative), (variable, positive)):
            derived = extend_bitblast_assumptions(plan, [literal])
            authorization = checker.verify_result_receipt(
                derived, result["backend_incremental_result_receipt"]
            )
            assert literal in authorization.failed_assumptions


def test_query_service_partition_configuration_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with pytest.raises(ValueError, match="0 or"):
            query_service_main(
                [
                    "--store",
                    str(root / "invalid-count"),
                    "--qfbv-partition-cubes",
                    "1",
                    "--once",
                ]
            )
        with pytest.raises(ValueError, match="exactly one CaDiCaL"):
            query_service_main(
                [
                    "--store",
                    str(root / "missing-backend"),
                    "--qfbv-partition-cubes",
                    "2",
                    "--once",
                ]
            )

        solver = root / "unused_solver.py"
        _write_cnf_solver(solver)
        portfolio = {
            "solvers": [
                {
                    "name": "f449-config-test",
                    "kind": "bitblast-cadical-qfbv",
                    "persistent": False,
                    "capabilities": {"incremental": True},
                    "command": [
                        sys.executable,
                        str(solver),
                        "--plain",
                        "--lrat",
                        "--no-binary",
                        "{cnf}",
                        "{proof}",
                    ],
                }
            ]
        }
        with pytest.raises(ValueError, match="--jobs"):
            query_service_main(
                [
                    "--store",
                    str(root / "oversubscribed"),
                    "--portfolio",
                    json.dumps(portfolio),
                    "--qfbv-partition-cubes",
                    "2",
                    "--qfbv-partition-parallelism",
                    "2",
                    "--jobs",
                    "3",
                    "--once",
                ]
            )


def test_executable_partition_execution_oracle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "oracle.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(
                    ROOT
                    / "benchmark/check_qfbv_partition_execution_oracles.py"
                ),
                "--rounds",
                "1",
                "--cube-counts",
                "2,4,8",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(output.read_text(encoding="ascii"))
        assert payload["status"] == "pass"
        assert [case["leaf_proofs"] for case in payload["cases"]] == [
            2,
            4,
            8,
        ]
        assert [case["resolution_steps"] for case in payload["cases"]] == [
            1,
            3,
            7,
        ]
