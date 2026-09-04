#!/usr/bin/env python3
"""Replayable utility-aware publisher/consumer pairing for checked QF_BV."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


UTILITY_PAIRING_PROTOCOL = "symcc-qfbv-utility-aware-worker-pairing-v1"
PAIRING_POLICY_SCHEMA = "symcc-qfbv-utility-pairing-policy-v1"
PAIRING_DECISION_SCHEMA = "symcc-qfbv-utility-pairing-decision-v1"
PAIRING_OUTCOME_SCHEMA = "symcc-qfbv-utility-pairing-outcome-v1"
PAIRING_SNAPSHOT_SCHEMA = "symcc-qfbv-utility-pairing-snapshot-v1"
MAX_PAIRS = 16_384
MAX_EVENTS = 2_000_000
_HEX64 = re.compile(r"[0-9a-f]{64}")


class UtilityPairingError(ValueError):
    """A pairing policy, decision, outcome, or snapshot is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise UtilityPairingError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise UtilityPairingError(f"{name} must be in [{lower}, {upper}]")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise UtilityPairingError(f"{name} must be a lowercase SHA-256")
    return value


def _worker(value: Any, name: str) -> str:
    if type(value) is not str:
        raise UtilityPairingError(f"{name} identity is invalid")
    encoded = value.encode("utf-8")
    if (
        not value
        or len(encoded) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UtilityPairingError(f"{name} identity is invalid")
    return value


def validate_pairing_worker_identity(
    value: Any, name: str = "pairing worker"
) -> str:
    """Validate one worker identity at every persistence/protocol boundary."""
    return _worker(value, name)


def formula_family_sha256(certificate: Mapping[str, Any]) -> str:
    """Return a value-insensitive, deterministic QF_BV/CNF family identity."""
    if not isinstance(certificate, Mapping):
        raise UtilityPairingError("formula-family certificate must be an object")
    integer_fields = (
        "root_count",
        "node_count",
        "variable_count",
        "clause_count",
        "input_offset_count",
        "maximum_width",
    )
    shape = {
        field: _integer(
            certificate.get(field), f"formula-family {field}", 0, 100_000_000
        )
        for field in integer_fields
    }
    operators_raw = certificate.get("operator_counts")
    if not isinstance(operators_raw, Mapping) or len(operators_raw) > 256:
        raise UtilityPairingError("formula-family operator counts are invalid")
    operators: dict[str, int] = {}
    for raw_name, raw_count in operators_raw.items():
        if (
            type(raw_name) is not str
            or not raw_name
            or len(raw_name) > 64
            or not raw_name.isascii()
        ):
            raise UtilityPairingError("formula-family operator name is invalid")
        operators[raw_name] = _integer(
            raw_count, "formula-family operator count", 0, 100_000_000
        )
    protocol_fields: dict[str, Any] = {}
    for field in ("schema", "cnf_protocol", "bit_order", "activation_guarded"):
        value = certificate.get(field)
        if field == "activation_guarded":
            if type(value) is not bool:
                raise UtilityPairingError(
                    "formula-family activation guard must be boolean"
                )
        elif type(value) is not str or not value or len(value) > 128:
            raise UtilityPairingError(f"formula-family {field} is invalid")
        protocol_fields[field] = value
    body = {
        "schema": "symcc-qfbv-formula-family-v1",
        **shape,
        "operator_counts": dict(sorted(operators.items())),
        **protocol_fields,
    }
    return _content_digest(body)


@dataclass(frozen=True)
class UtilityPairingPolicy:
    min_exploration_samples: int = 2
    refresh_after_events: int = 128
    min_score: int = 3250
    uncertainty_scale: int = 2000
    checker_reference_us: int = 1000
    event_lag_reference: int = 64
    unit_reward: int = 4500
    conflict_reward: int = 5000
    unactivated_reward: int = 500
    backpressure_reward: int = -500
    expired_reward: int = -250
    max_pairs: int = 4096
    max_events: int = 1_000_000

    def __post_init__(self) -> None:
        _integer(self.min_exploration_samples, "pairing exploration samples", 1, 64)
        _integer(self.refresh_after_events, "pairing refresh interval", 1, 1_000_000)
        _integer(self.min_score, "pairing minimum score", -20_000, 20_000)
        _integer(self.uncertainty_scale, "pairing uncertainty scale", 0, 20_000)
        _integer(
            self.checker_reference_us, "pairing checker reference", 1, 60_000_000
        )
        _integer(self.event_lag_reference, "pairing event-lag reference", 1, 1_000_000)
        for value, name in (
            (self.unit_reward, "unit"),
            (self.conflict_reward, "conflict"),
            (self.unactivated_reward, "unactivated"),
            (self.backpressure_reward, "backpressure"),
            (self.expired_reward, "expired"),
        ):
            _integer(value, f"pairing {name} reward", -20_000, 20_000)
        _integer(self.max_pairs, "pairing pair budget", 1, MAX_PAIRS)
        _integer(self.max_events, "pairing event budget", 2, MAX_EVENTS)

    def as_config(self) -> dict[str, int]:
        return {
            "min_exploration_samples": self.min_exploration_samples,
            "refresh_after_events": self.refresh_after_events,
            "min_score": self.min_score,
            "uncertainty_scale": self.uncertainty_scale,
            "checker_reference_us": self.checker_reference_us,
            "event_lag_reference": self.event_lag_reference,
            "unit_reward": self.unit_reward,
            "conflict_reward": self.conflict_reward,
            "unactivated_reward": self.unactivated_reward,
            "backpressure_reward": self.backpressure_reward,
            "expired_reward": self.expired_reward,
            "max_pairs": self.max_pairs,
            "max_events": self.max_events,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PAIRING_POLICY_SCHEMA,
            "protocol": UTILITY_PAIRING_PROTOCOL,
            **self.as_config(),
        }

    @property
    def sha256(self) -> str:
        return _content_digest(self.as_dict())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UtilityPairingPolicy":
        if not isinstance(raw, Mapping):
            raise UtilityPairingError("pairing policy must be an object")
        defaults = cls()
        allowed = set(defaults.as_config())
        unknown = set(raw) - allowed
        if unknown:
            raise UtilityPairingError(
                "unknown pairing policy option: " + sorted(unknown)[0]
            )
        values = defaults.as_config()
        values.update(raw)
        return cls(**values)

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "UtilityPairingPolicy":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != PAIRING_POLICY_SCHEMA
            or raw.get("protocol") != UTILITY_PAIRING_PROTOCOL
        ):
            raise UtilityPairingError("sealed pairing policy scope changed")
        if set(raw) != {"schema", "protocol", *cls().as_config()}:
            raise UtilityPairingError("sealed pairing policy shape changed")
        policy = cls.from_mapping(
            {key: raw[key] for key in cls().as_config()}
        )
        if policy.as_dict() != dict(raw):
            raise UtilityPairingError("sealed pairing policy changed")
        return policy


@dataclass(frozen=True)
class UtilityPairingCandidate:
    record_sha256: str
    stream_id: str
    publisher_worker: str
    consumer_worker: str
    formula_family_sha256: str
    event_sequence: int
    event_lag: int
    checker_elapsed_us: int

    def __post_init__(self) -> None:
        _digest(self.record_sha256, "pairing record")
        _digest(self.stream_id, "pairing stream")
        _worker(self.publisher_worker, "publisher worker")
        _worker(self.consumer_worker, "consumer worker")
        _digest(self.formula_family_sha256, "pairing formula family")
        _integer(self.event_sequence, "pairing event sequence", 1, (1 << 63) - 1)
        _integer(self.event_lag, "pairing event lag", 0, (1 << 63) - 1)
        _integer(
            self.checker_elapsed_us,
            "pairing checker elapsed time",
            0,
            (1 << 63) - 1,
        )

    @property
    def pair_sha256(self) -> str:
        return _content_digest({
            "protocol": UTILITY_PAIRING_PROTOCOL,
            "publisher_worker": self.publisher_worker,
            "consumer_worker": self.consumer_worker,
            "formula_family_sha256": self.formula_family_sha256,
        })

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_sha256": self.record_sha256,
            "stream_id": self.stream_id,
            "publisher_worker": self.publisher_worker,
            "consumer_worker": self.consumer_worker,
            "formula_family_sha256": self.formula_family_sha256,
            "pair_sha256": self.pair_sha256,
            "event_sequence": self.event_sequence,
            "event_lag": self.event_lag,
            "checker_elapsed_us": self.checker_elapsed_us,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UtilityPairingCandidate":
        expected = {
            "record_sha256",
            "stream_id",
            "publisher_worker",
            "consumer_worker",
            "formula_family_sha256",
            "pair_sha256",
            "event_sequence",
            "event_lag",
            "checker_elapsed_us",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise UtilityPairingError("pairing candidate shape changed")
        candidate = cls(**{key: raw[key] for key in expected - {"pair_sha256"}})
        if raw.get("pair_sha256") != candidate.pair_sha256:
            raise UtilityPairingError("pairing candidate identity changed")
        return candidate


@dataclass
class PairFeedback:
    decisions: int = 0
    explore_decisions: int = 0
    exploit_decisions: int = 0
    admitted: int = 0
    suppressed: int = 0
    unit: int = 0
    conflict: int = 0
    unactivated: int = 0
    backpressure: int = 0
    expired: int = 0
    reward_total: int = 0
    checker_total_us: int = 0
    event_lag_total: int = 0
    last_feedback_event_ordinal: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "decisions": self.decisions,
            "explore_decisions": self.explore_decisions,
            "exploit_decisions": self.exploit_decisions,
            "admitted": self.admitted,
            "suppressed": self.suppressed,
            "unit": self.unit,
            "conflict": self.conflict,
            "unactivated": self.unactivated,
            "backpressure": self.backpressure,
            "expired": self.expired,
            "reward_total": self.reward_total,
            "checker_total_us": self.checker_total_us,
            "event_lag_total": self.event_lag_total,
            "last_feedback_event_ordinal": self.last_feedback_event_ordinal,
        }

    @property
    def outcomes(self) -> int:
        return (
            self.unit
            + self.conflict
            + self.unactivated
            + self.backpressure
            + self.expired
        )

    @property
    def delivered(self) -> int:
        return self.unit + self.conflict + self.unactivated

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PairFeedback":
        if not isinstance(raw, Mapping) or set(raw) != set(cls().as_dict()):
            raise UtilityPairingError("pairing feedback shape changed")
        values: dict[str, int] = {}
        for name, value in raw.items():
            lower = -((1 << 63) - 1) if name == "reward_total" else 0
            values[name] = _integer(
                value, f"pairing feedback {name}", lower, (1 << 63) - 1
            )
        result = cls(**values)
        if (
            result.explore_decisions + result.exploit_decisions
            != result.decisions
            or result.admitted + result.suppressed != result.decisions
            or result.outcomes > result.admitted
        ):
            raise UtilityPairingError("pairing feedback is inconsistent")
        return result


@dataclass
class _PairState:
    publisher_worker: str
    consumer_worker: str
    formula_family_sha256: str
    feedback: PairFeedback

    def as_dict(self) -> dict[str, Any]:
        return {
            "publisher_worker": self.publisher_worker,
            "consumer_worker": self.consumer_worker,
            "formula_family_sha256": self.formula_family_sha256,
            "feedback": self.feedback.as_dict(),
        }


def _score_components(
    policy: UtilityPairingPolicy,
    candidate: UtilityPairingCandidate,
    feedback: PairFeedback,
) -> dict[str, int]:
    samples = feedback.outcomes
    delivered = feedback.delivered
    activated = feedback.unit + feedback.conflict
    historical_utility = feedback.reward_total // max(1, samples)
    delivery_yield = (delivered + 1) * 1000 // (samples + 2)
    activation_yield = (activated + 1) * 1000 // (delivered + 2)
    # Integer fixed point approximates scale / sqrt(samples + 1) without
    # platform-dependent floating-point decisions.
    uncertainty_bonus = (
        policy.uncertainty_scale
        * 1024
        // math.isqrt((samples + 1) * 1024 * 1024)
    )
    observations = feedback.decisions + 1
    average_checker_us = (
        feedback.checker_total_us + candidate.checker_elapsed_us
    ) // observations
    average_event_lag = (
        feedback.event_lag_total + candidate.event_lag
    ) // observations
    checker_penalty = min(
        3000,
        average_checker_us * 1000 // policy.checker_reference_us,
    )
    staleness_penalty = min(
        2000,
        average_event_lag * 1000 // policy.event_lag_reference,
    )
    return {
        "historical_utility": historical_utility,
        "delivery_yield": delivery_yield,
        "activation_yield": activation_yield * 2,
        "uncertainty_bonus": uncertainty_bonus,
        "checker_penalty": checker_penalty,
        "staleness_penalty": staleness_penalty,
    }


def _decision_body(
    policy: UtilityPairingPolicy,
    candidate: UtilityPairingCandidate,
    feedback: PairFeedback,
    *,
    decision_ordinal: int,
    controller_event_ordinal: int,
) -> dict[str, Any]:
    components = _score_components(policy, candidate, feedback)
    score = (
        components["historical_utility"]
        + components["delivery_yield"]
        + components["activation_yield"]
        + components["uncertainty_bonus"]
        - components["checker_penalty"]
        - components["staleness_penalty"]
    )
    refresh_due = (
        feedback.last_feedback_event_ordinal > 0
        and controller_event_ordinal - feedback.last_feedback_event_ordinal
        >= policy.refresh_after_events
    )
    explore = feedback.outcomes < policy.min_exploration_samples or refresh_due
    if explore:
        action = "admit"
        phase = "explore"
        reasons = [
            "minimum-samples"
            if feedback.outcomes < policy.min_exploration_samples
            else "periodic-refresh"
        ]
    elif score >= policy.min_score:
        action = "admit"
        phase = "exploit"
        reasons = ["utility-confidence-admit"]
    else:
        action = "suppress"
        phase = "exploit"
        reasons = ["utility-below-threshold"]
    return {
        "schema": PAIRING_DECISION_SCHEMA,
        "protocol": UTILITY_PAIRING_PROTOCOL,
        "policy_sha256": policy.sha256,
        "decision_ordinal": _integer(
            decision_ordinal, "pairing decision ordinal", 1, MAX_EVENTS
        ),
        "controller_event_ordinal": _integer(
            controller_event_ordinal, "pairing controller event ordinal", 1, MAX_EVENTS
        ),
        "candidate": candidate.as_dict(),
        "pair_feedback": feedback.as_dict(),
        "score_components": components,
        "score": score,
        "threshold": policy.min_score,
        "phase": phase,
        "action": action,
        "reasons": reasons,
    }


def verify_pairing_decision(
    raw: Mapping[str, Any], *, policy: UtilityPairingPolicy
) -> str:
    if not isinstance(raw, Mapping):
        raise UtilityPairingError("pairing decision must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("decision_sha256", ""), "pairing decision")
    if _content_digest(body) != supplied:
        raise UtilityPairingError("pairing decision identity changed")
    if (
        body.get("schema") != PAIRING_DECISION_SCHEMA
        or body.get("protocol") != UTILITY_PAIRING_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
    ):
        raise UtilityPairingError("pairing decision scope changed")
    candidate = UtilityPairingCandidate.from_mapping(body.get("candidate", {}))
    feedback = PairFeedback.from_mapping(body.get("pair_feedback", {}))
    expected = _decision_body(
        policy,
        candidate,
        feedback,
        decision_ordinal=body.get("decision_ordinal"),
        controller_event_ordinal=body.get("controller_event_ordinal"),
    )
    if body != expected:
        raise UtilityPairingError("pairing decision replay disagrees")
    return str(expected["action"])


def _reward(policy: UtilityPairingPolicy, outcome: str) -> int:
    rewards = {
        "unit": policy.unit_reward,
        "conflict": policy.conflict_reward,
        "unactivated": policy.unactivated_reward,
        "backpressure": policy.backpressure_reward,
        "expired": policy.expired_reward,
    }
    if type(outcome) is not str:
        raise UtilityPairingError("pairing outcome is invalid")
    try:
        return rewards[outcome]
    except KeyError as error:
        raise UtilityPairingError("pairing outcome is invalid") from error


def verify_pairing_outcome(
    raw: Mapping[str, Any], *, policy: UtilityPairingPolicy
) -> str:
    if not isinstance(raw, Mapping):
        raise UtilityPairingError("pairing outcome must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("outcome_sha256", ""), "pairing outcome")
    if _content_digest(body) != supplied:
        raise UtilityPairingError("pairing outcome identity changed")
    if set(body) != {
        "schema",
        "protocol",
        "policy_sha256",
        "controller_event_ordinal",
        "decision_sha256",
        "record_sha256",
        "pair_sha256",
        "outcome",
        "reward",
    }:
        raise UtilityPairingError("pairing outcome shape changed")
    outcome = body.get("outcome")
    if (
        body.get("schema") != PAIRING_OUTCOME_SCHEMA
        or body.get("protocol") != UTILITY_PAIRING_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
        or type(outcome) is not str
        or body.get("reward") != _reward(policy, outcome)
    ):
        raise UtilityPairingError("pairing outcome scope changed")
    _digest(body.get("decision_sha256"), "pairing outcome decision")
    _digest(body.get("record_sha256"), "pairing outcome record")
    _digest(body.get("pair_sha256"), "pairing outcome pair")
    _integer(
        body.get("controller_event_ordinal"),
        "pairing outcome event ordinal",
        1,
        MAX_EVENTS,
    )
    return outcome


class UtilityPairingController:
    """Bounded, sequential pairing controller shared across worker solves."""

    def __init__(self, policy: UtilityPairingPolicy) -> None:
        self.policy = policy
        self._pairs: dict[str, _PairState] = {}
        self._pending: dict[str, tuple[str, str]] = {}
        self._active_stream = ""
        self._next_decision_ordinal = 1
        self._next_event_ordinal = 1

    def begin_stream(self, stream_id: str) -> None:
        normalized = _digest(stream_id, "pairing controller stream")
        if normalized == self._active_stream:
            return
        if self._pending:
            raise UtilityPairingError(
                "pairing controller changed stream with pending outcomes"
            )
        self._active_stream = normalized

    @property
    def can_decide(self) -> bool:
        # Every admitted decision must retain one event slot for its outcome.
        return self._next_event_ordinal < self.policy.max_events

    def consider(self, candidate: UtilityPairingCandidate) -> dict[str, Any]:
        self.begin_stream(candidate.stream_id)
        if not self.can_decide:
            raise UtilityPairingError("pairing event budget is exhausted")
        pair = candidate.pair_sha256
        state = self._pairs.get(pair)
        if state is None:
            if len(self._pairs) >= self.policy.max_pairs:
                raise UtilityPairingError("pairing pair budget is exhausted")
            state = _PairState(
                publisher_worker=candidate.publisher_worker,
                consumer_worker=candidate.consumer_worker,
                formula_family_sha256=candidate.formula_family_sha256,
                feedback=PairFeedback(),
            )
            self._pairs[pair] = state
        elif (
            state.publisher_worker != candidate.publisher_worker
            or state.consumer_worker != candidate.consumer_worker
            or state.formula_family_sha256 != candidate.formula_family_sha256
        ):
            raise UtilityPairingError("pairing key identity changed")
        prior = PairFeedback(**state.feedback.as_dict())
        body = _decision_body(
            self.policy,
            candidate,
            prior,
            decision_ordinal=self._next_decision_ordinal,
            controller_event_ordinal=self._next_event_ordinal,
        )
        body["decision_sha256"] = _content_digest(body)
        self._next_decision_ordinal += 1
        self._next_event_ordinal += 1
        state.feedback.decisions += 1
        state.feedback.checker_total_us += candidate.checker_elapsed_us
        state.feedback.event_lag_total += candidate.event_lag
        if body["phase"] == "explore":
            state.feedback.explore_decisions += 1
        else:
            state.feedback.exploit_decisions += 1
        if body["action"] == "admit":
            state.feedback.admitted += 1
            self._pending[str(body["decision_sha256"])] = (
                pair,
                candidate.record_sha256,
            )
        else:
            state.feedback.suppressed += 1
        return body

    def observe(self, decision_sha256: str, *, outcome: str) -> dict[str, Any]:
        if self._next_event_ordinal > self.policy.max_events:
            raise UtilityPairingError("pairing event budget is exhausted")
        decision = _digest(decision_sha256, "pairing feedback decision")
        # Validate before consuming the pending admission so malformed feedback
        # cannot make an otherwise valid decision impossible to settle.
        reward = _reward(self.policy, outcome)
        pending = self._pending.pop(decision, None)
        if pending is None:
            raise UtilityPairingError("pairing feedback has no pending admission")
        pair, record = pending
        state = self._pairs[pair]
        body: dict[str, Any] = {
            "schema": PAIRING_OUTCOME_SCHEMA,
            "protocol": UTILITY_PAIRING_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "controller_event_ordinal": self._next_event_ordinal,
            "decision_sha256": decision,
            "record_sha256": record,
            "pair_sha256": pair,
            "outcome": outcome,
            "reward": reward,
        }
        body["outcome_sha256"] = _content_digest(body)
        self._next_event_ordinal += 1
        setattr(state.feedback, outcome, getattr(state.feedback, outcome) + 1)
        state.feedback.reward_total += reward
        state.feedback.last_feedback_event_ordinal = int(
            body["controller_event_ordinal"]
        )
        return body

    def snapshot(self) -> dict[str, Any]:
        pairs = {
            pair: state.as_dict() for pair, state in sorted(self._pairs.items())
        }
        totals = {
            name: sum(state["feedback"][name] for state in pairs.values())
            for name in PairFeedback().as_dict()
        }
        body: dict[str, Any] = {
            "schema": PAIRING_SNAPSHOT_SCHEMA,
            "protocol": UTILITY_PAIRING_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "next_decision_ordinal": self._next_decision_ordinal,
            "next_controller_event_ordinal": self._next_event_ordinal,
            "pending_outcomes": len(self._pending),
            "pairs": pairs,
            "totals": totals,
        }
        body["snapshot_sha256"] = _content_digest(body)
        return body

    @classmethod
    def from_snapshot(
        cls,
        policy: UtilityPairingPolicy,
        raw: Mapping[str, Any],
    ) -> "UtilityPairingController":
        snapshot = verify_pairing_snapshot(raw, policy=policy)
        if snapshot["pending_outcomes"]:
            raise UtilityPairingError("cannot restore pending pairing outcomes")
        controller = cls(policy)
        controller._next_decision_ordinal = snapshot["next_decision_ordinal"]
        controller._next_event_ordinal = snapshot[
            "next_controller_event_ordinal"
        ]
        for pair, raw_state in snapshot["pairs"].items():
            controller._pairs[pair] = _PairState(
                publisher_worker=raw_state["publisher_worker"],
                consumer_worker=raw_state["consumer_worker"],
                formula_family_sha256=raw_state["formula_family_sha256"],
                feedback=PairFeedback.from_mapping(raw_state["feedback"]),
            )
        return controller


def verify_pairing_snapshot(
    raw: Mapping[str, Any], *, policy: UtilityPairingPolicy
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UtilityPairingError("pairing snapshot must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("snapshot_sha256", ""), "pairing snapshot")
    if _content_digest(body) != supplied:
        raise UtilityPairingError("pairing snapshot identity changed")
    if set(body) != {
        "schema",
        "protocol",
        "policy_sha256",
        "next_decision_ordinal",
        "next_controller_event_ordinal",
        "pending_outcomes",
        "pairs",
        "totals",
    }:
        raise UtilityPairingError("pairing snapshot shape changed")
    if (
        body.get("schema") != PAIRING_SNAPSHOT_SCHEMA
        or body.get("protocol") != UTILITY_PAIRING_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
    ):
        raise UtilityPairingError("pairing snapshot scope changed")
    next_decision = _integer(
        body.get("next_decision_ordinal"),
        "pairing next decision ordinal",
        1,
        MAX_EVENTS + 1,
    )
    next_event = _integer(
        body.get("next_controller_event_ordinal"),
        "pairing next event ordinal",
        1,
        MAX_EVENTS + 1,
    )
    pending = _integer(
        body.get("pending_outcomes"), "pairing pending outcomes", 0, MAX_EVENTS
    )
    raw_pairs = body.get("pairs")
    if not isinstance(raw_pairs, Mapping) or len(raw_pairs) > policy.max_pairs:
        raise UtilityPairingError("pairing snapshot pairs are invalid")
    pairs: dict[str, dict[str, Any]] = {}
    for raw_pair, raw_state in raw_pairs.items():
        pair = _digest(raw_pair, "pairing snapshot pair")
        if not isinstance(raw_state, Mapping) or set(raw_state) != {
            "publisher_worker",
            "consumer_worker",
            "formula_family_sha256",
            "feedback",
        }:
            raise UtilityPairingError("pairing snapshot pair shape changed")
        publisher = _worker(raw_state["publisher_worker"], "publisher worker")
        consumer = _worker(raw_state["consumer_worker"], "consumer worker")
        family = _digest(raw_state["formula_family_sha256"], "formula family")
        expected_pair = _content_digest({
            "protocol": UTILITY_PAIRING_PROTOCOL,
            "publisher_worker": publisher,
            "consumer_worker": consumer,
            "formula_family_sha256": family,
        })
        if pair != expected_pair:
            raise UtilityPairingError("pairing snapshot pair identity changed")
        feedback = PairFeedback.from_mapping(raw_state["feedback"])
        expected_reward = sum(
            getattr(feedback, name) * _reward(policy, name)
            for name in (
                "unit",
                "conflict",
                "unactivated",
                "backpressure",
                "expired",
            )
        )
        if feedback.reward_total != expected_reward:
            raise UtilityPairingError("pairing snapshot reward total changed")
        if (
            (feedback.outcomes == 0)
            != (feedback.last_feedback_event_ordinal == 0)
            or feedback.last_feedback_event_ordinal >= next_event
        ):
            raise UtilityPairingError("pairing snapshot feedback ordinal changed")
        pairs[pair] = {
            "publisher_worker": publisher,
            "consumer_worker": consumer,
            "formula_family_sha256": family,
            "feedback": feedback.as_dict(),
        }
    totals = {
        name: sum(state["feedback"][name] for state in pairs.values())
        for name in PairFeedback().as_dict()
    }
    if body.get("totals") != totals:
        raise UtilityPairingError("pairing snapshot totals changed")
    if totals["decisions"] != next_decision - 1:
        raise UtilityPairingError("pairing snapshot decision ordinal changed")
    completed = sum(
        totals[name]
        for name in ("unit", "conflict", "unactivated", "backpressure", "expired")
    )
    if totals["admitted"] - completed != pending:
        raise UtilityPairingError("pairing snapshot pending count changed")
    if next_event - 1 != totals["decisions"] + completed:
        raise UtilityPairingError("pairing snapshot event ordinal changed")
    return {
        **body,
        "next_decision_ordinal": next_decision,
        "next_controller_event_ordinal": next_event,
        "pending_outcomes": pending,
        "pairs": pairs,
        "totals": totals,
        "snapshot_sha256": supplied,
    }


def _apply_decision(feedback: PairFeedback, decision: Mapping[str, Any]) -> None:
    candidate = UtilityPairingCandidate.from_mapping(decision["candidate"])
    feedback.decisions += 1
    feedback.checker_total_us += candidate.checker_elapsed_us
    feedback.event_lag_total += candidate.event_lag
    if decision["phase"] == "explore":
        feedback.explore_decisions += 1
    else:
        feedback.exploit_decisions += 1
    if decision["action"] == "admit":
        feedback.admitted += 1
    else:
        feedback.suppressed += 1


def _apply_outcome(feedback: PairFeedback, outcome: Mapping[str, Any]) -> None:
    name = str(outcome["outcome"])
    setattr(feedback, name, getattr(feedback, name) + 1)
    feedback.reward_total += int(outcome["reward"])
    feedback.last_feedback_event_ordinal = int(
        outcome["controller_event_ordinal"]
    )


def verify_pairing_stream_result(
    raw: Mapping[str, Any],
    *,
    stream_id: str,
    consumer_worker: str,
    formula_family: str,
    delivered_records: Sequence[str],
    activity_kinds: Mapping[str, str],
) -> dict[str, Any]:
    """Replay one complete pairing trace and close every admitted outcome."""
    normalized_stream = _digest(stream_id, "pairing result stream")
    normalized_consumer = _worker(consumer_worker, "consumer worker")
    normalized_family = _digest(formula_family, "pairing result formula family")
    if raw.get("backend_realtime_pairing_protocol") != UTILITY_PAIRING_PROTOCOL:
        raise UtilityPairingError("pairing result protocol changed")
    policy = UtilityPairingPolicy.from_sealed(
        raw.get("backend_realtime_pairing_policy", {})
    )
    if raw.get("backend_realtime_pairing_policy_sha256") != policy.sha256:
        raise UtilityPairingError("pairing result policy identity changed")
    if raw.get("backend_realtime_pairing_formula_family_sha256") != normalized_family:
        raise UtilityPairingError("pairing result formula family changed")
    if raw.get("backend_realtime_pairing_consumer_worker") != normalized_consumer:
        raise UtilityPairingError("pairing result consumer changed")
    decisions_raw = raw.get("backend_realtime_pairing_decisions")
    outcomes_raw = raw.get("backend_realtime_pairing_outcomes")
    if (
        not isinstance(decisions_raw, list)
        or not isinstance(outcomes_raw, list)
        or len(decisions_raw) + len(outcomes_raw) > policy.max_events
    ):
        raise UtilityPairingError("pairing result trace is not bounded")
    decisions: list[dict[str, Any]] = []
    decision_ids: set[str] = set()
    action_counts = {"admit": 0, "suppress": 0}
    phase_counts = {"explore": 0, "exploit": 0}
    for raw_decision in decisions_raw:
        action = verify_pairing_decision(raw_decision, policy=policy)
        decision = dict(raw_decision)
        identity = _digest(decision["decision_sha256"], "pairing trace decision")
        if identity in decision_ids:
            raise UtilityPairingError("pairing decision is duplicated")
        decision_ids.add(identity)
        candidate = UtilityPairingCandidate.from_mapping(decision["candidate"])
        if (
            candidate.stream_id != normalized_stream
            or candidate.consumer_worker != normalized_consumer
            or candidate.formula_family_sha256 != normalized_family
        ):
            raise UtilityPairingError("pairing decision session scope changed")
        action_counts[action] += 1
        phase_counts[str(decision["phase"])] += 1
        decisions.append(decision)
    outcomes: list[dict[str, Any]] = []
    outcome_decisions: set[str] = set()
    outcome_counts = {
        name: 0
        for name in ("unit", "conflict", "unactivated", "backpressure", "expired")
    }
    by_decision = {decision["decision_sha256"]: decision for decision in decisions}
    for raw_outcome in outcomes_raw:
        name = verify_pairing_outcome(raw_outcome, policy=policy)
        outcome = dict(raw_outcome)
        decision_id = str(outcome["decision_sha256"])
        decision = by_decision.get(decision_id)
        if (
            decision is None
            or decision["action"] != "admit"
            or decision_id in outcome_decisions
            or outcome["record_sha256"]
            != decision["candidate"]["record_sha256"]
            or outcome["pair_sha256"] != decision["candidate"]["pair_sha256"]
        ):
            raise UtilityPairingError("pairing outcome authorization changed")
        outcome_decisions.add(decision_id)
        outcome_counts[name] += 1
        outcomes.append(outcome)
    admitted_ids = {
        decision["decision_sha256"]
        for decision in decisions
        if decision["action"] == "admit"
    }
    if outcome_decisions != admitted_ids:
        raise UtilityPairingError("pairing admitted outcomes are incomplete")
    delivered = {
        _digest(record, "pairing delivered record") for record in delivered_records
    }
    if len(delivered) != len(tuple(delivered_records)):
        raise UtilityPairingError("pairing delivered records are duplicated")
    normalized_activity = {
        _digest(record, "pairing activity record"): kind
        for record, kind in activity_kinds.items()
    }
    if not set(normalized_activity) <= delivered or any(
        kind not in {"unit", "conflict"} for kind in normalized_activity.values()
    ):
        raise UtilityPairingError("pairing activity map is invalid")
    observed_delivered: set[str] = set()
    for outcome in outcomes:
        record = str(outcome["record_sha256"])
        name = str(outcome["outcome"])
        if name in {"unit", "conflict", "unactivated"}:
            if record not in delivered:
                raise UtilityPairingError("pairing outcome lacks native delivery")
            expected = normalized_activity.get(record, "unactivated")
            if name != expected:
                raise UtilityPairingError("pairing activity outcome changed")
            observed_delivered.add(record)
        elif record in delivered:
            raise UtilityPairingError("pairing failure outcome was delivered")
    if observed_delivered != delivered:
        raise UtilityPairingError("pairing delivered outcomes are incomplete")
    if raw.get("backend_realtime_pairing_action_counts") != action_counts:
        raise UtilityPairingError("pairing action counts changed")
    if raw.get("backend_realtime_pairing_phase_counts") != phase_counts:
        raise UtilityPairingError("pairing phase counts changed")
    if raw.get("backend_realtime_pairing_outcome_counts") != outcome_counts:
        raise UtilityPairingError("pairing outcome counts changed")

    events = sorted(
        [(int(item["controller_event_ordinal"]), "decision", item) for item in decisions]
        + [(int(item["controller_event_ordinal"]), "outcome", item) for item in outcomes]
    )
    if events and [event[0] for event in events] != list(
        range(events[0][0], events[0][0] + len(events))
    ):
        raise UtilityPairingError("pairing controller events are not contiguous")
    if decisions and [int(item["decision_ordinal"]) for item in decisions] != list(
        range(
            int(decisions[0]["decision_ordinal"]),
            int(decisions[0]["decision_ordinal"]) + len(decisions),
        )
    ):
        raise UtilityPairingError("pairing decision ordinals are not contiguous")
    replay: dict[str, PairFeedback] = {}
    for _ordinal, kind, item in events:
        pair = str(
            item["candidate"]["pair_sha256"]
            if kind == "decision"
            else item["pair_sha256"]
        )
        if kind == "decision":
            prior = PairFeedback.from_mapping(item["pair_feedback"])
            current = replay.setdefault(pair, PairFeedback(**prior.as_dict()))
            if current.as_dict() != prior.as_dict():
                raise UtilityPairingError("pairing feedback replay prefix changed")
            _apply_decision(current, item)
        else:
            if pair not in replay:
                raise UtilityPairingError("pairing outcome precedes its decision")
            _apply_outcome(replay[pair], item)
    snapshot = verify_pairing_snapshot(
        raw.get("backend_realtime_pairing_controller_snapshot", {}), policy=policy
    )
    if snapshot["pending_outcomes"] != 0:
        raise UtilityPairingError("pairing controller did not quiesce outcomes")
    for pair, feedback in replay.items():
        if snapshot["pairs"].get(pair, {}).get("feedback") != feedback.as_dict():
            raise UtilityPairingError("pairing snapshot does not close trace")
    if events and events[-1][0] != snapshot["next_controller_event_ordinal"] - 1:
        raise UtilityPairingError("pairing trace is not the snapshot event suffix")
    if decisions and (
        decisions[-1]["decision_ordinal"]
        != snapshot["next_decision_ordinal"] - 1
    ):
        raise UtilityPairingError("pairing trace is not the snapshot decision suffix")
    return {
        "backend_realtime_pairing_protocol": UTILITY_PAIRING_PROTOCOL,
        "backend_realtime_pairing_policy": policy.as_dict(),
        "backend_realtime_pairing_policy_sha256": policy.sha256,
        "backend_realtime_pairing_formula_family_sha256": normalized_family,
        "backend_realtime_pairing_consumer_worker": normalized_consumer,
        "backend_realtime_pairing_decisions": decisions,
        "backend_realtime_pairing_outcomes": outcomes,
        "backend_realtime_pairing_action_counts": action_counts,
        "backend_realtime_pairing_phase_counts": phase_counts,
        "backend_realtime_pairing_outcome_counts": outcome_counts,
        "backend_realtime_pairing_controller_snapshot": snapshot,
    }
