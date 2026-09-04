#!/usr/bin/env python3
"""Independent finite last-write value-summary oracle for F417."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from typing import Any, Mapping

from check_nested_loop_memoryphi_summary_oracles import (
    NestedSummaryCase,
    make_certificate as make_initialization_certificate,
    mathematically_supported as initialization_supported,
)


SCHEMA = "symcc-loop-memoryphi-byte-lane-induction-v7"


@dataclass(frozen=True)
class ValueSummaryCase:
    initialization: NestedSummaryCase
    values: tuple[int, ...]
    endianness: str


def _stored_bytes(width: int, operand: int, endianness: str) -> tuple[int, ...]:
    bits = width * 8
    unsigned = operand & ((1 << bits) - 1)
    order = range(width) if endianness == "little" else range(width - 1, -1, -1)
    return tuple((unsigned >> (8 * index)) & 0xFF for index in order)


def mathematically_supported(case: ValueSummaryCase) -> bool:
    writers = case.initialization.writers
    return (
        initialization_supported(case.initialization)
        and case.endianness in {"little", "big"}
        and len(case.values) == len(writers)
        and all(
            type(value) is int
            and -(1 << (width * 8 - 1))
            <= value
            < (1 << (width * 8 - 1))
            for width, value in zip(writers, case.values)
        )
    )


def make_certificate(case: ValueSummaryCase) -> dict[str, Any]:
    certificate = make_initialization_certificate(case.initialization)
    certificate["schema"] = SCHEMA
    certificate["summary"]["value_semantics"] = {
        "kind": "constant-byte-last-write",
        "endianness": case.endianness,
        "outer_activation": "outer-bound-positive",
        "case_order": "descending-inner-induction-then-writer-ordinal",
        "case_predicate": "inner-bound-at-least-minimum",
        "selection": "first-match",
        "fallback": "uninitialized",
    }
    value_bytes: list[tuple[int, ...]] = []
    for writer, operand in zip(
        certificate["summary"]["writers"], case.values
    ):
        stored = _stored_bytes(writer["bytes"], operand, case.endianness)
        value_bytes.append(stored)
        writer["stored_value"] = {
            "kind": "constant-integer",
            "bits": writer["bytes"] * 8,
            "operand": operand,
            "bytes": list(stored),
        }
    for witness in certificate["witnesses"]:
        alternatives = witness.pop("alternatives")
        alternatives.sort(
            key=lambda item: (
                item["inner_induction_value"], item["writer"]
            ),
            reverse=True,
        )
        witness["last_write_cases"] = [
            {
                **item,
                "minimum_inner_bound": item["inner_induction_value"] + 1,
                "value_byte": value_bytes[item["writer"]][
                    item["writer_lane"]
                ],
            }
            for item in alternatives
        ]
    return certificate


def validate_certificate(
    raw: Mapping[str, Any], case: ValueSummaryCase
) -> bool:
    return mathematically_supported(case) and raw == make_certificate(case)


def runtime_loads(
    case: ValueSummaryCase,
    *,
    outer_count: int,
    inner_count: int,
) -> tuple[tuple[int, ...] | None, ...]:
    source = case.initialization
    memory: dict[int, int] = {}
    bytes_by_writer = [
        _stored_bytes(width, value, case.endianness)
        for width, value in zip(source.writers, case.values)
    ]
    for _outer in range(source.outer_seed, outer_count, source.outer_step):
        for induction in range(
            source.inner_seed, inner_count, source.inner_step
        ):
            for writer, width in enumerate(source.writers):
                if induction + width > source.object_bytes:
                    continue
                for lane, value_byte in enumerate(bytes_by_writer[writer]):
                    memory[induction + lane] = value_byte
    return tuple(
        (
            tuple(memory[offset + lane] for lane in range(source.load_bytes))
            if all(
                offset + lane in memory for lane in range(source.load_bytes)
            )
            else None
        )
        for offset in source.load_offsets
    )


def summary_loads(
    certificate: Mapping[str, Any],
    *,
    outer_count: int,
    inner_count: int,
) -> tuple[tuple[int, ...] | None, ...]:
    load_bytes = int(certificate["load_bytes"])
    witnesses = certificate["witnesses"]
    by_load: dict[int, dict[int, int]] = {}
    if outer_count > 0:
        for witness in witnesses:
            selected = next(
                (
                    item
                    for item in witness["last_write_cases"]
                    if inner_count >= item["minimum_inner_bound"]
                ),
                None,
            )
            if selected is not None:
                by_load.setdefault(witness["load_address"], {})[
                    witness["lane"]
                ] = selected["value_byte"]
    return tuple(
        (
            tuple(by_load.get(address, {})[lane] for lane in range(load_bytes))
            if all(lane in by_load.get(address, {}) for lane in range(load_bytes))
            else None
        )
        for address in certificate["load_addresses"]
    )


def mutation_oracle() -> int:
    initialization = NestedSummaryCase(
        8, 0, 1, 0, 2, 8, 64, 2, tuple(range(7)), (1, 2)
    )
    case = ValueSummaryCase(initialization, (-86, 4660), "little")
    baseline = make_certificate(case)
    mutations: list[dict[str, Any]] = []
    for mutate in (
        lambda item: item.__setitem__("schema", "bad"),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "kind", "symbolic-last-write"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "endianness", "mixed"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "outer_activation", "always"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "case_order", "ascending"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "case_predicate", "inner-bound-greater-than-minimum"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "selection", "last-match"
        ),
        lambda item: item["summary"]["value_semantics"].__setitem__(
            "fallback", "zero"
        ),
        lambda item: item["summary"]["writers"][0][
            "stored_value"
        ].__setitem__("kind", "constant-byte-vector"),
        lambda item: item["summary"]["writers"][0][
            "stored_value"
        ].__setitem__("bits", 16),
        lambda item: item["summary"]["writers"][0][
            "stored_value"
        ].__setitem__("operand", -85),
        lambda item: item["summary"]["writers"][0][
            "stored_value"
        ]["bytes"].__setitem__(0, 171),
        lambda item: item["witnesses"][0]["last_write_cases"].reverse(),
        lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
            "writer", 0
        ),
        lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
            "minimum_inner_bound", 2
        ),
        lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
            "value_byte", 0
        ),
        lambda item: item["witnesses"][0]["last_write_cases"].pop(),
        lambda item: item["witnesses"].pop(),
    ):
        candidate = copy.deepcopy(baseline)
        mutate(candidate)
        mutations.append(candidate)
    if any(validate_certificate(candidate, case) for candidate in mutations):
        raise AssertionError("a mutated last-write value summary was accepted")
    return len(mutations)


def _writer_shapes(step: int) -> tuple[tuple[tuple[int, int], ...], ...]:
    wide = min(step, 2)
    wide_value = 4660 if wide == 2 else 17
    return (
        ((1, -1),),
        ((wide, wide_value),),
        ((1, -86), (wide, wide_value)),
        ((wide, wide_value), (1, 23)),
    )


def run_oracle() -> dict[str, Any]:
    cases = accepted = rejected = runtime_checks = byte_checks = 0
    complete_loads = incomplete_loads = case_evaluations = 0
    for object_bytes in range(2, 11):
        for outer_step in (1, 2):
            for inner_step in (1, 2, 3):
                for load_bytes in range(1, min(2, object_bytes) + 1):
                    last_load = object_bytes - load_bytes
                    offsets = tuple(range(last_load + 1))
                    for shape in _writer_shapes(inner_step):
                        widths = tuple(width for width, _value in shape)
                        values = tuple(value for _width, value in shape)
                        for endianness in ("little", "big"):
                            for outer_seed in (0, 1):
                                for inner_seed in (0, 1):
                                    initialization = NestedSummaryCase(
                                        object_bytes,
                                        outer_seed,
                                        outer_step,
                                        inner_seed,
                                        inner_step,
                                        8,
                                        16,
                                        load_bytes,
                                        offsets,
                                        widths,
                                    )
                                    case = ValueSummaryCase(
                                        initialization, values, endianness
                                    )
                                    certificate = make_certificate(case)
                                    observed = validate_certificate(
                                        certificate, case
                                    )
                                    expected = mathematically_supported(case)
                                    if observed != expected:
                                        raise AssertionError(
                                            "value-summary oracle disagrees with math"
                                        )
                                    cases += 1
                                    accepted += int(observed)
                                    rejected += int(not observed)
                                    if not observed:
                                        continue
                                    case_evaluations += sum(
                                        len(item["last_write_cases"])
                                        for item in certificate["witnesses"]
                                    )
                                    for outer_count in range(4):
                                        for inner_count in range(
                                            object_bytes + inner_step + 1
                                        ):
                                            runtime = runtime_loads(
                                                case,
                                                outer_count=outer_count,
                                                inner_count=inner_count,
                                            )
                                            summary = summary_loads(
                                                certificate,
                                                outer_count=outer_count,
                                                inner_count=inner_count,
                                            )
                                            if runtime != summary:
                                                raise AssertionError(
                                                    "last-write summary disagrees with runtime"
                                                )
                                            runtime_checks += 1
                                            for loaded in runtime:
                                                complete_loads += int(loaded is not None)
                                                incomplete_loads += int(loaded is None)
                                                byte_checks += (
                                                    0 if loaded is None else len(loaded)
                                                )
    return {
        "schema": "symcc-nested-loop-memoryphi-value-summary-oracle-v1",
        "all_passed": True,
        "cases": cases,
        "accepted_value_summaries": accepted,
        "rejected_unsupported_or_partial": rejected,
        "runtime_load_vector_equivalences": runtime_checks,
        "runtime_value_byte_equivalences": byte_checks,
        "runtime_complete_loads": complete_loads,
        "runtime_uninitialized_or_partial_loads": incomplete_loads,
        "sealed_last_write_cases": case_evaluations,
        "mutations_rejected": mutation_oracle(),
        "claim_boundary": (
            "Finite constant-store reference-model value and last-write "
            "equivalence only; not symbolic-value summaries, LLVM build cost, "
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
