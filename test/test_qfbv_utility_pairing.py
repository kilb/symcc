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

from qfbv_utility_pairing import (  # noqa: E402
    UTILITY_PAIRING_PROTOCOL,
    UtilityPairingCandidate,
    UtilityPairingController,
    UtilityPairingError,
    UtilityPairingPolicy,
    formula_family_sha256,
    validate_pairing_worker_identity,
    verify_pairing_decision,
    verify_pairing_outcome,
    verify_pairing_snapshot,
    verify_pairing_stream_result,
)


def _certificate(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "symcc-qfbv-bitblast-cnf-v1",
        "query_id": "query-a",
        "formula_sha256": "a" * 64,
        "assumption_sha256": "b" * 64,
        "cnf_sha256": "c" * 64,
        "input_map_sha256": "d" * 64,
        "certificate_sha256": "e" * 64,
        "root_count": 3,
        "node_count": 15,
        "variable_count": 33,
        "clause_count": 71,
        "input_offset_count": 2,
        "maximum_width": 8,
        "operator_counts": {"equal": 3, "land": 2, "read": 2},
        "activation_guarded": True,
        "bit_order": "least-significant-first",
        "cnf_protocol": "deterministic-tseitin-ripple-restoring-v1",
    }
    value.update(changes)
    return value


def _candidate(index: int = 0, **changes: object) -> UtilityPairingCandidate:
    values: dict[str, object] = {
        "record_sha256": hashlib.sha256(
            f"record-{index}".encode("ascii")
        ).hexdigest(),
        "stream_id": "1" * 64,
        "publisher_worker": "publisher-a",
        "consumer_worker": "consumer-a",
        "formula_family_sha256": formula_family_sha256(_certificate()),
        "event_sequence": index + 1,
        "event_lag": 0,
        "checker_elapsed_us": 0,
    }
    values.update(changes)
    return UtilityPairingCandidate(**values)


def _reseal(value: dict, field: str) -> None:
    body = {key: item for key, item in value.items() if key != field}
    encoded = json.dumps(
        body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    value[field] = hashlib.sha256(encoded).hexdigest()


def _result(
    controller: UtilityPairingController,
    decisions: list[dict],
    outcomes: list[dict],
) -> dict:
    return {
        "backend_realtime_pairing_protocol": UTILITY_PAIRING_PROTOCOL,
        "backend_realtime_pairing_policy": controller.policy.as_dict(),
        "backend_realtime_pairing_policy_sha256": controller.policy.sha256,
        "backend_realtime_pairing_formula_family_sha256": (
            _candidate().formula_family_sha256
        ),
        "backend_realtime_pairing_consumer_worker": "consumer-a",
        "backend_realtime_pairing_decisions": decisions,
        "backend_realtime_pairing_outcomes": outcomes,
        "backend_realtime_pairing_action_counts": {
            action: sum(item["action"] == action for item in decisions)
            for action in ("admit", "suppress")
        },
        "backend_realtime_pairing_phase_counts": {
            phase: sum(item["phase"] == phase for item in decisions)
            for phase in ("explore", "exploit")
        },
        "backend_realtime_pairing_outcome_counts": {
            name: sum(item["outcome"] == name for item in outcomes)
            for name in (
                "unit",
                "conflict",
                "unactivated",
                "backpressure",
                "expired",
            )
        },
        "backend_realtime_pairing_controller_snapshot": controller.snapshot(),
    }


def test_formula_family_is_value_insensitive_but_shape_sensitive() -> None:
    first = formula_family_sha256(_certificate())
    second = formula_family_sha256(_certificate(
        query_id="query-b",
        formula_sha256="f" * 64,
        cnf_sha256="0" * 64,
        certificate_sha256="9" * 64,
    ))
    assert first == second
    assert first != formula_family_sha256(_certificate(clause_count=72))
    assert first != formula_family_sha256(
        _certificate(operator_counts={"equal": 4, "land": 2, "read": 2})
    )
    with pytest.raises(UtilityPairingError):
        formula_family_sha256(_certificate(root_count=True))
    with pytest.raises(UtilityPairingError):
        formula_family_sha256(_certificate(operator_counts={"equal": 1.5}))


def test_policy_and_candidate_are_strict_and_stably_identified() -> None:
    policy = UtilityPairingPolicy.from_mapping({
        "min_exploration_samples": 3,
        "refresh_after_events": 17,
    })
    assert policy.min_exploration_samples == 3
    assert policy.refresh_after_events == 17
    assert len(policy.sha256) == 64
    assert policy.sha256 == UtilityPairingPolicy.from_sealed(
        policy.as_dict()
    ).sha256
    assert len(_candidate().pair_sha256) == 64

    with pytest.raises(UtilityPairingError):
        UtilityPairingPolicy.from_mapping({"unknown": 1})
    with pytest.raises(UtilityPairingError):
        UtilityPairingPolicy.from_mapping({"max_pairs": True})
    with pytest.raises(UtilityPairingError):
        UtilityPairingPolicy.from_mapping({"max_events": 1})
    with pytest.raises(UtilityPairingError):
        _candidate(publisher_worker="")
    with pytest.raises(UtilityPairingError):
        _candidate(event_lag=-1)
    unicode_worker = "consumer-\u8282\u70b9"
    assert validate_pairing_worker_identity(unicode_worker) == unicode_worker
    with pytest.raises(UtilityPairingError):
        validate_pairing_worker_identity("x" * 257)
    with pytest.raises(UtilityPairingError):
        validate_pairing_worker_identity("consumer\nworker")


def test_low_sample_exploration_then_suppresses_censored_low_utility() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy())
    decisions = []
    for index in range(2):
        decision = controller.consider(_candidate(index))
        decisions.append(decision)
        assert decision["phase"] == "explore"
        assert decision["action"] == "admit"
        outcome = controller.observe(
            decision["decision_sha256"], outcome="unactivated"
        )
        assert verify_pairing_outcome(outcome, policy=controller.policy) == (
            "unactivated"
        )

    suppressed = controller.consider(_candidate(2))
    assert suppressed["phase"] == "exploit"
    assert suppressed["action"] == "suppress"
    assert suppressed["score"] < controller.policy.min_score
    assert verify_pairing_decision(
        suppressed, policy=controller.policy
    ) == "suppress"
    snapshot = verify_pairing_snapshot(
        controller.snapshot(), policy=controller.policy
    )
    pair = snapshot["pairs"][_candidate().pair_sha256]["feedback"]
    assert pair["unactivated"] == 2
    assert pair["suppressed"] == 1
    assert pair["reward_total"] == 1000


def test_activated_pair_is_exploited_and_unactivated_is_not_hard_failure() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy())
    for index, kind in enumerate(("unit", "conflict")):
        decision = controller.consider(_candidate(index))
        controller.observe(decision["decision_sha256"], outcome=kind)
    exploited = controller.consider(_candidate(2, checker_elapsed_us=50))
    assert exploited["phase"] == "exploit"
    assert exploited["action"] == "admit"
    assert exploited["score_components"]["historical_utility"] == 4750

    controller.observe(exploited["decision_sha256"], outcome="unactivated")
    next_decision = controller.consider(_candidate(3))
    assert next_decision["action"] == "admit"


def test_pair_score_retains_historical_checker_and_staleness_cost() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy())
    for index in range(2):
        decision = controller.consider(_candidate(
            index,
            checker_elapsed_us=3000,
            event_lag=128,
        ))
        controller.observe(decision["decision_sha256"], outcome="unit")
    decision = controller.consider(_candidate(2))
    assert decision["score_components"]["checker_penalty"] == 2000
    assert decision["score_components"]["staleness_penalty"] == 1328


def test_periodic_refresh_reexplores_a_suppressed_pair() -> None:
    policy = UtilityPairingPolicy(refresh_after_events=16, max_pairs=32)
    controller = UtilityPairingController(policy)
    for index in range(2):
        decision = controller.consider(_candidate(index))
        controller.observe(decision["decision_sha256"], outcome="unactivated")
    assert controller.consider(_candidate(2))["action"] == "suppress"

    for index in range(8):
        other = _candidate(
            100 + index,
            publisher_worker=f"publisher-{index}",
        )
        decision = controller.consider(other)
        controller.observe(decision["decision_sha256"], outcome="unit")
    refreshed = controller.consider(_candidate(3))
    assert refreshed["phase"] == "explore"
    assert refreshed["action"] == "admit"
    assert refreshed["reasons"] == ["periodic-refresh"]


def test_snapshot_restore_preserves_policy_state_and_ordinals() -> None:
    policy = UtilityPairingPolicy()
    controller = UtilityPairingController(policy)
    decision = controller.consider(_candidate())
    controller.observe(decision["decision_sha256"], outcome="unit")
    snapshot = controller.snapshot()
    restored = UtilityPairingController.from_snapshot(policy, snapshot)
    next_decision = restored.consider(
        _candidate(1, stream_id="2" * 64)
    )
    assert next_decision["decision_ordinal"] == 2
    assert next_decision["controller_event_ordinal"] == 3

    pending = UtilityPairingController(policy)
    pending.consider(_candidate())
    with pytest.raises(UtilityPairingError, match="pending"):
        UtilityPairingController.from_snapshot(policy, pending.snapshot())


def test_outcome_and_snapshot_reject_validly_resealed_semantic_tamper() -> None:
    policy = UtilityPairingPolicy()
    controller = UtilityPairingController(policy)
    decision = controller.consider(_candidate())
    outcome = controller.observe(decision["decision_sha256"], outcome="unit")

    extra = copy.deepcopy(outcome)
    extra["unexpected"] = 1
    _reseal(extra, "outcome_sha256")
    with pytest.raises(UtilityPairingError, match="shape"):
        verify_pairing_outcome(extra, policy=policy)

    snapshot = controller.snapshot()
    pair = next(iter(snapshot["pairs"].values()))
    pair["feedback"]["reward_total"] += 1
    snapshot["totals"]["reward_total"] += 1
    _reseal(snapshot, "snapshot_sha256")
    with pytest.raises(UtilityPairingError, match="reward total"):
        verify_pairing_snapshot(snapshot, policy=policy)


def test_feedback_is_exactly_once_and_stream_change_requires_quiescence() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy())
    decision = controller.consider(_candidate())
    with pytest.raises(UtilityPairingError, match="outcome"):
        controller.observe(decision["decision_sha256"], outcome="unknown")
    assert controller.snapshot()["pending_outcomes"] == 1
    with pytest.raises(UtilityPairingError, match="pending"):
        controller.begin_stream("2" * 64)
    controller.observe(decision["decision_sha256"], outcome="expired")
    with pytest.raises(UtilityPairingError, match="no pending"):
        controller.observe(decision["decision_sha256"], outcome="expired")
    controller.begin_stream("2" * 64)


def test_complete_stream_trace_replays_activity_and_fails_closed() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy())
    decisions = [controller.consider(_candidate(0))]
    outcomes = [controller.observe(
        decisions[0]["decision_sha256"], outcome="unit"
    )]
    decisions.append(controller.consider(_candidate(1)))
    outcomes.append(controller.observe(
        decisions[1]["decision_sha256"], outcome="unactivated"
    ))
    decisions.append(controller.consider(_candidate(2)))
    assert decisions[-1]["action"] == "admit"
    outcomes.append(controller.observe(
        decisions[-1]["decision_sha256"], outcome="backpressure"
    ))
    raw = _result(controller, decisions, outcomes)
    verified = verify_pairing_stream_result(
        raw,
        stream_id="1" * 64,
        consumer_worker="consumer-a",
        formula_family=_candidate().formula_family_sha256,
        delivered_records=[
            decisions[0]["candidate"]["record_sha256"],
            decisions[1]["candidate"]["record_sha256"],
        ],
        activity_kinds={
            decisions[0]["candidate"]["record_sha256"]: "unit",
        },
    )
    assert verified["backend_realtime_pairing_outcome_counts"] == {
        "unit": 1,
        "conflict": 0,
        "unactivated": 1,
        "backpressure": 1,
        "expired": 0,
    }

    tampered = copy.deepcopy(raw)
    tampered["backend_realtime_pairing_outcomes"][0]["outcome"] = "conflict"
    with pytest.raises(UtilityPairingError):
        verify_pairing_stream_result(
            tampered,
            stream_id="1" * 64,
            consumer_worker="consumer-a",
            formula_family=_candidate().formula_family_sha256,
            delivered_records=[
                decisions[0]["candidate"]["record_sha256"],
                decisions[1]["candidate"]["record_sha256"],
            ],
            activity_kinds={
                decisions[0]["candidate"]["record_sha256"]: "unit",
            },
        )


def test_randomized_event_interleavings_replay_exactly() -> None:
    rng = random.Random(0xF437)
    policy = UtilityPairingPolicy(
        min_score=-20_000,
        max_pairs=32,
        max_events=4096,
    )
    controller = UtilityPairingController(policy)
    decisions: list[dict] = []
    outcomes: list[dict] = []
    pending: list[dict] = []
    delivered: list[str] = []
    activity: dict[str, str] = {}
    for index in range(256):
        decision = controller.consider(_candidate(
            index,
            publisher_worker=f"publisher-{index % 11}",
            event_lag=rng.randint(0, 500),
            checker_elapsed_us=rng.randint(0, 5000),
        ))
        decisions.append(decision)
        assert decision["action"] == "admit"
        pending.append(decision)
        if len(pending) >= 5 or rng.randrange(3) == 0:
            selected = pending.pop(rng.randrange(len(pending)))
            kind = rng.choice(
                ("unit", "conflict", "unactivated", "backpressure", "expired")
            )
            outcomes.append(controller.observe(
                selected["decision_sha256"], outcome=kind
            ))
            record = selected["candidate"]["record_sha256"]
            if kind in {"unit", "conflict", "unactivated"}:
                delivered.append(record)
                if kind != "unactivated":
                    activity[record] = kind
    while pending:
        selected = pending.pop()
        outcomes.append(controller.observe(
            selected["decision_sha256"], outcome="expired"
        ))
    verified = verify_pairing_stream_result(
        _result(controller, decisions, outcomes),
        stream_id="1" * 64,
        consumer_worker="consumer-a",
        formula_family=_candidate().formula_family_sha256,
        delivered_records=delivered,
        activity_kinds=activity,
    )
    assert verified["backend_realtime_pairing_controller_snapshot"][
        "pending_outcomes"
    ] == 0
    assert sum(
        verified["backend_realtime_pairing_outcome_counts"].values()
    ) == 256


def test_pair_budget_and_event_budget_fail_closed() -> None:
    controller = UtilityPairingController(UtilityPairingPolicy(
        max_pairs=1,
        max_events=2,
    ))
    first = controller.consider(_candidate())
    controller.observe(first["decision_sha256"], outcome="expired")
    with pytest.raises(UtilityPairingError, match="event budget"):
        controller.consider(_candidate(1))

    pairs = UtilityPairingController(UtilityPairingPolicy(max_pairs=1))
    first = pairs.consider(_candidate())
    pairs.observe(first["decision_sha256"], outcome="expired")
    with pytest.raises(UtilityPairingError, match="pair budget"):
        pairs.consider(_candidate(1, publisher_worker="publisher-b"))
