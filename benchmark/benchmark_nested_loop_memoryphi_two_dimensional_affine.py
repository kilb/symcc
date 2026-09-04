#!/usr/bin/env python3
"""Reference validation and selection benchmark for F418."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_nested_loop_memoryphi_two_dimensional_affine_oracles import (
    TwoDimensionalCase,
    make_certificate,
    summary_loads,
    validate_certificate,
)


def benchmark(*, repeats: int = 9, iterations: int = 500) -> dict[str, object]:
    if repeats < 1 or iterations < 1:
        raise ValueError("benchmark repetitions must be positive")
    case = TwoDimensionalCase(
        24, 1, 2, 2, 3, 0, 8, 1, 1, 2, tuple(range(23)),
        (2, 1), (4660, -86), "little",
    )
    certificate = make_certificate(case)
    if not validate_certificate(certificate, case):
        raise ValueError("benchmark case is outside the sealed F418 domain")
    validation_timings: list[int] = []
    selection_timings: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _iteration in range(iterations):
            if not validate_certificate(certificate, case):
                raise AssertionError("reference certificate was rejected")
        validation_timings.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        for iteration in range(iterations):
            summary_loads(
                certificate,
                outer_count=iteration % 4,
                inner_count=iteration % 8,
            )
        selection_timings.append(time.perf_counter_ns() - start)
    validation_median = int(statistics.median(validation_timings))
    selection_median = int(statistics.median(selection_timings))
    witnesses = certificate["witnesses"]
    last_write_cases = sum(
        len(item["last_write_cases"]) for item in witnesses
    )
    instance_records = sum(
        len(writer["instances"]) for writer in certificate["writers"]
    )
    return {
        "schema": "symcc-nested-loop-memoryphi-two-dimensional-affine-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": case.object_bytes,
            "outer_bound_bits": case.outer_bound_bits,
            "inner_bound_bits": case.inner_bound_bits,
            "outer_step": case.outer_step,
            "inner_step": case.inner_step,
            "affine_outer_scale": case.affine_outer_scale,
            "affine_inner_scale": case.affine_inner_scale,
            "writer_widths": list(case.writer_widths),
            "load_bytes": case.load_bytes,
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
        "median_validation_ns_per_certificate": validation_median // iterations,
        "median_selection_ns_per_query": selection_median // iterations,
        "analytic_certificate_cardinality": {
            "writer_value_records": len(certificate["writers"]),
            "writer_instance_records": instance_records,
            "fixed_point_pairs": len(certificate["fixed_point"]["domain"]),
            "byte_lane_witnesses": len(witnesses),
            "last_write_cases": last_write_cases,
            "maximum_cases_per_lane": max(
                len(item["last_write_cases"]) for item in witnesses
            ),
        },
        "claim_boundary": (
            "Python reference reconstruction and first-match selection cost "
            "only; not LLVM construction latency, executor throughput, "
            "coverage, solver time, defect yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        benchmark(repeats=args.repeats, iterations=args.iterations),
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
