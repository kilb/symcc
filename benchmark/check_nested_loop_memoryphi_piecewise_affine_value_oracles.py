#!/usr/bin/env python3
"""Independent finite guard-specialized piecewise-affine oracle for F420."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import itertools
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v10"
PREDICATES = {"eq", "ne", "ugt", "uge", "ult", "ule"}


@dataclass(frozen=True)
class AffineArm:
    constant: int
    outer_scale: int
    inner_scale: int
    input_scale: int


@dataclass(frozen=True)
class PiecewiseAffineValueCase:
    object_bytes: int
    outer_step: int
    inner_step: int
    outer_bound_bits: int
    inner_bound_bits: int
    address_outer_scale: int
    address_inner_scale: int
    value_bits: int
    predicate: str
    guard_induction: str
    guard_constant: int
    constant_on_left: bool
    when_true: AffineArm
    when_false: AffineArm
    load_bytes: int
    load_offsets: tuple[int, ...]
    overlay_byte: int | None
    endianness: str
    input_offset: int = 3


def _bound_maximum(bits: int) -> int:
    return (1 << bits) - 1 if type(bits) is int and 1 <= bits < 64 else 0


def _instances(case: PiecewiseAffineValueCase) -> tuple[dict[str, int], ...]:
    if case.outer_step <= 0 or case.inner_step <= 0:
        return ()
    return tuple(
        {
            "outer_induction_value": outer,
            "inner_induction_value": inner,
            "index_value": (
                case.address_outer_scale * outer
                + case.address_inner_scale * inner
            ),
            "address": (
                case.address_outer_scale * outer
                + case.address_inner_scale * inner
            ),
        }
        for outer in range(
            0, _bound_maximum(case.outer_bound_bits), case.outer_step
        )
        for inner in range(
            0, _bound_maximum(case.inner_bound_bits), case.inner_step
        )
    )


def _guard(case: PiecewiseAffineValueCase, outer: int, inner: int) -> bool:
    induction = outer if case.guard_induction == "outer" else inner
    left, right = (
        (case.guard_constant, induction)
        if case.constant_on_left else (induction, case.guard_constant)
    )
    return {
        "eq": left == right,
        "ne": left != right,
        "ugt": left > right,
        "uge": left >= right,
        "ult": left < right,
        "ule": left <= right,
    }[case.predicate]


def _arm_value(
    arm: AffineArm, outer: int, inner: int, input_value: int, bits: int,
) -> int:
    return (
        arm.constant
        + arm.outer_scale * outer
        + arm.inner_scale * inner
        + arm.input_scale * input_value
    ) & ((1 << bits) - 1)


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _stored_bytes(value: int, bits: int, endianness: str) -> tuple[int, ...]:
    width = bits // 8
    order = range(width) if endianness == "little" else range(width - 1, -1, -1)
    return tuple((value >> (8 * lane)) & 0xFF for lane in order)


def _valid_arm(arm: AffineArm) -> bool:
    return all(
        type(value) is int and 0 <= value <= (1 << 63) - 1
        for value in (
            arm.constant, arm.outer_scale, arm.inner_scale, arm.input_scale,
        )
    )


def _arms_equivalent(
    left: AffineArm, right: AffineArm, bits: int,
) -> bool:
    mask = (1 << bits) - 1
    return all(
        (left_value & mask) == (right_value & mask)
        for left_value, right_value in zip(
            (
                left.constant, left.outer_scale,
                left.inner_scale, left.input_scale,
            ),
            (
                right.constant, right.outer_scale,
                right.inner_scale, right.input_scale,
            ),
        )
    )


def mathematically_supported(case: PiecewiseAffineValueCase) -> bool:
    instances = _instances(case)
    width = case.value_bits // 8 if case.value_bits > 0 else 0
    covered = {
        instance["address"] + lane
        for instance in instances
        for lane in range(width)
    }
    return (
        type(case.object_bytes) is int
        and 2 <= case.object_bytes <= 64
        and type(case.outer_step) is int
        and 1 <= case.outer_step <= 64
        and type(case.inner_step) is int
        and 1 <= case.inner_step <= 64
        and 1 <= case.outer_bound_bits < 64
        and 1 <= case.inner_bound_bits < 64
        and 1 <= len(instances) <= 256
        and len(range(
            0, _bound_maximum(case.outer_bound_bits), case.outer_step
        )) <= 64
        and len(range(
            0, _bound_maximum(case.inner_bound_bits), case.inner_step
        )) <= 64
        and type(case.address_outer_scale) is int
        and 1 <= case.address_outer_scale <= (1 << 63) - 1
        and type(case.address_inner_scale) is int
        and 1 <= case.address_inner_scale <= (1 << 63) - 1
        and case.value_bits in {8, 16, 24, 32, 40, 48, 56, 64}
        and width <= case.address_inner_scale * case.inner_step
        and case.predicate in PREDICATES
        and case.guard_induction in {"outer", "inner"}
        and type(case.guard_constant) is int
        and 0 <= case.guard_constant <= (1 << 63) - 1
        and type(case.constant_on_left) is bool
        and _valid_arm(case.when_true)
        and _valid_arm(case.when_false)
        and not _arms_equivalent(
            case.when_true, case.when_false, case.value_bits
        )
        and type(case.input_offset) is int
        and case.input_offset >= 0
        and (
            case.overlay_byte is None
            or type(case.overlay_byte) is int
            and -128 <= case.overlay_byte <= 127
        )
        and case.endianness in {"little", "big"}
        and all(
            0 <= instance["address"]
            and instance["address"] + width <= case.object_bytes
            for instance in instances
        )
        and type(case.load_bytes) is int
        and 1 <= case.load_bytes <= min(8, case.object_bytes)
        and bool(case.load_offsets)
        and len(set(case.load_offsets)) == len(case.load_offsets)
        and all(
            type(offset) is int
            and 0 <= offset <= case.object_bytes - case.load_bytes
            and all(offset + lane in covered for lane in range(case.load_bytes))
            for offset in case.load_offsets
        )
    )


def _arm_record(case: PiecewiseAffineValueCase, arm: AffineArm) -> dict[str, Any]:
    return {
        "kind": "affine-bitvector-arm",
        "bits": case.value_bits,
        "constant": arm.constant,
        "outer_scale": arm.outer_scale,
        "inner_scale": arm.inner_scale,
        "input_scale": arm.input_scale,
        "input": {
            "variable": {"var": "arg3"},
            "offset": case.input_offset,
            "bytes": case.value_bits // 8,
        } if arm.input_scale else None,
        "semantics": "modulo-2^bits",
    }


def make_certificate(case: PiecewiseAffineValueCase) -> dict[str, Any]:
    instances = _instances(case)
    width = case.value_bits // 8
    stored_value = {
        "kind": "piecewise-affine-bitvector",
        "bits": case.value_bits,
        "guard": {
            "predicate": case.predicate,
            "induction": case.guard_induction,
            "constant_on_left": case.constant_on_left,
            "constant": case.guard_constant,
            "bits": case.value_bits,
        },
        "when_true": _arm_record(case, case.when_true),
        "when_false": _arm_record(case, case.when_false),
        "semantics": "guard-specialized-modulo-2^bits",
    }
    writers: list[dict[str, Any]] = [{
        "ordinal": 0,
        "bytes": width,
        "instances": [dict(item) for item in instances],
        "stored_value": stored_value,
    }]
    if case.overlay_byte is not None:
        writers.append({
            "ordinal": 1,
            "bytes": 1,
            "instances": [dict(item) for item in instances],
            "stored_value": {
                "kind": "constant-integer",
                "bits": 8,
                "operand": case.overlay_byte,
                "bytes": [case.overlay_byte & 0xFF],
            },
        })
    alternatives: dict[int, list[dict[str, Any]]] = {}
    for writer_index, writer in enumerate(writers):
        for instance in instances:
            for lane in range(writer["bytes"]):
                if writer_index == 0:
                    result = _guard(
                        case, instance["outer_induction_value"],
                        instance["inner_induction_value"],
                    )
                    arm = case.when_true if result else case.when_false
                    specialized = _signed(
                        arm.constant
                        + arm.outer_scale * instance["outer_induction_value"]
                        + arm.inner_scale * instance["inner_induction_value"],
                        case.value_bits,
                    )
                    value_lane = lane if case.endianness == "little" else width - lane - 1
                    expression = {
                        "kind": "guard-specialized-byte",
                        "guard_result": result,
                        "selected_arm": "true" if result else "false",
                        "value": {
                            "kind": "extract-affine-bitvector-byte",
                            "bits": case.value_bits,
                            "input": {"var": "arg3"} if arm.input_scale else None,
                            "input_scale": arm.input_scale,
                            "constant": specialized,
                            "low_bit": value_lane * 8,
                        },
                    }
                else:
                    expression = {
                        "kind": "constant-byte",
                        "value": case.overlay_byte & 0xFF,
                    }
                alternatives.setdefault(instance["address"] + lane, []).append({
                    "writer": writer_index,
                    "writer_address": instance["address"],
                    "writer_lane": lane,
                    "outer_induction_value": instance["outer_induction_value"],
                    "inner_induction_value": instance["inner_induction_value"],
                    "minimum_outer_bound": instance["outer_induction_value"] + 1,
                    "minimum_inner_bound": instance["inner_induction_value"] + 1,
                    "value_byte_expression": expression,
                })
    witnesses = [
        {
            "load_address": offset,
            "lane": lane,
            "last_write_cases": sorted(
                alternatives[offset + lane],
                key=lambda item: (
                    item["outer_induction_value"],
                    item["inner_induction_value"], item["writer"],
                ),
                reverse=True,
            ),
        }
        for offset in case.load_offsets
        for lane in range(case.load_bytes)
    ]
    return {
        "schema": SCHEMA,
        "endianness": case.endianness,
        "writers": writers,
        "load_bytes": case.load_bytes,
        "load_addresses": list(case.load_offsets),
        "witnesses": witnesses,
    }


def validate_certificate(
    raw: Mapping[str, Any], case: PiecewiseAffineValueCase,
) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_loads(
    case: PiecewiseAffineValueCase,
    *,
    outer_count: int,
    inner_count: int,
    input_value: int,
) -> tuple[tuple[int, ...] | None, ...]:
    memory: dict[int, int] = {}
    for outer in range(0, outer_count, case.outer_step):
        for inner in range(0, inner_count, case.inner_step):
            address = (
                case.address_outer_scale * outer
                + case.address_inner_scale * inner
            )
            arm = case.when_true if _guard(case, outer, inner) else case.when_false
            value = _arm_value(arm, outer, inner, input_value, case.value_bits)
            for lane, byte in enumerate(
                _stored_bytes(value, case.value_bits, case.endianness)
            ):
                memory[address + lane] = byte
            if case.overlay_byte is not None:
                memory[address] = case.overlay_byte & 0xFF
    return tuple(
        (
            tuple(memory[offset + lane] for lane in range(case.load_bytes))
            if all(offset + lane in memory for lane in range(case.load_bytes))
            else None
        )
        for offset in case.load_offsets
    )


def _evaluate_expression(expression: Mapping[str, Any], input_value: int) -> int:
    if expression["kind"] == "constant-byte":
        return int(expression["value"])
    value_expression = expression["value"]
    bits = int(value_expression["bits"])
    value = (
        int(value_expression["constant"])
        + int(value_expression["input_scale"]) * input_value
    ) & ((1 << bits) - 1)
    return (value >> int(value_expression["low_bit"])) & 0xFF


def summary_loads(
    certificate: Mapping[str, Any],
    *,
    outer_count: int,
    inner_count: int,
    input_value: int,
) -> tuple[tuple[int, ...] | None, ...]:
    selected: dict[int, dict[int, int]] = {}
    for witness in certificate["witnesses"]:
        selected_case = next((
            item for item in witness["last_write_cases"]
            if outer_count >= item["minimum_outer_bound"]
            and inner_count >= item["minimum_inner_bound"]
        ), None)
        if selected_case is not None:
            selected.setdefault(witness["load_address"], {})[
                witness["lane"]
            ] = _evaluate_expression(
                selected_case["value_byte_expression"], input_value
            )
    width = int(certificate["load_bytes"])
    return tuple(
        (
            tuple(selected[offset][lane] for lane in range(width))
            if offset in selected and len(selected[offset]) == width
            else None
        )
        for offset in certificate["load_addresses"]
    )


def _base_case(**changes: Any) -> PiecewiseAffineValueCase:
    values: dict[str, Any] = {
        "object_bytes": 24,
        "outer_step": 1,
        "inner_step": 2,
        "outer_bound_bits": 2,
        "inner_bound_bits": 3,
        "address_outer_scale": 8,
        "address_inner_scale": 1,
        "value_bits": 16,
        "predicate": "ult",
        "guard_induction": "inner",
        "guard_constant": 2,
        "constant_on_left": False,
        "when_true": AffineArm(17, 5, 7, 3),
        "when_false": AffineArm(1025, 2, 11, 3),
        "load_bytes": 2,
        "load_offsets": tuple(range(23)),
        "overlay_byte": -86,
        "endianness": "little",
    }
    values.update(changes)
    return PiecewiseAffineValueCase(**values)


def mutation_oracle() -> int:
    case = _base_case()
    paths = (
        (("schema",), "symcc-loop-memoryphi-byte-lane-induction-v9"),
        (("writers", 0, "stored_value", "bits"), 8),
        (("writers", 0, "stored_value", "semantics"), "integer"),
        (("writers", 0, "stored_value", "guard", "predicate"), "slt"),
        (("writers", 0, "stored_value", "guard", "induction"), "unknown"),
        (("writers", 0, "stored_value", "guard", "constant"), 3),
        (("writers", 0, "stored_value", "guard", "constant_on_left"), True),
        (("writers", 0, "stored_value", "when_true", "constant"), 18),
        (("writers", 0, "stored_value", "when_true", "outer_scale"), 6),
        (("writers", 0, "stored_value", "when_false", "inner_scale"), 12),
        (("writers", 0, "stored_value", "when_false", "input_scale"), 4),
        (("writers", 0, "stored_value", "when_false", "input", "offset"), 4),
        (("writers", 0, "instances", 0, "address"), 1),
        (("writers", 1, "stored_value", "bytes", 0), 0),
        (("witnesses", 0, "last_write_cases", 0, "minimum_outer_bound"), 2),
        (("witnesses", 0, "last_write_cases", 0, "minimum_inner_bound"), 2),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "guard_result"), False),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "selected_arm"), "false"),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "value", "constant"), 18),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "value", "low_bit"), 0),
    )
    mutations = []
    for path, replacement in paths:
        candidate = copy.deepcopy(make_certificate(case))
        target: Any = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        mutations.append(candidate)
    reordered = copy.deepcopy(make_certificate(case))
    reordered["witnesses"][0]["last_write_cases"].reverse()
    mutations.append(reordered)
    missing_writer = copy.deepcopy(make_certificate(case))
    missing_writer["writers"].pop(0)
    mutations.append(missing_writer)
    return sum(not validate_certificate(item, case) for item in mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = vector_equivalences = 0
    byte_equivalences = complete = partial = 0
    for predicate, induction, reversed_operands, endianness, supported in itertools.product(
        sorted(PREDICATES), ("outer", "inner"), (False, True),
        ("little", "big"), (True, False),
    ):
        case = _base_case(
            predicate=predicate if supported else "slt",
            guard_induction=induction,
            constant_on_left=reversed_operands,
            endianness=endianness,
        )
        cases += 1
        if not mathematically_supported(case):
            rejected += 1
            continue
        accepted += 1
        certificate = make_certificate(case)
        for outer_count, inner_count, input_value in itertools.product(
            range(4), range(8), (0, 1, 0x7FFF, 0xFFFF),
        ):
            concrete = runtime_loads(
                case, outer_count=outer_count, inner_count=inner_count,
                input_value=input_value,
            )
            summary = summary_loads(
                certificate, outer_count=outer_count,
                inner_count=inner_count, input_value=input_value,
            )
            if concrete != summary:
                raise AssertionError(
                    "piecewise affine value summary diverged from runtime"
                )
            vector_equivalences += 1
            for concrete_load, summary_load in zip(concrete, summary):
                if concrete_load is None:
                    partial += 1
                else:
                    complete += 1
                    byte_equivalences += len(concrete_load)
                if concrete_load != summary_load:
                    raise AssertionError("byte-vector mismatch")
    return {
        "schema": "symcc-nested-loop-memoryphi-piecewise-affine-value-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_piecewise_affine_summaries": accepted,
        "rejected_unsupported": rejected,
        "predicate_order_endian_configurations": accepted,
        "runtime_load_vector_equivalences": vector_equivalences,
        "runtime_value_byte_equivalences": byte_equivalences,
        "runtime_complete_loads": complete,
        "runtime_uninitialized_or_partial_loads": partial,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite two-level i16 guard-specialized piecewise-affine reference "
            "equivalence only; not general path merging, LLVM construction "
            "latency, coverage, solver time, defect yield, or speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(run_oracle(), sort_keys=True, separators=(",", ":"))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
