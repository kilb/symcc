#!/usr/bin/env python3
"""Replayable adaptive admission for proof-checked realtime QF_BV clauses."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


ADAPTIVE_EXCHANGE_PROTOCOL = "symcc-qfbv-adaptive-proof-admission-v1"
ADAPTIVE_POLICY_SCHEMA = "symcc-qfbv-adaptive-proof-policy-v1"
ADAPTIVE_DECISION_SCHEMA = "symcc-qfbv-adaptive-proof-decision-v1"
ADAPTIVE_SNAPSHOT_SCHEMA = "symcc-qfbv-adaptive-proof-snapshot-v1"
MAX_SOURCES = 4096
_HEX64 = re.compile(r"[0-9a-f]{64}")


class AdaptiveExchangeError(ValueError):
    """An adaptive admission policy, decision, or feedback trace is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise AdaptiveExchangeError(f"{name} must be an integer")
    result = value
    if not lower <= result <= upper:
        raise AdaptiveExchangeError(f"{name} must be in [{lower}, {upper}]")
    return result


def _digest(value: Any, name: str) -> str:
    if type(value) is not str:
        raise AdaptiveExchangeError(f"{name} must be a lowercase SHA-256")
    result = value
    if _HEX64.fullmatch(result) is None:
        raise AdaptiveExchangeError(f"{name} must be a lowercase SHA-256")
    return result


def _worker(value: Any) -> str:
    if type(value) is not str:
        raise AdaptiveExchangeError("source worker identity is invalid")
    result = value
    if (
        not result
        or len(result.encode("utf-8")) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
    ):
        raise AdaptiveExchangeError("source worker identity is invalid")
    return result


@dataclass(frozen=True)
class AdaptiveProofPolicy:
    """Bounded fixed-point policy; its identity is part of every decision."""

    queue_capacity: int
    high_watermark_permille: int = 750
    min_score: int = 1500
    max_deferred: int = 128
    max_retries: int = 4
    max_decisions: int = 4096
    max_clause_literals: int = 32
    checker_reference_us: int = 1000
    age_ramp_ms: int = 100
    minimum_remaining_ms: int = 2

    def __post_init__(self) -> None:
        _integer(self.queue_capacity, "adaptive queue capacity", 1, 4096)
        _integer(
            self.high_watermark_permille,
            "adaptive queue high watermark",
            1,
            1000,
        )
        _integer(self.min_score, "adaptive minimum score", -10_000, 10_000)
        _integer(self.max_deferred, "adaptive deferred capacity", 1, 4096)
        _integer(self.max_retries, "adaptive retry count", 1, 32)
        _integer(self.max_decisions, "adaptive decision count", 1, 1_000_000)
        _integer(
            self.max_clause_literals,
            "adaptive maximum clause length",
            1,
            65_536,
        )
        _integer(
            self.checker_reference_us,
            "adaptive checker reference",
            1,
            60_000_000,
        )
        _integer(self.age_ramp_ms, "adaptive solve-age ramp", 1, 3_600_000)
        _integer(
            self.minimum_remaining_ms,
            "adaptive minimum remaining solve time",
            0,
            3_600_000,
        )

    @property
    def protocol(self) -> str:
        return ADAPTIVE_EXCHANGE_PROTOCOL

    def as_config(self) -> dict[str, int]:
        return {
            "high_watermark_permille": self.high_watermark_permille,
            "min_score": self.min_score,
            "max_deferred": self.max_deferred,
            "max_retries": self.max_retries,
            "max_decisions": self.max_decisions,
            "max_clause_literals": self.max_clause_literals,
            "checker_reference_us": self.checker_reference_us,
            "age_ramp_ms": self.age_ramp_ms,
            "minimum_remaining_ms": self.minimum_remaining_ms,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ADAPTIVE_POLICY_SCHEMA,
            "protocol": ADAPTIVE_EXCHANGE_PROTOCOL,
            "queue_capacity": self.queue_capacity,
            **self.as_config(),
        }

    @property
    def sha256(self) -> str:
        return _content_digest(self.as_dict())

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        queue_capacity: int,
    ) -> "AdaptiveProofPolicy":
        if not isinstance(raw, Mapping):
            raise AdaptiveExchangeError("adaptive policy must be an object")
        defaults = cls(queue_capacity=queue_capacity)
        allowed = set(defaults.as_config())
        unknown = set(raw) - allowed
        if unknown:
            raise AdaptiveExchangeError(
                "unknown adaptive policy option: " + sorted(unknown)[0]
            )
        values = {
            name: _integer(
                raw.get(name, getattr(defaults, name)),
                f"adaptive {name.replace('_', ' ')}",
                -10_000 if name == "min_score" else 0,
                {
                    "high_watermark_permille": 1000,
                    "min_score": 10_000,
                    "max_deferred": 4096,
                    "max_retries": 32,
                    "max_decisions": 1_000_000,
                    "max_clause_literals": 65_536,
                    "checker_reference_us": 60_000_000,
                    "age_ramp_ms": 3_600_000,
                    "minimum_remaining_ms": 3_600_000,
                }[name],
            )
            for name in allowed
        }
        return cls(queue_capacity=queue_capacity, **values)

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "AdaptiveProofPolicy":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != ADAPTIVE_POLICY_SCHEMA
            or raw.get("protocol") != ADAPTIVE_EXCHANGE_PROTOCOL
        ):
            raise AdaptiveExchangeError("adaptive sealed policy scope changed")
        expected = {
            "schema",
            "protocol",
            "queue_capacity",
            "high_watermark_permille",
            "min_score",
            "max_deferred",
            "max_retries",
            "max_decisions",
            "max_clause_literals",
            "checker_reference_us",
            "age_ramp_ms",
            "minimum_remaining_ms",
        }
        if set(raw) != expected:
            raise AdaptiveExchangeError("adaptive sealed policy shape changed")
        queue_capacity = _integer(
            raw.get("queue_capacity"), "adaptive queue capacity", 1, 4096
        )
        policy = cls.from_mapping(
            {key: raw[key] for key in policy_config_keys()},
            queue_capacity=queue_capacity,
        )
        if policy.as_dict() != dict(raw):
            raise AdaptiveExchangeError("adaptive sealed policy changed")
        return policy


def policy_config_keys() -> tuple[str, ...]:
    return (
        "high_watermark_permille",
        "min_score",
        "max_deferred",
        "max_retries",
        "max_decisions",
        "max_clause_literals",
        "checker_reference_us",
        "age_ramp_ms",
        "minimum_remaining_ms",
    )


@dataclass(frozen=True)
class AdaptiveProofCandidate:
    record_sha256: str
    formula_sha256: str
    stream_id: str
    source_worker: str
    event_sequence: int
    clause_literals: int
    proof_steps: int
    propagation_count: int
    checker_elapsed_us: int
    retry_count: int = 0

    def __post_init__(self) -> None:
        _digest(self.record_sha256, "adaptive record")
        _digest(self.formula_sha256, "adaptive formula")
        _digest(self.stream_id, "adaptive stream")
        _worker(self.source_worker)
        _integer(self.event_sequence, "adaptive event sequence", 1, (1 << 63) - 1)
        _integer(self.clause_literals, "adaptive clause length", 1, 65_536)
        _integer(self.proof_steps, "adaptive proof steps", 0, 100_000_000)
        _integer(
            self.propagation_count,
            "adaptive propagation count",
            0,
            (1 << 63) - 1,
        )
        _integer(
            self.checker_elapsed_us,
            "adaptive checker elapsed time",
            0,
            (1 << 63) - 1,
        )
        _integer(self.retry_count, "adaptive retry count", 0, 32)

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_sha256": self.record_sha256,
            "formula_sha256": self.formula_sha256,
            "stream_id": self.stream_id,
            "source_worker": self.source_worker,
            "event_sequence": self.event_sequence,
            "clause_literals": self.clause_literals,
            "proof_steps": self.proof_steps,
            "propagation_count": self.propagation_count,
            "checker_elapsed_us": self.checker_elapsed_us,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AdaptiveProofCandidate":
        if not isinstance(raw, Mapping) or set(raw) != {
            "record_sha256",
            "formula_sha256",
            "stream_id",
            "source_worker",
            "event_sequence",
            "clause_literals",
            "proof_steps",
            "propagation_count",
            "checker_elapsed_us",
            "retry_count",
        }:
            raise AdaptiveExchangeError("adaptive candidate shape changed")
        return cls(**dict(raw))


@dataclass(frozen=True)
class AdaptiveProofObservation:
    solve_generation: int
    solving: bool
    solve_age_ms: int
    remaining_ms: int
    queue_depth: int
    ack_depth: int
    deferred_depth: int

    def __post_init__(self) -> None:
        _integer(
            self.solve_generation,
            "adaptive solve generation",
            0,
            (1 << 63) - 1,
        )
        if not isinstance(self.solving, bool):
            raise AdaptiveExchangeError("adaptive solving state must be boolean")
        for value, name, upper in (
            (self.solve_age_ms, "solve age", 3_600_000),
            (self.remaining_ms, "remaining solve time", 3_600_000),
            (self.queue_depth, "queue depth", 4096),
            (self.ack_depth, "ACK depth", 4096),
            (self.deferred_depth, "deferred depth", 4096),
        ):
            _integer(value, f"adaptive {name}", 0, upper)

    def as_dict(self) -> dict[str, Any]:
        return {
            "solve_generation": self.solve_generation,
            "solving": self.solving,
            "solve_age_ms": self.solve_age_ms,
            "remaining_ms": self.remaining_ms,
            "queue_depth": self.queue_depth,
            "ack_depth": self.ack_depth,
            "deferred_depth": self.deferred_depth,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AdaptiveProofObservation":
        if not isinstance(raw, Mapping) or set(raw) != {
            "solve_generation",
            "solving",
            "solve_age_ms",
            "remaining_ms",
            "queue_depth",
            "ack_depth",
            "deferred_depth",
        }:
            raise AdaptiveExchangeError("adaptive observation shape changed")
        return cls(**dict(raw))


@dataclass
class SourceFeedback:
    verified: int = 0
    admit_decisions: int = 0
    defer_decisions: int = 0
    reject_decisions: int = 0
    delivered: int = 0
    backpressure: int = 0
    expired: int = 0
    ack_latency_total_us: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "verified": self.verified,
            "admit_decisions": self.admit_decisions,
            "defer_decisions": self.defer_decisions,
            "reject_decisions": self.reject_decisions,
            "delivered": self.delivered,
            "backpressure": self.backpressure,
            "expired": self.expired,
            "ack_latency_total_us": self.ack_latency_total_us,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SourceFeedback":
        if not isinstance(raw, Mapping) or set(raw) != set(cls().as_dict()):
            raise AdaptiveExchangeError("adaptive source feedback shape changed")
        values = {
            name: _integer(value, f"adaptive source {name}", 0, (1 << 63) - 1)
            for name, value in raw.items()
        }
        result = cls(**values)
        if (
            result.delivered + result.backpressure + result.expired
            > result.admit_decisions
            or result.admit_decisions
            + result.defer_decisions
            + result.reject_decisions
            != result.verified
        ):
            raise AdaptiveExchangeError("adaptive source feedback is inconsistent")
        return result


def _decision_body(
    policy: AdaptiveProofPolicy,
    candidate: AdaptiveProofCandidate,
    observation: AdaptiveProofObservation,
    source_feedback: SourceFeedback,
    decision_ordinal: int,
) -> dict[str, Any]:
    ordinal = _integer(
        decision_ordinal, "adaptive decision ordinal", 1, (1 << 63) - 1
    )
    length_quality = min(4000, 8000 // candidate.clause_literals)
    proof_density = min(
        2000,
        candidate.propagation_count * 1000 // max(1, candidate.proof_steps),
    )
    source_yield_permille = (
        (source_feedback.delivered + 1)
        * 1000
        // (source_feedback.admit_decisions + 2)
    )
    source_penalty_permille = (
        (source_feedback.backpressure + source_feedback.expired)
        * 1000
        // (source_feedback.admit_decisions + 2)
    )
    solve_age_bonus = min(
        1000, observation.solve_age_ms * 1000 // policy.age_ramp_ms
    )
    checker_penalty = min(
        3000,
        candidate.checker_elapsed_us * 1000 // policy.checker_reference_us,
    )
    queue_permille = min(
        1000, observation.queue_depth * 1000 // policy.queue_capacity
    )
    queue_penalty = queue_permille * 3
    retry_penalty = candidate.retry_count * 250
    components = {
        "length_quality": length_quality,
        "proof_density": proof_density,
        "source_yield": source_yield_permille * 2,
        "solve_age_bonus": solve_age_bonus,
        "checker_penalty": checker_penalty,
        "source_failure_penalty": source_penalty_permille * 2,
        "queue_penalty": queue_penalty,
        "retry_penalty": retry_penalty,
    }
    score = (
        components["length_quality"]
        + components["proof_density"]
        + components["source_yield"]
        + components["solve_age_bonus"]
        - components["checker_penalty"]
        - components["source_failure_penalty"]
        - components["queue_penalty"]
        - components["retry_penalty"]
    )
    hard_reasons: list[str] = []
    if candidate.clause_literals > policy.max_clause_literals:
        hard_reasons.append("clause-length-limit")
    if candidate.retry_count >= policy.max_retries:
        hard_reasons.append("retry-limit")
    if (
        candidate.retry_count == 0
        and observation.deferred_depth >= policy.max_deferred
    ):
        hard_reasons.append("deferred-capacity")
    soft_reasons: list[str] = []
    if not observation.solving:
        soft_reasons.append("solver-inactive")
    if queue_permille >= policy.high_watermark_permille:
        soft_reasons.append("queue-high-watermark")
    if (
        policy.minimum_remaining_ms
        and observation.remaining_ms <= policy.minimum_remaining_ms
    ):
        soft_reasons.append("solve-tail-guard")
    if score < policy.min_score:
        soft_reasons.append("score-below-threshold")
    if hard_reasons:
        action = "reject"
        reasons = hard_reasons
    elif soft_reasons:
        action = "defer"
        reasons = soft_reasons
    else:
        action = "admit"
        reasons = ["score-and-capacity-admit"]
    return {
        "schema": ADAPTIVE_DECISION_SCHEMA,
        "protocol": ADAPTIVE_EXCHANGE_PROTOCOL,
        "policy_sha256": policy.sha256,
        "decision_ordinal": ordinal,
        "candidate": candidate.as_dict(),
        "observation": observation.as_dict(),
        "source_feedback": source_feedback.as_dict(),
        "score_components": components,
        "score": score,
        "threshold": policy.min_score,
        "action": action,
        "reasons": reasons,
    }


def verify_admission_decision(
    raw: Mapping[str, Any], *, policy: AdaptiveProofPolicy
) -> str:
    if not isinstance(raw, Mapping):
        raise AdaptiveExchangeError("adaptive decision must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("decision_sha256", ""), "adaptive decision")
    if _content_digest(body) != supplied:
        raise AdaptiveExchangeError("adaptive decision identity changed")
    if (
        body.get("schema") != ADAPTIVE_DECISION_SCHEMA
        or body.get("protocol") != ADAPTIVE_EXCHANGE_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
    ):
        raise AdaptiveExchangeError("adaptive decision scope changed")
    candidate = AdaptiveProofCandidate.from_mapping(body.get("candidate", {}))
    observation = AdaptiveProofObservation.from_mapping(
        body.get("observation", {})
    )
    feedback = SourceFeedback.from_mapping(body.get("source_feedback", {}))
    expected = _decision_body(
        policy,
        candidate,
        observation,
        feedback,
        body.get("decision_ordinal"),
    )
    if body != expected:
        raise AdaptiveExchangeError("adaptive decision replay disagrees")
    return str(expected["action"])


class AdaptiveProofController:
    """Sequential controller shared across solves of one persistent worker."""

    def __init__(self, policy: AdaptiveProofPolicy) -> None:
        self.policy = policy
        self._sources: dict[str, SourceFeedback] = {}
        self._pending: dict[str, str] = {}
        self._next_ordinal = 1
        self._active_stream = ""
        self._stream_decisions = 0

    @property
    def can_decide(self) -> bool:
        return self._stream_decisions < self.policy.max_decisions

    def begin_stream(self, stream_id: str) -> None:
        normalized = _digest(stream_id, "adaptive controller stream")
        if normalized == self._active_stream:
            return
        if self._pending:
            raise AdaptiveExchangeError(
                "adaptive controller changed stream with pending feedback"
            )
        self._active_stream = normalized
        self._stream_decisions = 0

    def consider(
        self,
        candidate: AdaptiveProofCandidate,
        observation: AdaptiveProofObservation,
    ) -> dict[str, Any]:
        self.begin_stream(candidate.stream_id)
        if not self.can_decide:
            raise AdaptiveExchangeError("adaptive decision budget is exhausted")
        source = candidate.source_worker
        if source not in self._sources and len(self._sources) >= MAX_SOURCES:
            raise AdaptiveExchangeError("adaptive source budget is exhausted")
        feedback = self._sources.setdefault(source, SourceFeedback())
        body = _decision_body(
            self.policy,
            candidate,
            observation,
            SourceFeedback(**feedback.as_dict()),
            self._next_ordinal,
        )
        body["decision_sha256"] = _content_digest(body)
        self._next_ordinal += 1
        self._stream_decisions += 1
        feedback.verified += 1
        action = str(body["action"])
        if action == "admit":
            feedback.admit_decisions += 1
            self._pending[str(body["decision_sha256"])] = source
        elif action == "defer":
            feedback.defer_decisions += 1
        else:
            feedback.reject_decisions += 1
        return body

    def _complete(self, decision_sha256: str, outcome: str, value: int = 0) -> None:
        digest = _digest(decision_sha256, "adaptive feedback decision")
        source = self._pending.pop(digest, None)
        if source is None:
            raise AdaptiveExchangeError("adaptive feedback has no pending admission")
        feedback = self._sources[source]
        if outcome == "delivered":
            feedback.delivered += 1
            feedback.ack_latency_total_us += _integer(
                value, "adaptive ACK latency", 0, (1 << 63) - 1
            )
        elif outcome == "backpressure":
            feedback.backpressure += 1
        elif outcome == "expired":
            feedback.expired += 1
        else:
            raise AdaptiveExchangeError("adaptive feedback outcome is invalid")

    def observe_delivery(self, decision_sha256: str, *, latency_us: int) -> None:
        self._complete(decision_sha256, "delivered", latency_us)

    def observe_backpressure(self, decision_sha256: str) -> None:
        self._complete(decision_sha256, "backpressure")

    def observe_expired(self, decision_sha256: str) -> None:
        self._complete(decision_sha256, "expired")

    def snapshot(self) -> dict[str, Any]:
        sources = {
            source: feedback.as_dict()
            for source, feedback in sorted(self._sources.items())
        }
        totals = {
            name: sum(values[name] for values in sources.values())
            for name in SourceFeedback().as_dict()
        }
        body: dict[str, Any] = {
            "schema": ADAPTIVE_SNAPSHOT_SCHEMA,
            "protocol": ADAPTIVE_EXCHANGE_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "next_decision_ordinal": self._next_ordinal,
            "pending_feedback": len(self._pending),
            "sources": sources,
            "totals": totals,
        }
        body["snapshot_sha256"] = _content_digest(body)
        return body


def verify_controller_snapshot(
    raw: Mapping[str, Any], *, policy: AdaptiveProofPolicy
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AdaptiveExchangeError("adaptive snapshot must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("snapshot_sha256", ""), "adaptive snapshot")
    if _content_digest(body) != supplied:
        raise AdaptiveExchangeError("adaptive snapshot identity changed")
    if (
        body.get("schema") != ADAPTIVE_SNAPSHOT_SCHEMA
        or body.get("protocol") != ADAPTIVE_EXCHANGE_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
    ):
        raise AdaptiveExchangeError("adaptive snapshot scope changed")
    next_ordinal = _integer(
        body.get("next_decision_ordinal"),
        "adaptive next decision ordinal",
        1,
        (1 << 63) - 1,
    )
    pending = _integer(
        body.get("pending_feedback"), "adaptive pending feedback", 0, 1_000_000
    )
    raw_sources = body.get("sources")
    if not isinstance(raw_sources, Mapping) or len(raw_sources) > MAX_SOURCES:
        raise AdaptiveExchangeError("adaptive snapshot sources are invalid")
    sources: dict[str, dict[str, int]] = {}
    for source, raw_feedback in raw_sources.items():
        normalized_source = _worker(source)
        feedback = SourceFeedback.from_mapping(raw_feedback)
        sources[normalized_source] = feedback.as_dict()
    totals = {
        name: sum(values[name] for values in sources.values())
        for name in SourceFeedback().as_dict()
    }
    if body.get("totals") != totals:
        raise AdaptiveExchangeError("adaptive snapshot totals changed")
    if totals["verified"] != next_ordinal - 1:
        raise AdaptiveExchangeError("adaptive snapshot ordinal is inconsistent")
    completed = (
        totals["delivered"] + totals["backpressure"] + totals["expired"]
    )
    if totals["admit_decisions"] - completed != pending:
        raise AdaptiveExchangeError("adaptive pending feedback count changed")
    return {
        **body,
        "sources": sources,
        "totals": totals,
        "snapshot_sha256": supplied,
    }


def verify_adaptive_stream_result(
    raw: Mapping[str, Any],
    *,
    stream_id: str,
    delivered_records: tuple[str, ...],
    authorized: int,
    backpressure: int,
) -> dict[str, Any]:
    """Replay one session's decisions and close enqueue/feedback accounting."""
    normalized_stream = _digest(stream_id, "adaptive result stream")
    bounded_authorized = _integer(
        authorized, "adaptive authorized imports", 0, 4096
    )
    bounded_backpressure = _integer(
        backpressure, "adaptive native backpressure", 0, 1_000_000
    )
    if raw.get("backend_realtime_adaptive_protocol") != ADAPTIVE_EXCHANGE_PROTOCOL:
        raise AdaptiveExchangeError("adaptive result protocol changed")
    policy = AdaptiveProofPolicy.from_sealed(
        raw.get("backend_realtime_adaptive_policy", {})
    )
    if raw.get("backend_realtime_adaptive_policy_sha256") != policy.sha256:
        raise AdaptiveExchangeError("adaptive result policy identity changed")
    raw_decisions = raw.get("backend_realtime_adaptive_decisions")
    if (
        not isinstance(raw_decisions, list)
        or len(raw_decisions) > policy.max_decisions
    ):
        raise AdaptiveExchangeError("adaptive decision trace is not bounded")
    decisions: list[dict[str, Any]] = []
    decision_ids: set[str] = set()
    ordinals: list[int] = []
    actions = {"admit": 0, "defer": 0, "reject": 0}
    retried = 0
    for raw_decision in raw_decisions:
        action = verify_admission_decision(raw_decision, policy=policy)
        decision = dict(raw_decision)
        digest = _digest(
            decision.get("decision_sha256"), "adaptive trace decision"
        )
        if digest in decision_ids:
            raise AdaptiveExchangeError("adaptive decision identity is duplicated")
        decision_ids.add(digest)
        candidate = AdaptiveProofCandidate.from_mapping(decision["candidate"])
        if candidate.stream_id != normalized_stream:
            raise AdaptiveExchangeError("adaptive decision stream identity changed")
        ordinals.append(int(decision["decision_ordinal"]))
        actions[action] += 1
        retried += int(candidate.retry_count > 0)
        decisions.append(decision)
    if ordinals and ordinals != list(range(ordinals[0], ordinals[0] + len(ordinals))):
        raise AdaptiveExchangeError("adaptive decision ordinals are not contiguous")
    if raw.get("backend_realtime_adaptive_action_counts") != actions:
        raise AdaptiveExchangeError("adaptive action counts changed")
    if _integer(
        raw.get("backend_realtime_adaptive_retried"),
        "adaptive retry decisions",
        0,
        policy.max_decisions,
    ) != retried:
        raise AdaptiveExchangeError("adaptive retry accounting changed")
    budget_exhausted = raw.get(
        "backend_realtime_adaptive_decision_budget_exhausted"
    )
    if not isinstance(budget_exhausted, bool):
        raise AdaptiveExchangeError("adaptive decision budget state is invalid")
    if budget_exhausted and len(decisions) != policy.max_decisions:
        raise AdaptiveExchangeError("adaptive decision budget claim is inconsistent")
    budget_dropped = _integer(
        raw.get("backend_realtime_adaptive_decision_budget_dropped"),
        "adaptive budget-dropped candidates",
        0,
        1_000_000,
    )
    rejected = _integer(
        raw.get("backend_realtime_adaptive_rejected"),
        "adaptive rejected candidates",
        0,
        1_000_000,
    )
    if rejected != actions["reject"] + budget_dropped:
        raise AdaptiveExchangeError("adaptive rejection accounting changed")
    deferred_final = _integer(
        raw.get("backend_realtime_adaptive_deferred_final"),
        "adaptive final deferred candidates",
        0,
        policy.max_deferred,
    )
    raw_enqueued = raw.get("backend_realtime_adaptive_enqueued_decisions")
    if not isinstance(raw_enqueued, Mapping) or len(raw_enqueued) != bounded_authorized:
        raise AdaptiveExchangeError("adaptive enqueue map cardinality changed")
    by_id = {str(item["decision_sha256"]): item for item in decisions}
    enqueued: dict[str, str] = {}
    for raw_record, raw_decision_id in raw_enqueued.items():
        record = _digest(raw_record, "adaptive enqueued record")
        decision_id = _digest(
            raw_decision_id, "adaptive enqueued decision identity"
        )
        decision = by_id.get(decision_id)
        if (
            decision is None
            or decision["action"] != "admit"
            or decision["candidate"]["record_sha256"] != record
        ):
            raise AdaptiveExchangeError("adaptive enqueue authorization changed")
        enqueued[record] = decision_id
    delivered = tuple(
        _digest(value, "adaptive delivered record") for value in delivered_records
    )
    if len(set(delivered)) != len(delivered) or not set(delivered) <= set(enqueued):
        raise AdaptiveExchangeError("adaptive delivered set was not admitted")
    if actions["admit"] != bounded_authorized + bounded_backpressure:
        raise AdaptiveExchangeError("adaptive admit outcomes are not conserved")
    snapshot = verify_controller_snapshot(
        raw.get("backend_realtime_adaptive_controller_snapshot", {}),
        policy=policy,
    )
    if snapshot["pending_feedback"] != 0:
        raise AdaptiveExchangeError("adaptive controller did not quiesce feedback")
    if ordinals and ordinals[-1] != snapshot["next_decision_ordinal"] - 1:
        raise AdaptiveExchangeError(
            "adaptive stream trace is not the controller snapshot suffix"
        )
    return {
        "backend_realtime_adaptive_protocol": ADAPTIVE_EXCHANGE_PROTOCOL,
        "backend_realtime_adaptive_policy": policy.as_dict(),
        "backend_realtime_adaptive_policy_sha256": policy.sha256,
        "backend_realtime_adaptive_decisions": decisions,
        "backend_realtime_adaptive_action_counts": actions,
        "backend_realtime_adaptive_retried": retried,
        "backend_realtime_adaptive_rejected": rejected,
        "backend_realtime_adaptive_deferred_final": deferred_final,
        "backend_realtime_adaptive_decision_budget_exhausted": budget_exhausted,
        "backend_realtime_adaptive_decision_budget_dropped": budget_dropped,
        "backend_realtime_adaptive_enqueued_decisions": enqueued,
        "backend_realtime_adaptive_controller_snapshot": snapshot,
    }
