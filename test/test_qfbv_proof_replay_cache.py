#!/usr/bin/env python3
# RUN: python3 -m pytest -q %s

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    REPLAY_CACHE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    make_imported_lrup_clause_record,
    make_rup_clause_record,
)
from qfbv_incremental_sat import BitBlastPlan, bitblast_qfbv_query  # noqa: E402
from symcc_query_service import main as query_service_main  # noqa: E402


def _plan(query_id: str = "f450-proof-cache"):
    expressions = {
        "true": {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": True},
        },
        "false": {
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": False},
        },
    }
    return bitblast_qfbv_query(query_id, ["true", "false"], expressions)


def _record(plan, sequence: int = 0):
    return make_rup_clause_record(
        plan,
        tuple(-literal for literal in plan.assumptions),
        dependency_assumptions=plan.assumptions,
        source_worker=f"f450-source-{sequence}",
        worker_epoch=0,
        sequence=sequence,
    )


def test_replay_cache_configuration_is_strict_and_policy_bound() -> None:
    with pytest.raises(IncrementalProofError, match="disabled together"):
        IncrementalProofChecker(replay_cache_entries=0, replay_cache_bytes=1)
    with pytest.raises(IncrementalProofError, match="must be an integer"):
        IncrementalProofChecker(replay_cache_entries=True)
    disabled = IncrementalProofChecker(
        replay_cache_entries=0,
        replay_cache_bytes=0,
    )
    enabled = IncrementalProofChecker(
        replay_cache_entries=2,
        replay_cache_bytes=4096,
    )
    assert disabled.policy_sha256 == enabled.policy_sha256
    assert disabled.replay_cache_policy_sha256 != enabled.replay_cache_policy_sha256
    assert enabled.replay_cache_stats() == {
        "schema": REPLAY_CACHE_PROTOCOL,
        "policy_sha256": enabled.replay_cache_policy_sha256,
        "max_entries": 2,
        "max_bytes": 4096,
        "entries": 0,
        "accounted_bytes": 0,
        "hits": 0,
        "misses": 0,
        "inserts": 0,
        "evictions": 0,
        "oversized": 0,
        "bypassed": 0,
        "attestation_failures": 0,
        "attested_objects": 0,
        "attested_bytes": 0,
    }


def test_cache_hit_requires_the_same_plan_object_and_canonical_record() -> None:
    plan = _plan()
    record = _record(plan)
    checker = IncrementalProofChecker()
    cold = checker.verify_clause_record(plan, record)
    hot = checker.verify_clause_record(plan, copy.deepcopy(record))
    assert cold.record_sha256 == hot.record_sha256
    assert not cold.replay_cache_hit
    assert hot.replay_cache_hit
    assert checker.replay_cache_stats()["hits"] == 1

    equivalent_plan = _plan()
    isolated = checker.verify_clause_record(equivalent_plan, record)
    assert not isolated.replay_cache_hit
    assert checker.replay_cache_stats()["entries"] == 2

    tampered = copy.deepcopy(record)
    tampered["source_worker"] = "changed-after-cache"
    with pytest.raises(IncrementalProofError, match="identity changed"):
        checker.verify_clause_record(plan, tampered)


def test_lru_and_byte_budgets_are_both_enforced() -> None:
    plan = _plan()
    checker = IncrementalProofChecker(
        replay_cache_entries=2,
        replay_cache_bytes=4096,
    )
    records = [_record(plan, sequence) for sequence in range(3)]
    for record in records:
        assert not checker.verify_clause_record(plan, record).replay_cache_hit
    stats = checker.replay_cache_stats()
    assert stats["entries"] == 2
    assert stats["evictions"] == 1
    assert not checker.verify_clause_record(plan, records[0]).replay_cache_hit
    assert checker.replay_cache_stats()["evictions"] == 2

    tiny = IncrementalProofChecker(
        replay_cache_entries=2,
        replay_cache_bytes=1,
    )
    assert not tiny.verify_clause_record(plan, records[0]).replay_cache_hit
    assert tiny.replay_cache_stats()["entries"] == 0
    assert tiny.replay_cache_stats()["oversized"] == 1


def test_disabled_cache_preserves_replay_and_counts_bypasses() -> None:
    plan = _plan()
    record = _record(plan)
    checker = IncrementalProofChecker(
        replay_cache_entries=0,
        replay_cache_bytes=0,
    )
    assert not checker.verify_clause_record(plan, record).replay_cache_hit
    assert not checker.verify_clause_record(plan, record).replay_cache_hit
    stats = checker.replay_cache_stats()
    assert stats["entries"] == stats["hits"] == stats["misses"] == 0
    assert stats["bypassed"] == 2


def test_noncanonical_mutable_plan_is_never_cached() -> None:
    plan = _plan()
    mutable = BitBlastPlan(
        query_id=plan.query_id,
        clauses=(list(plan.clauses[0]), *plan.clauses[1:]),
        max_variable=plan.max_variable,
        input_literals=plan.input_literals,
        assumptions=plan.assumptions,
        increments=plan.increments,
        certificate=plan.certificate,
    )
    record = _record(plan)
    checker = IncrementalProofChecker()
    assert not checker.verify_clause_record(mutable, record).replay_cache_hit
    assert not checker.verify_clause_record(mutable, record).replay_cache_hit
    stats = checker.replay_cache_stats()
    assert stats["entries"] == stats["hits"] == 0
    assert stats["bypassed"] == 2


def test_cached_parent_still_stable_reads_the_complete_import_closure() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        store = IncrementalProofStore(directory)
        builder = IncrementalProofChecker(store)
        leaf = _record(plan)
        leaf_authorization = builder.verify_clause_record(plan, leaf)
        leaf_digest, _created = store.publish(leaf)
        imported_id = len(plan.clauses) + 1
        parent = make_imported_lrup_clause_record(
            plan,
            [leaf_authorization],
            [(leaf_authorization.clause, [imported_id])],
            dependency_assumptions=plan.assumptions,
            source_worker="f450-parent",
            worker_epoch=0,
            sequence=1,
        )
        checker = IncrementalProofChecker(store)
        cold = checker.verify_clause_record(plan, parent)
        hot = checker.verify_clause_record(plan, parent)
        assert not cold.replay_cache_hit
        assert hot.replay_cache_hit
        assert checker.replay_cache_stats()["hits"] >= 1

        leaf_path = store._path(leaf_digest)
        altered = json.loads(leaf_path.read_text(encoding="ascii"))
        altered["source_worker"] = "f450-source-X"
        leaf_path.write_text(
            json.dumps(
                altered,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
        with pytest.raises(IncrementalProofError):
            checker.verify_clause_record(plan, parent)
        failed_stats = checker.replay_cache_stats()
        assert failed_stats["hits"] == 1
        assert failed_stats["attestation_failures"] == 1
        assert failed_stats["evictions"] == 1
        assert failed_stats["entries"] == 1


def test_query_service_cache_budgets_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="disabled together"):
            query_service_main(
                [
                    "--store",
                    directory,
                    "--qfbv-proof-replay-cache-entries",
                    "0",
                    "--qfbv-proof-replay-cache-bytes",
                    "1",
                    "--once",
                ]
            )
        with pytest.raises(ValueError, match="entries must be"):
            query_service_main(
                [
                    "--store",
                    directory,
                    "--qfbv-proof-replay-cache-entries",
                    "-1",
                    "--qfbv-proof-replay-cache-bytes",
                    "0",
                    "--once",
                ]
            )


def test_replay_cache_is_thread_safe_and_conservative_under_races() -> None:
    plan = _plan()
    record = _record(plan)
    checker = IncrementalProofChecker()
    barrier = threading.Barrier(16)
    results: list[tuple[str, bool]] = []
    failures: list[BaseException] = []
    result_lock = threading.Lock()

    def verify() -> None:
        try:
            barrier.wait(timeout=5)
            authorization = checker.verify_clause_record(plan, record)
            with result_lock:
                results.append(
                    (authorization.record_sha256, authorization.replay_cache_hit)
                )
        except BaseException as error:  # pragma: no cover - asserted below
            with result_lock:
                failures.append(error)

    threads = [threading.Thread(target=verify) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures
    assert len(results) == 16
    assert len({digest for digest, _hit in results}) == 1
    stats = checker.replay_cache_stats()
    assert stats["hits"] + stats["misses"] == 16
    assert stats["inserts"] >= 1
    assert stats["entries"] == 1


def test_executable_proof_replay_cache_oracle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "oracle.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "benchmark/check_qfbv_proof_replay_cache_oracles.py"),
                "--rounds",
                "2",
                "--depths",
                "2,4",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(output.read_text(encoding="ascii"))
        assert payload["status"] == "pass"
        assert [case["depth"] for case in payload["cases"]] == [2, 4]
        assert all(case["proof_records"] == case["depth"] for case in payload["cases"])
        assert all(
            case["enabled_cache"]["entries"] == case["depth"]
            for case in payload["cases"]
        )
