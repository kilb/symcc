#!/usr/bin/env python3
"""Independent finite affine-bitvector last-write oracle for F419."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import itertools
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v9"


@dataclass(frozen=True)
class AffineSymbolicValueCase:
    object_bytes: int
    outer_step: int
    inner_step: int
    outer_bound_bits: int
    inner_bound_bits: int
    address_outer_scale: int
    address_inner_scale: int
    value_bits: int
    value_constant: int
    value_outer_scale: int
    value_inner_scale: int
    value_input_scale: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    overlay_byte: int | None
    endianness: str
    input_offset: int = 3


def _bound_maximum(bits: int) -> int:
    return (1 << bits) - 1 if type(bits) is int and 1 <= bits < 64 else 0


def _instances(case: AffineSymbolicValueCase) -> tuple[dict[str, int], ...]:
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


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _stored_bytes(value: int, bits: int, endianness: str) -> tuple[int, ...]:
    width = bits // 8
    order = range(width) if endianness == "little" else range(width - 1, -1, -1)
    return tuple((value >> (8 * lane)) & 0xFF for lane in order)


def mathematically_supported(case: AffineSymbolicValueCase) -> bool:
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
        and width <= 8
        and width <= case.address_inner_scale * case.inner_step
        and type(case.value_constant) is int
        and 0 <= case.value_constant <= (1 << 63) - 1
        and all(
            type(coefficient) is int
            and 0 <= coefficient <= (1 << 63) - 1
            for coefficient in (
                case.value_outer_scale,
                case.value_inner_scale,
                case.value_input_scale,
            )
        )
        and any((
            case.value_outer_scale,
            case.value_inner_scale,
            case.value_input_scale,
        ))
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


def make_certificate(case: AffineSymbolicValueCase) -> dict[str, Any]:
    instances = _instances(case)
    width = case.value_bits // 8
    symbolic_writer = {
        "ordinal": 0,
        "bytes": width,
        "instances": [dict(item) for item in instances],
        "stored_value": {
            "kind": "affine-bitvector",
            "bits": case.value_bits,
            "constant": case.value_constant,
            "outer_scale": case.value_outer_scale,
            "inner_scale": case.value_inner_scale,
            "input_scale": case.value_input_scale,
            "input": {
                "variable": {"var": "arg3"},
                "offset": case.input_offset,
                "bytes": width,
            } if case.value_input_scale else None,
            "semantics": "modulo-2^bits",
        },
    }
    writers = [symbolic_writer]
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
    for writer, writer_data in enumerate(writers):
        for instance in instances:
            for lane in range(writer_data["bytes"]):
                if writer == 0:
                    specialized = _signed(
                        case.value_constant
                        + case.value_outer_scale
                        * instance["outer_induction_value"]
                        + case.value_inner_scale
                        * instance["inner_induction_value"],
                        case.value_bits,
                    )
                    value_lane = (
                        lane
                        if case.endianness == "little"
                        else width - lane - 1
                    )
                    expression = {
                        "kind": "extract-affine-bitvector-byte",
                        "bits": case.value_bits,
                        "input": (
                            {"var": "arg3"}
                            if case.value_input_scale else None
                        ),
                        "input_scale": case.value_input_scale,
                        "constant": specialized,
                        "low_bit": value_lane * 8,
                    }
                else:
                    expression = {
                        "kind": "constant-byte",
                        "value": case.overlay_byte & 0xFF,
                    }
                alternatives.setdefault(instance["address"] + lane, []).append({
                    "writer": writer,
                    "writer_address": instance["address"],
                    "writer_lane": lane,
                    "outer_induction_value": instance["outer_induction_value"],
                    "inner_induction_value": instance["inner_induction_value"],
                    "minimum_outer_bound": (
                        instance["outer_induction_value"] + 1
                    ),
                    "minimum_inner_bound": (
                        instance["inner_induction_value"] + 1
                    ),
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
                    item["inner_induction_value"],
                    item["writer"],
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
    raw: Mapping[str, Any], case: AffineSymbolicValueCase,
) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_loads(
    case: AffineSymbolicValueCase,
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
            value = (
                case.value_constant
                + case.value_outer_scale * outer
                + case.value_inner_scale * inner
                + case.value_input_scale * input_value
            ) & ((1 << case.value_bits) - 1)
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
    bits = int(expression["bits"])
    value = (
        int(expression["constant"])
        + int(expression["input_scale"]) * input_value
    ) & ((1 << bits) - 1)
    return (value >> int(expression["low_bit"])) & 0xFF


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


def mutation_oracle() -> int:
    case = AffineSymbolicValueCase(
        12, 1, 2, 2, 2, 4, 1, 16, 17, 5, 7, 3,
        2, tuple(range(11)), -86, "little",
    )
    mutations = []
    for path, replacement in (
        (("schema",), "symcc-loop-memoryphi-byte-lane-induction-v8"),
        (("writers", 0, "stored_value", "bits"), 8),
        (("writers", 0, "stored_value", "constant"), 18),
        (("writers", 0, "stored_value", "outer_scale"), 6),
        (("writers", 0, "stored_value", "inner_scale"), 8),
        (("writers", 0, "stored_value", "input_scale"), 4),
        (("writers", 0, "stored_value", "semantics"), "integer"),
        (("writers", 0, "stored_value", "input", "offset"), 4),
        (("writers", 0, "stored_value", "input", "bytes"), 1),
        (("writers", 0, "instances", 0, "address"), 1),
        (("writers", 1, "stored_value", "bytes", 0), 0),
        (("witnesses", 0, "last_write_cases", 0,
          "minimum_outer_bound"), 2),
        (("witnesses", 0, "last_write_cases", 0,
          "minimum_inner_bound"), 2),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "constant"), 18),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "low_bit"), 0),
        (("witnesses", 1, "last_write_cases", 0,
          "value_byte_expression", "input_scale"), 4),
    ):
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
    for (
        object_bytes, outer_step, inner_step, outer_bits, inner_bits,
        address_outer, address_inner, coefficients, endianness,
    ) in itertools.product(
        (8, 12), (0, 1, 2), (0, 1, 2), (1, 2), (1, 2),
        (2, 4), (1, 2), ((17, 5, 7, 3), (0, 0, 0, 0)),
        ("little", "big"),
    ):
        case = AffineSymbolicValueCase(
            object_bytes, outer_step, inner_step, outer_bits, inner_bits,
            address_outer, address_inner, 16, *coefficients, 2,
            tuple(range(max(1, object_bytes - 1))), -86, endianness,
        )
        cases += 1
        if not mathematically_supported(case):
            rejected += 1
            continue
        accepted += 1
        certificate = make_certificate(case)
        input_values = (0, 1, 0x7FFF, 0xFFFF)
        for outer_count in range(_bound_maximum(outer_bits) + 1):
            for inner_count in range(_bound_maximum(inner_bits) + 1):
                for input_value in input_values:
                    concrete = runtime_loads(
                        case, outer_count=outer_count,
                        inner_count=inner_count, input_value=input_value,
                    )
                    summary = summary_loads(
                        certificate, outer_count=outer_count,
                        inner_count=inner_count, input_value=input_value,
                    )
                    if concrete != summary:
                        raise AssertionError(
                            "affine symbolic value summary diverged from runtime"
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
        "schema": "symcc-nested-loop-memoryphi-affine-symbolic-value-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_affine_symbolic_summaries": accepted,
        "rejected_unsupported_or_partial": rejected,
        "runtime_load_vector_equivalences": vector_equivalences,
        "runtime_value_byte_equivalences": byte_equivalences,
        "runtime_complete_loads": complete,
        "runtime_uninitialized_or_partial_loads": partial,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite two-level i16 affine-bitvector reference equivalence only; "
            "not general recurrence solving, LLVM construction latency, "
            "coverage, solver speed, defect yield, or end-to-end speedup"
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
