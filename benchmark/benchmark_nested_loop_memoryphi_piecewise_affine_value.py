#!/usr/bin/env python3
"""Reference validation and guard-selection benchmark for F420."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_nested_loop_memoryphi_piecewise_affine_value_oracles import (
    AffineArm,
    PiecewiseAffineValueCase,
    make_certificate,
    summary_loads,
    validate_certificate,
)


def benchmark(*, repeats: int = 9, iterations: int = 500) -> dict[str, object]:
    if repeats < 1 or iterations < 1:
        raise ValueError("benchmark repetitions must be positive")
    case = PiecewiseAffineValueCase(
        24, 1, 2, 2, 3, 8, 1, 16, "ult", "inner", 2, False,
        AffineArm(17, 5, 7, 3), AffineArm(1025, 2, 11, 3),
        2, tuple(range(23)), -86, "little",
    )
    certificate = make_certificate(case)
    if not validate_certificate(certificate, case):
        raise ValueError("benchmark case is outside the sealed F420 domain")
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
                input_value=(iteration * 257) & 0xFFFF,
            )
        selection_timings.append(time.perf_counter_ns() - start)
    validation_median = int(statistics.median(validation_timings))
    selection_median = int(statistics.median(selection_timings))
    witnesses = certificate["witnesses"]
    last_write_cases = sum(
        len(item["last_write_cases"]) for item in witnesses
    )
    guarded_expressions = sum(
        case_item["value_byte_expression"]["kind"]
        == "guard-specialized-byte"
        for witness in witnesses
        for case_item in witness["last_write_cases"]
    )
    selected_true = sum(
        case_item["value_byte_expression"].get("selected_arm") == "true"
        for witness in witnesses
        for case_item in witness["last_write_cases"]
    )
    selected_false = sum(
        case_item["value_byte_expression"].get("selected_arm") == "false"
        for witness in witnesses
        for case_item in witness["last_write_cases"]
    )
    return {
        "schema": "symcc-nested-loop-memoryphi-piecewise-affine-value-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": case.object_bytes,
            "outer_bound_bits": case.outer_bound_bits,
            "inner_bound_bits": case.inner_bound_bits,
            "value_bits": case.value_bits,
            "predicate": case.predicate,
            "guard_induction": case.guard_induction,
            "guard_constant": case.guard_constant,
            "constant_on_left": case.constant_on_left,
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
            "writer_instance_records": sum(
                len(writer["instances"]) for writer in certificate["writers"]
            ),
            "byte_lane_witnesses": len(witnesses),
            "last_write_cases": last_write_cases,
            "guard_specialized_byte_expressions": guarded_expressions,
            "true_arm_expressions": selected_true,
            "false_arm_expressions": selected_false,
            "maximum_cases_per_lane": max(
                len(item["last_write_cases"]) for item in witnesses
            ),
        },
        "claim_boundary": (
            "Python reference certificate reconstruction and finite guard "
            "selection cost only; not LLVM construction latency, executor "
            "throughput, coverage, solver time, defect yield, or speedup"
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
