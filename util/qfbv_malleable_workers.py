#!/usr/bin/env python3
"""Generation-fenced logical malleability for prelaunched solver workers.

The protocol deliberately treats MPI ranks or service threads as a fixed
physical slot pool.  Jobs own logical slots.  Reallocation is a replayable
two-phase operation: changed assignments stop admitting leases, retire their
exact lease set, acknowledge every durable proof artifact, and only then move
to a freshly fenced assignment generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from qfbv_utility_pairing import (
    PairFeedback,
    UtilityPairingPolicy,
    verify_pairing_snapshot,
)


MALLEABLE_WORKER_PROTOCOL = "symcc-generation-fenced-malleable-worker-pool-v1"
MALLEABLE_POLICY_SCHEMA = "symcc-malleable-worker-policy-v1"
MALLEABLE_SIGNAL_SCHEMA = "symcc-malleable-job-signal-v1"
MALLEABLE_TRANSITION_SCHEMA = "symcc-malleable-worker-transition-v1"
MALLEABLE_DRAIN_RECEIPT_SCHEMA = "symcc-malleable-worker-drain-receipt-v1"
MALLEABLE_COMMIT_SCHEMA = "symcc-malleable-worker-commit-v1"
MALLEABLE_SNAPSHOT_SCHEMA = "symcc-malleable-worker-snapshot-v1"
MAX_SLOTS = 4096
MAX_JOBS = 4096
MAX_EVENTS = 2_000_000
MAX_LEASES_PER_WORKER = 4096
MAX_PROOFS_PER_WORKER = 65_536
_HEX64 = re.compile(r"[0-9a-f]{64}")


class MalleableWorkerError(ValueError):
    """A malleability policy, event, receipt, or snapshot is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise MalleableWorkerError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise MalleableWorkerError(f"{name} must be in [{lower}, {upper}]")
    return value


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise MalleableWorkerError(f"{name} identity is invalid")
    encoded = value.encode("utf-8")
    if (
        not value
        or len(encoded) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MalleableWorkerError(f"{name} identity is invalid")
    return value


def validate_malleable_identity(value: Any, name: str = "malleable identity") -> str:
    """Validate a bounded identity at persistence and protocol boundaries."""
    return _identity(value, name)


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise MalleableWorkerError(f"{name} must be a lowercase SHA-256")
    return value


def _bounded_id_set(
    values: Any,
    name: str,
    maximum: int,
    *,
    digests: bool = False,
) -> set[str]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or len(values) > maximum
    ):
        raise MalleableWorkerError(f"{name} must be a bounded list")
    normalized = {
        _digest(value, name) if digests else _identity(value, name) for value in values
    }
    if len(normalized) != len(values):
        raise MalleableWorkerError(f"{name} contains duplicates")
    return normalized


@dataclass(frozen=True)
class MalleableWorkerPolicy:
    total_slots: int
    min_slots_per_runnable_job: int = 1
    backlog_per_slot: int = 1
    rebalance_hysteresis_slots: int = 1
    backlog_weight: int = 1000
    utility_weight: int = 1
    activation_weight: int = 2
    checker_reference_us: int = 1000
    event_lag_reference: int = 64
    max_jobs: int = 1024
    max_events: int = 1_000_000
    max_leases_per_worker: int = 256
    max_proofs_per_worker: int = 4096

    def __post_init__(self) -> None:
        _integer(self.total_slots, "malleable total slots", 1, MAX_SLOTS)
        _integer(
            self.min_slots_per_runnable_job,
            "malleable minimum slots",
            0,
            self.total_slots,
        )
        _integer(self.backlog_per_slot, "malleable backlog per slot", 1, 1_000_000)
        _integer(
            self.rebalance_hysteresis_slots,
            "malleable rebalance hysteresis",
            0,
            self.total_slots,
        )
        for value, name in (
            (self.backlog_weight, "backlog weight"),
            (self.utility_weight, "utility weight"),
            (self.activation_weight, "activation weight"),
        ):
            _integer(value, f"malleable {name}", 0, 100_000)
        _integer(
            self.checker_reference_us,
            "malleable checker reference",
            1,
            60_000_000,
        )
        _integer(
            self.event_lag_reference,
            "malleable event-lag reference",
            1,
            1_000_000,
        )
        _integer(self.max_jobs, "malleable job budget", 1, MAX_JOBS)
        _integer(self.max_events, "malleable event budget", 1, MAX_EVENTS)
        _integer(
            self.max_leases_per_worker,
            "malleable lease budget",
            1,
            MAX_LEASES_PER_WORKER,
        )
        _integer(
            self.max_proofs_per_worker,
            "malleable proof budget",
            1,
            MAX_PROOFS_PER_WORKER,
        )

    def as_config(self) -> dict[str, int]:
        return {
            "total_slots": self.total_slots,
            "min_slots_per_runnable_job": self.min_slots_per_runnable_job,
            "backlog_per_slot": self.backlog_per_slot,
            "rebalance_hysteresis_slots": self.rebalance_hysteresis_slots,
            "backlog_weight": self.backlog_weight,
            "utility_weight": self.utility_weight,
            "activation_weight": self.activation_weight,
            "checker_reference_us": self.checker_reference_us,
            "event_lag_reference": self.event_lag_reference,
            "max_jobs": self.max_jobs,
            "max_events": self.max_events,
            "max_leases_per_worker": self.max_leases_per_worker,
            "max_proofs_per_worker": self.max_proofs_per_worker,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MALLEABLE_POLICY_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            **self.as_config(),
        }

    @property
    def sha256(self) -> str:
        return _content_digest(self.as_dict())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MalleableWorkerPolicy":
        if not isinstance(raw, Mapping):
            raise MalleableWorkerError("malleable policy must be an object")
        if "total_slots" not in raw:
            raise MalleableWorkerError("malleable policy requires total_slots")
        defaults = cls(total_slots=raw["total_slots"])
        allowed = set(defaults.as_config())
        unknown = set(raw) - allowed
        if unknown:
            raise MalleableWorkerError(
                "unknown malleable policy option: " + sorted(unknown)[0]
            )
        values = defaults.as_config()
        values.update(raw)
        return cls(**values)

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "MalleableWorkerPolicy":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != MALLEABLE_POLICY_SCHEMA
            or raw.get("protocol") != MALLEABLE_WORKER_PROTOCOL
        ):
            raise MalleableWorkerError("sealed malleable policy scope changed")
        policy = cls.from_mapping(
            {
                key: value
                for key, value in raw.items()
                if key not in {"schema", "protocol"}
            }
        )
        if policy.as_dict() != dict(raw):
            raise MalleableWorkerError("sealed malleable policy changed")
        return policy


@dataclass(frozen=True)
class MalleableJobSignal:
    job_id: str
    formula_family_sha256: str
    backlog: int
    reward_total: int = 0
    outcomes: int = 0
    delivered: int = 0
    activated: int = 0
    checker_total_us: int = 0
    event_lag_total: int = 0

    def __post_init__(self) -> None:
        _identity(self.job_id, "malleable job")
        _digest(self.formula_family_sha256, "malleable formula family")
        _integer(self.backlog, "malleable backlog", 0, (1 << 63) - 1)
        _integer(
            self.reward_total,
            "malleable reward total",
            -((1 << 63) - 1),
            (1 << 63) - 1,
        )
        for value, name in (
            (self.outcomes, "outcomes"),
            (self.delivered, "delivered"),
            (self.activated, "activated"),
            (self.checker_total_us, "checker elapsed"),
            (self.event_lag_total, "event lag"),
        ):
            _integer(value, f"malleable {name}", 0, (1 << 63) - 1)
        if self.activated > self.delivered or self.delivered > self.outcomes:
            raise MalleableWorkerError("malleable feedback counts are inconsistent")
        if self.outcomes == 0 and any(
            (self.reward_total, self.delivered, self.activated)
        ):
            raise MalleableWorkerError("malleable feedback lacks outcomes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MALLEABLE_SIGNAL_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "job_id": self.job_id,
            "formula_family_sha256": self.formula_family_sha256,
            "backlog": self.backlog,
            "reward_total": self.reward_total,
            "outcomes": self.outcomes,
            "delivered": self.delivered,
            "activated": self.activated,
            "checker_total_us": self.checker_total_us,
            "event_lag_total": self.event_lag_total,
        }

    @property
    def sha256(self) -> str:
        return _content_digest(self.as_dict())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MalleableJobSignal":
        fields = {
            "job_id",
            "formula_family_sha256",
            "backlog",
            "reward_total",
            "outcomes",
            "delivered",
            "activated",
            "checker_total_us",
            "event_lag_total",
        }
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"schema", "protocol", *fields}
            or raw.get("schema") != MALLEABLE_SIGNAL_SCHEMA
            or raw.get("protocol") != MALLEABLE_WORKER_PROTOCOL
        ):
            raise MalleableWorkerError("malleable job signal shape changed")
        return cls(**{field: raw[field] for field in fields})


def job_signals_from_pairing_snapshots(
    jobs: Mapping[str, tuple[str, int]],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    pairing_policy: UtilityPairingPolicy,
) -> list[MalleableJobSignal]:
    """Aggregate independently verified F437 feedback by formula family."""
    if not isinstance(jobs, Mapping) or len(jobs) > MAX_JOBS:
        raise MalleableWorkerError("malleable job catalog is invalid")
    catalog: dict[str, tuple[str, int]] = {}
    for raw_job, raw_scope in jobs.items():
        job = _identity(raw_job, "malleable job")
        if not isinstance(raw_scope, tuple) or len(raw_scope) != 2:
            raise MalleableWorkerError("malleable job catalog scope is invalid")
        family = _digest(raw_scope[0], "malleable formula family")
        backlog = _integer(raw_scope[1], "malleable backlog", 0, (1 << 63) - 1)
        catalog[job] = (family, backlog)
    by_family: dict[str, PairFeedback] = {}
    seen_snapshots: set[str] = set()
    for raw in snapshots:
        try:
            snapshot = verify_pairing_snapshot(raw, policy=pairing_policy)
        except Exception as error:
            raise MalleableWorkerError(
                "malleable input pairing snapshot is invalid"
            ) from error
        identity = str(snapshot["snapshot_sha256"])
        if identity in seen_snapshots:
            raise MalleableWorkerError("duplicate pairing snapshot")
        seen_snapshots.add(identity)
        for state in snapshot["pairs"].values():
            family = str(state["formula_family_sha256"])
            feedback = PairFeedback.from_mapping(state["feedback"])
            aggregate = by_family.setdefault(family, PairFeedback())
            for name in aggregate.as_dict():
                setattr(
                    aggregate, name, getattr(aggregate, name) + getattr(feedback, name)
                )
    signals: list[MalleableJobSignal] = []
    for job, (family, backlog) in sorted(catalog.items()):
        feedback = by_family.get(family, PairFeedback())
        signals.append(
            MalleableJobSignal(
                job_id=job,
                formula_family_sha256=family,
                backlog=backlog,
                reward_total=feedback.reward_total,
                outcomes=feedback.outcomes,
                delivered=feedback.delivered,
                activated=feedback.unit + feedback.conflict,
                checker_total_us=feedback.checker_total_us,
                event_lag_total=feedback.event_lag_total,
            )
        )
    return signals


def _signal_score(policy: MalleableWorkerPolicy, signal: MalleableJobSignal) -> int:
    outcomes = max(1, signal.outcomes)
    delivered = max(1, signal.delivered)
    historical = signal.reward_total // outcomes
    delivery_yield = signal.delivered * 1000 // outcomes
    activation_yield = signal.activated * 1000 // delivered
    checker_penalty = min(
        5000,
        (signal.checker_total_us // outcomes) * 1000 // policy.checker_reference_us,
    )
    lag_penalty = min(
        5000,
        (signal.event_lag_total // outcomes) * 1000 // policy.event_lag_reference,
    )
    return (
        min(1_000_000, signal.backlog * policy.backlog_weight)
        + historical * policy.utility_weight
        + delivery_yield
        + activation_yield * policy.activation_weight
        - checker_penalty
        - lag_penalty
    )


def recommend_job_slots(
    policy: MalleableWorkerPolicy,
    signals: Sequence[MalleableJobSignal],
) -> dict[str, int]:
    """Return deterministic integer allocations with exact slot conservation."""
    if (
        not isinstance(signals, Sequence)
        or isinstance(signals, (str, bytes))
        or len(signals) > policy.max_jobs
    ):
        raise MalleableWorkerError("malleable signals must be a bounded list")
    unique: dict[str, MalleableJobSignal] = {}
    for signal in signals:
        if not isinstance(signal, MalleableJobSignal):
            raise MalleableWorkerError("malleable signal is invalid")
        if signal.job_id in unique:
            raise MalleableWorkerError("duplicate malleable job signal")
        unique[signal.job_id] = signal
    runnable = [signal for signal in unique.values() if signal.backlog > 0]
    demand = {
        signal.job_id: min(
            policy.total_slots,
            (signal.backlog + policy.backlog_per_slot - 1) // policy.backlog_per_slot,
        )
        for signal in runnable
    }
    allocation = {job: 0 for job in sorted(unique)}
    ordered = sorted(
        runnable,
        key=lambda signal: (-_signal_score(policy, signal), signal.job_id),
    )
    remaining = policy.total_slots
    minimum = policy.min_slots_per_runnable_job
    if minimum:
        for signal in ordered:
            granted = min(minimum, demand[signal.job_id], remaining)
            allocation[signal.job_id] += granted
            remaining -= granted
            if remaining == 0:
                break
    while remaining:
        eligible = [
            signal
            for signal in ordered
            if allocation[signal.job_id] < demand[signal.job_id]
        ]
        if not eligible:
            break
        selected = min(
            eligible,
            key=lambda signal: (
                -(_signal_score(policy, signal) // (allocation[signal.job_id] + 1)),
                signal.job_id,
            ),
        )
        allocation[selected.job_id] += 1
        remaining -= 1
    return allocation


def _desired_worker_jobs(
    policy: MalleableWorkerPolicy,
    worker_states: Mapping[str, tuple[str, int]],
    signals: Sequence[MalleableJobSignal],
) -> tuple[dict[str, str], dict[str, int]]:
    allocation = recommend_job_slots(policy, signals)
    desired = {worker: "" for worker in worker_states}
    retained: set[str] = set()
    for job in sorted(allocation):
        current = sorted(
            (
                worker
                for worker, (current_job, _leases) in worker_states.items()
                if current_job == job
            ),
            key=lambda worker: (-worker_states[worker][1], worker),
        )
        for worker in current[: allocation[job]]:
            desired[worker] = job
            retained.add(worker)
    free = sorted(
        set(worker_states) - retained,
        key=lambda worker: (
            0 if worker_states[worker][0] else 1,
            worker_states[worker][1],
            worker,
        ),
    )
    scores = {signal.job_id: _signal_score(policy, signal) for signal in signals}
    cursor = 0
    for job in sorted(allocation, key=lambda item: (-scores[item], item)):
        missing = allocation[job] - sum(value == job for value in desired.values())
        for worker in free[cursor : cursor + missing]:
            desired[worker] = job
        cursor += missing
    return desired, allocation


def _assignment_token(
    pool_id: str, worker_id: str, generation: int, job_id: str
) -> str:
    return _content_digest(
        {
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "pool_id": pool_id,
            "worker_id": worker_id,
            "assignment_generation": generation,
            "job_id": job_id,
        }
    )


@dataclass
class _Assignment:
    worker_id: str
    job_id: str
    generation: int
    token: str
    draining: bool = False
    leases: set[str] = field(default_factory=set)
    durable_proofs: set[str] = field(default_factory=set)
    proof_cursor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "job_id": self.job_id,
            "assignment_generation": self.generation,
            "assignment_token": self.token,
            "state": "draining"
            if self.draining
            else ("active" if self.job_id else "standby"),
            "leases": sorted(self.leases),
            "durable_proofs": sorted(self.durable_proofs),
            "proof_cursor": self.proof_cursor,
        }


class MalleableWorkerController:
    """Replayable two-phase controller for one fixed physical worker pool."""

    def __init__(
        self,
        pool_id: str,
        worker_ids: Sequence[str],
        policy: MalleableWorkerPolicy,
    ) -> None:
        self.pool_id = _identity(pool_id, "malleable pool")
        if (
            not isinstance(worker_ids, Sequence)
            or isinstance(worker_ids, (str, bytes))
            or len(worker_ids) != policy.total_slots
        ):
            raise MalleableWorkerError("worker inventory must equal total_slots")
        workers = [_identity(worker, "malleable worker") for worker in worker_ids]
        if len(set(workers)) != len(workers):
            raise MalleableWorkerError("malleable worker inventory has duplicates")
        self.policy = policy
        self._allocation_generation = 0
        self._next_event_ordinal = 1
        self._assignments = {
            worker: _Assignment(
                worker_id=worker,
                job_id="",
                generation=0,
                token=_assignment_token(self.pool_id, worker, 0, ""),
            )
            for worker in sorted(workers)
        }
        self._pending: dict[str, Any] | None = None
        self._totals = {
            "prepared": 0,
            "committed": 0,
            "aborted": 0,
            "drain_receipts": 0,
            "leases_attached": 0,
            "leases_retired": 0,
            "proofs_durable": 0,
        }

    @property
    def allocation_generation(self) -> int:
        return self._allocation_generation

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def _consume_event(self) -> int:
        ordinal = _integer(
            self._next_event_ordinal,
            "malleable event ordinal",
            1,
            self.policy.max_events,
        )
        self._next_event_ordinal += 1
        return ordinal

    def permission(self, worker_id: str) -> dict[str, Any]:
        worker = _identity(worker_id, "malleable worker")
        try:
            assignment = self._assignments[worker]
        except KeyError as error:
            raise MalleableWorkerError("worker is outside the pool") from error
        body = {
            "schema": "symcc-malleable-worker-permission-v1",
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "pool_id": self.pool_id,
            "policy_sha256": self.policy.sha256,
            "allocation_generation": self._allocation_generation,
            **assignment.as_dict(),
        }
        body["permission_sha256"] = _content_digest(body)
        return body

    def _check_fence(self, worker_id: str, generation: int, token: str) -> _Assignment:
        worker = _identity(worker_id, "malleable worker")
        assignment = self._assignments.get(worker)
        if assignment is None:
            raise MalleableWorkerError("worker is outside the pool")
        if (
            type(generation) is not int
            or generation != assignment.generation
            or _digest(token, "malleable assignment token") != assignment.token
        ):
            raise MalleableWorkerError("stale malleable assignment fence")
        return assignment

    def attach_lease(
        self,
        worker_id: str,
        generation: int,
        token: str,
        lease_id: str,
    ) -> dict[str, Any]:
        assignment = self._check_fence(worker_id, generation, token)
        lease = _identity(lease_id, "malleable lease")
        if not assignment.job_id or assignment.draining:
            raise MalleableWorkerError("assignment is not admitting leases")
        if lease in assignment.leases:
            raise MalleableWorkerError("malleable lease is already attached")
        if len(assignment.leases) >= self.policy.max_leases_per_worker:
            raise MalleableWorkerError("malleable lease budget is exhausted")
        assignment.leases.add(lease)
        event = {
            "event_ordinal": self._consume_event(),
            "worker_id": assignment.worker_id,
            "assignment_generation": assignment.generation,
            "assignment_token": assignment.token,
            "lease_id": lease,
        }
        event["event_sha256"] = _content_digest(event)
        self._totals["leases_attached"] += 1
        return event

    def finish_lease(
        self,
        worker_id: str,
        generation: int,
        token: str,
        lease_id: str,
        *,
        durable_proofs: Sequence[str] = (),
    ) -> dict[str, Any]:
        assignment = self._check_fence(worker_id, generation, token)
        lease = _identity(lease_id, "malleable lease")
        if lease not in assignment.leases:
            raise MalleableWorkerError("malleable lease is not owned")
        proofs = _bounded_id_set(
            durable_proofs,
            "malleable durable proof",
            self.policy.max_proofs_per_worker,
            digests=True,
        )
        if len(assignment.durable_proofs | proofs) > self.policy.max_proofs_per_worker:
            raise MalleableWorkerError("malleable proof budget is exhausted")
        assignment.leases.remove(lease)
        new_proofs = proofs - assignment.durable_proofs
        assignment.durable_proofs.update(proofs)
        assignment.proof_cursor += len(new_proofs)
        if (
            self._pending is not None
            and assignment.worker_id in self._pending["retired_leases"]
        ):
            expected = set(
                self._pending["transition"]["expected_leases"][assignment.worker_id]
            )
            if lease in expected:
                self._pending["retired_leases"][assignment.worker_id].add(lease)
        event = {
            "event_ordinal": self._consume_event(),
            "worker_id": assignment.worker_id,
            "assignment_generation": assignment.generation,
            "assignment_token": assignment.token,
            "lease_id": lease,
            "durable_proofs": sorted(proofs),
            "proof_cursor": assignment.proof_cursor,
        }
        event["event_sha256"] = _content_digest(event)
        self._totals["leases_retired"] += 1
        self._totals["proofs_durable"] += len(new_proofs)
        return event

    def _desired_jobs(
        self,
        signals: Sequence[MalleableJobSignal],
    ) -> tuple[dict[str, str], dict[str, int]]:
        return _desired_worker_jobs(
            self.policy,
            {
                worker: (assignment.job_id, len(assignment.leases))
                for worker, assignment in self._assignments.items()
            },
            signals,
        )

    def prepare(self, signals: Sequence[MalleableJobSignal]) -> dict[str, Any] | None:
        if self._pending is not None:
            raise MalleableWorkerError("malleable transition is already pending")
        signal_list = list(signals)
        desired, allocation = self._desired_jobs(signal_list)
        current = {worker: item.job_id for worker, item in self._assignments.items()}
        changed = sum(current[worker] != desired[worker] for worker in current)
        if changed == 0:
            return None
        if (
            self._allocation_generation > 0
            and changed < self.policy.rebalance_hysteresis_slots
        ):
            return None
        target_generation = self._allocation_generation + 1
        drains = sorted(
            worker
            for worker, assignment in self._assignments.items()
            if assignment.job_id and desired[worker] != assignment.job_id
        )
        expected_leases = {
            worker: sorted(self._assignments[worker].leases) for worker in drains
        }
        transition = {
            "schema": MALLEABLE_TRANSITION_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "pool_id": self.pool_id,
            "policy_sha256": self.policy.sha256,
            "event_ordinal": self._consume_event(),
            "base_allocation_generation": self._allocation_generation,
            "target_allocation_generation": target_generation,
            "signals": [
                signal.as_dict()
                for signal in sorted(signal_list, key=lambda item: item.job_id)
            ],
            "allocation": dict(sorted(allocation.items())),
            "desired_jobs": dict(sorted(desired.items())),
            "drain_workers": drains,
            "expected_leases": expected_leases,
        }
        transition["transition_sha256"] = _content_digest(transition)
        for worker in drains:
            self._assignments[worker].draining = True
        self._pending = {
            "transition": transition,
            "retired_leases": {worker: set() for worker in drains},
            "receipts": {},
        }
        self._totals["prepared"] += 1
        return dict(transition)

    def acknowledge_drain(
        self,
        worker_id: str,
        generation: int,
        token: str,
        *,
        returned_leases: Sequence[str],
        durable_proofs: Sequence[str],
        proof_cursor: int,
    ) -> dict[str, Any]:
        if self._pending is None:
            raise MalleableWorkerError("no malleable transition is pending")
        assignment = self._check_fence(worker_id, generation, token)
        worker = assignment.worker_id
        transition = self._pending["transition"]
        if worker not in transition["drain_workers"] or not assignment.draining:
            raise MalleableWorkerError("worker is not draining")
        if worker in self._pending["receipts"]:
            raise MalleableWorkerError("worker drain is already acknowledged")
        returned = _bounded_id_set(
            returned_leases,
            "malleable returned lease",
            self.policy.max_leases_per_worker,
        )
        proofs = _bounded_id_set(
            durable_proofs,
            "malleable durable proof",
            self.policy.max_proofs_per_worker,
            digests=True,
        )
        expected = set(transition["expected_leases"][worker])
        if (
            assignment.leases
            or returned != expected
            or self._pending["retired_leases"][worker] != expected
        ):
            raise MalleableWorkerError("drain did not retire the exact lease set")
        if proofs != assignment.durable_proofs:
            raise MalleableWorkerError("drain did not seal every durable proof")
        cursor = _integer(
            proof_cursor,
            "malleable proof cursor",
            0,
            (1 << 63) - 1,
        )
        if cursor != assignment.proof_cursor:
            raise MalleableWorkerError("malleable proof cursor changed")
        receipt = {
            "schema": MALLEABLE_DRAIN_RECEIPT_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "transition_sha256": transition["transition_sha256"],
            "event_ordinal": self._consume_event(),
            "worker_id": worker,
            "assignment_generation": assignment.generation,
            "assignment_token": assignment.token,
            "returned_leases": sorted(returned),
            "durable_proofs": sorted(proofs),
            "proof_cursor": cursor,
        }
        receipt["receipt_sha256"] = _content_digest(receipt)
        self._pending["receipts"][worker] = receipt
        self._totals["drain_receipts"] += 1
        return dict(receipt)

    def commit(self) -> dict[str, Any]:
        if self._pending is None:
            raise MalleableWorkerError("no malleable transition is pending")
        transition = self._pending["transition"]
        if set(self._pending["receipts"]) != set(transition["drain_workers"]):
            raise MalleableWorkerError("malleable drains are incomplete")
        old_tokens = {
            worker: assignment.token for worker, assignment in self._assignments.items()
        }
        target_generation = int(transition["target_allocation_generation"])
        for worker, assignment in self._assignments.items():
            job = str(transition["desired_jobs"][worker])
            if job != assignment.job_id:
                if assignment.leases:
                    raise MalleableWorkerError("changed assignment still owns leases")
                assignment.job_id = job
                assignment.generation = target_generation
                assignment.token = _assignment_token(
                    self.pool_id, worker, target_generation, job
                )
                assignment.durable_proofs.clear()
            assignment.draining = False
        self._allocation_generation = target_generation
        event_ordinal = self._consume_event()
        changed = sorted(
            worker
            for worker, assignment in self._assignments.items()
            if assignment.token != old_tokens[worker]
        )
        commit = {
            "schema": MALLEABLE_COMMIT_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "pool_id": self.pool_id,
            "policy_sha256": self.policy.sha256,
            "transition_sha256": transition["transition_sha256"],
            "event_ordinal": event_ordinal,
            "allocation_generation": self._allocation_generation,
            "changed_workers": changed,
            "assignment_tokens": {
                worker: self._assignments[worker].token for worker in changed
            },
            "allocation": dict(transition["allocation"]),
        }
        commit["commit_sha256"] = _content_digest(commit)
        self._pending = None
        self._totals["committed"] += 1
        return commit

    def abort(self) -> dict[str, Any]:
        if self._pending is None:
            raise MalleableWorkerError("no malleable transition is pending")
        transition = self._pending["transition"]
        for worker in transition["drain_workers"]:
            self._assignments[worker].draining = False
        event = {
            "schema": "symcc-malleable-worker-abort-v1",
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "transition_sha256": transition["transition_sha256"],
            "event_ordinal": self._consume_event(),
        }
        event["abort_sha256"] = _content_digest(event)
        self._pending = None
        self._totals["aborted"] += 1
        return event

    def snapshot(self) -> dict[str, Any]:
        pending = None
        if self._pending is not None:
            pending = {
                "transition": self._pending["transition"],
                "retired_leases": {
                    worker: sorted(leases)
                    for worker, leases in sorted(
                        self._pending["retired_leases"].items()
                    )
                },
                "receipts": dict(sorted(self._pending["receipts"].items())),
            }
        body = {
            "schema": MALLEABLE_SNAPSHOT_SCHEMA,
            "protocol": MALLEABLE_WORKER_PROTOCOL,
            "pool_id": self.pool_id,
            "policy_sha256": self.policy.sha256,
            "allocation_generation": self._allocation_generation,
            "next_event_ordinal": self._next_event_ordinal,
            "assignments": {
                worker: assignment.as_dict()
                for worker, assignment in sorted(self._assignments.items())
            },
            "pending_transition": pending,
            "totals": dict(self._totals),
        }
        body["snapshot_sha256"] = _content_digest(body)
        return body

    @classmethod
    def from_snapshot(
        cls,
        policy: MalleableWorkerPolicy,
        raw: Mapping[str, Any],
    ) -> "MalleableWorkerController":
        snapshot = verify_malleable_snapshot(raw, policy=policy)
        controller = cls(
            snapshot["pool_id"],
            sorted(snapshot["assignments"]),
            policy,
        )
        controller._allocation_generation = snapshot["allocation_generation"]
        controller._next_event_ordinal = snapshot["next_event_ordinal"]
        controller._totals = dict(snapshot["totals"])
        controller._assignments = {}
        for worker, raw_assignment in snapshot["assignments"].items():
            controller._assignments[worker] = _Assignment(
                worker_id=worker,
                job_id=raw_assignment["job_id"],
                generation=raw_assignment["assignment_generation"],
                token=raw_assignment["assignment_token"],
                draining=raw_assignment["state"] == "draining",
                leases=set(raw_assignment["leases"]),
                durable_proofs=set(raw_assignment["durable_proofs"]),
                proof_cursor=raw_assignment["proof_cursor"],
            )
        raw_pending = snapshot["pending_transition"]
        if raw_pending is not None:
            controller._pending = {
                "transition": dict(raw_pending["transition"]),
                "retired_leases": {
                    worker: set(leases)
                    for worker, leases in raw_pending["retired_leases"].items()
                },
                "receipts": {
                    worker: dict(receipt)
                    for worker, receipt in raw_pending["receipts"].items()
                },
            }
        return controller


def verify_malleable_snapshot(
    raw: Mapping[str, Any], *, policy: MalleableWorkerPolicy
) -> dict[str, Any]:
    """Strictly verify a persistent controller checkpoint and its conservation."""
    if not isinstance(raw, Mapping):
        raise MalleableWorkerError("malleable snapshot must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("snapshot_sha256", ""), "malleable snapshot")
    if _content_digest(body) != supplied:
        raise MalleableWorkerError("malleable snapshot identity changed")
    if set(body) != {
        "schema",
        "protocol",
        "pool_id",
        "policy_sha256",
        "allocation_generation",
        "next_event_ordinal",
        "assignments",
        "pending_transition",
        "totals",
    }:
        raise MalleableWorkerError("malleable snapshot shape changed")
    if (
        body.get("schema") != MALLEABLE_SNAPSHOT_SCHEMA
        or body.get("protocol") != MALLEABLE_WORKER_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
    ):
        raise MalleableWorkerError("malleable snapshot scope changed")
    pool = _identity(body.get("pool_id"), "malleable pool")
    generation = _integer(
        body.get("allocation_generation"),
        "malleable allocation generation",
        0,
        policy.max_events,
    )
    next_event = _integer(
        body.get("next_event_ordinal"),
        "malleable next event ordinal",
        1,
        policy.max_events + 1,
    )
    raw_assignments = body.get("assignments")
    if (
        not isinstance(raw_assignments, Mapping)
        or len(raw_assignments) != policy.total_slots
    ):
        raise MalleableWorkerError("malleable worker inventory changed")
    assignments: dict[str, dict[str, Any]] = {}
    for raw_worker, raw_assignment in raw_assignments.items():
        worker = _identity(raw_worker, "malleable worker")
        if not isinstance(raw_assignment, Mapping) or set(raw_assignment) != {
            "worker_id",
            "job_id",
            "assignment_generation",
            "assignment_token",
            "state",
            "leases",
            "durable_proofs",
            "proof_cursor",
        }:
            raise MalleableWorkerError("malleable assignment shape changed")
        if raw_assignment.get("worker_id") != worker:
            raise MalleableWorkerError("malleable worker identity changed")
        job = raw_assignment.get("job_id")
        if type(job) is not str or (job and _identity(job, "malleable job") != job):
            raise MalleableWorkerError("malleable assignment job is invalid")
        assignment_generation = _integer(
            raw_assignment.get("assignment_generation"),
            "malleable assignment generation",
            0,
            generation,
        )
        token = _digest(
            raw_assignment.get("assignment_token"), "malleable assignment token"
        )
        if token != _assignment_token(pool, worker, assignment_generation, job):
            raise MalleableWorkerError("malleable assignment token changed")
        state = raw_assignment.get("state")
        if state not in {"active", "draining", "standby"}:
            raise MalleableWorkerError("malleable assignment state changed")
        if (not job) != (state == "standby"):
            raise MalleableWorkerError("malleable standby assignment changed")
        leases = _bounded_id_set(
            raw_assignment.get("leases"),
            "malleable lease",
            policy.max_leases_per_worker,
        )
        proofs = _bounded_id_set(
            raw_assignment.get("durable_proofs"),
            "malleable durable proof",
            policy.max_proofs_per_worker,
            digests=True,
        )
        proof_cursor = _integer(
            raw_assignment.get("proof_cursor"),
            "malleable proof cursor",
            len(proofs),
            (1 << 63) - 1,
        )
        assignments[worker] = {
            **dict(raw_assignment),
            "leases": sorted(leases),
            "durable_proofs": sorted(proofs),
            "proof_cursor": proof_cursor,
        }
    totals = body.get("totals")
    total_fields = {
        "prepared",
        "committed",
        "aborted",
        "drain_receipts",
        "leases_attached",
        "leases_retired",
        "proofs_durable",
    }
    if not isinstance(totals, Mapping) or set(totals) != total_fields:
        raise MalleableWorkerError("malleable totals shape changed")
    normalized_totals = {
        name: _integer(value, f"malleable total {name}", 0, (1 << 63) - 1)
        for name, value in totals.items()
    }
    if (
        normalized_totals["committed"] + normalized_totals["aborted"]
        > normalized_totals["prepared"]
    ):
        raise MalleableWorkerError("malleable transition totals are inconsistent")
    pending = body.get("pending_transition")
    if pending is None:
        if any(item["state"] == "draining" for item in assignments.values()):
            raise MalleableWorkerError("draining worker lacks a transition")
    else:
        pending = _verify_pending(
            pending,
            policy=policy,
            pool_id=pool,
            allocation_generation=generation,
            next_event_ordinal=next_event,
            assignments=assignments,
        )
    pending_count = int(pending is not None)
    if (
        normalized_totals["prepared"]
        != normalized_totals["committed"] + normalized_totals["aborted"] + pending_count
        or generation != normalized_totals["committed"]
        or normalized_totals["leases_attached"] - normalized_totals["leases_retired"]
        != sum(len(item["leases"]) for item in assignments.values())
        or normalized_totals["proofs_durable"]
        != sum(item["proof_cursor"] for item in assignments.values())
        or next_event - 1
        != normalized_totals["prepared"]
        + normalized_totals["committed"]
        + normalized_totals["aborted"]
        + normalized_totals["drain_receipts"]
        + normalized_totals["leases_attached"]
        + normalized_totals["leases_retired"]
    ):
        raise MalleableWorkerError("malleable event conservation failed")
    active_slots = sum(bool(item["job_id"]) for item in assignments.values())
    if not 0 <= active_slots <= policy.total_slots:
        raise MalleableWorkerError("malleable slot conservation failed")
    return {
        **body,
        "pool_id": pool,
        "allocation_generation": generation,
        "next_event_ordinal": next_event,
        "assignments": assignments,
        "pending_transition": pending,
        "totals": normalized_totals,
        "snapshot_sha256": supplied,
    }


def _verify_pending(
    raw: Any,
    *,
    policy: MalleableWorkerPolicy,
    pool_id: str,
    allocation_generation: int,
    next_event_ordinal: int,
    assignments: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "transition",
        "retired_leases",
        "receipts",
    }:
        raise MalleableWorkerError("malleable pending transition shape changed")
    transition = raw.get("transition")
    if not isinstance(transition, Mapping):
        raise MalleableWorkerError("malleable transition is invalid")
    sealed = dict(transition)
    transition_sha = _digest(
        sealed.pop("transition_sha256", ""), "malleable transition"
    )
    if _content_digest(sealed) != transition_sha:
        raise MalleableWorkerError("malleable transition identity changed")
    expected_fields = {
        "schema",
        "protocol",
        "pool_id",
        "policy_sha256",
        "event_ordinal",
        "base_allocation_generation",
        "target_allocation_generation",
        "signals",
        "allocation",
        "desired_jobs",
        "drain_workers",
        "expected_leases",
    }
    if set(sealed) != expected_fields or (
        sealed.get("schema") != MALLEABLE_TRANSITION_SCHEMA
        or sealed.get("protocol") != MALLEABLE_WORKER_PROTOCOL
        or sealed.get("pool_id") != pool_id
        or sealed.get("policy_sha256") != policy.sha256
    ):
        raise MalleableWorkerError("malleable transition scope changed")
    base = _integer(
        sealed.get("base_allocation_generation"),
        "malleable transition base generation",
        0,
        policy.max_events,
    )
    target = _integer(
        sealed.get("target_allocation_generation"),
        "malleable transition target generation",
        1,
        policy.max_events,
    )
    if base != allocation_generation or target != base + 1:
        raise MalleableWorkerError("malleable transition generation changed")
    event = _integer(
        sealed.get("event_ordinal"),
        "malleable transition event",
        1,
        next_event_ordinal - 1,
    )
    signals_raw = sealed.get("signals")
    if not isinstance(signals_raw, list):
        raise MalleableWorkerError("malleable transition signals changed")
    signals = [MalleableJobSignal.from_mapping(item) for item in signals_raw]
    if [signal.job_id for signal in signals] != sorted(
        signal.job_id for signal in signals
    ):
        raise MalleableWorkerError("malleable transition signals are not canonical")
    recomputed_desired, allocation = _desired_worker_jobs(
        policy,
        {
            worker: (
                str(assignment["job_id"]),
                len(assignment["leases"]),
            )
            for worker, assignment in assignments.items()
        },
        signals,
    )
    if sealed.get("allocation") != dict(sorted(allocation.items())):
        raise MalleableWorkerError("malleable transition allocation changed")
    desired = sealed.get("desired_jobs")
    if not isinstance(desired, Mapping) or set(desired) != set(assignments):
        raise MalleableWorkerError("malleable desired worker inventory changed")
    if desired != dict(sorted(recomputed_desired.items())):
        raise MalleableWorkerError("malleable desired worker mapping changed")
    desired_counts: dict[str, int] = {job: 0 for job in allocation}
    for worker, raw_job in desired.items():
        if type(raw_job) is not str or raw_job not in {*allocation, ""}:
            raise MalleableWorkerError("malleable desired job changed")
        if raw_job:
            desired_counts[raw_job] += 1
    if desired_counts != allocation:
        raise MalleableWorkerError("malleable desired slot conservation failed")
    drains = _bounded_id_set(
        sealed.get("drain_workers"),
        "malleable drain worker",
        policy.total_slots,
    )
    if not drains <= set(assignments):
        raise MalleableWorkerError("malleable drain worker is outside the pool")
    expected_drains = {
        worker
        for worker, assignment in assignments.items()
        if assignment["job_id"] and desired[worker] != assignment["job_id"]
    }
    if drains != expected_drains:
        raise MalleableWorkerError("malleable drain set changed")
    expected_raw = sealed.get("expected_leases")
    if not isinstance(expected_raw, Mapping) or set(expected_raw) != drains:
        raise MalleableWorkerError("malleable expected lease scope changed")
    expected = {
        worker: sorted(
            _bounded_id_set(
                expected_raw[worker],
                "malleable expected lease",
                policy.max_leases_per_worker,
            )
        )
        for worker in drains
    }
    retired_raw = raw.get("retired_leases")
    if not isinstance(retired_raw, Mapping) or set(retired_raw) != drains:
        raise MalleableWorkerError("malleable retired lease scope changed")
    retired: dict[str, list[str]] = {}
    for worker in drains:
        values = _bounded_id_set(
            retired_raw[worker],
            "malleable retired lease",
            policy.max_leases_per_worker,
        )
        if not values <= set(expected[worker]):
            raise MalleableWorkerError("malleable retired lease was not expected")
        retired[worker] = sorted(values)
        if assignments[worker]["state"] != "draining":
            raise MalleableWorkerError("malleable drain state changed")
        live = set(assignments[worker]["leases"])
        if live & values or live | values != set(expected[worker]):
            raise MalleableWorkerError("malleable lease conservation failed")
    receipts_raw = raw.get("receipts")
    if not isinstance(receipts_raw, Mapping) or not set(receipts_raw) <= drains:
        raise MalleableWorkerError("malleable receipt scope changed")
    receipts: dict[str, dict[str, Any]] = {}
    for worker, raw_receipt in receipts_raw.items():
        receipt = _verify_drain_receipt(
            raw_receipt,
            policy=policy,
            transition_sha256=transition_sha,
            next_event_ordinal=next_event_ordinal,
            assignment=assignments[worker],
            expected_leases=expected[worker],
            retired_leases=retired[worker],
        )
        receipts[worker] = receipt
    event_ordinals = [event] + [
        int(receipt["event_ordinal"]) for receipt in receipts.values()
    ]
    if len(set(event_ordinals)) != len(event_ordinals) or any(
        ordinal <= event for ordinal in event_ordinals[1:]
    ):
        raise MalleableWorkerError("malleable transition event order changed")
    return {
        "transition": {
            **sealed,
            "event_ordinal": event,
            "transition_sha256": transition_sha,
        },
        "retired_leases": retired,
        "receipts": receipts,
    }


def _verify_drain_receipt(
    raw: Any,
    *,
    policy: MalleableWorkerPolicy,
    transition_sha256: str,
    next_event_ordinal: int,
    assignment: Mapping[str, Any],
    expected_leases: Sequence[str],
    retired_leases: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MalleableWorkerError("malleable drain receipt is invalid")
    body = dict(raw)
    supplied = _digest(body.pop("receipt_sha256", ""), "malleable drain receipt")
    if _content_digest(body) != supplied:
        raise MalleableWorkerError("malleable drain receipt identity changed")
    if set(body) != {
        "schema",
        "protocol",
        "policy_sha256",
        "transition_sha256",
        "event_ordinal",
        "worker_id",
        "assignment_generation",
        "assignment_token",
        "returned_leases",
        "durable_proofs",
        "proof_cursor",
    } or (
        body.get("schema") != MALLEABLE_DRAIN_RECEIPT_SCHEMA
        or body.get("protocol") != MALLEABLE_WORKER_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
        or body.get("transition_sha256") != transition_sha256
        or body.get("worker_id") != assignment["worker_id"]
        or body.get("assignment_generation") != assignment["assignment_generation"]
        or body.get("assignment_token") != assignment["assignment_token"]
    ):
        raise MalleableWorkerError("malleable drain receipt scope changed")
    _integer(
        body.get("event_ordinal"),
        "malleable receipt event",
        1,
        next_event_ordinal - 1,
    )
    returned = sorted(
        _bounded_id_set(
            body.get("returned_leases"),
            "malleable returned lease",
            policy.max_leases_per_worker,
        )
    )
    proofs = sorted(
        _bounded_id_set(
            body.get("durable_proofs"),
            "malleable durable proof",
            policy.max_proofs_per_worker,
            digests=True,
        )
    )
    cursor = _integer(
        body.get("proof_cursor"),
        "malleable receipt proof cursor",
        0,
        (1 << 63) - 1,
    )
    if (
        returned != list(expected_leases)
        or returned != list(retired_leases)
        or proofs != assignment["durable_proofs"]
        or cursor != assignment["proof_cursor"]
        or assignment["leases"]
    ):
        raise MalleableWorkerError("malleable drain receipt is incomplete")
    return {**body, "receipt_sha256": supplied}
