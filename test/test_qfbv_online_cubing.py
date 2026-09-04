#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_online_cubing import (  # noqa: E402
    ONLINE_CUBING_ARMS,
    ONLINE_CUBING_PROTOCOL,
    OnlineCubingError,
    OnlineCubingPolicy,
    OnlineCubingPolicyStore,
    normalize_online_cubing_result,
    verify_online_cubing_decision,
    verify_online_cubing_outcome,
)
from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_partition_execution import (  # noqa: E402
    PARTITION_EXECUTION_PROTOCOL,
    PartitionExecutionPolicy,
    PartitionExecutionStore,
    PartitioningQfbvBackend,
)
from qfbv_proof_prefix_partition import ProofPrefixPartitionStore  # noqa: E402
from qfbv_utility_pairing import formula_family_sha256  # noqa: E402


FAMILY = "a" * 64
CERTIFICATE = {"certificate_sha256": "b" * 64}


def _query_ir() -> tuple[list[str], dict[str, dict]]:
    return ["root"], {
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


def _observe(
    store: OnlineCubingPolicyStore,
    decision: dict,
    *,
    status: str = "sat",
    elapsed_us: int = 10_000,
) -> dict:
    guided = bool(decision["prerun_requested"])
    return store.observe(
        decision,
        status=status,
        partition_executed=True,
        elapsed_us=elapsed_us,
        prerun_elapsed_us=100 if guided else 0,
        completed_cubes=(decision["cube_count"] if status == "unsat" else 1),
        activity_receipts=1 if guided else 0,
    )


def _resign(value: dict, digest_name: str) -> dict:
    body = copy.deepcopy(value)
    body.pop(digest_name, None)
    body[digest_name] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return body


def test_policy_identity_and_canonical_configuration_are_strict() -> None:
    policy = OnlineCubingPolicy()
    assert policy.strategies == ONLINE_CUBING_ARMS
    assert policy.sha256 == OnlineCubingPolicy.from_sealed(
        policy.as_dict()
    ).sha256
    assert policy.as_dict()["protocol"] == ONLINE_CUBING_PROTOCOL
    with pytest.raises(OnlineCubingError, match="cube count"):
        OnlineCubingPolicy(base_cube_count=True)
    with pytest.raises(OnlineCubingError, match="candidate"):
        OnlineCubingPolicy(cube_candidates=(4, 2, 8))
    with pytest.raises(OnlineCubingError, match="static fallback"):
        OnlineCubingPolicy(strategies=("activity", "cost"))
    with pytest.raises(OnlineCubingError, match="canonical"):
        OnlineCubingPolicy(strategies=("static", "cost", "activity"))
    with pytest.raises(OnlineCubingError, match="must be a candidate"):
        OnlineCubingPolicy(base_cube_count=3)
    with pytest.raises(OnlineCubingError, match="pending budget"):
        OnlineCubingPolicy(max_pending_per_family=0)
    tampered = policy.as_dict()
    tampered["cost_reference_us"] += 1
    assert OnlineCubingPolicy.from_sealed(tampered).sha256 != policy.sha256


def test_no_activity_capability_uses_identity_bound_static_fallback() -> None:
    policy = OnlineCubingPolicy()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "policy.sqlite3"
        store = OnlineCubingPolicyStore(path, policy)
        decision = store.decide("query-1", FAMILY, activity_available=False)
        assert decision["arm"] == "static"
        assert decision["selection_reason"] == "deterministic-static-fallback"
        assert decision["propensity_numerator"] == 1
        assert decision["propensity_denominator"] == 1
        assert decision["prerun_requested"] is False
        reopened = OnlineCubingPolicyStore(path, policy)
        assert reopened.decide("query-1", FAMILY, activity_available=False) == decision
        with pytest.raises(OnlineCubingError, match="availability"):
            reopened.decide("query-1", FAMILY, activity_available=True)


def test_bounded_exploration_observes_all_three_arms() -> None:
    policy = OnlineCubingPolicy(min_exploration_samples=1)
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        decisions = []
        for index in range(3):
            decision = store.decide(
                f"query-{index}", FAMILY, activity_available=True
            )
            decisions.append(decision)
            _observe(store, decision)
        assert {decision["arm"] for decision in decisions} == set(
            ONLINE_CUBING_ARMS
        )
        assert all(
            0 < decision["propensity_numerator"]
            <= decision["propensity_denominator"]
            for decision in decisions
        )
        for decision in decisions:
            assert Fraction(
                decision["propensity_numerator"],
                decision["propensity_denominator"],
            ) == Fraction(
                decision["arm_propensity_numerator"],
                decision["arm_propensity_denominator"],
            ) * Fraction(
                decision["cube_propensity_numerator"],
                decision["cube_propensity_denominator"],
            )
        snapshot = store.snapshot()
        assert snapshot["decisions"] == 3
        assert snapshot["outcomes"] == 3
        assert snapshot["pending"] == 0


def test_cost_arm_explores_candidates_then_uses_observed_utility() -> None:
    policy = OnlineCubingPolicy(
        strategies=("static", "cost"),
        min_exploration_samples=1,
        exploration_permille=0,
        uncertainty_scale=0,
    )
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        cost_candidates: set[int] = set()
        exploited: list[int] = []
        for index in range(64):
            decision = store.decide(
                f"cost-query-{index}", FAMILY, activity_available=True
            )
            if decision["arm"] == "cost":
                cost_candidates.add(int(decision["cube_count"]))
                if decision["cube_selection_reason"] == "cost-utility-exploitation":
                    exploited.append(int(decision["cube_count"]))
                _observe(
                    store,
                    decision,
                    elapsed_us=int(decision["cube_count"]) * 1000,
                )
            else:
                _observe(store, decision, status="error", elapsed_us=1_000_000)
            if cost_candidates == set(policy.cube_candidates) and exploited:
                break
        assert cost_candidates == set(policy.cube_candidates)
        assert exploited[-1] == min(policy.cube_candidates)


def test_decisions_are_concurrent_unique_and_same_query_idempotent() -> None:
    policy = OnlineCubingPolicy(min_exploration_samples=1)
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        decisions: list[dict] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def decide(index: int) -> None:
            try:
                value = store.decide(
                    f"parallel-{index}", FAMILY, activity_available=True
                )
                with lock:
                    decisions.append(value)
            except BaseException as error:  # pragma: no cover - assertion payload
                with lock:
                    failures.append(error)

        threads = [threading.Thread(target=decide, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []
        assert len({item["decision_sha256"] for item in decisions}) == 24
        assert sorted(item["family_ordinal"] for item in decisions) == list(
            range(1, 25)
        )
        counts = {
            arm: sum(item["arm"] == arm for item in decisions)
            for arm in ONLINE_CUBING_ARMS
        }
        assert max(counts.values()) - min(counts.values()) <= 1

        repeated: list[dict] = []

        def repeat() -> None:
            repeated.append(
                store.decide("same-query", "c" * 64, activity_available=False)
            )

        repeaters = [threading.Thread(target=repeat) for _ in range(8)]
        for thread in repeaters:
            thread.start()
        for thread in repeaters:
            thread.join()
        assert len({item["decision_sha256"] for item in repeated}) == 1


def test_pending_and_verified_history_budgets_fail_closed() -> None:
    policy = OnlineCubingPolicy(
        min_exploration_samples=1,
        max_pending_per_family=3,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "policy.sqlite3"
        store = OnlineCubingPolicyStore(path, policy)
        for index in range(3):
            store.decide(f"pending-{index}", FAMILY, activity_available=True)
        with pytest.raises(OnlineCubingError, match="pending budget"):
            store.decide("pending-overflow", FAMILY, activity_available=True)

        other = "e" * 64
        decision = store.decide("observed", other, activity_available=False)
        outcome = _observe(store, decision)
        with sqlite3.connect(path) as database:
            database.execute(
                "UPDATE online_cubing_outcomes SET reward_micros=? "
                "WHERE outcome_sha256=?",
                (outcome["reward_micros"] + 1, outcome["outcome_sha256"]),
            )
        with pytest.raises(OnlineCubingError, match="outcome columns"):
            store.decide("after-corruption", other, activity_available=False)


def test_randomized_online_trace_preserves_replay_and_budget_invariants() -> None:
    generator = random.Random(453)
    policy = OnlineCubingPolicy(
        min_exploration_samples=2,
        cost_min_exploration_samples=1,
        exploration_permille=333,
        max_pending_per_family=8,
        max_history_per_family=64,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "policy.sqlite3"
        store = OnlineCubingPolicyStore(path, policy)
        for index in range(300):
            family = hashlib.sha256(f"family-{index % 7}".encode()).hexdigest()
            decision = store.decide(
                f"random-{index}", family, activity_available=True
            )
            assert verify_online_cubing_decision(
                decision,
                policy=policy,
                query_id=f"random-{index}",
                formula_family_sha256=family,
            ) == decision
            status = generator.choice(("sat", "unsat", "unknown", "error"))
            elapsed = generator.randint(1, 2_000_000)
            guided = bool(decision["prerun_requested"])
            prerun = generator.randint(1, elapsed) if guided else 0
            completed = (
                int(decision["cube_count"])
                if status == "unsat"
                else (1 if status == "sat" else generator.randrange(
                    int(decision["cube_count"]) + 1
                ))
            )
            outcome = store.observe(
                decision,
                status=status,
                partition_executed=True,
                elapsed_us=elapsed,
                prerun_elapsed_us=prerun,
                completed_cubes=completed,
                activity_receipts=generator.randrange(4) if guided else 0,
            )
            assert verify_online_cubing_outcome(
                outcome, decision=decision, policy=policy
            ) == outcome
            assert outcome["effective_cpu_budget_ms"] <= outcome[
                "configured_cpu_budget_ms"
            ]
            assert Fraction(
                decision["propensity_numerator"],
                decision["propensity_denominator"],
            ) == Fraction(
                decision["arm_propensity_numerator"],
                decision["arm_propensity_denominator"],
            ) * Fraction(
                decision["cube_propensity_numerator"],
                decision["cube_propensity_denominator"],
            )
        snapshot = OnlineCubingPolicyStore(path, policy).snapshot()
        assert snapshot["decisions"] == 300
        assert snapshot["outcomes"] == 300
        assert snapshot["pending"] == 0


def test_outcomes_are_idempotent_and_conflicts_fail_closed() -> None:
    policy = OnlineCubingPolicy(strategies=("static",))
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        decision = store.decide("query", FAMILY, activity_available=False)
        outcome = _observe(store, decision)
        assert _observe(store, decision) == outcome
        with pytest.raises(OnlineCubingError, match="conflicting"):
            _observe(store, decision, elapsed_us=10_001)
        tampered = copy.deepcopy(outcome)
        tampered["reward_micros"] += 1
        with pytest.raises(OnlineCubingError, match="identity"):
            verify_online_cubing_outcome(
                tampered, decision=decision, policy=policy
            )
        budget_tamper = copy.deepcopy(outcome)
        budget_tamper["effective_cpu_budget_ms"] -= 1
        budget_tamper = _resign(budget_tamper, "outcome_sha256")
        with pytest.raises(OnlineCubingError, match="CPU budget"):
            verify_online_cubing_outcome(
                budget_tamper, decision=decision, policy=policy
            )
        incomplete = store.decide("unsat-incomplete", FAMILY, activity_available=False)
        with pytest.raises(OnlineCubingError, match="incomplete"):
            store.observe(
                incomplete,
                status="unsat",
                partition_executed=True,
                elapsed_us=10,
                prerun_elapsed_us=0,
                completed_cubes=1,
                activity_receipts=0,
            )


def test_store_rejects_symlinks_metadata_and_noncanonical_decisions() -> None:
    policy = OnlineCubingPolicy(strategies=("static",))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target.sqlite3"
        sqlite3.connect(target).close()
        link = root / "link.sqlite3"
        link.symlink_to(target)
        with pytest.raises(OnlineCubingError, match="symlink"):
            OnlineCubingPolicyStore(link, policy)

        path = root / "policy.sqlite3"
        store = OnlineCubingPolicyStore(path, policy)
        decision = store.decide("query", FAMILY, activity_available=False)
        with sqlite3.connect(path) as database:
            database.execute(
                "UPDATE online_cubing_decisions SET decision_json=? "
                "WHERE decision_sha256=?",
                (
                    '{"schema":"duplicate","schema":"duplicate"}',
                    decision["decision_sha256"],
                ),
            )
        with pytest.raises(OnlineCubingError, match="duplicate"):
            store.decide("query", FAMILY, activity_available=False)

        metadata_path = root / "metadata.sqlite3"
        OnlineCubingPolicyStore(metadata_path, policy)
        with sqlite3.connect(metadata_path) as database:
            database.execute(
                "UPDATE online_cubing_metadata SET value='foreign' "
                "WHERE key='policy_sha256'"
            )
        with pytest.raises(OnlineCubingError, match="metadata"):
            OnlineCubingPolicyStore(metadata_path, policy)


def test_result_normalization_binds_policy_decision_outcome_and_partition() -> None:
    policy = OnlineCubingPolicy(strategies=("static",))
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        decision = store.decide("query", FAMILY, activity_available=False)
        outcome = _observe(store, decision)
        result = {
            "backend_online_cubing_protocol": ONLINE_CUBING_PROTOCOL,
            "backend_online_cubing_policy": policy.as_dict(),
            "backend_online_cubing_policy_sha256": policy.sha256,
            "backend_online_cubing_query_id": "query",
            "backend_online_cubing_formula_family_sha256": FAMILY,
            "backend_online_cubing_decision": decision,
            "backend_online_cubing_outcome": outcome,
            "backend_partition_execution_protocol": (
                "symcc-qfbv-proof-aware-execution-v1"
            ),
            "backend_partition_cube_count": decision["cube_count"],
            "backend_partition_completed_cubes": 1,
        }
        normalized = normalize_online_cubing_result(
            result,
            certificate=CERTIFICATE,
            status="sat",
            formula_family_sha256=FAMILY,
        )
        assert normalized["backend_online_cubing_decision"] == decision
        assert normalized["backend_online_cubing_outcome"] == outcome
        changed = copy.deepcopy(result)
        changed["backend_partition_cube_count"] += 1
        with pytest.raises(OnlineCubingError, match="accounting"):
            normalize_online_cubing_result(
                changed,
                certificate=CERTIFICATE,
                status="sat",
                formula_family_sha256=FAMILY,
            )


def test_decision_and_outcome_digest_tampering_is_rejected() -> None:
    policy = OnlineCubingPolicy(strategies=("static",))
    with tempfile.TemporaryDirectory() as directory:
        store = OnlineCubingPolicyStore(Path(directory) / "policy.sqlite3", policy)
        decision = store.decide("query", FAMILY, activity_available=False)
        changed = copy.deepcopy(decision)
        changed["cube_count"] = 8
        with pytest.raises(OnlineCubingError, match="identity"):
            verify_online_cubing_decision(
                changed,
                policy=policy,
                query_id="query",
                formula_family_sha256=FAMILY,
            )
        outcome = _observe(store, decision)
        encoded = json.dumps(outcome, sort_keys=True)
        assert outcome["outcome_sha256"] in encoded


def test_partition_backend_runs_bounded_prerun_then_certified_fallback() -> None:
    roots, expressions = _query_ir()
    seed_plan = bitblast_qfbv_query("seed", roots, expressions)
    family = formula_family_sha256(seed_plan.certificate)

    class FakeQueryStore:
        def load_query_ir(self, _query_id: str):
            return roots, expressions

        def renew(self, *_args, **_kwargs) -> bool:
            return True

        def validate_candidate(self, _query_id: str, candidate: bytes) -> bool:
            return candidate == b"\x00"

    class PrerunBackend:
        def __init__(self, calls: list) -> None:
            self.calls = calls

        def __call__(self, request):
            self.calls.append((request.timeout_ms, request.query_id, "run"))
            return {
                "status": "unknown",
                "assignments": {},
                "elapsed_us": request.timeout_ms * 1000,
            }

        def close(self) -> None:
            self.calls.append((0, "", "close"))

    class Executor:
        def __init__(self) -> None:
            self.certificate = None
            self.policy = None

        def execute(self, plan, certificate, _lease, policy, **_kwargs):
            self.certificate = certificate
            self.policy = policy
            return {
                "status": "unknown",
                "assignments": {},
                "solver": "f453-fake-executor",
                "backend_partition_execution_protocol": (
                    PARTITION_EXECUTION_PROTOCOL
                ),
                "backend_partition_execution_sha256": "d" * 64,
                "backend_partition_sha256": certificate["partition_sha256"],
                "backend_partition_cube_count": len(certificate["cubes"]),
                "backend_partition_completed_cubes": 0,
                "backend_partition_execution_result": "incomplete",
            }

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        policy = OnlineCubingPolicy(
            base_cube_count=2,
            cube_candidates=(2,),
            strategies=("static", "activity"),
            prerun_budget_ms=17,
            base_cube_timeout_ms=100,
            min_exploration_samples=1,
        )
        online = OnlineCubingPolicyStore(root / "online.sqlite3", policy)
        selected = None
        for index in range(4):
            candidate = online.decide(
                f"guided-{index}", family, activity_available=True
            )
            if candidate["arm"] == "activity":
                selected = candidate
                break
            _observe(online, candidate)
        assert selected is not None
        proof_store = IncrementalProofStore(root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        calls: list[tuple[int, str, str]] = []
        backend = PartitioningQfbvBackend(
            FakeQueryStore(),
            ProofPrefixPartitionStore(root / "partitions"),
            PartitionExecutionStore(root / "executions"),
            proof_store,
            checker,
            lambda _index: PrerunBackend(calls),
            capabilities=normalize_qfbv_capabilities({"incremental": True}),
            cube_count=2,
            policy=PartitionExecutionPolicy(
                parallelism=1,
                cube_timeout_ms=100,
                task_lease_ms=500,
            ),
            source_worker="f453-test",
            parent_lease_seconds=5.0,
            online_policy_store=online,
            online_activity_available=True,
            prerun_backend_factory=lambda _index: PrerunBackend(calls),
        )
        executor = Executor()
        backend.executor = executor
        result = backend(
            SimpleNamespace(
                query_id=selected["query_id"],
                token=1,
                timeout_ms=100,
                input_hex="00",
            )
        )
        backend.close()
        assert calls[0] == (17, selected["query_id"], "run")
        assert calls[-1] == (0, "", "close")
        assert executor.certificate["selection_source"] == "static-input-fallback"
        assert executor.policy.cube_timeout_ms == 99
        assert result["backend_online_cubing_decision"]["arm"] == "activity"
        outcome = result["backend_online_cubing_outcome"]
        assert outcome["partition_executed"] is True
        assert outcome["activity_receipts"] == 0
        assert outcome["prerun_elapsed_us"] > 0


def test_equal_budget_online_cubing_oracle_is_executable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "oracle.json"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "benchmark/check_qfbv_online_cubing_oracles.py"),
                "--rounds",
                "3",
                "--cube-candidates",
                "2,4,8",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        payload = json.loads(output.read_text(encoding="ascii"))
        assert payload["status"] == "pass"
        assert payload["total_trials"] == 9
        assert payload["budget_contract"] == {
            "configured_cpu_budget_ms": 8000,
            "base_cube_timeout_ms": 1000,
            "cube_count_is_normalized_within_total_cpu_budget": True,
            "guided_prerun_is_deducted_from_total_cpu_budget": True,
            "same_formula_family": True,
            "same_rounds_per_arm": True,
        }
        assert {
            arm: payload["arms"][arm]["samples"] for arm in ONLINE_CUBING_ARMS
        } == {"static": 3, "activity": 3, "cost": 3}
