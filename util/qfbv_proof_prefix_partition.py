#!/usr/bin/env python3
"""Proof-guided, independently replayable QF_BV search partitioning.

Checked clause-activity receipts rank split variables.  Soundness does not
depend on that ranking: a verifier replays the complete binary split
transcript and proves that its final cubes are exhaustive and pairwise
disjoint.  Certificates are bound to the deterministic bit-blast plan and can
be published as lifecycle-managed proof artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import fcntl
from contextlib import nullcontext
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbv_artifact_lifecycle import (
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from qfbv_incremental_proof import IncrementalProofChecker
from qfbv_incremental_sat import BitBlastPlan
from qfbv_realtime_stream import verify_clause_activity_receipt
from qfbv_utility_pairing import formula_family_sha256


PARTITION_PROTOCOL = "symcc-qfbv-proof-prefix-partition-v1"
PARTITION_SCHEMA = "symcc-qfbv-proof-prefix-partition-certificate-v1"
PARTITION_POLICY_SCHEMA = "symcc-qfbv-proof-prefix-partition-policy-v1"
PARTITION_STORE_SCHEMA = "symcc-qfbv-proof-prefix-partition-store-v1"
MAX_CUBES = 4096
MAX_DEPTH = 64
MAX_ACTIVITY_RECEIPTS = 4096
MAX_RECORD_BYTES = 64 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TEMP_OBJECT = re.compile(r"\.[0-9a-f]{62}\.json\.[0-9]+\.[0-9a-f]{16}\.tmp")


class ProofPrefixPartitionError(ValueError):
    """A partition policy, evidence item, certificate, or CAS object is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex_digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ProofPrefixPartitionError(f"{name} must be a lowercase SHA-256")
    return value


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise ProofPrefixPartitionError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ProofPrefixPartitionError(f"{name} must be in [{lower}, {upper}]")
    return value


def _bounded_identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ProofPrefixPartitionError(f"{name} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProofPrefixPartitionError(f"{name} is invalid") from error
    if (
        not value
        or len(encoded) > 256
        or any(character < 32 or character == 127 for character in encoded)
    ):
        raise ProofPrefixPartitionError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class ProofPrefixPartitionPolicy:
    cube_count: int
    max_depth: int = 16
    max_activity_receipts: int = 4096
    input_variables_only: bool = True
    allow_static_fallback: bool = True

    def __post_init__(self) -> None:
        _bounded_int(self.cube_count, "partition cube count", 1, MAX_CUBES)
        _bounded_int(self.max_depth, "partition maximum depth", 0, MAX_DEPTH)
        _bounded_int(
            self.max_activity_receipts,
            "partition activity budget",
            0,
            MAX_ACTIVITY_RECEIPTS,
        )
        if type(self.input_variables_only) is not bool:
            raise ProofPrefixPartitionError(
                "partition input_variables_only must be boolean"
            )
        if type(self.allow_static_fallback) is not bool:
            raise ProofPrefixPartitionError(
                "partition allow_static_fallback must be boolean"
            )
        required_depth = 0 if self.cube_count == 1 else math.ceil(
            math.log2(self.cube_count)
        )
        if required_depth > self.max_depth:
            raise ProofPrefixPartitionError(
                "partition cube count exceeds the depth budget"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PARTITION_POLICY_SCHEMA,
            "protocol": PARTITION_PROTOCOL,
            "cube_count": self.cube_count,
            "max_depth": self.max_depth,
            "max_activity_receipts": self.max_activity_receipts,
            "input_variables_only": self.input_variables_only,
            "allow_static_fallback": self.allow_static_fallback,
        }

    @property
    def sha256(self) -> str:
        return _digest(_canonical_json(self.as_dict()))

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "ProofPrefixPartitionPolicy":
        if not isinstance(raw, Mapping):
            raise ProofPrefixPartitionError("partition policy must be an object")
        if (
            raw.get("schema") != PARTITION_POLICY_SCHEMA
            or raw.get("protocol") != PARTITION_PROTOCOL
            or set(raw)
            != {
                "schema",
                "protocol",
                "cube_count",
                "max_depth",
                "max_activity_receipts",
                "input_variables_only",
                "allow_static_fallback",
            }
        ):
            raise ProofPrefixPartitionError("partition policy scope changed")
        policy = cls(
            cube_count=raw.get("cube_count"),
            max_depth=raw.get("max_depth"),
            max_activity_receipts=raw.get("max_activity_receipts"),
            input_variables_only=raw.get("input_variables_only"),
            allow_static_fallback=raw.get("allow_static_fallback"),
        )
        if policy.as_dict() != dict(raw):
            raise ProofPrefixPartitionError("partition policy changed")
        return policy


def _plan_scope(plan: BitBlastPlan) -> dict[str, Any]:
    certificate_sha256 = _hex_digest(
        plan.certificate.get("certificate_sha256"), "bit-blast certificate"
    )
    if certificate_sha256 != _digest(
        _canonical_json(
            {
                key: value
                for key, value in plan.certificate.items()
                if key != "certificate_sha256"
            }
        )
    ):
        raise ProofPrefixPartitionError("bit-blast certificate identity changed")
    return {
        "query_id": _bounded_identity(plan.query_id, "partition query"),
        "formula_sha256": _hex_digest(plan.formula_sha256, "partition formula"),
        "cnf_sha256": _hex_digest(
            plan.certificate.get("cnf_sha256"), "partition CNF"
        ),
        "assumption_sha256": _hex_digest(
            plan.assumption_sha256, "partition assumptions"
        ),
        "bitblast_certificate_sha256": certificate_sha256,
        "max_variable": _bounded_int(
            plan.max_variable, "partition maximum variable", 1, 20_000_000
        ),
        "base_assumptions": list(plan.assumptions),
    }


def _input_variables(plan: BitBlastPlan) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for _offset, literals in plan.input_literals:
        for literal in literals:
            variable = abs(
                _bounded_int(
                    literal,
                    "partition input literal",
                    -plan.max_variable,
                    plan.max_variable,
                )
            )
            if variable and variable not in seen:
                seen.add(variable)
                result.append(variable)
    return tuple(result)


def _activity_ranking(
    plan: BitBlastPlan,
    evidence: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    checker: IncrementalProofChecker | None,
    policy: ProofPrefixPartitionPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(evidence) > policy.max_activity_receipts:
        raise ProofPrefixPartitionError("partition activity budget exceeded")
    if evidence and checker is None:
        raise ProofPrefixPartitionError(
            "proof-guided partitioning requires an incremental proof checker"
        )
    eligible_inputs = (
        set(_input_variables(plan)) if policy.input_variables_only else None
    )
    assumed_variables = {abs(literal) for literal in plan.assumptions}
    scores: dict[int, dict[str, int]] = {}
    receipts: list[dict[str, Any]] = []
    seen_receipts: set[str] = set()
    for raw_receipt, raw_ack in evidence:
        if not isinstance(raw_receipt, Mapping) or not isinstance(raw_ack, Mapping):
            raise ProofPrefixPartitionError("partition activity evidence is invalid")
        try:
            authorization = verify_clause_activity_receipt(
                plan, raw_receipt, ack=raw_ack, checker=checker  # type: ignore[arg-type]
            )
        except Exception as error:
            raise ProofPrefixPartitionError(
                "partition activity evidence failed proof replay"
            ) from error
        receipt_sha256 = _hex_digest(
            raw_receipt.get("activity_sha256"), "partition activity receipt"
        )
        if receipt_sha256 in seen_receipts:
            raise ProofPrefixPartitionError("duplicate partition activity receipt")
        seen_receipts.add(receipt_sha256)
        kind = raw_receipt.get("kind")
        if kind not in {"unit", "conflict"}:
            raise ProofPrefixPartitionError("partition activity kind changed")
        decision_level = _bounded_int(
            raw_receipt.get("decision_level"),
            "partition activity decision level",
            0,
            (1 << 63) - 1,
        )
        base_weight = (8 if kind == "conflict" else 4) + max(
            0, 32 - min(decision_level, 32)
        )
        for literal in authorization.clause:
            variable = abs(literal)
            if (
                variable in assumed_variables
                or (
                    eligible_inputs is not None
                    and variable not in eligible_inputs
                )
            ):
                continue
            row = scores.setdefault(
                variable,
                {
                    "variable": variable,
                    "score": 0,
                    "occurrences": 0,
                    "positive": 0,
                    "negative": 0,
                    "unit": 0,
                    "conflict": 0,
                },
            )
            row["score"] += base_weight
            row["occurrences"] += 1
            row["positive" if literal > 0 else "negative"] += 1
            row[kind] += 1
        receipts.append(
            {
                "receipt": dict(raw_receipt),
                "ack": dict(raw_ack),
                "activity_sha256": receipt_sha256,
                "ack_sha256": _hex_digest(
                    raw_ack.get("ack_sha256"), "partition activity ACK"
                ),
                "record_sha256": authorization.record_sha256,
                "source_worker": _bounded_identity(
                    authorization.source_worker, "partition source worker"
                ),
                "decision_level": decision_level,
                "kind": kind,
            }
        )
    ranking = sorted(scores.values(), key=lambda row: (-row["score"], row["variable"]))
    return ranking, receipts


def _ordered_split_variables(
    plan: BitBlastPlan,
    ranking: Sequence[Mapping[str, Any]],
    policy: ProofPrefixPartitionPolicy,
) -> tuple[tuple[int, ...], str]:
    required = 0 if policy.cube_count == 1 else math.ceil(math.log2(policy.cube_count))
    ordered = [int(row["variable"]) for row in ranking]
    seen = set(ordered)
    if len(ordered) < required and policy.allow_static_fallback:
        candidates: Sequence[int] = (
            _input_variables(plan)
            if policy.input_variables_only
            else range(1, plan.max_variable + 1)
        )
        assumptions = {abs(literal) for literal in plan.assumptions}
        for variable in candidates:
            if variable not in seen and variable not in assumptions:
                ordered.append(variable)
                seen.add(variable)
                if len(ordered) >= required:
                    break
    if len(ordered) < required:
        raise ProofPrefixPartitionError(
            "partition lacks enough independent split variables"
        )
    source = "checked-proof-activity"
    if not ranking:
        source = "static-input-fallback"
    elif any(variable not in {int(row["variable"]) for row in ranking} for variable in ordered[:required]):
        source = "checked-proof-activity-plus-static-fallback"
    return tuple(ordered[:required]), source


def _cube_identity(scope: Mapping[str, Any], literals: Sequence[int]) -> str:
    return _digest(
        _canonical_json(
            {
                "protocol": PARTITION_PROTOCOL,
                "formula_sha256": scope["formula_sha256"],
                "cnf_sha256": scope["cnf_sha256"],
                "base_assumption_sha256": scope["assumption_sha256"],
                "literals": list(literals),
            }
        )
    )


def _assumption_identity(assumptions: Sequence[int]) -> str:
    return _digest(
        _canonical_json(
            {
                "schema": "symcc-qfbv-cube-assumptions-v1",
                "protocol": PARTITION_PROTOCOL,
                "assumptions": list(assumptions),
            }
        )
    )


def build_proof_prefix_partition(
    plan: BitBlastPlan,
    policy: ProofPrefixPartitionPolicy,
    *,
    activity_evidence: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Any]]
    ] = (),
    checker: IncrementalProofChecker | None = None,
) -> dict[str, Any]:
    """Build a deterministic partition certificate over one bit-blast plan."""
    scope = _plan_scope(plan)
    ranking, receipts = _activity_ranking(
        plan, activity_evidence, checker=checker, policy=policy
    )
    variables, selection_source = _ordered_split_variables(
        plan, ranking, policy
    )
    if receipts and selection_source == "static-input-fallback":
        selection_source = "checked-proof-activity-plus-static-fallback"
    leaves: set[tuple[int, ...]] = {()}
    steps: list[dict[str, Any]] = []
    for ordinal in range(1, policy.cube_count):
        parent = min(leaves, key=lambda path: (len(path), path))
        depth = len(parent)
        if depth >= len(variables) or depth >= policy.max_depth:
            raise ProofPrefixPartitionError("partition split depth is exhausted")
        variable = variables[depth]
        negative = parent + (-variable,)
        positive = parent + (variable,)
        leaves.remove(parent)
        leaves.update((negative, positive))
        steps.append(
            {
                "ordinal": ordinal,
                "parent": list(parent),
                "split_variable": variable,
                "negative_child": list(negative),
                "positive_child": list(positive),
            }
        )
    cubes: list[dict[str, Any]] = []
    for ordinal, literals in enumerate(sorted(leaves, key=lambda path: (len(path), path))):
        assumptions = tuple(scope["base_assumptions"]) + literals
        cubes.append(
            {
                "ordinal": ordinal,
                "literals": list(literals),
                "cube_sha256": _cube_identity(scope, literals),
                "assumptions": list(assumptions),
                "assumption_sha256": _assumption_identity(assumptions),
                "estimated_load_numerator": 1,
                "estimated_load_denominator": 1 << len(literals),
            }
        )
    body: dict[str, Any] = {
        "schema": PARTITION_SCHEMA,
        "protocol": PARTITION_PROTOCOL,
        **scope,
        "policy": policy.as_dict(),
        "policy_sha256": policy.sha256,
        "selection_source": selection_source,
        "activity_receipts": receipts,
        "variable_ranking": [dict(row) for row in ranking],
        "split_variables": list(variables),
        "split_steps": steps,
        "cubes": cubes,
        "coverage_witness": {
            "method": "complete-binary-split-replay-v1",
            "root_count": 1,
            "split_count": len(steps),
            "leaf_count": len(cubes),
            "estimated_load_sum_numerator": 1,
            "estimated_load_sum_denominator": 1,
        },
    }
    body["partition_sha256"] = _digest(_canonical_json(body))
    return verify_proof_prefix_partition(plan, body, checker=checker)


def _literal_path(
    raw: Any, *, max_variable: int, max_depth: int, name: str
) -> tuple[int, ...]:
    if (
        not isinstance(raw, list)
        or len(raw) > max_depth
        or any(type(value) is not int for value in raw)
    ):
        raise ProofPrefixPartitionError(f"{name} is not a bounded literal path")
    result = tuple(
        _bounded_int(value, name, -max_variable, max_variable) for value in raw
    )
    if any(literal == 0 for literal in result):
        raise ProofPrefixPartitionError(f"{name} contains literal zero")
    variables = [abs(literal) for literal in result]
    if len(set(variables)) != len(variables):
        raise ProofPrefixPartitionError(f"{name} repeats a split variable")
    return result


def verify_proof_prefix_partition(
    plan: BitBlastPlan,
    raw: Mapping[str, Any],
    *,
    checker: IncrementalProofChecker | None = None,
) -> dict[str, Any]:
    """Verify identity, scope, evidence shape, and exhaustive split replay."""
    if not isinstance(raw, Mapping):
        raise ProofPrefixPartitionError("partition certificate must be an object")
    body = dict(raw)
    supplied = _hex_digest(
        body.pop("partition_sha256", ""), "partition certificate"
    )
    if _digest(_canonical_json(body)) != supplied:
        raise ProofPrefixPartitionError("partition certificate identity changed")
    expected_fields = {
        "schema",
        "protocol",
        "query_id",
        "formula_sha256",
        "cnf_sha256",
        "assumption_sha256",
        "bitblast_certificate_sha256",
        "max_variable",
        "base_assumptions",
        "policy",
        "policy_sha256",
        "selection_source",
        "activity_receipts",
        "variable_ranking",
        "split_variables",
        "split_steps",
        "cubes",
        "coverage_witness",
    }
    if set(body) != expected_fields:
        raise ProofPrefixPartitionError("partition certificate shape changed")
    if (
        body.get("schema") != PARTITION_SCHEMA
        or body.get("protocol") != PARTITION_PROTOCOL
    ):
        raise ProofPrefixPartitionError("partition certificate scope changed")
    scope = _plan_scope(plan)
    for name, expected in scope.items():
        if body.get(name) != expected:
            raise ProofPrefixPartitionError(f"partition {name} changed")
    policy = ProofPrefixPartitionPolicy.from_sealed(body.get("policy"))
    if body.get("policy_sha256") != policy.sha256:
        raise ProofPrefixPartitionError("partition policy identity changed")
    source = body.get("selection_source")
    if source not in {
        "checked-proof-activity",
        "checked-proof-activity-plus-static-fallback",
        "static-input-fallback",
    }:
        raise ProofPrefixPartitionError("partition selection source changed")
    receipts = body.get("activity_receipts")
    if (
        not isinstance(receipts, list)
        or len(receipts) > policy.max_activity_receipts
    ):
        raise ProofPrefixPartitionError("partition activity receipt list changed")
    receipt_ids: set[str] = set()
    sealed_evidence: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "receipt",
            "ack",
            "activity_sha256",
            "ack_sha256",
            "record_sha256",
            "source_worker",
            "decision_level",
            "kind",
        }:
            raise ProofPrefixPartitionError("partition activity receipt shape changed")
        identity = _hex_digest(
            receipt.get("activity_sha256"), "partition activity receipt"
        )
        if identity in receipt_ids:
            raise ProofPrefixPartitionError("partition activity receipt duplicated")
        receipt_ids.add(identity)
        _hex_digest(receipt.get("ack_sha256"), "partition activity ACK")
        _hex_digest(receipt.get("record_sha256"), "partition proof record")
        _bounded_identity(receipt.get("source_worker"), "partition source worker")
        _bounded_int(
            receipt.get("decision_level"),
            "partition activity decision level",
            0,
            (1 << 63) - 1,
        )
        if receipt.get("kind") not in {"unit", "conflict"}:
            raise ProofPrefixPartitionError("partition activity kind changed")
        raw_receipt = receipt.get("receipt")
        raw_ack = receipt.get("ack")
        if not isinstance(raw_receipt, Mapping) or not isinstance(raw_ack, Mapping):
            raise ProofPrefixPartitionError(
                "partition sealed activity evidence changed"
            )
        if (
            raw_receipt.get("activity_sha256") != identity
            or raw_ack.get("ack_sha256") != receipt.get("ack_sha256")
        ):
            raise ProofPrefixPartitionError(
                "partition activity evidence identity changed"
            )
        sealed_evidence.append((raw_receipt, raw_ack))
    if source == "static-input-fallback" and receipts:
        raise ProofPrefixPartitionError("static partition unexpectedly names activity")
    ranking = body.get("variable_ranking")
    if not isinstance(ranking, list) or len(ranking) > plan.max_variable:
        raise ProofPrefixPartitionError("partition variable ranking changed")
    ranking_variables: set[int] = set()
    previous_key: tuple[int, int] | None = None
    for row in ranking:
        if not isinstance(row, Mapping) or set(row) != {
            "variable",
            "score",
            "occurrences",
            "positive",
            "negative",
            "unit",
            "conflict",
        }:
            raise ProofPrefixPartitionError("partition variable ranking shape changed")
        variable = _bounded_int(
            row.get("variable"), "partition ranked variable", 1, plan.max_variable
        )
        if variable in ranking_variables:
            raise ProofPrefixPartitionError("partition ranked variable duplicated")
        ranking_variables.add(variable)
        score = _bounded_int(
            row.get("score"), "partition variable score", 1, (1 << 63) - 1
        )
        occurrences = _bounded_int(
            row.get("occurrences"),
            "partition variable occurrences",
            1,
            MAX_ACTIVITY_RECEIPTS,
        )
        counts = [
            _bounded_int(row.get(name), f"partition variable {name}", 0, occurrences)
            for name in ("positive", "negative", "unit", "conflict")
        ]
        if counts[0] + counts[1] != occurrences or counts[2] + counts[3] != occurrences:
            raise ProofPrefixPartitionError("partition variable counts disagree")
        key = (-score, variable)
        if previous_key is not None and key <= previous_key:
            raise ProofPrefixPartitionError("partition variable ranking is not canonical")
        previous_key = key
    if receipts:
        if checker is None:
            raise ProofPrefixPartitionError(
                "partition activity replay requires an incremental proof checker"
            )
        recomputed_ranking, recomputed_receipts = _activity_ranking(
            plan,
            sealed_evidence,
            checker=checker,
            policy=policy,
        )
        if recomputed_ranking != ranking or recomputed_receipts != receipts:
            raise ProofPrefixPartitionError(
                "partition activity-derived ranking changed"
            )
    raw_variables = body.get("split_variables")
    if not isinstance(raw_variables, list) or len(raw_variables) > policy.max_depth:
        raise ProofPrefixPartitionError("partition split-variable list changed")
    split_variables = tuple(
        _bounded_int(value, "partition split variable", 1, plan.max_variable)
        for value in raw_variables
    )
    if len(set(split_variables)) != len(split_variables):
        raise ProofPrefixPartitionError("partition split variables repeat")
    if {abs(value) for value in plan.assumptions} & set(split_variables):
        raise ProofPrefixPartitionError("partition splits a base assumption")
    if policy.input_variables_only and not set(split_variables) <= set(
        _input_variables(plan)
    ):
        raise ProofPrefixPartitionError("partition splits a non-input variable")
    expected_variables, expected_source = _ordered_split_variables(
        plan, ranking, policy
    )
    if receipts and expected_source == "static-input-fallback":
        expected_source = "checked-proof-activity-plus-static-fallback"
    if split_variables != expected_variables or source != expected_source:
        raise ProofPrefixPartitionError(
            "partition proof-guided variable selection changed"
        )
    steps = body.get("split_steps")
    if not isinstance(steps, list) or len(steps) != policy.cube_count - 1:
        raise ProofPrefixPartitionError("partition split count changed")
    leaves: set[tuple[int, ...]] = {()}
    for expected_ordinal, step in enumerate(steps, 1):
        if not isinstance(step, Mapping) or set(step) != {
            "ordinal",
            "parent",
            "split_variable",
            "negative_child",
            "positive_child",
        }:
            raise ProofPrefixPartitionError("partition split step shape changed")
        if step.get("ordinal") != expected_ordinal:
            raise ProofPrefixPartitionError("partition split order changed")
        parent = _literal_path(
            step.get("parent"),
            max_variable=plan.max_variable,
            max_depth=policy.max_depth,
            name="partition parent",
        )
        if parent not in leaves:
            raise ProofPrefixPartitionError("partition splits a non-leaf cube")
        depth = len(parent)
        if depth >= len(split_variables):
            raise ProofPrefixPartitionError("partition split exceeds variable order")
        variable = _bounded_int(
            step.get("split_variable"),
            "partition split variable",
            1,
            plan.max_variable,
        )
        if variable != split_variables[depth] or variable in {abs(item) for item in parent}:
            raise ProofPrefixPartitionError("partition split variable changed")
        negative = _literal_path(
            step.get("negative_child"),
            max_variable=plan.max_variable,
            max_depth=policy.max_depth,
            name="partition negative child",
        )
        positive = _literal_path(
            step.get("positive_child"),
            max_variable=plan.max_variable,
            max_depth=policy.max_depth,
            name="partition positive child",
        )
        if negative != parent + (-variable,) or positive != parent + (variable,):
            raise ProofPrefixPartitionError("partition children do not cover the parent")
        leaves.remove(parent)
        leaves.update((negative, positive))
    cubes = body.get("cubes")
    if not isinstance(cubes, list) or len(cubes) != policy.cube_count:
        raise ProofPrefixPartitionError("partition cube list changed")
    observed: list[tuple[int, ...]] = []
    load = Fraction(0, 1)
    for expected_ordinal, cube in enumerate(cubes):
        if not isinstance(cube, Mapping) or set(cube) != {
            "ordinal",
            "literals",
            "cube_sha256",
            "assumptions",
            "assumption_sha256",
            "estimated_load_numerator",
            "estimated_load_denominator",
        }:
            raise ProofPrefixPartitionError("partition cube shape changed")
        if cube.get("ordinal") != expected_ordinal:
            raise ProofPrefixPartitionError("partition cube order changed")
        literals = _literal_path(
            cube.get("literals"),
            max_variable=plan.max_variable,
            max_depth=policy.max_depth,
            name="partition cube",
        )
        if cube.get("cube_sha256") != _cube_identity(scope, literals):
            raise ProofPrefixPartitionError("partition cube identity changed")
        assumptions = tuple(scope["base_assumptions"]) + literals
        if cube.get("assumptions") != list(assumptions):
            raise ProofPrefixPartitionError("partition cube assumptions changed")
        if cube.get("assumption_sha256") != _assumption_identity(assumptions):
            raise ProofPrefixPartitionError("partition cube assumption identity changed")
        numerator = _bounded_int(
            cube.get("estimated_load_numerator"),
            "partition load numerator",
            1,
            1 << MAX_DEPTH,
        )
        denominator = _bounded_int(
            cube.get("estimated_load_denominator"),
            "partition load denominator",
            1,
            1 << MAX_DEPTH,
        )
        if numerator != 1 or denominator != 1 << len(literals):
            raise ProofPrefixPartitionError("partition load estimate changed")
        load += Fraction(numerator, denominator)
        observed.append(literals)
    canonical_leaves = sorted(leaves, key=lambda path: (len(path), path))
    if observed != canonical_leaves:
        raise ProofPrefixPartitionError("partition leaves differ from split replay")
    if load != Fraction(1, 1):
        raise ProofPrefixPartitionError("partition leaves do not cover unit load")
    witness = body.get("coverage_witness")
    if witness != {
        "method": "complete-binary-split-replay-v1",
        "root_count": 1,
        "split_count": len(steps),
        "leaf_count": len(cubes),
        "estimated_load_sum_numerator": 1,
        "estimated_load_sum_denominator": 1,
    }:
        raise ProofPrefixPartitionError("partition coverage witness changed")
    verified = dict(body)
    verified["partition_sha256"] = supplied
    return verified


def partition_job_catalog(
    plan: BitBlastPlan,
    certificate: Mapping[str, Any],
    *,
    checker: IncrementalProofChecker | None = None,
    backlog_per_cube: int = 1,
) -> dict[str, tuple[str, int]]:
    """Project verified cube identities into the malleable-worker job catalog."""
    verified = verify_proof_prefix_partition(
        plan, certificate, checker=checker
    )
    partition = str(verified["partition_sha256"])
    family = formula_family_sha256(plan.certificate)
    backlog = _bounded_int(
        backlog_per_cube, "partition cube backlog", 0, (1 << 63) - 1
    )
    cubes = verified.get("cubes")
    if not isinstance(cubes, list) or not cubes:
        raise ProofPrefixPartitionError("partition certificate has no cubes")
    result: dict[str, tuple[str, int]] = {}
    for ordinal, cube in enumerate(cubes):
        if not isinstance(cube, Mapping) or cube.get("ordinal") != ordinal:
            raise ProofPrefixPartitionError("partition cube catalog is invalid")
        _hex_digest(cube.get("cube_sha256"), "partition cube")
        result[f"qfbv-cube:{partition[:24]}:{ordinal}"] = (family, backlog)
    return result


def _partition_dependencies(
    certificate: Mapping[str, Any],
) -> tuple[ArtifactRef, ...]:
    receipts = certificate.get("activity_receipts")
    if not isinstance(receipts, list) or len(receipts) > MAX_ACTIVITY_RECEIPTS:
        raise ProofPrefixPartitionError(
            "partition activity dependencies exceed their bound"
        )
    dependencies: list[ArtifactRef] = []
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ProofPrefixPartitionError(
                "partition activity dependency is invalid"
            )
        digest = _hex_digest(
            receipt.get("record_sha256"), "partition proof dependency"
        )
        if digest not in seen:
            seen.add(digest)
            dependencies.append(ArtifactRef("sat-proof", digest))
    return tuple(dependencies)


def _reject_duplicate_json_members(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProofPrefixPartitionError(
                "partition object contains duplicate JSON members"
            )
        result[key] = value
    return result


class ProofPrefixPartitionStore:
    """Small descriptor-anchored immutable CAS for partition certificates."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ) -> None:
        if lifecycle_lease is not None and lifecycle is None:
            raise ProofPrefixPartitionError(
                "partition lifecycle lease requires a registry"
            )
        root_path = Path(root)
        if root_path.is_symlink():
            raise ProofPrefixPartitionError("partition store root must not be a symlink")
        self.root = root_path.resolve()
        self.objects = self.root / "objects"
        self.db_path = self.root / "index.sqlite3"
        self.initialize_lock_path = self.root / ".initialize.lock"
        self.objects.mkdir(parents=True, exist_ok=True)
        if self.objects.is_symlink():
            raise ProofPrefixPartitionError(
                "partition object root must not be a symlink"
            )
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self.identity_sha256 = _digest(
            _canonical_json(
                {
                    "schema": PARTITION_STORE_SCHEMA,
                    "protocol": PARTITION_PROTOCOL,
                    "root": str(self.root),
                    "lifecycle_identity_sha256": (
                        lifecycle.identity_sha256 if lifecycle is not None else ""
                    ),
                }
            )
        )
        if self.lifecycle is None:
            self._initialize()
        else:
            with self.lifecycle.maintenance():
                self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path.is_symlink():
            raise ProofPrefixPartitionError(
                "partition store index must not be a symlink"
            )
        database = sqlite3.connect(self.db_path, timeout=30.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def _initialize(self) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError(
                "O_NOFOLLOW is required for partition store initialization"
            )
        descriptor = os.open(
            self.initialize_lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            path_state = os.stat(
                self.initialize_lock_path, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_state.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (path_state.st_dev, path_state.st_ino)
            ):
                raise ProofPrefixPartitionError(
                    "partition initialization lock is not stable"
                )
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except InterruptedError:
                    continue
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "partition store initialization lock timeout"
                        ) from error
                    time.sleep(min(0.01, remaining))
            locked_state = os.stat(
                self.initialize_lock_path, follow_symlinks=False
            )
            if (opened.st_dev, opened.st_ino) != (
                locked_state.st_dev,
                locked_state.st_ino,
            ):
                raise ProofPrefixPartitionError(
                    "partition initialization lock changed while waiting"
                )
            self._initialize_locked()
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _initialize_locked(self) -> None:
        with self._connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS store_metadata(" 
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            expected = {
                "schema": PARTITION_STORE_SCHEMA,
                "protocol": PARTITION_PROTOCOL,
                "root": str(self.root),
            }
            if self.lifecycle is not None:
                expected["lifecycle_identity_sha256"] = (
                    self.lifecycle.identity_sha256
                )
            for key, value in expected.items():
                row = database.execute(
                    "SELECT value FROM store_metadata WHERE key=?", (key,)
                ).fetchone()
                if row is None:
                    database.execute(
                        "INSERT INTO store_metadata(key,value) VALUES(?,?)",
                        (key, value),
                    )
                elif str(row["value"]) != value:
                    raise ProofPrefixPartitionError(
                        f"partition store metadata mismatch for {key}"
                    )
            if self.lifecycle is None:
                managed = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key='lifecycle_identity_sha256'"
                ).fetchone()
                if managed is not None:
                    raise ProofPrefixPartitionError(
                        "managed partition store requires its artifact lifecycle"
                    )

    def _assert_lifecycle_mode(self) -> None:
        with self._connect() as database:
            row = database.execute(
                "SELECT value FROM store_metadata "
                "WHERE key='lifecycle_identity_sha256'"
            ).fetchone()
        if self.lifecycle is None:
            if row is not None:
                raise ProofPrefixPartitionError(
                    "managed partition store requires its artifact lifecycle"
                )
            return
        if row is None or str(row["value"]) != self.lifecycle.identity_sha256:
            raise ProofPrefixPartitionError(
                "partition store lifecycle identity changed"
            )

    def _open_shard(self, digest: str, *, create: bool) -> tuple[int, str]:
        identity = _hex_digest(digest, "partition object")
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            objects = os.open(self.objects, flags | no_follow)
        except OSError as error:
            raise ProofPrefixPartitionError(
                "partition object root is not a stable directory"
            ) from error
        try:
            if create:
                created = False
                try:
                    os.mkdir(identity[:2], mode=0o700, dir_fd=objects)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    os.fsync(objects)
            try:
                shard = os.open(identity[:2], flags | no_follow, dir_fd=objects)
            except FileNotFoundError:
                raise FileNotFoundError(identity) from None
            except OSError as error:
                raise ProofPrefixPartitionError(
                    "partition shard is not a stable directory"
                ) from error
            opened = os.fstat(shard)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(shard)
                raise ProofPrefixPartitionError(
                    "partition shard is not a directory"
                )
            return shard, identity[2:] + ".json"
        finally:
            os.close(objects)

    @staticmethod
    def _read_at(directory: int, name: str) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
                raise ProofPrefixPartitionError(
                    "partition object is not a bounded regular file"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    raise ProofPrefixPartitionError("partition object was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ProofPrefixPartitionError("partition object grew while reading")
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ProofPrefixPartitionError("partition object identity changed")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def publish(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        *,
        checker: IncrementalProofChecker | None = None,
    ) -> tuple[str, bool]:
        self._assert_lifecycle_mode()
        verified = verify_proof_prefix_partition(
            plan, certificate, checker=checker
        )
        digest = str(verified["partition_sha256"])
        encoded = _canonical_json(verified)
        if len(encoded) > MAX_RECORD_BYTES:
            raise ProofPrefixPartitionError("partition certificate exceeds its byte limit")
        operation = self.lifecycle.operation() if self.lifecycle is not None else nullcontext()
        with operation:
            dependencies = _partition_dependencies(verified)
            if self.lifecycle is not None and dependencies:
                self.lifecycle.touch(
                    dependencies,
                    lease=self.lifecycle_lease,
                )
            directory, name = self._open_shard(digest, create=True)
            temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            created = False
            descriptor = -1
            try:
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=directory,
                    )
                    view = memoryview(encoded)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short partition CAS write")
                        view = view[written:]
                    os.fsync(descriptor)
                    os.close(descriptor)
                    descriptor = -1
                    try:
                        os.link(
                            temporary,
                            name,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                        created = True
                    except FileExistsError:
                        pass
                    if self._read_at(directory, name) != encoded:
                        raise ProofPrefixPartitionError(
                            "partition CAS publication lost identity"
                        )
                    os.fsync(directory)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(directory)
            if self.lifecycle is not None:
                self.lifecycle.record_artifact(
                    ArtifactRef("partition", digest),
                    encoded_bytes=len(encoded),
                    edges=dependencies,
                    lease=self.lifecycle_lease,
                )
            return digest, created

    def load(
        self,
        plan: BitBlastPlan,
        digest: str,
        *,
        checker: IncrementalProofChecker | None = None,
    ) -> dict[str, Any]:
        self._assert_lifecycle_mode()
        directory, name = self._open_shard(digest, create=False)
        try:
            encoded = self._read_at(directory, name)
        finally:
            os.close(directory)
        try:
            raw = json.loads(
                encoded.decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProofPrefixPartitionError("partition object is not canonical JSON") from error
        verified = verify_proof_prefix_partition(plan, raw, checker=checker)
        if verified["partition_sha256"] != digest:
            raise ProofPrefixPartitionError("partition object path identity changed")
        if _canonical_json(verified) != encoded:
            raise ProofPrefixPartitionError("partition object encoding is not canonical")
        return verified

    def delete(self, digest: str, expected_bytes: int) -> int:
        self._assert_lifecycle_mode()
        expected = _bounded_int(
            expected_bytes,
            "partition deletion size",
            0,
            MAX_RECORD_BYTES,
        )
        try:
            directory, name = self._open_shard(digest, create=False)
        except FileNotFoundError:
            return 0
        try:
            encoded = self._read_at(directory, name)
            if len(encoded) != expected:
                raise ProofPrefixPartitionError("partition deletion size changed")
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
            return len(encoded)
        finally:
            os.close(directory)

    def delete_lifecycle_artifact(
        self, kind: str, digest: str, expected_bytes: int
    ) -> int:
        if kind != "partition":
            raise ProofPrefixPartitionError(
                "invalid proof-prefix partition artifact kind"
            )
        return self.delete(digest, expected_bytes)

    def _stored_objects(
        self, *, max_entries: int
    ) -> tuple[list[tuple[str, int]], bool]:
        limit = _bounded_int(
            max_entries, "partition lifecycle scan limit", 1, 10_000_000
        )
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            objects = os.open(self.objects, flags | no_follow)
        except OSError as error:
            raise ProofPrefixPartitionError(
                "partition object root is not a stable directory"
            ) from error
        found: list[tuple[str, int]] = []
        complete = True
        try:
            for shard_name in sorted(os.listdir(objects)):
                if re.fullmatch(r"[0-9a-f]{2}", shard_name) is None:
                    raise ProofPrefixPartitionError(
                        "partition store contains a noncanonical shard"
                    )
                try:
                    shard = os.open(
                        shard_name, flags | no_follow, dir_fd=objects
                    )
                except OSError as error:
                    raise ProofPrefixPartitionError(
                        "partition shard is not a stable directory"
                    ) from error
                try:
                    for leaf_name in sorted(os.listdir(shard)):
                        if _TEMP_OBJECT.fullmatch(leaf_name) is not None:
                            continue
                        if re.fullmatch(r"[0-9a-f]{62}\.json", leaf_name) is None:
                            raise ProofPrefixPartitionError(
                                "partition store contains a noncanonical object"
                            )
                        if len(found) >= limit:
                            complete = False
                            return found, complete
                        opened = os.stat(
                            leaf_name, dir_fd=shard, follow_symlinks=False
                        )
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_size > MAX_RECORD_BYTES
                        ):
                            raise ProofPrefixPartitionError(
                                "partition store object is not a bounded regular file"
                            )
                        found.append(
                            (shard_name + leaf_name[:-5], opened.st_size)
                        )
                finally:
                    os.close(shard)
        finally:
            os.close(objects)
        return found, complete

    def _load_stored_identity(self, digest: str) -> tuple[dict[str, Any], int]:
        directory, name = self._open_shard(digest, create=False)
        try:
            encoded = self._read_at(directory, name)
        finally:
            os.close(directory)
        try:
            raw = json.loads(
                encoded.decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProofPrefixPartitionError(
                "partition object is not canonical JSON"
            ) from error
        if not isinstance(raw, dict):
            raise ProofPrefixPartitionError("partition object must be an object")
        body = dict(raw)
        supplied = _hex_digest(
            body.pop("partition_sha256", ""), "partition certificate"
        )
        if supplied != digest or _digest(_canonical_json(body)) != digest:
            raise ProofPrefixPartitionError(
                "partition stored identity changed"
            )
        if _canonical_json(raw) != encoded:
            raise ProofPrefixPartitionError(
                "partition object encoding is not canonical"
            )
        return raw, len(encoded)

    def synchronize_lifecycle(
        self, *, max_entries: int
    ) -> dict[str, int | bool]:
        if self.lifecycle is None:
            raise ProofPrefixPartitionError("partition lifecycle is disabled")
        self._assert_lifecycle_mode()
        with self.lifecycle.operation():
            objects, complete = self._stored_objects(max_entries=max_entries)
            for digest, observed_size in objects:
                certificate, exact_size = self._load_stored_identity(digest)
                if exact_size != observed_size:
                    raise ProofPrefixPartitionError(
                        "partition object size changed during lifecycle scan"
                    )
                dependencies = _partition_dependencies(certificate)
                if dependencies:
                    self.lifecycle.touch(dependencies)
                self.lifecycle.record_artifact(
                    ArtifactRef("partition", digest),
                    encoded_bytes=exact_size,
                    edges=dependencies,
                )
        return {
            "indexed": len(objects),
            "total": len(objects) if complete else len(objects) + 1,
            "complete": complete,
        }

    def stats(self, *, max_entries: int = 1_000_000) -> dict[str, int | bool | str]:
        self._assert_lifecycle_mode()
        objects, complete = self._stored_objects(max_entries=max_entries)
        return {
            "schema": PARTITION_STORE_SCHEMA,
            "partitions": len(objects),
            "bytes": sum(size for _digest_value, size in objects),
            "complete": complete,
            "identity_sha256": self.identity_sha256,
        }
