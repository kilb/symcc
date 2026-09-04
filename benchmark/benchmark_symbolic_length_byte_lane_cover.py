#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F410."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_symbolic_length_byte_lane_oracles import (
    CoverCase,
    make_certificate,
    validate_certificate,
)


def benchmark(
    *, aliases: int = 64, repeats: int = 11, iterations: int = 1000
) -> dict[str, object]:
    if not 2 <= aliases <= 64 or repeats < 1 or iterations < 1:
        raise ValueError("benchmark parameters are outside the sealed domain")
    case = CoverCase(64, 0, 64, 1, tuple(range(aliases)))
    certificate = make_certificate(case)
    timings: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            if not validate_certificate(certificate, case):
                raise AssertionError("reference certificate was rejected")
        timings.append(time.perf_counter_ns() - start)
    median_batch = int(statistics.median(timings))
    return {
        "schema": "symcc-symbolic-length-byte-lane-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "aliases": aliases,
            "lanes_per_alias": 1,
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_validation_batch_cost": {
            "minimum_ns": min(timings),
            "median_ns": median_batch,
            "maximum_ns": max(timings),
        },
        "median_ns_per_certificate": median_batch // iterations,
        "analytic_bounded_effect_cardinality": {
            "bounded_length_values": 65,
            "conditional_byte_writes": 64,
            "continuation_state_forks": 0,
        },
        "claim_boundary": (
            "Python reference-validator cost and analytic bounded-effect "
            "cardinality only; not LLVM construction latency, executor "
            "throughput, coverage, solver time, bug yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aliases", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        benchmark(
            aliases=args.aliases,
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
