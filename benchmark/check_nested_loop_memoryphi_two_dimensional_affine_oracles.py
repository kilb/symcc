#!/usr/bin/env python3
"""Independent finite 2D-affine last-write oracle for F418."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v8"


@dataclass(frozen=True)
class TwoDimensionalCase:
    object_bytes: int
    outer_step: int
    inner_step: int
    outer_bound_bits: int
    inner_bound_bits: int
    affine_constant: int
    affine_outer_scale: int
    affine_inner_scale: int
    pointer_scale: int
    load_bytes: int
    load_offsets: tuple[int, ...]
    writer_widths: tuple[int, ...]
    writer_values: tuple[int, ...]
    endianness: str


def _stored_bytes(width: int, operand: int, endianness: str) -> tuple[int, ...]:
    unsigned = operand & ((1 << (width * 8)) - 1)
    order = range(width) if endianness == "little" else range(width - 1, -1, -1)
    return tuple((unsigned >> (8 * lane)) & 0xFF for lane in order)


def _bound_maximum(bits: int) -> int:
    return (1 << bits) - 1 if type(bits) is int and 1 <= bits < 64 else 0


def _instances(case: TwoDimensionalCase) -> tuple[dict[str, int], ...]:
    return tuple(
        {
            "outer_induction_value": outer,
            "inner_induction_value": inner,
            "index_value": (
                case.affine_constant
                + case.affine_outer_scale * outer
                + case.affine_inner_scale * inner
            ),
            "address": case.pointer_scale * (
                case.affine_constant
                + case.affine_outer_scale * outer
                + case.affine_inner_scale * inner
            ),
        }
        for outer in range(0, _bound_maximum(case.outer_bound_bits), case.outer_step)
        for inner in range(0, _bound_maximum(case.inner_bound_bits), case.inner_step)
    ) if case.outer_step > 0 and case.inner_step > 0 else ()


def mathematically_supported(case: TwoDimensionalCase) -> bool:
    instances = _instances(case)
    covered = {
        instance["address"] + lane
        for instance in instances
        for width in case.writer_widths
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
        and len(range(0, _bound_maximum(case.outer_bound_bits), case.outer_step)) <= 64
        and len(range(0, _bound_maximum(case.inner_bound_bits), case.inner_step)) <= 64
        and type(case.affine_constant) is int
        and 0 <= case.affine_constant <= (1 << 63) - 1
        and type(case.affine_outer_scale) is int
        and 1 <= case.affine_outer_scale <= (1 << 63) - 1
        and type(case.affine_inner_scale) is int
        and 1 <= case.affine_inner_scale <= (1 << 63) - 1
        and type(case.pointer_scale) is int
        and case.pointer_scale > 0
        and 1 <= len(case.writer_widths) <= 4
        and len(case.writer_values) == len(case.writer_widths)
        and all(
            type(width) is int
            and 1 <= width <= min(
                8,
                case.object_bytes,
                case.affine_inner_scale * case.pointer_scale * case.inner_step,
            )
            for width in case.writer_widths
        )
        and all(
            type(value) is int
            and -(1 << (width * 8 - 1)) <= value < (1 << (width * 8 - 1))
            for width, value in zip(case.writer_widths, case.writer_values)
        )
        and case.endianness in {"little", "big"}
        and all(
            0 <= instance["index_value"] <= (1 << 63) - 1
            and instance["address"] >= 0
            and instance["address"] + width <= case.object_bytes
            for instance in instances
            for width in case.writer_widths
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


def make_certificate(case: TwoDimensionalCase) -> dict[str, Any]:
    instances = _instances(case)
    writers: list[dict[str, Any]] = []
    alternatives: dict[int, list[dict[str, int]]] = {}
    for writer, (width, operand) in enumerate(
        zip(case.writer_widths, case.writer_values)
    ):
        value_bytes = _stored_bytes(width, operand, case.endianness)
        writers.append({
            "ordinal": writer,
            "bytes": width,
            "pointer_scale": case.pointer_scale,
            "affine_index": {
                "constant": case.affine_constant,
                "outer_scale": case.affine_outer_scale,
                "inner_scale": case.affine_inner_scale,
            },
            "instances": [dict(instance) for instance in instances],
            "stored_value": {
                "bits": width * 8,
                "operand": operand,
                "bytes": list(value_bytes),
            },
        })
        for instance in instances:
            for lane in range(width):
                alternatives.setdefault(instance["address"] + lane, []).append({
                    "writer": writer,
                    "writer_address": instance["address"],
                    "writer_lane": lane,
                    "outer_induction_value": instance["outer_induction_value"],
                    "inner_induction_value": instance["inner_induction_value"],
                    "minimum_outer_bound": instance["outer_induction_value"] + 1,
                    "minimum_inner_bound": instance["inner_induction_value"] + 1,
                    "value_byte": value_bytes[lane],
                })
    pairs = sorted({
        (item["outer_induction_value"], item["inner_induction_value"])
        for item in instances
    })
    closed: set[int] = set()
    rounds: list[dict[str, Any]] = []
    for outer, inner in pairs:
        before = len(closed)
        for instance in instances:
            if (
                instance["outer_induction_value"] == outer
                and instance["inner_induction_value"] == inner
            ):
                for width in case.writer_widths:
                    closed.update(range(instance["address"], instance["address"] + width))
        rounds.append({
            "outer_induction_value": outer,
            "inner_induction_value": inner,
            "new_lanes": len(closed) - before,
            "total_lanes": len(closed),
        })
    rounds.append({"kind": "stability-check", "new_lanes": 0, "total_lanes": len(closed)})
    return {
        "schema": SCHEMA,
        "value_semantics": {
            "case_order": "descending-outer-then-inner-induction-then-writer-ordinal",
            "case_predicate": "outer-and-inner-bounds-at-least-minima",
            "selection": "first-match",
            "fallback": "uninitialized",
            "endianness": case.endianness,
        },
        "writers": writers,
        "fixed_point": {
            "domain": [
                {"outer_induction_value": outer, "inner_induction_value": inner}
                for outer, inner in pairs
            ],
            "rounds": rounds,
            "final_lanes": sorted(closed),
            "stable": True,
        },
        "load_addresses": list(case.load_offsets),
        "load_bytes": case.load_bytes,
        "witnesses": [
            {
                "load_address": offset,
                "lane": lane,
                "last_write_cases": sorted(
                    alternatives.get(offset + lane, []),
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
        ],
    }


def validate_certificate(raw: Mapping[str, Any], case: TwoDimensionalCase) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_loads(
    case: TwoDimensionalCase, *, outer_count: int, inner_count: int
) -> tuple[tuple[int, ...] | None, ...]:
    memory: dict[int, int] = {}
    values = [
        _stored_bytes(width, operand, case.endianness)
        for width, operand in zip(case.writer_widths, case.writer_values)
    ]
    for outer in range(0, outer_count, case.outer_step):
        for inner in range(0, inner_count, case.inner_step):
            index = (
                case.affine_constant
                + case.affine_outer_scale * outer
                + case.affine_inner_scale * inner
            )
            address = case.pointer_scale * index
            for width, value_bytes in zip(case.writer_widths, values):
                if address < 0 or address + width > case.object_bytes:
                    continue
                for lane, value_byte in enumerate(value_bytes):
                    memory[address + lane] = value_byte
    return tuple(
        (
            tuple(memory[offset + lane] for lane in range(case.load_bytes))
            if all(offset + lane in memory for lane in range(case.load_bytes))
            else None
        )
        for offset in case.load_offsets
    )


def summary_loads(
    certificate: Mapping[str, Any], *, outer_count: int, inner_count: int
) -> tuple[tuple[int, ...] | None, ...]:
    selected: dict[int, dict[int, int]] = {}
    for witness in certificate["witnesses"]:
        case = next((
            item for item in witness["last_write_cases"]
            if outer_count >= item["minimum_outer_bound"]
            and inner_count >= item["minimum_inner_bound"]
        ), None)
        if case is not None:
            selected.setdefault(witness["load_address"], {})[
                witness["lane"]
            ] = case["value_byte"]
    width = int(certificate["load_bytes"])
    return tuple(
        (
            tuple(selected.get(address, {})[lane] for lane in range(width))
            if all(lane in selected.get(address, {}) for lane in range(width))
            else None
        )
        for address in certificate["load_addresses"]
    )


def mutation_oracle() -> int:
    case = TwoDimensionalCase(
        12, 1, 2, 2, 2, 0, 4, 1, 1, 2, tuple(range(11)),
        (2, 1), (4660, -86), "little",
    )
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item["value_semantics"].__setitem__("case_order", "ascending"),
        lambda item: item["value_semantics"].__setitem__("case_predicate", "or"),
        lambda item: item["value_semantics"].__setitem__("selection", "last-match"),
        lambda item: item["value_semantics"].__setitem__("fallback", "zero"),
        lambda item: item["writers"][0].__setitem__("pointer_scale", 2),
        lambda item: item["writers"][0]["affine_index"].__setitem__("constant", 1),
        lambda item: item["writers"][0]["affine_index"].__setitem__("outer_scale", 5),
        lambda item: item["writers"][0]["affine_index"].__setitem__("inner_scale", 2),
        lambda item: item["writers"][0]["instances"][0].__setitem__("address", 1),
        lambda item: item["writers"][0]["instances"][0].__setitem__("index_value", 1),
        lambda item: item["writers"][0]["stored_value"]["bytes"].__setitem__(0, 0),
        lambda item: item["fixed_point"]["domain"].pop(),
        lambda item: item["fixed_point"]["rounds"][0].__setitem__("new_lanes", 0),
        lambda item: item["witnesses"][0]["last_write_cases"].reverse(),
        lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__("minimum_outer_bound", 2),
        lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__("minimum_inner_bound", 2),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated 2D-affine certificate was accepted")
    return len(mutations)


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = vector_checks = byte_checks = 0
    complete = incomplete = sealed_cases = 0
    for object_bytes in (8, 12, 16, 24):
        for outer_bits in (1, 2):
            for inner_bits in (1, 2, 3):
                for outer_step in (1, 2):
                    for inner_step in (1, 2):
                        for outer_scale in (2, 4, 8):
                            for inner_scale in (1, 2):
                                for constant in (0, 1):
                                    for widths, values in (((1,), (17,)), ((2, 1), (4660, -86))):
                                        for endianness in ("little", "big"):
                                            load_bytes = min(2, object_bytes)
                                            case = TwoDimensionalCase(
                                                object_bytes, outer_step, inner_step,
                                                outer_bits, inner_bits, constant,
                                                outer_scale, inner_scale, 1,
                                                load_bytes,
                                                tuple(range(object_bytes - load_bytes + 1)),
                                                widths, values, endianness,
                                            )
                                            certificate = make_certificate(case)
                                            observed = validate_certificate(certificate, case)
                                            expected = mathematically_supported(case)
                                            if observed != expected:
                                                raise AssertionError("2D oracle disagrees with math")
                                            cases += 1
                                            accepted += int(observed)
                                            rejected += int(not observed)
                                            if not observed:
                                                continue
                                            sealed_cases += sum(
                                                len(item["last_write_cases"])
                                                for item in certificate["witnesses"]
                                            )
                                            for outer_count in range(_bound_maximum(outer_bits) + 1):
                                                for inner_count in range(_bound_maximum(inner_bits) + 1):
                                                    runtime = runtime_loads(
                                                        case, outer_count=outer_count,
                                                        inner_count=inner_count,
                                                    )
                                                    summary = summary_loads(
                                                        certificate, outer_count=outer_count,
                                                        inner_count=inner_count,
                                                    )
                                                    if runtime != summary:
                                                        raise AssertionError(
                                                            "2D summary disagrees with runtime"
                                                        )
                                                    vector_checks += 1
                                                    for loaded in runtime:
                                                        complete += int(loaded is not None)
                                                        incomplete += int(loaded is None)
                                                        byte_checks += 0 if loaded is None else len(loaded)
    return {
        "schema": "symcc-nested-loop-memoryphi-two-dimensional-affine-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_two_dimensional_summaries": accepted,
        "rejected_unsupported_or_partial": rejected,
        "runtime_load_vector_equivalences": vector_checks,
        "runtime_value_byte_equivalences": byte_checks,
        "runtime_complete_loads": complete,
        "runtime_uninitialized_or_partial_loads": incomplete,
        "sealed_last_write_cases": sealed_cases,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite two-level affine constant-store reference equivalence only; "
            "not general polyhedral analysis, LLVM construction latency, "
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
