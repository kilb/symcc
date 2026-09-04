#!/usr/bin/env python3
"""Versioned reusable solution generators for asynchronous SymCC queries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any


GENERATOR_SCHEMA = "symcc-solution-generator-v1"
OPTIMISTIC_SCHEMA = "symcc-optimistic-simplification-v1"
TACTIC_CONVERTER_SCHEMA = "symcc-z3-tactic-model-converter-v1"
QUERY_IR_CONVERTER_SCHEMA = "symcc-query-ir-converter-v1"
_MAX_OFFSET = (1 << 32) - 1


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def generator_hash(generator: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(generator)).hexdigest()


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return value


def _assignments(
    raw: Any,
    name: str,
    *,
    maximum: int = 65536,
) -> dict[str, int]:
    if not isinstance(raw, Mapping) or len(raw) > maximum:
        raise ValueError(f"{name} must be a bounded object")
    result: dict[str, int] = {}
    for raw_offset, raw_value in raw.items():
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} offsets must be integers") from exc
        _integer(offset, f"{name} offset", 0, _MAX_OFFSET)
        result[str(offset)] = _integer(raw_value, f"{name} value", 0, 255)
    return result


def _index_list(raw: Any, name: str, maximum: int) -> list[int]:
    if not isinstance(raw, Sequence) or isinstance(
            raw, (str, bytes)) or len(raw) > maximum:
        raise ValueError(f"{name} must be a bounded list")
    values = [
        _integer(value, name, 0, max(0, maximum - 1)) for value in raw
    ]
    if values != sorted(set(values)):
        raise ValueError(f"{name} must be sorted and unique")
    return values


def _optimistic_simplification(
    raw: Any,
    query_id: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("optimistic_simplification must be an object")
    if raw.get("schema") != OPTIMISTIC_SCHEMA:
        raise ValueError(
            f"optimistic schema must be {OPTIMISTIC_SCHEMA}")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("optimistic enabled must be a boolean")
    strategy = str(raw.get("strategy", ""))
    if strategy not in {"disabled", "target-assertion-slice"}:
        raise ValueError("unsupported optimistic simplification strategy")
    if enabled != (strategy != "disabled"):
        raise ValueError("optimistic enabled and strategy disagree")
    original_query_hash = str(raw.get("original_query_hash", ""))
    if len(original_query_hash) > 128:
        raise ValueError("optimistic original_query_hash is too long")
    if query_id and original_query_hash not in {"", query_id}:
        raise ValueError("optimistic original_query_hash does not match query")
    original_assertions = _integer(
        raw.get("original_assertions", 0),
        "optimistic original_assertions", 0, 65536)
    target_start = _integer(
        raw.get("target_assertion_start", 0),
        "optimistic target_assertion_start", 0, original_assertions)
    kept = _index_list(
        raw.get("kept_assertions", ()),
        "optimistic kept_assertions", original_assertions)
    dropped = _index_list(
        raw.get("dropped_assertions", ()),
        "optimistic dropped_assertions", original_assertions)
    if set(kept) & set(dropped) or sorted(kept + dropped) != list(
            range(original_assertions)):
        raise ValueError("optimistic assertion partition is incomplete")
    if any(index not in kept for index in range(
            target_start, original_assertions)):
        raise ValueError("optimistic simplification cannot drop target assertions")
    if enabled and (
            not dropped or dropped != list(range(target_start))):
        raise ValueError(
            "target-assertion-slice must drop exactly the prefix assertions")
    if not enabled and dropped:
        raise ValueError("disabled optimistic simplification cannot drop assertions")
    if raw.get("implication") != "original-implies-weakened":
        raise ValueError("optimistic implication direction is invalid")
    if raw.get("proposal_only") is not True:
        raise ValueError("optimistic simplification must be proposal-only")
    if raw.get("full_validation_required") is not True:
        raise ValueError("optimistic candidates must require full validation")
    return {
        "schema": OPTIMISTIC_SCHEMA,
        "enabled": enabled,
        "strategy": strategy,
        "original_query_hash": original_query_hash,
        "original_assertions": original_assertions,
        "target_assertion_start": target_start,
        "kept_assertions": kept,
        "dropped_assertions": dropped,
        "implication": "original-implies-weakened",
        "proposal_only": True,
        "full_validation_required": True,
    }


def _tactic_model_converter(raw: Any, query_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("tactic_model_converter must be an object")
    if raw.get("schema") != TACTIC_CONVERTER_SCHEMA:
        raise ValueError(
            f"tactic converter schema must be {TACTIC_CONVERTER_SCHEMA}")
    enabled = raw.get("enabled")
    applied = raw.get("applied")
    if not isinstance(enabled, bool) or not isinstance(applied, bool):
        raise ValueError("tactic converter flags must be booleans")
    if applied and not enabled:
        raise ValueError("disabled tactic converter cannot be applied")
    pipeline_raw = raw.get("pipeline", ())
    if not isinstance(pipeline_raw, Sequence) or isinstance(
            pipeline_raw, (str, bytes)) or not 1 <= len(pipeline_raw) <= 8:
        raise ValueError("tactic converter pipeline must be bounded")
    pipeline = [str(stage) for stage in pipeline_raw]
    if pipeline != ["simplify", "solve-eqs"]:
        raise ValueError("unsupported tactic model-converter pipeline")
    original_query_hash = str(raw.get("original_query_hash", ""))
    if len(original_query_hash) > 128:
        raise ValueError("tactic converter query hash is too long")
    if query_id and original_query_hash not in {"", query_id}:
        raise ValueError("tactic converter query hash does not match query")
    integer_fields = {
        name: _integer(
            raw.get(name, 0), f"tactic converter {name}", 0, 65536)
        for name in (
            "input_assertions",
            "subgoals",
            "converted_models",
            "full_validation_checks",
            "full_validation_failures",
            "full_validation_accepted",
            "errors",
        )
    }
    if integer_fields["full_validation_checks"] != (
            integer_fields["full_validation_failures"] +
            integer_fields["full_validation_accepted"]):
        raise ValueError("tactic converter validation metrics are inconsistent")
    if not applied and (
            integer_fields["converted_models"] or
            integer_fields["full_validation_checks"]):
        raise ValueError("unapplied tactic converter cannot emit models")
    if raw.get("full_validation_required") is not True:
        raise ValueError("tactic-converted models require full validation")
    return {
        "schema": TACTIC_CONVERTER_SCHEMA,
        "enabled": enabled,
        "applied": applied,
        "pipeline": pipeline,
        "original_query_hash": original_query_hash,
        **integer_fields,
        "full_validation_required": True,
    }


def _query_ir_converter(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("converter entries must be objects")
    schema = raw.get("schema")
    if schema not in {None, QUERY_IR_CONVERTER_SCHEMA}:
        raise ValueError(
            f"query IR converter schema must be {QUERY_IR_CONVERTER_SCHEMA}")
    root = _integer(raw.get("root"), "converter root", 0, (1 << 31) - 1)
    if raw.get("relation") != "equal":
        raise ValueError("converter relation must be equal")
    steps_raw = raw.get("steps", ())
    if not isinstance(steps_raw, Sequence) or isinstance(
            steps_raw, (str, bytes)) or len(steps_raw) > 64:
        raise ValueError("converter steps must be a bounded list")
    steps: list[dict[str, Any]] = []
    unary = {"not", "neg"}
    extensions = {"zext", "sext"}
    binary = {"xor", "add", "sub", "mul", "rol", "ror"}
    for raw_step in steps_raw:
        if not isinstance(raw_step, Mapping):
            raise ValueError("converter steps must be objects")
        op = str(raw_step.get("op", ""))
        step: dict[str, Any] = {"op": op}
        if op == "concat-split":
            step["bits"] = _integer(
                raw_step.get("bits"), "converter step bits", 2, 64)
        elif op in unary:
            step["bits"] = _integer(
                raw_step.get("bits"), "converter step bits", 1, 64)
        elif op in extensions:
            source_bits = _integer(
                raw_step.get("from_bits"),
                "converter extension source bits", 1, 64)
            target_bits = _integer(
                raw_step.get("to_bits"),
                "converter extension target bits", source_bits, 64)
            step.update({"from_bits": source_bits, "to_bits": target_bits})
        elif op in binary:
            bits = _integer(
                raw_step.get("bits"), "converter step bits", 1, 64)
            constant = _integer(
                raw_step.get("constant"),
                "converter step constant", 0, (1 << bits) - 1)
            side = str(raw_step.get("constant_side", ""))
            if side not in {"left", "right"}:
                raise ValueError(
                    "converter constant_side must be left or right")
            step.update({
                "bits": bits,
                "constant": constant,
                "constant_side": side,
            })
        else:
            raise ValueError("unsupported query IR converter step")
        steps.append(step)
    assignments = _assignments(
        raw.get("assignments", {}),
        "converter assignments",
        maximum=16,
    )
    if not assignments:
        raise ValueError("converter assignments cannot be empty")
    if raw.get("proposal_only", True) is not True:
        raise ValueError("query IR converter must be proposal-only")
    if raw.get("full_validation_required", True) is not True:
        raise ValueError("query IR converter requires full validation")
    return {
        "schema": QUERY_IR_CONVERTER_SCHEMA,
        "root": root,
        "relation": "equal",
        "steps": steps,
        "assignments": assignments,
        "proposal_only": True,
        "full_validation_required": True,
    }


def normalize_generator(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("generator must be an object")
    if raw.get("schema") != GENERATOR_SCHEMA:
        raise ValueError(f"generator schema must be {GENERATOR_SCHEMA}")
    query_id = str(raw.get("query_id", ""))
    if len(query_id) > 128:
        raise ValueError("generator query_id is too long")
    input_size = _integer(
        raw.get("input_size", 0), "generator input_size", 0, 128 * 1024 * 1024)
    fixed = _assignments(raw.get("fixed", {}), "generator fixed")

    ranges_raw = raw.get("ranges", ())
    if not isinstance(ranges_raw, Sequence) or isinstance(
            ranges_raw, (str, bytes)) or len(ranges_raw) > 4096:
        raise ValueError("generator ranges must be a bounded list")
    ranges: list[list[int]] = []
    seen_ranges: set[int] = set()
    for row in ranges_raw:
        if not isinstance(row, Sequence) or isinstance(
                row, (str, bytes)) or len(row) != 3:
            raise ValueError("generator range rows must be [offset,min,max]")
        offset = _integer(row[0], "range offset", 0, _MAX_OFFSET)
        lower = _integer(row[1], "range lower", 0, 255)
        upper = _integer(row[2], "range upper", lower, 255)
        if offset in seen_ranges:
            raise ValueError("generator range offsets must be unique")
        seen_ranges.add(offset)
        ranges.append([offset, lower, upper])

    fields_raw = raw.get("fields", ())
    if not isinstance(fields_raw, Sequence) or isinstance(
            fields_raw, (str, bytes)) or len(fields_raw) > 4096:
        raise ValueError("generator fields must be a bounded list")
    fields: list[list[int]] = []
    for row in fields_raw:
        if not isinstance(row, Sequence) or isinstance(
                row, (str, bytes)) or not 1 <= len(row) <= 16:
            raise ValueError("generator fields must contain 1..16 offsets")
        offsets = [
            _integer(value, "field offset", 0, _MAX_OFFSET) for value in row
        ]
        if len(set(offsets)) != len(offsets):
            raise ValueError("generator field offsets must be unique")
        fields.append(offsets)

    converters_raw = raw.get("converter_chain", ())
    if not isinstance(converters_raw, Sequence) or isinstance(
            converters_raw, (str, bytes)) or len(converters_raw) > 4096:
        raise ValueError("converter_chain must be a bounded list")
    converters: list[dict[str, Any]] = []
    for converter in converters_raw:
        normalized_converter = _query_ir_converter(converter)
        encoded = _canonical(normalized_converter)
        if len(encoded) > 64 * 1024:
            raise ValueError("converter entry is too large")
        converters.append(json.loads(encoded.decode("ascii")))

    models_raw = raw.get("verified_models", ())
    if not isinstance(models_raw, Sequence) or isinstance(
            models_raw, (str, bytes)) or len(models_raw) > 64:
        raise ValueError("verified_models must be a bounded list")
    models = [
        _assignments(model, "verified model", maximum=65536)
        for model in models_raw
    ]

    metrics_raw = raw.get("metrics", {})
    if not isinstance(metrics_raw, Mapping) or len(metrics_raw) > 32:
        raise ValueError("generator metrics must be a bounded object")
    metrics = {
        str(key)[:64]: _integer(value, "generator metric", 0, (1 << 63) - 1)
        for key, value in metrics_raw.items()
    }
    seed = _integer(raw.get("seed", 0), "generator seed", 0, (1 << 64) - 1)
    normalized = {
        "schema": GENERATOR_SCHEMA,
        "query_id": query_id,
        "input_size": input_size,
        "seed": seed,
        "fixed": fixed,
        "ranges": ranges,
        "fields": fields,
        "converter_chain": converters,
        "verified_models": models,
        "metrics": metrics,
    }
    optimistic_raw = raw.get("optimistic_simplification")
    if optimistic_raw is not None:
        optimistic = _optimistic_simplification(optimistic_raw, query_id)
        if optimistic["enabled"]:
            checks = metrics.get("full_validation_checks")
            failures = metrics.get("full_validation_failures")
            accepted = metrics.get("full_validation_accepted")
            if checks is None or failures is None or accepted is None:
                raise ValueError(
                    "optimistic generator lacks full validation metrics")
            if checks != failures + accepted or accepted != len(models):
                raise ValueError(
                    "optimistic generator validation metrics are inconsistent")
        normalized["optimistic_simplification"] = optimistic
    tactic_raw = raw.get("tactic_model_converter")
    if tactic_raw is not None:
        tactic_converter = _tactic_model_converter(tactic_raw, query_id)
        if tactic_converter["full_validation_accepted"] > len(models):
            raise ValueError(
                "tactic converter accepted count exceeds verified models")
        normalized["tactic_model_converter"] = tactic_converter
    return normalized


def _constant(nodes: Sequence[Mapping[str, Any]], node_id: int) -> int | None:
    node = nodes[node_id]
    if node.get("op") != "constant":
        return None
    try:
        return int(str(node.get("attrs", {}).get("value_hex", "")), 16)
    except ValueError:
        return None


def derive_converter_chains(
    nodes: Sequence[Mapping[str, Any]],
    roots: Sequence[int],
) -> list[dict[str, Any]]:
    """Extract exact invertible equalities from a topological expression DAG."""

    def invert(
        node_id: int,
        target: int,
        steps: list[dict[str, Any]],
    ) -> dict[int, int] | None:
        node = nodes[node_id]
        op = str(node.get("op", ""))
        bits = int(node.get("bits", 0))
        if not 1 <= bits <= 64:
            return None
        mask = (1 << bits) - 1 if bits < 64 else (1 << 64) - 1
        target &= mask
        children = list(node.get("children", ()))
        if op == "read" and bits == 8:
            offset = int(node.get("attrs", {}).get("index", -1))
            return {offset: target} if 0 <= offset <= _MAX_OFFSET else None
        if op == "concat" and len(children) == 2:
            right_bits = int(nodes[children[1]].get("bits", 0))
            if not 1 <= right_bits < bits:
                return None
            left = invert(children[0], target >> right_bits, steps)
            right = invert(
                children[1], target & ((1 << right_bits) - 1), steps)
            if left is None or right is None:
                return None
            for offset, value in right.items():
                if offset in left and left[offset] != value:
                    return None
                left[offset] = value
            steps.append({"op": "concat-split", "bits": bits})
            return left
        if op in {"zext", "sext"} and len(children) == 1:
            child_bits = int(nodes[children[0]].get("bits", 0))
            if not 1 <= child_bits <= bits:
                return None
            child_mask = (1 << child_bits) - 1
            child_target = target & child_mask
            if op == "zext" and target != child_target:
                return None
            if op == "sext":
                sign = (child_target >> (child_bits - 1)) & 1
                expected = child_target
                if sign and bits > child_bits:
                    expected |= mask ^ child_mask
                if target != expected:
                    return None
            steps.append({"op": op, "from_bits": child_bits, "to_bits": bits})
            return invert(children[0], child_target, steps)
        if op in {"not", "neg"} and len(children) == 1:
            source = target ^ mask if op == "not" else (-target) & mask
            steps.append({"op": op, "bits": bits})
            return invert(children[0], source, steps)
        if op not in {"xor", "add", "sub", "mul", "rol", "ror"} or (
                len(children) != 2):
            return None
        left_constant = _constant(nodes, children[0])
        right_constant = _constant(nodes, children[1])
        if (left_constant is None) == (right_constant is None):
            return None
        constant = (
            right_constant if right_constant is not None else left_constant)
        assert constant is not None
        constant &= mask
        symbolic = children[0] if right_constant is not None else children[1]
        if op == "xor":
            source = target ^ constant
        elif op == "add":
            source = (target - constant) & mask
        elif op == "sub":
            source = (
                (target + constant) if right_constant is not None
                else (constant - target)
            ) & mask
        elif op == "mul":
            if constant & 1 == 0:
                return None
            source = (target * pow(constant, -1, 1 << bits)) & mask
        else:
            amount = constant % bits
            if op == "rol":
                source = (
                    (target >> amount) |
                    (target << (bits - amount if amount else 0))
                ) & mask if amount else target
            else:
                source = (
                    (target << amount) |
                    (target >> (bits - amount if amount else 0))
                ) & mask if amount else target
        steps.append({
            "op": op,
            "bits": bits,
            "constant": constant,
            "constant_side": "right" if right_constant is not None else "left",
        })
        return invert(symbolic, source, steps)

    converters: list[dict[str, Any]] = []
    for root in roots:
        if not 0 <= root < len(nodes):
            continue
        relation = nodes[root]
        relation_op = relation.get("op")
        relation_children = list(relation.get("children", ()))
        if relation_op == "lnot" and len(relation_children) == 1:
            nested = nodes[relation_children[0]]
            if nested.get("op") != "distinct":
                continue
            relation = nested
            relation_op = "equal"
            relation_children = list(relation.get("children", ()))
        if relation_op != "equal" or len(relation_children) != 2:
            continue
        left_constant = _constant(nodes, relation_children[0])
        right_constant = _constant(nodes, relation_children[1])
        if (left_constant is None) == (right_constant is None):
            continue
        target = right_constant if right_constant is not None else left_constant
        symbolic = (
            relation_children[0]
            if right_constant is not None else relation_children[1])
        assert target is not None
        steps: list[dict[str, Any]] = []
        assignments = invert(symbolic, target, steps)
        if not assignments:
            continue
        converters.append({
            "schema": QUERY_IR_CONVERTER_SCHEMA,
            "root": root,
            "relation": "equal",
            "steps": steps,
            "assignments": {
                str(offset): value
                for offset, value in sorted(assignments.items())
            },
            "proposal_only": True,
            "full_validation_required": True,
        })
    return converters


def sample_generator(
    raw: Mapping[str, Any],
    witness: bytes,
    budget: int,
    verifier: Callable[[bytes], bool],
) -> tuple[list[bytes], dict[str, int]]:
    """Deterministically sample a generator; only verifier-approved bytes return."""

    generator = normalize_generator(raw)
    budget = max(0, min(int(budget), 256))
    if generator["input_size"] and len(witness) != generator["input_size"]:
        raise ValueError("witness size does not match generator input_size")
    base = bytearray(witness)
    for offset, value in generator["fixed"].items():
        index = int(offset)
        if index >= len(base):
            raise ValueError("fixed assignment lies outside witness")
        base[index] = value
    range_by_offset = {
        offset: (lower, upper)
        for offset, lower, upper in generator["ranges"]
    }
    for offset in range_by_offset:
        if offset >= len(base):
            raise ValueError("range assignment lies outside witness")
    for field in generator["fields"]:
        for offset in field:
            if offset >= len(base):
                raise ValueError("field assignment lies outside witness")

    proposals: list[bytes] = []
    fixed_base_attempts = 0
    fixed_base = bytes(base)
    if fixed_base != witness:
        proposals.append(fixed_base)
        fixed_base_attempts = 1
    for model in generator["verified_models"]:
        candidate = bytearray(base)
        valid = True
        for offset, value in model.items():
            index = int(offset)
            if index >= len(candidate):
                valid = False
                break
            candidate[index] = value
        if valid:
            proposals.append(bytes(candidate))

    state = generator["seed"] or 0x9E3779B97F4A7C15
    ranges = generator["ranges"]
    proposal_limit = max(budget * 8, budget)
    converter_attempts = 0
    converter_conflicts = 0
    cumulative = bytearray(base)
    cumulative_assignments: dict[int, int] = {}
    cumulative_valid = True

    for converter in generator["converter_chain"]:
        if len(proposals) >= proposal_limit:
            break
        assignments = {
            int(offset): value
            for offset, value in converter["assignments"].items()
        }
        if any(offset >= len(base) for offset in assignments):
            raise ValueError("converter assignment lies outside witness")
        fixed_conflict = any(
            str(offset) in generator["fixed"]
            and generator["fixed"][str(offset)] != value
            for offset, value in assignments.items()
        )
        if fixed_conflict:
            converter_conflicts += 1
            cumulative_valid = False
            continue

        individual = bytearray(base)
        for offset, value in assignments.items():
            individual[offset] = value
        if individual != base:
            proposals.append(bytes(individual))
            converter_attempts += 1

        if any(
                offset in cumulative_assignments
                and cumulative_assignments[offset] != value
                for offset, value in assignments.items()):
            converter_conflicts += 1
            cumulative_valid = False
        if cumulative_valid:
            for offset, value in assignments.items():
                cumulative[offset] = value
                cumulative_assignments[offset] = value
            if (cumulative != base and cumulative != individual
                    and len(proposals) < proposal_limit):
                proposals.append(bytes(cumulative))
                converter_attempts += 1

    field_attempts = 0

    for field in generator["fields"]:
        if len(proposals) >= proposal_limit:
            break
        for selector in ("lower", "mid", "upper"):
            candidate = bytearray(base)
            changed = False
            for offset in field:
                bounds = range_by_offset.get(offset)
                if bounds is None:
                    continue
                lower, upper = bounds
                if selector == "lower":
                    value = lower
                elif selector == "upper":
                    value = upper
                else:
                    value = (lower + upper) // 2
                if candidate[offset] != value:
                    changed = True
                    candidate[offset] = value
            if changed:
                proposals.append(bytes(candidate))
                field_attempts += 1
            if len(proposals) >= proposal_limit:
                break

    attempts = 0
    while len(proposals) < proposal_limit and ranges:
        candidate = bytearray(base)
        for offset, lower, upper in ranges:
            state ^= state >> 12
            state ^= (state << 25) & ((1 << 64) - 1)
            state ^= state >> 27
            state &= (1 << 64) - 1
            mixed = (state * 2685821657736338717) & ((1 << 64) - 1)
            candidate[offset] = lower + mixed % (upper - lower + 1)
        proposals.append(bytes(candidate))
        attempts += 1

    accepted: list[bytes] = []
    seen: set[bytes] = set()
    verified = 0
    for candidate in proposals:
        if len(accepted) >= budget or candidate in seen:
            continue
        seen.add(candidate)
        verified += 1
        if verifier(candidate):
            accepted.append(candidate)
    return accepted, {
        "proposals": len(seen),
        "verified": verified,
        "accepted": len(accepted),
        "fixed_base_attempts": fixed_base_attempts,
        "converter_attempts": converter_attempts,
        "converter_conflicts": converter_conflicts,
        "field_attempts": field_attempts,
        "synthetic_attempts": attempts,
    }
