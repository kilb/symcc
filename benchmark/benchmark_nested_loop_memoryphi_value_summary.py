#!/usr/bin/env python3
"""Reference-validator and first-match mechanism benchmark for F417."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_nested_loop_memoryphi_summary_oracles import NestedSummaryCase
from check_nested_loop_memoryphi_value_summary_oracles import (
    ValueSummaryCase,
    make_certificate,
    summary_loads,
    validate_certificate,
)


def benchmark(
    *,
    object_bytes: int = 64,
    inner_step: int = 8,
    writer_widths: tuple[int, ...] = (1, 2, 4, 8),
    writer_values: tuple[int, ...] = (17, 4660, 305419896, 72623859790382856),
    load_bytes: int = 8,
    repeats: int = 11,
    iterations: int = 1000,
) -> dict[str, object]:
    if (
        not 2 <= object_bytes <= 64
        or not 1 <= inner_step <= 64
        or not 1 <= len(writer_widths) <= 4
        or len(writer_values) != len(writer_widths)
        or any(
            not 1 <= width <= min(8, inner_step, object_bytes)
            for width in writer_widths
        )
        or any(
            not -(1 << (width * 8 - 1))
            <= value
            < (1 << (width * 8 - 1))
            for width, value in zip(writer_widths, writer_values)
        )
        or not 1 <= load_bytes <= min(8, object_bytes)
        or repeats < 1
        or iterations < 1
    ):
        raise ValueError("benchmark parameters are outside the sealed domain")
    load_aliases = object_bytes - load_bytes + 1
    initialization = NestedSummaryCase(
        object_bytes,
        0,
        1,
        0,
        inner_step,
        8,
        64,
        load_bytes,
        tuple(range(load_aliases)),
        writer_widths,
    )
    case = ValueSummaryCase(initialization, writer_values, "little")
    certificate = make_certificate(case)
    if not validate_certificate(certificate, case):
        raise ValueError("benchmark shape lacks complete value-summary cover")
    validation_timings: list[int] = []
    selection_timings: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            if not validate_certificate(certificate, case):
                raise AssertionError("reference certificate was rejected")
        validation_timings.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        for iteration in range(iterations):
            summary_loads(
                certificate,
                outer_count=1,
                inner_count=(iteration % object_bytes) + 1,
            )
        selection_timings.append(time.perf_counter_ns() - start)
    validation_median = int(statistics.median(validation_timings))
    selection_median = int(statistics.median(selection_timings))
    witnesses = certificate["witnesses"]
    cases = sum(len(item["last_write_cases"]) for item in witnesses)
    return {
        "schema": "symcc-nested-loop-memoryphi-value-summary-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "inner_step": inner_step,
            "writer_widths": list(writer_widths),
            "writer_values": list(writer_values),
            "load_bytes": load_bytes,
            "load_aliases": load_aliases,
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_validation_batch_cost": {
            "minimum_ns": min(validation_timings),
            "median_ns": validation_median,
            "maximum_ns": max(validation_timings),
        },
        "reference_selection_batch_cost": {
            "minimum_ns": min(selection_timings),
            "median_ns": selection_median,
            "maximum_ns": max(selection_timings),
        },
        "median_validation_ns_per_certificate": (
            validation_median // iterations
        ),
        "median_selection_ns_per_query": selection_median // iterations,
        "analytic_certificate_cardinality": {
            "writer_value_records": len(writer_widths),
            "stored_value_bytes": sum(writer_widths),
            "byte_lane_witnesses": len(witnesses),
            "last_write_cases": cases,
            "maximum_cases_per_lane": max(
                len(item["last_write_cases"]) for item in witnesses
            ),
        },
        "claim_boundary": (
            "Python reference validation and first-match selection cost only; "
            "not LLVM construction latency, executor throughput, coverage, "
            "solver time, defect yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--inner-step", type=int, default=8)
    parser.add_argument("--writer-widths", default="1,2,4,8")
    parser.add_argument(
        "--writer-values", default="17,4660,305419896,72623859790382856"
    )
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        benchmark(
            object_bytes=args.object_bytes,
            inner_step=args.inner_step,
            writer_widths=tuple(
                int(value) for value in args.writer_widths.split(",")
            ),
            writer_values=tuple(
                int(value) for value in args.writer_values.split(",")
            ),
            load_bytes=args.load_bytes,
            repeats=args.repeats,
            iterations=args.iterations,
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
