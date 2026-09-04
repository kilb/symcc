#!/usr/bin/env python3
"""Fail-closed contracts for multi-rank checked QF_BV proof-stream trials."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from qfbv_incremental_sat import BitBlastPlan, bitblast_qfbv_query
from qfbv_utility_pairing import (
    UTILITY_PAIRING_PROTOCOL,
    UtilityPairingError,
    formula_family_sha256,
    verify_pairing_stream_result,
)


PROTOCOL = "symcc-qfbv-realtime-multirank-v1"
CLAUSE_ACTIVITY_PROTOCOL = "symcc-qfbv-native-clause-activity-v1"
CONFIG_SCHEMA = "symcc-f434-multirank-config-v1"
RANK_REPORT_SCHEMA = "symcc-f434-multirank-rank-report-v1"
RESULT_SCHEMA = "symcc-f434-multirank-result-v1"
MAX_RANKS = 16_384
MAX_ROUNDS = 4096
MAX_VARIABLES = 4096
MAX_CLAUSES = 50_000
_HEX64 = re.compile(r"[0-9a-f]{64}")


class MultirankEvaluationError(ValueError):
    """The experiment configuration or evidence failed closed."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise MultirankEvaluationError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise MultirankEvaluationError(f"{name} must be in [{lower}, {upper}]")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise MultirankEvaluationError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class MultirankConfig:
    world_size: int
    publishers: int
    rounds: int
    seed: int
    variables: int
    clauses: int
    mode: str = "active"
    solve_timeout_ms: int = 30_000
    poll_interval_ms: int = 1
    track_clause_activity: bool = False
    utility_pairing: bool = False

    def __post_init__(self) -> None:
        size = _integer(self.world_size, "world size", 3, MAX_RANKS)
        publishers = _integer(self.publishers, "publisher count", 1, size - 2)
        _integer(self.rounds, "round count", 1, MAX_ROUNDS)
        _integer(self.seed, "experiment seed", 0, (1 << 63) - 1)
        variables = _integer(self.variables, "SAT variable count", 8, MAX_VARIABLES)
        clauses = _integer(self.clauses, "SAT clause count", 1, MAX_CLAUSES)
        if clauses < variables:
            raise MultirankEvaluationError(
                "SAT clause count must not be smaller than its variable count"
            )
        if self.mode not in {"active", "preloaded"}:
            raise MultirankEvaluationError("mode must be active or preloaded")
        _integer(self.solve_timeout_ms, "solve timeout", 100, 3_600_000)
        _integer(self.poll_interval_ms, "poll interval", 1, 1000)
        if type(self.track_clause_activity) is not bool:
            raise MultirankEvaluationError(
                "clause-activity tracking must be boolean"
            )
        if type(self.utility_pairing) is not bool:
            raise MultirankEvaluationError("utility pairing must be boolean")
        if self.utility_pairing and not self.track_clause_activity:
            raise MultirankEvaluationError(
                "utility pairing requires clause-activity tracking"
            )
        if self.utility_pairing and self.rounds < 3:
            raise MultirankEvaluationError(
                "utility pairing evaluation requires at least three rounds"
            )
        if publishers >= size - 1:
            raise MultirankEvaluationError("at least one consumer rank is required")

    @property
    def publisher_ranks(self) -> tuple[int, ...]:
        return tuple(range(1, 1 + self.publishers))

    @property
    def consumer_ranks(self) -> tuple[int, ...]:
        return tuple(range(1 + self.publishers, self.world_size))

    def role(self, rank: int) -> str:
        normalized = _integer(rank, "rank", 0, self.world_size - 1)
        if normalized == 0:
            return "coordinator"
        return "publisher" if normalized in self.publisher_ranks else "consumer"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONFIG_SCHEMA,
            "protocol": PROTOCOL,
            "world_size": self.world_size,
            "publishers": self.publishers,
            "publisher_ranks": list(self.publisher_ranks),
            "consumer_ranks": list(self.consumer_ranks),
            "rounds": self.rounds,
            "seed": self.seed,
            "variables": self.variables,
            "clauses": self.clauses,
            "mode": self.mode,
            "solve_timeout_ms": self.solve_timeout_ms,
            "poll_interval_ms": self.poll_interval_ms,
            "track_clause_activity": self.track_clause_activity,
            "utility_pairing": self.utility_pairing,
        }

    @property
    def sha256(self) -> str:
        return content_digest(self.as_dict())


def build_random_3sat_plan(config: MultirankConfig, round_index: int) -> BitBlastPlan:
    """Build one deterministic activation-guarded near-threshold 3-SAT plan."""
    ordinal = _integer(round_index, "round index", 0, config.rounds - 1)
    rng = random.Random(config.seed + ordinal * 0x9E3779B1)
    negated_positions = set(rng.sample(
        range(config.clauses * 3),
        (config.clauses * 3) // 2,
    ))
    expressions: dict[str, dict[str, Any]] = {}
    variables: list[str] = []
    for index in range(config.variables):
        read = f"read-{index // 8}"
        expressions.setdefault(read, {
            "op": "read", "bits": 8, "children": [],
            "attrs": {"index": index // 8},
        })
        bit = f"bit-{index}"
        expressions[bit] = {
            "op": "extract", "bits": 1, "children": [read],
            "attrs": {"index": index % 8},
        }
        one = f"one-{index}"
        expressions[one] = {
            "op": "constant", "bits": 1, "children": [],
            "attrs": {"value_hex": "1"},
        }
        variable = f"variable-{index}"
        expressions[variable] = {
            "op": "equal", "bits": 1, "children": [bit, one], "attrs": {},
        }
        variables.append(variable)

    formula_nodes: list[str] = []
    for clause_index in range(config.clauses):
        terms: list[str] = []
        for term_index, variable_index in enumerate(
            rng.sample(range(config.variables), 3)
        ):
            term = variables[variable_index]
            # Random positions with a fixed cardinality keep formula-family
            # shape stable and make baseline/pairing trials exactly paired.
            if clause_index * 3 + term_index in negated_positions:
                negated = f"not-{clause_index}-{term_index}"
                expressions[negated] = {
                    "op": "lnot", "bits": 1,
                    "children": [term], "attrs": {},
                }
                term = negated
            terms.append(term)
        clause = f"clause-{clause_index}"
        expressions[clause] = {
            "op": "lor", "bits": 1, "children": terms, "attrs": {},
        }
        formula_nodes.append(clause)

    level = 0
    while len(formula_nodes) > 1:
        next_level: list[str] = []
        for offset in range(0, len(formula_nodes), 3):
            children = formula_nodes[offset:offset + 3]
            if len(children) == 1:
                next_level.append(children[0])
                continue
            node = f"conjunction-{level}-{offset // 3}"
            expressions[node] = {
                "op": "land", "bits": 1, "children": children, "attrs": {},
            }
            next_level.append(node)
        formula_nodes = next_level
        level += 1

    return bitblast_qfbv_query(
        f"f434-random-3sat-{config.seed}-{ordinal}",
        formula_nodes,
        expressions,
        {
            "incremental": True,
            "max_nodes": min(250_000, len(expressions) + 16),
            "max_input_bytes": (config.variables + 7) // 8,
        },
    )


def exchange_clauses(plan: BitBlastPlan, count: int) -> tuple[tuple[int, ...], ...]:
    """Select deterministic distinct base clauses for independent publishers."""
    needed = _integer(count, "exchange clause count", 1, 4096)
    unique = sorted(
        {tuple(clause) for clause in plan.clauses if clause},
        key=lambda clause: (len(clause), clause),
    )
    if len(unique) < needed:
        raise MultirankEvaluationError("plan has too few distinct exchange clauses")
    return tuple(unique[:needed])


def percentile_summary(samples: Sequence[int]) -> dict[str, int]:
    values = sorted(_integer(value, "timing sample", 0, (1 << 63) - 1)
                    for value in samples)
    if not values:
        return {"samples": 0, "minimum_us": 0, "median_us": 0,
                "p95_us": 0, "maximum_us": 0, "total_us": 0}
    return {
        "samples": len(values),
        "minimum_us": values[0],
        "median_us": int(statistics.median(values)),
        "p95_us": values[max(0, math.ceil(0.95 * len(values)) - 1)],
        "maximum_us": values[-1],
        "total_us": sum(values),
    }


def aggregate_rank_reports(
    config: MultirankConfig,
    reports: Sequence[Mapping[str, Any]],
    *,
    filesystem_qualification: Mapping[str, Any],
    library_sha256: str,
) -> dict[str, Any]:
    """Validate complete rank evidence and derive bounded mechanism metrics."""
    _digest(library_sha256, "native library")
    if len(reports) != config.world_size:
        raise MultirankEvaluationError("rank report cardinality changed")
    by_rank: dict[int, Mapping[str, Any]] = {}
    processors: set[str] = set()
    native_signatures: set[str] = set()
    proof_store_identities: set[str] = set()
    for raw in reports:
        if not isinstance(raw, Mapping) or raw.get("schema") != RANK_REPORT_SCHEMA:
            raise MultirankEvaluationError("rank report schema changed")
        if raw.get("protocol") != PROTOCOL or raw.get("config_sha256") != config.sha256:
            raise MultirankEvaluationError("rank report protocol identity changed")
        rank = _integer(raw.get("rank"), "reported rank", 0, config.world_size - 1)
        if rank in by_rank:
            raise MultirankEvaluationError("duplicate rank report")
        if raw.get("role") != config.role(rank):
            raise MultirankEvaluationError("rank role assignment changed")
        if _integer(
            raw.get("world_size"), "reported world size", 3, MAX_RANKS
        ) != config.world_size:
            raise MultirankEvaluationError("rank world size changed")
        if raw.get("library_sha256") != library_sha256:
            raise MultirankEvaluationError("rank native library identity changed")
        processor = str(raw.get("processor", ""))
        if not processor or len(processor) > 255:
            raise MultirankEvaluationError("rank processor identity is invalid")
        if raw.get("error"):
            raise MultirankEvaluationError(f"rank {rank} failed: {str(raw['error'])[:256]}")
        rounds = raw.get("rounds")
        if not isinstance(rounds, list) or len(rounds) != config.rounds:
            raise MultirankEvaluationError("rank round evidence is incomplete")
        by_rank[rank] = raw
        processors.add(processor)
        native_signatures.add(str(raw.get("native_signature", "")))
        proof_store_identities.add(_digest(
            raw.get("proof_store_identity_sha256"), "proof store identity"
        ))
    if (
        len(native_signatures) != 1
        or not next(iter(native_signatures)).startswith(
            "symcc-qfbv-realtime-v1|cadical-3.0."
        )
        or len(proof_store_identities) != 1
    ):
        raise MultirankEvaluationError(
            "native or proof-store identity differs across ranks"
        )

    qualification = dict(filesystem_qualification)
    if not qualification.get("accepted") or not qualification.get("clean"):
        raise MultirankEvaluationError("shared filesystem qualification failed")

    publish_samples: list[int] = []
    solve_samples: list[int] = []
    finish_samples: list[int] = []
    epoch_samples: list[int] = []
    checker_samples: list[int] = []
    expected_imports = 0
    delivered_imports = 0
    active_deliveries = 0
    activity_unit = 0
    activity_conflict = 0
    activity_unactivated = 0
    activity_receipts = 0
    pairing_opportunities = 0
    pairing_admitted = 0
    pairing_suppressed = 0
    pairing_explore = 0
    pairing_exploit = 0
    pairing_outcomes = {
        name: 0
        for name in ("unit", "conflict", "unactivated", "backpressure", "expired")
    }
    pairing_policy_identities: set[str] = set()
    previous_round_max_event = 0
    formulas: list[str] = []
    for ordinal in range(config.rounds):
        coordinator = by_rank[0]["rounds"][ordinal]
        if _integer(
            coordinator.get("round"), "coordinator round", 0,
            config.rounds - 1,
        ) != ordinal:
            raise MultirankEvaluationError("coordinator round identity changed")
        formula = _digest(coordinator.get("formula_sha256"), "round formula")
        formulas.append(formula)
        epoch_samples.append(_integer(
            coordinator.get("epoch_makespan_us"), "epoch makespan", 0, (1 << 63) - 1
        ))
        records: set[str] = set()
        record_sources: dict[str, str] = {}
        events: set[int] = set()
        cnf = _digest(coordinator.get("cnf_sha256"), "round CNF")
        for rank in config.publisher_ranks:
            row = by_rank[rank]["rounds"][ordinal]
            if (
                row.get("formula_sha256") != formula
                or row.get("cnf_sha256") != cnf
                or _integer(
                    row.get("round"), "publisher round", 0,
                    config.rounds - 1,
                ) != ordinal
            ):
                raise MultirankEvaluationError("publisher formula identity changed")
            record = _digest(row.get("record_sha256"), "published record")
            event = _integer(row.get("event_sequence"), "proof event", 1, (1 << 63) - 1)
            if record in records or event in events:
                raise MultirankEvaluationError("publisher event is not unique")
            records.add(record)
            record_sources[record] = f"f434-publisher-rank-{rank}"
            events.add(event)
            if not bool(row.get("created")):
                raise MultirankEvaluationError(
                    "publisher record was not newly created"
                )
            publish_samples.append(_integer(
                row.get("publish_elapsed_us"), "publish elapsed", 0, (1 << 63) - 1
            ))
        if min(events) <= previous_round_max_event:
            raise MultirankEvaluationError(
                "proof events did not advance across rounds"
            )
        previous_round_max_event = max(events)
        for rank in config.consumer_ranks:
            row = by_rank[rank]["rounds"][ordinal]
            if (
                row.get("formula_sha256") != formula
                or row.get("cnf_sha256") != cnf
                or _integer(
                    row.get("round"), "consumer round", 0,
                    config.rounds - 1,
                ) != ordinal
            ):
                raise MultirankEvaluationError("consumer formula identity changed")
            delivered = row.get("delivered_record_sha256")
            if not isinstance(delivered, list):
                raise MultirankEvaluationError("consumer delivery evidence is malformed")
            delivered_records = {
                _digest(record, "delivered record") for record in delivered
            }
            if (
                len(delivered_records) != len(delivered)
                or not delivered_records <= records
                or (not config.utility_pairing and delivered_records != records)
            ):
                raise MultirankEvaluationError(
                    "consumer delivered record set is invalid"
                )
            acks = row.get("acks")
            if not isinstance(acks, list) or len(acks) != len(delivered_records):
                raise MultirankEvaluationError(
                    "consumer ACK evidence is incomplete"
                )
            ack_records: set[str] = set()
            for ack in acks:
                if not isinstance(ack, Mapping):
                    raise MultirankEvaluationError(
                        "consumer ACK evidence is malformed"
                    )
                ack_records.add(_digest(
                    ack.get("record_sha256"), "ACK record"
                ))
            if ack_records != delivered_records:
                raise MultirankEvaluationError(
                    "consumer ACK set differs from delivered records"
                )
            ack_digests = {
                _digest(ack.get("ack_sha256"), "ACK identity")
                for ack in acks
            } if config.track_clause_activity else set()
            if config.track_clause_activity:
                if row.get("activity_protocol") != CLAUSE_ACTIVITY_PROTOCOL:
                    raise MultirankEvaluationError(
                        "consumer clause-activity protocol changed"
                    )
                unit = _integer(
                    row.get("activity_unit"), "unit activities", 0,
                    len(delivered_records),
                )
                conflict = _integer(
                    row.get("activity_conflict"),
                    "conflict activities", 0, len(delivered_records),
                )
                unactivated = _integer(
                    row.get("activity_unactivated"),
                    "unactivated imports", 0, len(delivered_records),
                )
                receipts = row.get("activity_receipts")
                if (
                    unit + conflict + unactivated != len(delivered_records)
                    or not isinstance(receipts, list)
                    or len(receipts) != unit + conflict
                ):
                    raise MultirankEvaluationError(
                        "consumer clause-activity accounting changed"
                    )
                receipt_records: set[str] = set()
                observed_unit = 0
                observed_conflict = 0
                for receipt in receipts:
                    if not isinstance(receipt, Mapping):
                        raise MultirankEvaluationError(
                            "consumer clause-activity receipt is malformed"
                        )
                    record = _digest(
                        receipt.get("record_sha256"), "activity record"
                    )
                    ack_identity = _digest(
                        receipt.get("ack_sha256"), "activity ACK"
                    )
                    kind = receipt.get("kind")
                    if (
                        record not in delivered_records
                        or record in receipt_records
                        or ack_identity not in ack_digests
                        or kind not in {"unit", "conflict"}
                    ):
                        raise MultirankEvaluationError(
                            "consumer clause-activity identity changed"
                        )
                    receipt_records.add(record)
                    observed_unit += int(kind == "unit")
                    observed_conflict += int(kind == "conflict")
                if observed_unit != unit or observed_conflict != conflict:
                    raise MultirankEvaluationError(
                        "consumer clause-activity kinds changed"
                    )
                activity_unit += unit
                activity_conflict += conflict
                activity_unactivated += unactivated
                activity_receipts += len(receipts)

            admitted_records = set(records)
            if config.utility_pairing:
                raw_pairing = row.get("pairing_evidence")
                stream_id = row.get("stream_id")
                if not isinstance(raw_pairing, Mapping):
                    raise MultirankEvaluationError(
                        "consumer pairing evidence is missing"
                    )
                plan = build_random_3sat_plan(config, ordinal)
                if (
                    plan.formula_sha256 != formula
                    or plan.certificate["cnf_sha256"] != cnf
                ):
                    raise MultirankEvaluationError(
                        "pairing formula cannot be independently rebuilt"
                    )
                activity_kinds = {
                    str(receipt["record_sha256"]): str(receipt["kind"])
                    for receipt in row["activity_receipts"]
                }
                try:
                    verified_pairing = verify_pairing_stream_result(
                        raw_pairing,
                        stream_id=str(stream_id),
                        consumer_worker=f"f434-consumer-rank-{rank}",
                        formula_family=formula_family_sha256(plan.certificate),
                        delivered_records=delivered,
                        activity_kinds=activity_kinds,
                    )
                except UtilityPairingError as error:
                    raise MultirankEvaluationError(
                        f"consumer pairing evidence failed: {error}"
                    ) from error
                decisions = verified_pairing[
                    "backend_realtime_pairing_decisions"
                ]
                candidate_records = {
                    str(decision["candidate"]["record_sha256"])
                    for decision in decisions
                }
                if (
                    len(decisions) != len(records)
                    or candidate_records != records
                    or any(
                        decision["candidate"]["publisher_worker"]
                        != record_sources[
                            str(decision["candidate"]["record_sha256"])
                        ]
                        for decision in decisions
                    )
                ):
                    raise MultirankEvaluationError(
                        "pairing decisions do not cover exact publisher opportunities"
                    )
                admitted_records = {
                    str(decision["candidate"]["record_sha256"])
                    for decision in decisions
                    if decision["action"] == "admit"
                }
                if not delivered_records <= admitted_records:
                    raise MultirankEvaluationError(
                        "native delivery was not admitted by pairing"
                    )
                actions = verified_pairing[
                    "backend_realtime_pairing_action_counts"
                ]
                phases = verified_pairing[
                    "backend_realtime_pairing_phase_counts"
                ]
                outcomes = verified_pairing[
                    "backend_realtime_pairing_outcome_counts"
                ]
                pairing_opportunities += len(records)
                pairing_admitted += int(actions["admit"])
                pairing_suppressed += int(actions["suppress"])
                pairing_explore += int(phases["explore"])
                pairing_exploit += int(phases["exploit"])
                for name in pairing_outcomes:
                    pairing_outcomes[name] += int(outcomes[name])
                pairing_policy_identities.add(str(
                    verified_pairing[
                        "backend_realtime_pairing_policy_sha256"
                    ]
                ))
            elif "pairing_evidence" in row:
                raise MultirankEvaluationError(
                    "pairing evidence is present while pairing is disabled"
                )
            if (
                not bool(row.get("imports_enqueued_before_wait_deadline"))
                or _integer(
                    row.get("imports_expected"), "expected imports", 0, 4096
                ) != len(admitted_records)
                or _integer(
                    row.get("imports_delivered"), "delivered imports", 0, 4096
                ) != len(delivered_records)
                or bool(row.get("timed_out"))
                or bool(row.get("stream_error"))
                or bool(row.get("solve_error"))
                or _integer(
                    row.get("backpressure"), "stream backpressure", 0,
                    (1 << 63) - 1,
                ) != 0
            ):
                raise MultirankEvaluationError(
                    "consumer runtime evidence failed"
                )
            active = bool(row.get("active_at_publication"))
            if config.mode == "active" and not active:
                raise MultirankEvaluationError(
                    "active-mode publication missed the solve"
                )
            expected_imports += len(admitted_records)
            delivered_imports += len(delivered)
            if active:
                active_deliveries += len(delivered)
            if _integer(row.get("solve_result"), "solve result", 0, 20) not in {10, 20}:
                raise MultirankEvaluationError("consumer solve did not complete")
            solve_samples.append(_integer(
                row.get("solve_elapsed_us"), "solve elapsed", 0, (1 << 63) - 1
            ))
            finish_samples.append(_integer(
                row.get("notification_to_finish_us"), "notification-to-finish", 0,
                (1 << 63) - 1,
            ))
            checker_samples.append(_integer(
                row.get("checker_elapsed_us"), "checker elapsed", 0, (1 << 63) - 1
            ))
    if len(set(formulas)) != config.rounds:
        raise MultirankEvaluationError("round formulas are not unique")
    if config.utility_pairing and (
        len(pairing_policy_identities) != 1
        or pairing_admitted + pairing_suppressed != pairing_opportunities
        or sum(pairing_outcomes.values()) != pairing_admitted
        or (
            pairing_outcomes["unit"]
            + pairing_outcomes["conflict"]
            + pairing_outcomes["unactivated"]
            != delivered_imports
        )
    ):
        raise MultirankEvaluationError(
            "utility-pairing aggregate conservation failed"
        )

    qualification_scope = str(qualification.get("scope", ""))
    if qualification_scope == "same-host-subprocess-v1":
        scope = "local-host-mpi-mechanism"
    elif qualification_scope == "cross-host-mpi-lock-v2":
        scope = "qualified-multi-host-mpi-mechanism"
    else:
        raise MultirankEvaluationError(
            "filesystem qualification scope changed"
        )
    body: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "protocol": PROTOCOL,
        "status": "pass",
        "config": config.as_dict(),
        "config_sha256": config.sha256,
        "library_sha256": library_sha256,
        "processor_count": len(processors),
        "processors": sorted(processors),
        "scope": scope,
        "filesystem_qualification": qualification,
        "expected_imports": expected_imports,
        "delivered_imports": delivered_imports,
        "delivery_rate": delivered_imports / max(1, expected_imports),
        "import_opportunities": (
            pairing_opportunities if config.utility_pairing else expected_imports
        ),
        "opportunity_delivery_rate": delivered_imports / max(
            1,
            pairing_opportunities if config.utility_pairing else expected_imports,
        ),
        "active_delivery_rate": active_deliveries / max(1, expected_imports),
        "clause_activity_enabled": config.track_clause_activity,
        "clause_activity_protocol": (
            CLAUSE_ACTIVITY_PROTOCOL if config.track_clause_activity else ""
        ),
        "clause_activity_unit": activity_unit,
        "clause_activity_conflict": activity_conflict,
        "clause_activity_unactivated": activity_unactivated,
        "clause_activity_receipts": activity_receipts,
        "clause_activation_rate": (
            (activity_unit + activity_conflict) / max(1, delivered_imports)
            if config.track_clause_activity else 0.0
        ),
        "utility_pairing_enabled": config.utility_pairing,
        "utility_pairing_protocol": (
            UTILITY_PAIRING_PROTOCOL if config.utility_pairing else ""
        ),
        "utility_pairing_policy_sha256": (
            next(iter(pairing_policy_identities))
            if config.utility_pairing else ""
        ),
        "utility_pairing_opportunities": pairing_opportunities,
        "utility_pairing_admitted": pairing_admitted,
        "utility_pairing_suppressed": pairing_suppressed,
        "utility_pairing_explore": pairing_explore,
        "utility_pairing_exploit": pairing_exploit,
        "utility_pairing_outcomes": pairing_outcomes,
        "utility_pairing_suppression_rate": (
            pairing_suppressed / max(1, pairing_opportunities)
            if config.utility_pairing else 0.0
        ),
        "publish": percentile_summary(publish_samples),
        "solve": percentile_summary(solve_samples),
        "notification_to_finish": percentile_summary(finish_samples),
        "checker_cpu": percentile_summary(checker_samples),
        "epoch_makespan": percentile_summary(epoch_samples),
        "rank_reports": [dict(by_rank[rank]) for rank in range(config.world_size)],
        "claim_boundary": (
            "checked proof-stream mechanism and MPI/CAS scaling evidence only; "
            "no fuzzing coverage, defect-yield, or public-benchmark effectiveness claim"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    return body
