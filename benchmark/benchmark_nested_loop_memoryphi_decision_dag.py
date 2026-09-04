#!/usr/bin/env python3
"""Reference Decision DAG specialization benchmark for F421."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_nested_loop_memoryphi_decision_dag_oracles import (
    DecisionDagCase,
    Guard,
    selected_arm,
    summary_memory,
    supported,
)


def benchmark(*, repeats: int = 9, iterations: int = 500) -> dict[str, object]:
    if repeats < 1 or iterations < 1:
        raise ValueError("benchmark repetitions must be positive")
    case = DecisionDagCase(
        Guard("ult", "inner", 2),
        Guard("ule", "outer", 1),
        shared_leaf=True,
    )
    if not supported(case):
        raise AssertionError("reference Decision DAG is outside the sealed domain")
    validation_timings: list[int] = []
    selection_timings: list[int] = []
    summary_timings: list[int] = []
    checksum = 0
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _iteration in range(iterations):
            if not supported(case):
                raise AssertionError("reference Decision DAG validation changed")
        validation_timings.append(time.perf_counter_ns() - start)

        start = time.perf_counter_ns()
        for iteration in range(iterations):
            outer = iteration % 3
            inner = (iteration % 2) * 2
            arm = selected_arm(case, outer, inner)
            checksum ^= arm.evaluate(outer, inner, iteration, case.value_bits)
        selection_timings.append(time.perf_counter_ns() - start)

        start = time.perf_counter_ns()
        for iteration in range(iterations):
            memory, initialized = summary_memory(
                case,
                outer_count=iteration % 4,
                inner_count=(iteration // 4) % 4,
                payload=iteration & 0xFFFF,
            )
            checksum ^= sum(memory) + sum(initialized)
        summary_timings.append(time.perf_counter_ns() - start)

    def timing(values: list[int]) -> dict[str, int]:
        return {
            "minimum_ns": min(values),
            "median_ns": int(statistics.median(values)),
            "maximum_ns": max(values),
        }

    return {
        "schema": "symcc-nested-loop-memoryphi-decision-dag-benchmark-v1",
        "all_passed": True,
        "parameters": {
            "repeats": repeats,
            "iterations_per_repeat": iterations,
            "dag_nodes": 4,
            "guard_nodes": 2,
            "affine_leaves": 2,
            "shared_child_edges": 2,
            "maximum_depth": 2,
        },
        "reference_validation_batch_cost": timing(validation_timings),
        "reference_guard_selection_batch_cost": timing(selection_timings),
        "reference_last_write_summary_batch_cost": timing(summary_timings),
        "median_validation_ns_per_dag": (
            int(statistics.median(validation_timings)) // iterations
        ),
        "median_guard_selection_ns_per_instance": (
            int(statistics.median(selection_timings)) // iterations
        ),
        "median_last_write_summary_ns_per_query": (
            int(statistics.median(summary_timings)) // iterations
        ),
        "checksum": checksum,
        "claim_boundary": (
            "Python finite reference validation, guard specialization and "
            "last-write reconstruction cost only; not LLVM lowering latency, "
            "executor throughput, coverage, solver time, defect yield, or speedup"
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
