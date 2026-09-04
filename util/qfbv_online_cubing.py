#!/usr/bin/env python3
"""Persistent online policy for proof-aware QF_BV cubing.

The policy never decides SAT or UNSAT.  It chooses a bounded cubing arm and
cube count from independently checkable solver activity and observed runtime
cost.  Every choice carries a stable policy identity and an exact selection
propensity so equal-budget evaluations can account for adaptive sampling.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ONLINE_CUBING_PROTOCOL = "symcc-qfbv-online-cubing-v1"
ONLINE_CUBING_POLICY_SCHEMA = "symcc-qfbv-online-cubing-policy-v1"
ONLINE_CUBING_DECISION_SCHEMA = "symcc-qfbv-online-cubing-decision-v1"
ONLINE_CUBING_OUTCOME_SCHEMA = "symcc-qfbv-online-cubing-outcome-v1"
ONLINE_CUBING_STORE_SCHEMA = "symcc-qfbv-online-cubing-store-v1"
ONLINE_CUBING_ARMS = ("static", "activity", "cost")
MAX_CUBE_COUNT = 4096
MAX_CANDIDATES = 16
MAX_DECISIONS = 1_000_000
_HEX64 = re.compile(r"[0-9a-f]{64}")


class OnlineCubingError(ValueError):
    """An online-cubing policy, decision, outcome, or store is invalid."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise OnlineCubingError("online-cubing value is not canonical JSON") from error


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _reject_duplicate_members(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OnlineCubingError(
                "online-cubing JSON contains duplicate members"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise OnlineCubingError(f"online-cubing JSON contains {value}")


def _load_canonical_json(raw: Any, name: str) -> dict[str, Any]:
    if type(raw) is not str:
        raise OnlineCubingError(f"stored {name} must be text")
    try:
        encoded = raw.encode("ascii")
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, OnlineCubingError):
            raise
        raise OnlineCubingError(f"stored {name} is invalid") from error
    if not isinstance(value, dict) or _canonical_json(value) != encoded:
        raise OnlineCubingError(f"stored {name} is not canonical JSON")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise OnlineCubingError(f"{name} must be a lowercase SHA-256")
    return value


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise OnlineCubingError(f"{name} must be in [{lower}, {upper}]")
    return value


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise OnlineCubingError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise OnlineCubingError(f"{name} is invalid") from error
    if (
        not encoded
        or len(encoded) > 256
        or any(byte < 32 or byte == 127 for byte in encoded)
    ):
        raise OnlineCubingError(f"{name} is invalid")
    return value


def _normalize_arms(raw: Sequence[str]) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)):
        raise OnlineCubingError("online-cubing strategies must be a sequence")
    supplied = tuple(raw)
    if not supplied or len(supplied) > len(ONLINE_CUBING_ARMS):
        raise OnlineCubingError("online-cubing strategy set is invalid")
    if any(type(value) is not str for value in supplied):
        raise OnlineCubingError("online-cubing strategy is invalid")
    canonical = tuple(arm for arm in ONLINE_CUBING_ARMS if arm in supplied)
    if canonical != supplied or len(set(supplied)) != len(supplied):
        raise OnlineCubingError("online-cubing strategies are not canonical")
    if not canonical or canonical[0] != "static":
        raise OnlineCubingError(
            "online-cubing strategies require the static fallback"
        )
    return canonical


def _normalize_candidates(raw: Sequence[int]) -> tuple[int, ...]:
    if isinstance(raw, (str, bytes)):
        raise OnlineCubingError("online-cubing candidates must be a sequence")
    supplied = tuple(raw)
    if not supplied or len(supplied) > MAX_CANDIDATES:
        raise OnlineCubingError("online-cubing candidate set is invalid")
    normalized = tuple(
        _integer(value, "online-cubing cube candidate", 2, MAX_CUBE_COUNT)
        for value in supplied
    )
    if tuple(sorted(set(normalized))) != normalized:
        raise OnlineCubingError("online-cubing candidates are not canonical")
    return normalized


@dataclass(frozen=True)
class OnlineCubingPolicy:
    """Fixed-point, replayable policy configuration."""

    base_cube_count: int = 4
    cube_candidates: tuple[int, ...] = (2, 4, 8, 16)
    strategies: tuple[str, ...] = ONLINE_CUBING_ARMS
    prerun_budget_ms: int = 250
    base_cube_timeout_ms: int = 30_000
    max_cube_attempts: int = 3
    min_exploration_samples: int = 2
    cost_min_exploration_samples: int = 1
    exploration_permille: int = 100
    uncertainty_scale: int = 250_000
    cost_reference_us: int = 1_000_000
    max_pending_per_family: int = 64
    max_history_per_family: int = 4096
    max_decisions: int = MAX_DECISIONS

    def __post_init__(self) -> None:
        _integer(
            self.base_cube_count,
            "online-cubing base cube count",
            2,
            MAX_CUBE_COUNT,
        )
        candidates = _normalize_candidates(self.cube_candidates)
        arms = _normalize_arms(self.strategies)
        if candidates != self.cube_candidates or arms != self.strategies:
            raise OnlineCubingError("online-cubing policy values are not canonical")
        if self.base_cube_count not in candidates:
            raise OnlineCubingError(
                "online-cubing base cube count must be a candidate"
            )
        _integer(self.prerun_budget_ms, "online-cubing prerun budget", 1, 3_600_000)
        _integer(
            self.base_cube_timeout_ms,
            "online-cubing base cube timeout",
            1,
            3_600_000,
        )
        _integer(
            self.max_cube_attempts,
            "online-cubing maximum cube attempts",
            1,
            32,
        )
        _integer(
            self.min_exploration_samples,
            "online-cubing minimum exploration samples",
            1,
            64,
        )
        _integer(
            self.cost_min_exploration_samples,
            "online-cubing cost minimum exploration samples",
            1,
            64,
        )
        _integer(
            self.exploration_permille,
            "online-cubing exploration rate",
            0,
            1000,
        )
        _integer(
            self.uncertainty_scale,
            "online-cubing uncertainty scale",
            0,
            10_000_000,
        )
        _integer(
            self.cost_reference_us,
            "online-cubing cost reference",
            1,
            (1 << 63) - 1,
        )
        _integer(
            self.max_pending_per_family,
            "online-cubing pending budget",
            1,
            4096,
        )
        _integer(
            self.max_history_per_family,
            "online-cubing family history budget",
            3,
            65_536,
        )
        _integer(
            self.max_decisions,
            "online-cubing decision budget",
            1,
            MAX_DECISIONS,
        )
        if "activity" in arms or "cost" in arms:
            if self.prerun_budget_ms <= 0:
                raise OnlineCubingError("guided cubing requires a prerun budget")

    def as_config(self) -> dict[str, Any]:
        return {
            "base_cube_count": self.base_cube_count,
            "cube_candidates": list(self.cube_candidates),
            "strategies": list(self.strategies),
            "prerun_budget_ms": self.prerun_budget_ms,
            "base_cube_timeout_ms": self.base_cube_timeout_ms,
            "max_cube_attempts": self.max_cube_attempts,
            "min_exploration_samples": self.min_exploration_samples,
            "cost_min_exploration_samples": self.cost_min_exploration_samples,
            "exploration_permille": self.exploration_permille,
            "uncertainty_scale": self.uncertainty_scale,
            "cost_reference_us": self.cost_reference_us,
            "max_pending_per_family": self.max_pending_per_family,
            "max_history_per_family": self.max_history_per_family,
            "max_decisions": self.max_decisions,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ONLINE_CUBING_POLICY_SCHEMA,
            "protocol": ONLINE_CUBING_PROTOCOL,
            **self.as_config(),
        }

    @property
    def sha256(self) -> str:
        return _content_digest(self.as_dict())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OnlineCubingPolicy":
        if not isinstance(raw, Mapping):
            raise OnlineCubingError("online-cubing policy must be an object")
        defaults = cls()
        allowed = set(defaults.as_config())
        unknown = set(raw) - allowed
        if unknown:
            raise OnlineCubingError(
                "unknown online-cubing policy option: " + sorted(unknown)[0]
            )
        values = defaults.as_config()
        values.update(raw)
        values["cube_candidates"] = _normalize_candidates(values["cube_candidates"])
        values["strategies"] = _normalize_arms(values["strategies"])
        return cls(**values)

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "OnlineCubingPolicy":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != ONLINE_CUBING_POLICY_SCHEMA
            or raw.get("protocol") != ONLINE_CUBING_PROTOCOL
        ):
            raise OnlineCubingError("sealed online-cubing policy scope changed")
        expected = {"schema", "protocol", *cls().as_config()}
        if set(raw) != expected:
            raise OnlineCubingError("sealed online-cubing policy shape changed")
        policy = cls.from_mapping({key: raw[key] for key in cls().as_config()})
        if policy.as_dict() != dict(raw):
            raise OnlineCubingError("sealed online-cubing policy changed")
        return policy


def _hash_index(parts: Sequence[Any], count: int) -> int:
    if count <= 0:
        raise OnlineCubingError("online-cubing choice set is empty")
    return int(_content_digest(list(parts))[:16], 16) % count


def _score(count: int, reward_sum: int, total: int, scale: int) -> int:
    if count <= 0:
        return (1 << 63) - 1
    mean = reward_sum // count
    bonus = scale * math.isqrt(max(1, (total + 1) * 1_000_000 // count)) // 1000
    return mean + bonus


def _reward(
    policy: OnlineCubingPolicy,
    *,
    status: str,
    elapsed_us: int,
    completed_cubes: int,
    cube_count: int,
) -> int:
    if status in {"sat", "unsat"}:
        base = 1_000_000
    elif status == "unknown":
        base = 100_000 * completed_cubes // max(1, cube_count)
    else:
        base = -500_000
    penalty = min(
        1_000_000,
        elapsed_us * 250_000 // policy.cost_reference_us,
    )
    return base - penalty


def online_cubing_budget(
    policy: OnlineCubingPolicy,
    *,
    cube_count: int,
    prerun_elapsed_us: int,
) -> dict[str, int]:
    """Return an integer CPU-ms budget split with no hidden guided-arm credit."""
    cubes = _integer(
        cube_count, "online-cubing budget cube count", 2, MAX_CUBE_COUNT
    )
    if cubes not in policy.cube_candidates:
        raise OnlineCubingError("online-cubing budget selected a foreign cube count")
    prerun_us = _integer(
        prerun_elapsed_us,
        "online-cubing budget prerun elapsed time",
        0,
        (1 << 63) - 1,
    )
    configured = (
        policy.base_cube_count
        * policy.base_cube_timeout_ms
        * policy.max_cube_attempts
    )
    attempt_slots = cubes * policy.max_cube_attempts
    requested_charge = (prerun_us + 999) // 1000 if prerun_us else 0
    charged = min(requested_charge, max(0, configured - attempt_slots))
    remaining = configured - charged
    per_cube = min(3_600_000, max(1, remaining // attempt_slots))
    effective = charged + per_cube * attempt_slots
    return {
        "configured_cpu_budget_ms": configured,
        "prerun_budget_charged_ms": charged,
        "effective_cube_timeout_ms": per_cube,
        "effective_cpu_budget_ms": effective,
    }


def verify_online_cubing_decision(
    raw: Mapping[str, Any],
    *,
    policy: OnlineCubingPolicy,
    query_id: str,
    formula_family_sha256: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise OnlineCubingError("online-cubing decision must be an object")
    body = dict(raw)
    supplied = _digest(
        body.pop("decision_sha256", ""), "online-cubing decision"
    )
    if _content_digest(body) != supplied:
        raise OnlineCubingError("online-cubing decision identity changed")
    expected = {
        "schema",
        "protocol",
        "policy",
        "policy_sha256",
        "query_id",
        "formula_family_sha256",
        "family_ordinal",
        "arm",
        "cube_count",
        "selection_reason",
        "cube_selection_reason",
        "arm_propensity_numerator",
        "arm_propensity_denominator",
        "cube_propensity_numerator",
        "cube_propensity_denominator",
        "propensity_numerator",
        "propensity_denominator",
        "activity_available",
        "prerun_requested",
    }
    if set(body) != expected:
        raise OnlineCubingError("online-cubing decision shape changed")
    if (
        body.get("schema") != ONLINE_CUBING_DECISION_SCHEMA
        or body.get("protocol") != ONLINE_CUBING_PROTOCOL
        or body.get("policy_sha256") != policy.sha256
        or body.get("query_id") != _identity(query_id, "online-cubing query")
        or body.get("formula_family_sha256")
        != _digest(formula_family_sha256, "online-cubing formula family")
    ):
        raise OnlineCubingError("online-cubing decision scope changed")
    sealed_policy = OnlineCubingPolicy.from_sealed(body.get("policy"))
    if sealed_policy != policy:
        raise OnlineCubingError("online-cubing decision policy changed")
    _integer(
        body.get("family_ordinal"),
        "online-cubing family ordinal",
        1,
        policy.max_decisions,
    )
    arm = body.get("arm")
    if arm not in policy.strategies:
        raise OnlineCubingError("online-cubing decision arm changed")
    cube_count = _integer(
        body.get("cube_count"),
        "online-cubing selected cube count",
        2,
        MAX_CUBE_COUNT,
    )
    if cube_count not in policy.cube_candidates:
        raise OnlineCubingError("online-cubing selected a foreign cube count")
    if arm != "cost" and cube_count != policy.base_cube_count:
        raise OnlineCubingError("non-cost arm changed the base cube count")
    if body.get("selection_reason") not in {
        "bounded-exploration",
        "bounded-pending-balance",
        "epsilon-exploration",
        "utility-exploitation",
        "deterministic-static-fallback",
    }:
        raise OnlineCubingError("online-cubing selection reason changed")
    if body.get("cube_selection_reason") not in {
        "fixed-base",
        "bounded-cost-exploration",
        "bounded-cost-pending-balance",
        "cost-utility-exploitation",
    }:
        raise OnlineCubingError("online-cubing cube selection reason changed")
    numerator = _integer(
        body.get("propensity_numerator"),
        "online-cubing propensity numerator",
        1,
        1_000_000,
    )
    denominator = _integer(
        body.get("propensity_denominator"),
        "online-cubing propensity denominator",
        numerator,
        1_000_000,
    )
    arm_numerator = _integer(
        body.get("arm_propensity_numerator"),
        "online-cubing arm propensity numerator",
        1,
        1_000_000,
    )
    arm_denominator = _integer(
        body.get("arm_propensity_denominator"),
        "online-cubing arm propensity denominator",
        arm_numerator,
        1_000_000,
    )
    cube_numerator = _integer(
        body.get("cube_propensity_numerator"),
        "online-cubing cube propensity numerator",
        1,
        1_000_000,
    )
    cube_denominator = _integer(
        body.get("cube_propensity_denominator"),
        "online-cubing cube propensity denominator",
        cube_numerator,
        1_000_000,
    )
    joint_numerator = arm_numerator * cube_numerator
    joint_denominator = arm_denominator * cube_denominator
    divisor = math.gcd(joint_numerator, joint_denominator)
    if (
        numerator != joint_numerator // divisor
        or denominator != joint_denominator // divisor
    ):
        raise OnlineCubingError("online-cubing joint propensity changed")
    activity_available = body.get("activity_available")
    prerun_requested = body.get("prerun_requested")
    if type(activity_available) is not bool or type(prerun_requested) is not bool:
        raise OnlineCubingError("online-cubing availability flags are invalid")
    if not activity_available and arm != "static":
        raise OnlineCubingError("guided cubing lacks checked-activity support")
    if prerun_requested != (arm in {"activity", "cost"}):
        raise OnlineCubingError("online-cubing prerun decision changed")
    if arm != "cost" and cube_numerator != cube_denominator:
        raise OnlineCubingError("fixed cube count has non-unit propensity")
    if body.get("selection_reason") == "deterministic-static-fallback" and (
        arm != "static" or arm_numerator != arm_denominator
    ):
        raise OnlineCubingError("online-cubing fallback propensity changed")
    verified = dict(body)
    verified["decision_sha256"] = supplied
    return verified


def verify_online_cubing_outcome(
    raw: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    policy: OnlineCubingPolicy,
) -> dict[str, Any]:
    verified_decision = verify_online_cubing_decision(
        decision,
        policy=policy,
        query_id=str(decision.get("query_id", "")),
        formula_family_sha256=str(decision.get("formula_family_sha256", "")),
    )
    if not isinstance(raw, Mapping):
        raise OnlineCubingError("online-cubing outcome must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("outcome_sha256", ""), "online-cubing outcome")
    if _content_digest(body) != supplied:
        raise OnlineCubingError("online-cubing outcome identity changed")
    expected = {
        "schema",
        "protocol",
        "policy_sha256",
        "decision_sha256",
        "query_id",
        "formula_family_sha256",
        "arm",
        "cube_count",
        "status",
        "partition_executed",
        "elapsed_us",
        "prerun_elapsed_us",
        "completed_cubes",
        "activity_receipts",
        "configured_cpu_budget_ms",
        "prerun_budget_charged_ms",
        "effective_cube_timeout_ms",
        "effective_cpu_budget_ms",
        "reward_micros",
    }
    if set(body) != expected:
        raise OnlineCubingError("online-cubing outcome shape changed")
    for name in (
        "policy_sha256",
        "decision_sha256",
        "query_id",
        "formula_family_sha256",
        "arm",
        "cube_count",
    ):
        expected_value = (
            policy.sha256 if name == "policy_sha256" else verified_decision[name]
        )
        if body.get(name) != expected_value:
            raise OnlineCubingError(f"online-cubing outcome {name} changed")
    if (
        body.get("schema") != ONLINE_CUBING_OUTCOME_SCHEMA
        or body.get("protocol") != ONLINE_CUBING_PROTOCOL
    ):
        raise OnlineCubingError("online-cubing outcome scope changed")
    status = body.get("status")
    if status not in {"sat", "unsat", "unknown", "error"}:
        raise OnlineCubingError("online-cubing outcome status changed")
    partition_executed = body.get("partition_executed")
    if type(partition_executed) is not bool:
        raise OnlineCubingError("online-cubing partition state is invalid")
    elapsed_us = _integer(
        body.get("elapsed_us"), "online-cubing elapsed time", 0, (1 << 63) - 1
    )
    prerun_elapsed_us = _integer(
        body.get("prerun_elapsed_us"),
        "online-cubing prerun elapsed time",
        0,
        elapsed_us,
    )
    completed = _integer(
        body.get("completed_cubes"),
        "online-cubing completed cubes",
        0,
        int(verified_decision["cube_count"]),
    )
    receipts = _integer(
        body.get("activity_receipts"),
        "online-cubing activity receipts",
        0,
        4096,
    )
    budget = online_cubing_budget(
        policy,
        cube_count=int(verified_decision["cube_count"]),
        prerun_elapsed_us=prerun_elapsed_us,
    )
    if any(body.get(name) != value for name, value in budget.items()):
        raise OnlineCubingError("online-cubing CPU budget accounting changed")
    if not verified_decision["prerun_requested"] and (
        prerun_elapsed_us or receipts
    ):
        raise OnlineCubingError("static cubing carries prerun evidence")
    if not partition_executed and completed:
        raise OnlineCubingError("prerun-only outcome completed partition cubes")
    if partition_executed and status == "sat" and completed < 1:
        raise OnlineCubingError("online-cubing SAT outcome lacks a completed cube")
    if partition_executed and status == "unsat" and completed != int(
        verified_decision["cube_count"]
    ):
        raise OnlineCubingError("online-cubing UNSAT outcome is incomplete")
    expected_reward = _reward(
        policy,
        status=status,
        elapsed_us=elapsed_us,
        completed_cubes=completed,
        cube_count=int(verified_decision["cube_count"]),
    )
    if body.get("reward_micros") != expected_reward:
        raise OnlineCubingError("online-cubing reward changed")
    verified = dict(body)
    verified["outcome_sha256"] = supplied
    return verified


class OnlineCubingPolicyStore:
    """Descriptor-checked SQLite ledger for decisions and observed outcomes."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        policy: OnlineCubingPolicy,
    ) -> None:
        supplied_path = Path(path).absolute()
        if supplied_path.is_symlink():
            raise OnlineCubingError("online-cubing store must not be a symlink")
        cursor = supplied_path.parent
        while cursor != cursor.parent:
            if cursor.exists() and cursor.is_symlink():
                raise OnlineCubingError(
                    "online-cubing store ancestry must not contain a symlink"
                )
            cursor = cursor.parent
        self.path = supplied_path.resolve(strict=False)
        self.policy = policy
        parent = self.path.parent
        if parent.is_symlink():
            raise OnlineCubingError("online-cubing store parent must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OnlineCubingError("online-cubing store must not be a symlink")
        self.identity_sha256 = _content_digest(
            {
                "schema": ONLINE_CUBING_STORE_SCHEMA,
                "protocol": ONLINE_CUBING_PROTOCOL,
                "path": str(self.path),
                "policy_sha256": policy.sha256,
            }
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise OnlineCubingError("online-cubing store must not be a symlink")
        database = sqlite3.connect(self.path, timeout=30.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        row = database.execute("PRAGMA database_list").fetchone()
        if row is None or Path(str(row[2])).resolve() != self.path:
            database.close()
            raise OnlineCubingError("online-cubing database identity changed")
        state = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISREG(state.st_mode):
            database.close()
            raise OnlineCubingError("online-cubing store is not a regular file")
        return database

    def _initialize(self) -> None:
        with self._connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS online_cubing_metadata("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS online_cubing_decisions("
                "decision_sha256 TEXT PRIMARY KEY,policy_sha256 TEXT NOT NULL,"
                "formula_family_sha256 TEXT NOT NULL,query_id TEXT NOT NULL,"
                "family_ordinal INTEGER NOT NULL,arm TEXT NOT NULL,"
                "cube_count INTEGER NOT NULL,decision_json TEXT NOT NULL,"
                "created_ns INTEGER NOT NULL,"
                "UNIQUE(policy_sha256,formula_family_sha256,query_id),"
                "UNIQUE(policy_sha256,formula_family_sha256,family_ordinal))"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS online_cubing_outcomes("
                "decision_sha256 TEXT PRIMARY KEY,outcome_sha256 TEXT NOT NULL UNIQUE,"
                "status TEXT NOT NULL,elapsed_us INTEGER NOT NULL,"
                "reward_micros INTEGER NOT NULL,outcome_json TEXT NOT NULL,"
                "created_ns INTEGER NOT NULL,"
                "FOREIGN KEY(decision_sha256) REFERENCES online_cubing_decisions("
                "decision_sha256))"
            )
            expected = {
                "schema": ONLINE_CUBING_STORE_SCHEMA,
                "protocol": ONLINE_CUBING_PROTOCOL,
                "path": str(self.path),
                "policy_sha256": self.policy.sha256,
                "policy_json": _canonical_json(self.policy.as_dict()).decode("ascii"),
            }
            for key, value in expected.items():
                row = database.execute(
                    "SELECT value FROM online_cubing_metadata WHERE key=?", (key,)
                ).fetchone()
                if row is None:
                    database.execute(
                        "INSERT INTO online_cubing_metadata(key,value) VALUES(?,?)",
                        (key, value),
                    )
                elif str(row["value"]) != value:
                    raise OnlineCubingError(
                        f"online-cubing store metadata mismatch for {key}"
                    )

    def _assert_metadata(self, database: sqlite3.Connection) -> None:
        expected = {
            "schema": ONLINE_CUBING_STORE_SCHEMA,
            "protocol": ONLINE_CUBING_PROTOCOL,
            "path": str(self.path),
            "policy_sha256": self.policy.sha256,
            "policy_json": _canonical_json(self.policy.as_dict()).decode("ascii"),
        }
        observed = {
            str(row["key"]): str(row["value"])
            for row in database.execute(
                "SELECT key,value FROM online_cubing_metadata"
            )
        }
        if observed != expected:
            raise OnlineCubingError("online-cubing store metadata changed")

    def _family_history(
        self, database: sqlite3.Connection, family: str
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        rows = database.execute(
            "SELECT d.decision_sha256,d.policy_sha256,d.formula_family_sha256,"
            "d.query_id,d.family_ordinal,d.arm,d.cube_count,d.decision_json,"
            "o.outcome_sha256,o.status,o.elapsed_us,o.reward_micros,o.outcome_json "
            "FROM online_cubing_decisions d LEFT JOIN online_cubing_outcomes o "
            "USING(decision_sha256) WHERE d.policy_sha256=? "
            "AND d.formula_family_sha256=? ORDER BY d.family_ordinal DESC LIMIT ?",
            (
                self.policy.sha256,
                family,
                self.policy.max_history_per_family,
            ),
        ).fetchall()
        history: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        ordinals: set[int] = set()
        for row in rows:
            decision = _load_canonical_json(
                str(row["decision_json"]), "online-cubing decision"
            )
            verified_decision = verify_online_cubing_decision(
                decision,
                policy=self.policy,
                query_id=str(row["query_id"]),
                formula_family_sha256=family,
            )
            ordinal = int(verified_decision["family_ordinal"])
            if (
                row["decision_sha256"] != verified_decision["decision_sha256"]
                or row["policy_sha256"] != self.policy.sha256
                or row["formula_family_sha256"] != family
                or int(row["family_ordinal"]) != ordinal
                or row["arm"] != verified_decision["arm"]
                or int(row["cube_count"]) != verified_decision["cube_count"]
                or ordinal in ordinals
            ):
                raise OnlineCubingError("online-cubing decision columns changed")
            ordinals.add(ordinal)
            outcome: dict[str, Any] | None = None
            if row["outcome_json"] is not None:
                raw_outcome = _load_canonical_json(
                    str(row["outcome_json"]), "online-cubing outcome"
                )
                outcome = verify_online_cubing_outcome(
                    raw_outcome,
                    decision=verified_decision,
                    policy=self.policy,
                )
                if (
                    row["outcome_sha256"] != outcome["outcome_sha256"]
                    or row["status"] != outcome["status"]
                    or int(row["elapsed_us"]) != outcome["elapsed_us"]
                    or int(row["reward_micros"]) != outcome["reward_micros"]
                ):
                    raise OnlineCubingError("online-cubing outcome columns changed")
            history.append((verified_decision, outcome))
        return history

    def _arm_stats(
        self,
        history: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
    ) -> dict[str, tuple[int, int, int]]:
        result = {arm: [0, 0, 0] for arm in self.policy.strategies}
        for decision, outcome in history:
            arm = str(decision["arm"])
            result[arm][0] += 1
            if outcome is not None:
                result[arm][1] += 1
                result[arm][2] += int(outcome["reward_micros"])
        return {arm: tuple(values) for arm, values in result.items()}

    def _cost_stats(
        self,
        history: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
    ) -> dict[int, tuple[int, int, int]]:
        result = {
            candidate: [0, 0, 0] for candidate in self.policy.cube_candidates
        }
        for decision, outcome in history:
            if decision["arm"] != "cost":
                continue
            candidate = int(decision["cube_count"])
            result[candidate][0] += 1
            if outcome is not None:
                result[candidate][1] += 1
                result[candidate][2] += int(outcome["reward_micros"])
        return {candidate: tuple(values) for candidate, values in result.items()}

    def _choose_arm(
        self,
        stats: Mapping[str, tuple[int, int, int]],
        *,
        family: str,
        ordinal: int,
        activity_available: bool,
    ) -> tuple[str, str, int, int]:
        allowed = tuple(
            arm
            for arm in self.policy.strategies
            if activity_available or arm == "static"
        )
        if not allowed:
            allowed = ("static",)
        if not activity_available:
            return "static", "deterministic-static-fallback", 1, 1
        underexplored = tuple(
            arm
            for arm in allowed
            if stats[arm][0] < self.policy.min_exploration_samples
        )
        if underexplored:
            choice = underexplored[
                _hash_index(
                    (self.policy.sha256, family, ordinal, "arm-warmup"),
                    len(underexplored),
                )
            ]
            return choice, "bounded-exploration", 1, len(underexplored)
        unobserved = tuple(arm for arm in allowed if stats[arm][1] == 0)
        if unobserved:
            minimum = min(stats[arm][0] for arm in unobserved)
            balanced = tuple(
                arm for arm in unobserved if stats[arm][0] == minimum
            )
            choice = balanced[
                _hash_index(
                    (self.policy.sha256, family, ordinal, "arm-pending"),
                    len(balanced),
                )
            ]
            return choice, "bounded-pending-balance", 1, len(balanced)
        total = sum(stats[arm][1] for arm in allowed)
        best = min(
            allowed,
            key=lambda arm: (
                -_score(
                    stats[arm][1],
                    stats[arm][2],
                    total,
                    self.policy.uncertainty_scale,
                ),
                ONLINE_CUBING_ARMS.index(arm),
            ),
        )
        rate = self.policy.exploration_permille
        draw = _hash_index(
            (self.policy.sha256, family, ordinal, "arm-epsilon"), 1000
        )
        if rate and draw < rate:
            choice = allowed[
                _hash_index(
                    (self.policy.sha256, family, ordinal, "arm-choice"),
                    len(allowed),
                )
            ]
            reason = "epsilon-exploration"
        else:
            choice = best
            reason = "utility-exploitation"
        denominator = 1000 * len(allowed)
        numerator = rate
        if choice == best:
            numerator += (1000 - rate) * len(allowed)
        if numerator == 0:
            return choice, reason, 1, 1
        divisor = math.gcd(numerator, denominator)
        return choice, reason, numerator // divisor, denominator // divisor

    def _choose_cube_count(
        self,
        stats: Mapping[int, tuple[int, int, int]],
        *,
        family: str,
        ordinal: int,
    ) -> tuple[int, str, int, int]:
        underexplored = tuple(
            candidate
            for candidate in self.policy.cube_candidates
            if stats[candidate][0] < self.policy.cost_min_exploration_samples
        )
        if underexplored:
            return (
                underexplored[
                    _hash_index(
                        (self.policy.sha256, family, ordinal, "cube-warmup"),
                        len(underexplored),
                    )
                ],
                "bounded-cost-exploration",
                1,
                len(underexplored),
            )
        unobserved = tuple(
            candidate
            for candidate in self.policy.cube_candidates
            if stats[candidate][1] == 0
        )
        if unobserved:
            minimum = min(stats[candidate][0] for candidate in unobserved)
            balanced = tuple(
                candidate
                for candidate in unobserved
                if stats[candidate][0] == minimum
            )
            selected = balanced[
                _hash_index(
                    (self.policy.sha256, family, ordinal, "cube-pending"),
                    len(balanced),
                )
            ]
            return selected, "bounded-cost-pending-balance", 1, len(balanced)
        total = sum(
            stats[candidate][1] for candidate in self.policy.cube_candidates
        )
        selected = min(
            self.policy.cube_candidates,
            key=lambda candidate: (
                -_score(
                    stats[candidate][1],
                    stats[candidate][2],
                    total,
                    self.policy.uncertainty_scale,
                ),
                candidate,
            ),
        )
        return selected, "cost-utility-exploitation", 1, 1

    def decide(
        self,
        query_id: str,
        formula_family_sha256: str,
        *,
        activity_available: bool,
    ) -> dict[str, Any]:
        query = _identity(query_id, "online-cubing query")
        family = _digest(formula_family_sha256, "online-cubing formula family")
        if type(activity_available) is not bool:
            raise OnlineCubingError("online-cubing activity availability is invalid")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._assert_metadata(database)
            existing = database.execute(
                "SELECT decision_sha256,policy_sha256,formula_family_sha256,"
                "query_id,family_ordinal,arm,cube_count,decision_json "
                "FROM online_cubing_decisions "
                "WHERE policy_sha256=? AND formula_family_sha256=? AND query_id=?",
                (self.policy.sha256, family, query),
            ).fetchone()
            if existing is not None:
                raw = _load_canonical_json(
                    str(existing["decision_json"]), "online-cubing decision"
                )
                decision = verify_online_cubing_decision(
                    raw,
                    policy=self.policy,
                    query_id=query,
                    formula_family_sha256=family,
                )
                if decision["activity_available"] != activity_available:
                    raise OnlineCubingError(
                        "online-cubing retry changed activity availability"
                    )
                if (
                    existing["decision_sha256"] != decision["decision_sha256"]
                    or existing["policy_sha256"] != self.policy.sha256
                    or existing["formula_family_sha256"] != family
                    or existing["query_id"] != query
                    or int(existing["family_ordinal"])
                    != decision["family_ordinal"]
                    or existing["arm"] != decision["arm"]
                    or int(existing["cube_count"]) != decision["cube_count"]
                ):
                    raise OnlineCubingError(
                        "online-cubing retry columns changed"
                    )
                return decision
            total_row = database.execute(
                "SELECT COUNT(*) FROM online_cubing_decisions WHERE policy_sha256=?",
                (self.policy.sha256,),
            ).fetchone()
            if total_row is None or int(total_row[0]) >= self.policy.max_decisions:
                raise OnlineCubingError("online-cubing decision budget exhausted")
            pending_row = database.execute(
                "SELECT COUNT(*) FROM online_cubing_decisions d "
                "LEFT JOIN online_cubing_outcomes o USING(decision_sha256) "
                "WHERE d.policy_sha256=? AND d.formula_family_sha256=? "
                "AND o.decision_sha256 IS NULL",
                (self.policy.sha256, family),
            ).fetchone()
            if (
                pending_row is None
                or int(pending_row[0]) >= self.policy.max_pending_per_family
            ):
                raise OnlineCubingError(
                    "online-cubing family pending budget exhausted"
                )
            ordinal_row = database.execute(
                "SELECT COUNT(*) FROM online_cubing_decisions "
                "WHERE policy_sha256=? AND formula_family_sha256=?",
                (self.policy.sha256, family),
            ).fetchone()
            ordinal = int(ordinal_row[0]) + 1 if ordinal_row is not None else 1
            history = self._family_history(database, family)
            arm, reason, arm_numerator, arm_denominator = self._choose_arm(
                self._arm_stats(history),
                family=family,
                ordinal=ordinal,
                activity_available=activity_available,
            )
            if arm == "cost":
                (
                    cube_count,
                    cube_reason,
                    cube_numerator,
                    cube_denominator,
                ) = self._choose_cube_count(
                    self._cost_stats(history),
                    family=family,
                    ordinal=ordinal,
                )
            else:
                cube_count = self.policy.base_cube_count
                cube_reason = "fixed-base"
                cube_numerator = 1
                cube_denominator = 1
            joint_numerator = arm_numerator * cube_numerator
            joint_denominator = arm_denominator * cube_denominator
            joint_divisor = math.gcd(joint_numerator, joint_denominator)
            body: dict[str, Any] = {
                "schema": ONLINE_CUBING_DECISION_SCHEMA,
                "protocol": ONLINE_CUBING_PROTOCOL,
                "policy": self.policy.as_dict(),
                "policy_sha256": self.policy.sha256,
                "query_id": query,
                "formula_family_sha256": family,
                "family_ordinal": ordinal,
                "arm": arm,
                "cube_count": cube_count,
                "selection_reason": reason,
                "cube_selection_reason": cube_reason,
                "arm_propensity_numerator": arm_numerator,
                "arm_propensity_denominator": arm_denominator,
                "cube_propensity_numerator": cube_numerator,
                "cube_propensity_denominator": cube_denominator,
                "propensity_numerator": joint_numerator // joint_divisor,
                "propensity_denominator": joint_denominator // joint_divisor,
                "activity_available": activity_available,
                "prerun_requested": arm in {"activity", "cost"},
            }
            body["decision_sha256"] = _content_digest(body)
            decision = verify_online_cubing_decision(
                body,
                policy=self.policy,
                query_id=query,
                formula_family_sha256=family,
            )
            database.execute(
                "INSERT INTO online_cubing_decisions("
                "decision_sha256,policy_sha256,formula_family_sha256,query_id,"
                "family_ordinal,arm,cube_count,decision_json,created_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    decision["decision_sha256"],
                    self.policy.sha256,
                    family,
                    query,
                    ordinal,
                    arm,
                    cube_count,
                    _canonical_json(decision).decode("ascii"),
                    time.time_ns(),
                ),
            )
            return decision

    def observe(
        self,
        decision: Mapping[str, Any],
        *,
        status: str,
        partition_executed: bool,
        elapsed_us: int,
        prerun_elapsed_us: int,
        completed_cubes: int,
        activity_receipts: int,
    ) -> dict[str, Any]:
        query = str(decision.get("query_id", ""))
        family = str(decision.get("formula_family_sha256", ""))
        verified_decision = verify_online_cubing_decision(
            decision,
            policy=self.policy,
            query_id=query,
            formula_family_sha256=family,
        )
        if status not in {"sat", "unsat", "unknown", "error"}:
            raise OnlineCubingError("online-cubing observation status is invalid")
        if type(partition_executed) is not bool:
            raise OnlineCubingError("online-cubing partition state is invalid")
        elapsed = _integer(
            elapsed_us, "online-cubing elapsed time", 0, (1 << 63) - 1
        )
        prerun = _integer(
            prerun_elapsed_us, "online-cubing prerun elapsed time", 0, elapsed
        )
        completed = _integer(
            completed_cubes,
            "online-cubing completed cubes",
            0,
            int(verified_decision["cube_count"]),
        )
        receipts = _integer(
            activity_receipts, "online-cubing activity receipts", 0, 4096
        )
        body: dict[str, Any] = {
            "schema": ONLINE_CUBING_OUTCOME_SCHEMA,
            "protocol": ONLINE_CUBING_PROTOCOL,
            "policy_sha256": self.policy.sha256,
            "decision_sha256": verified_decision["decision_sha256"],
            "query_id": query,
            "formula_family_sha256": family,
            "arm": verified_decision["arm"],
            "cube_count": verified_decision["cube_count"],
            "status": status,
            "partition_executed": partition_executed,
            "elapsed_us": elapsed,
            "prerun_elapsed_us": prerun,
            "completed_cubes": completed,
            "activity_receipts": receipts,
            **online_cubing_budget(
                self.policy,
                cube_count=int(verified_decision["cube_count"]),
                prerun_elapsed_us=prerun,
            ),
            "reward_micros": _reward(
                self.policy,
                status=status,
                elapsed_us=elapsed,
                completed_cubes=completed,
                cube_count=int(verified_decision["cube_count"]),
            ),
        }
        body["outcome_sha256"] = _content_digest(body)
        outcome = verify_online_cubing_outcome(
            body, decision=verified_decision, policy=self.policy
        )
        encoded = _canonical_json(outcome).decode("ascii")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._assert_metadata(database)
            stored_decision = database.execute(
                "SELECT decision_json FROM online_cubing_decisions "
                "WHERE decision_sha256=?",
                (verified_decision["decision_sha256"],),
            ).fetchone()
            if stored_decision is None or _load_canonical_json(
                str(stored_decision["decision_json"]),
                "online-cubing decision",
            ) != verified_decision:
                raise OnlineCubingError("online-cubing decision is not store-owned")
            existing = database.execute(
                "SELECT outcome_json FROM online_cubing_outcomes "
                "WHERE decision_sha256=?",
                (verified_decision["decision_sha256"],),
            ).fetchone()
            if existing is not None:
                if str(existing["outcome_json"]) != encoded:
                    raise OnlineCubingError(
                        "online-cubing decision has a conflicting outcome"
                    )
                return outcome
            database.execute(
                "INSERT INTO online_cubing_outcomes("
                "decision_sha256,outcome_sha256,status,elapsed_us,reward_micros,"
                "outcome_json,created_ns) VALUES(?,?,?,?,?,?,?)",
                (
                    verified_decision["decision_sha256"],
                    outcome["outcome_sha256"],
                    status,
                    elapsed,
                    outcome["reward_micros"],
                    encoded,
                    time.time_ns(),
                ),
            )
        return outcome

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as database:
            self._assert_metadata(database)
            decisions = int(
                database.execute(
                    "SELECT COUNT(*) FROM online_cubing_decisions"
                ).fetchone()[0]
            )
            outcomes = int(
                database.execute(
                    "SELECT COUNT(*) FROM online_cubing_outcomes"
                ).fetchone()[0]
            )
            rows = database.execute(
                "SELECT d.arm,COUNT(*) AS samples,"
                "COALESCE(SUM(o.elapsed_us),0) AS elapsed_us,"
                "COALESCE(SUM(o.reward_micros),0) AS reward_micros "
                "FROM online_cubing_decisions d LEFT JOIN online_cubing_outcomes o "
                "USING(decision_sha256) GROUP BY d.arm ORDER BY d.arm"
            ).fetchall()
        body: dict[str, Any] = {
            "schema": "symcc-qfbv-online-cubing-snapshot-v1",
            "protocol": ONLINE_CUBING_PROTOCOL,
            "store_identity_sha256": self.identity_sha256,
            "policy_sha256": self.policy.sha256,
            "decisions": decisions,
            "outcomes": outcomes,
            "pending": decisions - outcomes,
            "arms": {
                str(row["arm"]): {
                    "decisions": int(row["samples"]),
                    "elapsed_us": int(row["elapsed_us"]),
                    "reward_micros": int(row["reward_micros"]),
                }
                for row in rows
            },
        }
        body["snapshot_sha256"] = _content_digest(body)
        return body


def normalize_online_cubing_result(
    result: Mapping[str, Any],
    *,
    certificate: Mapping[str, Any],
    status: str,
    formula_family_sha256: str,
) -> dict[str, Any]:
    """Validate result telemetry before QueryStore persists it."""
    if result.get("backend_online_cubing_protocol") != ONLINE_CUBING_PROTOCOL:
        raise OnlineCubingError("online-cubing result protocol changed")
    policy = OnlineCubingPolicy.from_sealed(
        result.get("backend_online_cubing_policy")
    )
    if result.get("backend_online_cubing_policy_sha256") != policy.sha256:
        raise OnlineCubingError("online-cubing result policy identity changed")
    query_id = _identity(
        result.get("backend_online_cubing_query_id"), "online-cubing result query"
    )
    family = _digest(
        formula_family_sha256, "online-cubing result formula family"
    )
    if result.get("backend_online_cubing_formula_family_sha256") != family:
        raise OnlineCubingError("online-cubing result formula family changed")
    decision = verify_online_cubing_decision(
        result.get("backend_online_cubing_decision"),
        policy=policy,
        query_id=query_id,
        formula_family_sha256=family,
    )
    outcome = verify_online_cubing_outcome(
        result.get("backend_online_cubing_outcome"),
        decision=decision,
        policy=policy,
    )
    if outcome["status"] != status:
        raise OnlineCubingError("online-cubing outcome disagrees with result status")
    partition_protocol = result.get("backend_partition_execution_protocol")
    if outcome["partition_executed"]:
        if partition_protocol != "symcc-qfbv-proof-aware-execution-v1":
            raise OnlineCubingError("online-cubing outcome lacks partition evidence")
        if (
            result.get("backend_partition_cube_count") != decision["cube_count"]
            or result.get("backend_partition_completed_cubes")
            != outcome["completed_cubes"]
        ):
            raise OnlineCubingError("online-cubing partition accounting changed")
    elif partition_protocol:
        raise OnlineCubingError("prerun-only outcome carries partition evidence")
    certificate_digest = _digest(
        certificate.get("certificate_sha256"), "online-cubing bit-blast certificate"
    )
    return {
        "backend_online_cubing_protocol": ONLINE_CUBING_PROTOCOL,
        "backend_online_cubing_policy": policy.as_dict(),
        "backend_online_cubing_policy_sha256": policy.sha256,
        "backend_online_cubing_query_id": query_id,
        "backend_online_cubing_formula_family_sha256": family,
        "backend_online_cubing_bitblast_certificate_sha256": certificate_digest,
        "backend_online_cubing_decision": decision,
        "backend_online_cubing_outcome": outcome,
    }
