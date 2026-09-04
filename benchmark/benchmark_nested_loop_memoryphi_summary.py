#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F416."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_nested_loop_memoryphi_summary_oracles import (
    NestedSummaryCase,
    make_certificate,
    validate_certificate,
)


def benchmark(
    *,
    object_bytes: int = 64,
    outer_step: int = 1,
    inner_step: int = 8,
    writer_widths: tuple[int, ...] = (1, 2, 4, 8),
    load_bytes: int = 8,
    repeats: int = 11,
    iterations: int = 1000,
) -> dict[str, object]:
    if (
        not 2 <= object_bytes <= 64
        or not 1 <= outer_step <= 64
        or not 1 <= inner_step <= 64
        or not 1 <= len(writer_widths) <= 4
        or any(
            not 1 <= width <= min(8, inner_step, object_bytes)
            for width in writer_widths
        )
        or not 1 <= load_bytes <= min(8, object_bytes)
        or repeats < 1
        or iterations < 1
    ):
        raise ValueError("benchmark parameters are outside the sealed domain")
    load_aliases = object_bytes - load_bytes + 1
    case = NestedSummaryCase(
        object_bytes,
        0,
        outer_step,
        0,
        inner_step,
        8,
        64,
        load_bytes,
        tuple(range(load_aliases)),
        writer_widths,
    )
    certificate = make_certificate(case)
    if not validate_certificate(certificate, case):
        raise ValueError("benchmark shape does not have complete lane cover")
    timings: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            if not validate_certificate(certificate, case):
                raise AssertionError("reference certificate was rejected")
        timings.append(time.perf_counter_ns() - start)
    median_batch = int(statistics.median(timings))
    writers = certificate["summary"]["writers"]
    witnesses = certificate["witnesses"]
    return {
        "schema": "symcc-nested-loop-memoryphi-summary-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "outer_step": outer_step,
            "inner_step": inner_step,
            "writer_widths": list(writer_widths),
            "load_bytes": load_bytes,
            "load_aliases": load_aliases,
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_validation_batch_cost": {
            "minimum_ns": min(timings),
            "median_ns": median_batch,
            "maximum_ns": max(timings),
        },
        "median_ns_per_certificate": median_batch // iterations,
        "analytic_certificate_cardinality": {
            "loop_levels": 2,
            "memory_phi_nodes": 2,
            "writer_metadata_records": len(writers),
            "reachable_writer_instances": sum(
                len(writer["reachable_addresses"]) for writer in writers
            ),
            "potential_writer_bytes": sum(
                len(writer["reachable_addresses"]) * writer["bytes"]
                for writer in writers
            ),
            "load_aliases": load_aliases,
            "byte_lane_witnesses": len(witnesses),
            "witness_alternatives": sum(
                len(witness["alternatives"]) for witness in witnesses
            ),
            "fixed_point_rounds_including_stability": len(
                certificate["summary"]["fixed_point"]["rounds"]
            ),
        },
        "claim_boundary": (
            "Python reference-validator cost and certificate cardinality only; "
            "not LLVM construction latency, executor throughput, coverage, "
            "solver time, defect yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--outer-step", type=int, default=1)
    parser.add_argument("--inner-step", type=int, default=8)
    parser.add_argument("--writer-widths", default="1,2,4,8")
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    widths = tuple(int(value) for value in args.writer_widths.split(","))
    payload = json.dumps(
        benchmark(
            object_bytes=args.object_bytes,
            outer_step=args.outer_step,
            inner_step=args.inner_step,
            writer_widths=widths,
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
