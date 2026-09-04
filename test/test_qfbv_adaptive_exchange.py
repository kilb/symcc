from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import copy
import hashlib
import random
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from qfbv_adaptive_exchange import (  # noqa: E402
    ADAPTIVE_EXCHANGE_PROTOCOL,
    AdaptiveExchangeError,
    AdaptiveProofCandidate,
    AdaptiveProofController,
    AdaptiveProofObservation,
    AdaptiveProofPolicy,
    verify_admission_decision,
    verify_adaptive_stream_result,
    verify_controller_snapshot,
)
from qfbv_multirank_evaluation import content_digest  # noqa: E402
from check_qfbv_adaptive_exchange_oracle import (  # noqa: E402
    verify_oracle_result,
)


def _candidate(**changes: int | str) -> AdaptiveProofCandidate:
    values: dict[str, int | str] = {
        "record_sha256": "a" * 64,
        "formula_sha256": "b" * 64,
        "stream_id": "c" * 64,
        "source_worker": "publisher-1",
        "event_sequence": 7,
        "clause_literals": 3,
        "proof_steps": 2,
        "propagation_count": 8,
        "checker_elapsed_us": 25,
        "retry_count": 0,
    }
    values.update(changes)
    return AdaptiveProofCandidate(**values)


def _observation(**changes: int | bool) -> AdaptiveProofObservation:
    values: dict[str, int | bool] = {
        "solve_generation": 1,
        "solving": True,
        "solve_age_ms": 20,
        "remaining_ms": 980,
        "queue_depth": 0,
        "ack_depth": 0,
        "deferred_depth": 0,
    }
    values.update(changes)
    return AdaptiveProofObservation(**values)


def test_policy_is_bounded_and_has_stable_identity() -> None:
    policy = AdaptiveProofPolicy.from_mapping(
        {"high_watermark_permille": 500, "max_deferred": 9},
        queue_capacity=8,
    )
    assert policy.protocol == ADAPTIVE_EXCHANGE_PROTOCOL
    assert policy.queue_capacity == 8
    assert policy.high_watermark_permille == 500
    assert policy.max_deferred == 9
    assert len(policy.sha256) == 64
    assert policy.sha256 == AdaptiveProofPolicy.from_mapping(
        {"high_watermark_permille": 500, "max_deferred": 9},
        queue_capacity=8,
    ).sha256

    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofPolicy.from_mapping({"unknown": 1}, queue_capacity=8)
    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofPolicy.from_mapping(
            {"high_watermark_permille": True}, queue_capacity=8
        )
    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofPolicy.from_mapping(
            {"high_watermark_permille": 500.5}, queue_capacity=8
        )
    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofPolicy.from_mapping(
            {"max_decisions": "4096"}, queue_capacity=8
        )
    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofPolicy.from_mapping({}, queue_capacity=0)
    with pytest.raises(AdaptiveExchangeError):
        AdaptiveProofCandidate(
            **{**_candidate().as_dict(), "source_worker": None}
        )


def test_decisions_are_replayable_and_fail_closed_on_tamper() -> None:
    policy = AdaptiveProofPolicy(queue_capacity=4)
    controller = AdaptiveProofController(policy)
    admitted = controller.consider(_candidate(), _observation())
    assert admitted["action"] == "admit"
    assert verify_admission_decision(admitted, policy=policy) == "admit"

    deferred = controller.consider(
        _candidate(record_sha256="d" * 64),
        _observation(queue_depth=3),
    )
    assert deferred["action"] == "defer"
    assert "queue-high-watermark" in deferred["reasons"]

    inactive = controller.consider(
        _candidate(record_sha256="e" * 64),
        _observation(solving=False),
    )
    assert inactive["action"] == "defer"
    assert "solver-inactive" in inactive["reasons"]

    tampered = copy.deepcopy(admitted)
    tampered["score"] += 1
    with pytest.raises(AdaptiveExchangeError):
        verify_admission_decision(tampered, policy=policy)


def test_hard_bounds_and_retry_limit_reject_without_queueing() -> None:
    policy = AdaptiveProofPolicy(
        queue_capacity=4,
        max_clause_literals=8,
        max_retries=2,
        max_deferred=2,
    )
    controller = AdaptiveProofController(policy)
    too_long = controller.consider(
        _candidate(clause_literals=9), _observation()
    )
    exhausted = controller.consider(
        _candidate(record_sha256="d" * 64, retry_count=2), _observation()
    )
    full = controller.consider(
        _candidate(record_sha256="e" * 64),
        _observation(deferred_depth=2),
    )
    assert too_long["action"] == "reject"
    assert exhausted["action"] == "reject"
    assert full["action"] == "reject"


def test_delivery_and_backpressure_feedback_are_sealed() -> None:
    policy = AdaptiveProofPolicy(queue_capacity=4)
    controller = AdaptiveProofController(policy)
    first = controller.consider(_candidate(), _observation())
    controller.observe_delivery(first["decision_sha256"], latency_us=125)
    second = controller.consider(
        _candidate(record_sha256="d" * 64), _observation()
    )
    controller.observe_backpressure(second["decision_sha256"])
    third = controller.consider(
        _candidate(record_sha256="e" * 64), _observation()
    )
    controller.observe_expired(third["decision_sha256"])

    snapshot = controller.snapshot()
    verified = verify_controller_snapshot(snapshot, policy=policy)
    source = verified["sources"]["publisher-1"]
    assert source == {
        "verified": 3,
        "admit_decisions": 3,
        "defer_decisions": 0,
        "reject_decisions": 0,
        "delivered": 1,
        "backpressure": 1,
        "expired": 1,
        "ack_latency_total_us": 125,
    }
    assert snapshot["next_decision_ordinal"] == 4

    tampered = copy.deepcopy(snapshot)
    tampered["sources"]["publisher-1"]["delivered"] = 2
    with pytest.raises(AdaptiveExchangeError):
        verify_controller_snapshot(tampered, policy=policy)


def test_decision_budget_fails_closed() -> None:
    policy = AdaptiveProofPolicy(queue_capacity=4, max_decisions=2)
    controller = AdaptiveProofController(policy)
    first = controller.consider(_candidate(), _observation())
    second = controller.consider(
        _candidate(record_sha256="d" * 64), _observation()
    )
    assert not controller.can_decide
    with pytest.raises(AdaptiveExchangeError, match="decision budget"):
        controller.consider(
            _candidate(record_sha256="e" * 64), _observation()
        )
    controller.observe_expired(first["decision_sha256"])
    controller.observe_expired(second["decision_sha256"])
    next_stream = controller.consider(
        _candidate(record_sha256="f" * 64, stream_id="1" * 64),
        _observation(),
    )
    assert next_stream["decision_ordinal"] == 3


def test_stream_change_requires_quiescent_feedback() -> None:
    controller = AdaptiveProofController(AdaptiveProofPolicy(queue_capacity=2))
    decision = controller.consider(_candidate(), _observation())
    with pytest.raises(AdaptiveExchangeError, match="pending feedback"):
        controller.begin_stream("1" * 64)
    controller.observe_expired(decision["decision_sha256"])
    controller.begin_stream("1" * 64)
    assert controller.can_decide


def test_randomized_fixed_point_decisions_replay_and_conserve_feedback() -> None:
    rng = random.Random(0xF435)
    policy = AdaptiveProofPolicy(
        queue_capacity=17,
        max_deferred=31,
        max_retries=8,
        max_decisions=512,
        max_clause_literals=64,
    )
    controller = AdaptiveProofController(policy)
    for ordinal in range(256):
        record = hashlib.sha256(f"record-{ordinal}".encode("ascii")).hexdigest()
        candidate = _candidate(
            record_sha256=record,
            source_worker=f"source-{ordinal % 7}",
            event_sequence=ordinal + 1,
            clause_literals=rng.randint(1, 80),
            proof_steps=rng.randint(0, 5000),
            propagation_count=rng.randint(0, 20_000),
            checker_elapsed_us=rng.randint(0, 5000),
            retry_count=rng.randint(0, 8),
        )
        observation = _observation(
            solve_generation=rng.randint(0, 4),
            solving=bool(rng.getrandbits(1)),
            solve_age_ms=rng.randint(0, 1000),
            remaining_ms=rng.randint(0, 1000),
            queue_depth=rng.randint(0, 17),
            ack_depth=rng.randint(0, 17),
            deferred_depth=rng.randint(0, 31),
        )
        decision = controller.consider(candidate, observation)
        assert verify_admission_decision(decision, policy=policy) == decision["action"]
        if decision["action"] == "admit":
            outcome = rng.choice(("delivered", "backpressure", "expired"))
            if outcome == "delivered":
                controller.observe_delivery(
                    decision["decision_sha256"], latency_us=rng.randint(0, 100_000)
                )
            elif outcome == "backpressure":
                controller.observe_backpressure(decision["decision_sha256"])
            else:
                controller.observe_expired(decision["decision_sha256"])
    snapshot = verify_controller_snapshot(controller.snapshot(), policy=policy)
    assert snapshot["totals"]["verified"] == 256
    assert snapshot["pending_feedback"] == 0


def test_feedback_is_exactly_once_and_rejects_unknown_decisions() -> None:
    controller = AdaptiveProofController(AdaptiveProofPolicy(queue_capacity=2))
    decision = controller.consider(_candidate(), _observation())
    controller.observe_delivery(decision["decision_sha256"], latency_us=3)
    with pytest.raises(AdaptiveExchangeError, match="no pending"):
        controller.observe_delivery(decision["decision_sha256"], latency_us=3)
    with pytest.raises(AdaptiveExchangeError, match="no pending"):
        controller.observe_expired("f" * 64)


def test_stream_trace_conserves_admit_outcomes_and_replays() -> None:
    policy = AdaptiveProofPolicy(queue_capacity=4)
    controller = AdaptiveProofController(policy)
    admitted = controller.consider(_candidate(), _observation())
    controller.observe_delivery(admitted["decision_sha256"], latency_us=50)
    deferred = controller.consider(
        _candidate(record_sha256="d" * 64),
        _observation(queue_depth=3),
    )
    raw = {
        "backend_realtime_adaptive_protocol": ADAPTIVE_EXCHANGE_PROTOCOL,
        "backend_realtime_adaptive_policy": policy.as_dict(),
        "backend_realtime_adaptive_policy_sha256": policy.sha256,
        "backend_realtime_adaptive_decisions": [admitted, deferred],
        "backend_realtime_adaptive_action_counts": {
            "admit": 1,
            "defer": 1,
            "reject": 0,
        },
        "backend_realtime_adaptive_retried": 0,
        "backend_realtime_adaptive_rejected": 0,
        "backend_realtime_adaptive_deferred_final": 1,
        "backend_realtime_adaptive_decision_budget_exhausted": False,
        "backend_realtime_adaptive_decision_budget_dropped": 0,
        "backend_realtime_adaptive_enqueued_decisions": {
            "a" * 64: admitted["decision_sha256"]
        },
        "backend_realtime_adaptive_controller_snapshot": controller.snapshot(),
    }
    verified = verify_adaptive_stream_result(
        raw,
        stream_id="c" * 64,
        delivered_records=("a" * 64,),
        authorized=1,
        backpressure=0,
    )
    assert verified["backend_realtime_adaptive_action_counts"]["defer"] == 1

    tampered = copy.deepcopy(raw)
    tampered["backend_realtime_adaptive_enqueued_decisions"]["e" * 64] = (
        tampered["backend_realtime_adaptive_enqueued_decisions"].pop("a" * 64)
    )
    with pytest.raises(AdaptiveExchangeError):
        verify_adaptive_stream_result(
            tampered,
            stream_id="c" * 64,
            delivered_records=("a" * 64,),
            authorized=1,
            backpressure=0,
        )

    wrong_suffix = copy.deepcopy(raw)
    wrong_suffix["backend_realtime_adaptive_controller_snapshot"] = (
        copy.deepcopy(controller.snapshot())
    )
    snapshot = wrong_suffix["backend_realtime_adaptive_controller_snapshot"]
    snapshot["next_decision_ordinal"] += 1
    snapshot["sources"]["later-source"] = {
        "verified": 1,
        "admit_decisions": 0,
        "defer_decisions": 1,
        "reject_decisions": 0,
        "delivered": 0,
        "backpressure": 0,
        "expired": 0,
        "ack_latency_total_us": 0,
    }
    snapshot["totals"]["verified"] += 1
    snapshot["totals"]["defer_decisions"] += 1
    snapshot_body = dict(snapshot)
    snapshot_body.pop("snapshot_sha256")
    snapshot["snapshot_sha256"] = content_digest(snapshot_body)
    with pytest.raises(AdaptiveExchangeError, match="snapshot suffix"):
        verify_adaptive_stream_result(
            wrong_suffix,
            stream_id="c" * 64,
            delivered_records=("a" * 64,),
            authorized=1,
            backpressure=0,
        )


def test_real_oracle_result_contract_rejects_tamper() -> None:
    body = {
        "schema": "symcc-f435-adaptive-proof-oracle-v1",
        "status": "pass",
        "records": 8,
        "queue_capacity": 2,
        "static": {
            "published": 8,
            "events": 8,
            "settled_candidates": 8,
            "duplicate_clauses": 0,
            "delivered": 2,
            "backpressure": 6,
        },
        "adaptive": {
            "published": 8,
            "events": 8,
            "settled_candidates": 8,
            "duplicate_clauses": 0,
            "delivered": 8,
            "pending": 0,
            "backpressure": 0,
            "timed_out": False,
            "solve_error": "",
            "adaptive": {
                "backend_realtime_adaptive_protocol": (
                    ADAPTIVE_EXCHANGE_PROTOCOL
                )
            },
        },
        "delivery_gain": 6,
    }
    body["artifact_sha256"] = content_digest(body)
    assert verify_oracle_result(body)["delivery_gain"] == 6
    tampered = copy.deepcopy(body)
    tampered["delivery_gain"] = 4
    with pytest.raises(ValueError, match="identity"):
        verify_oracle_result(tampered)
