#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F411."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_loop_memoryphi_byte_lane_oracles import (
    InductionCase,
    make_certificate,
    validate_certificate,
)


def benchmark(
    *, object_bytes: int = 64, load_bytes: int = 8,
    repeats: int = 11, iterations: int = 1000,
) -> dict[str, object]:
    if (
        not 2 <= object_bytes <= 64
        or not 1 <= load_bytes <= min(8, object_bytes)
        or repeats < 1
        or iterations < 1
    ):
        raise ValueError("benchmark parameters are outside the sealed domain")
    aliases = object_bytes - load_bytes + 1
    case = InductionCase(
        object_bytes, 0, 1, 8, load_bytes, tuple(range(aliases))
    )
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
        "schema": "symcc-loop-memoryphi-byte-lane-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "load_bytes": load_bytes,
            "load_aliases": aliases,
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
            "writer_aliases": object_bytes,
            "load_aliases": aliases,
            "byte_lane_witnesses": aliases * load_bytes,
            "memory_phi_incoming_edges": 2,
        },
        "claim_boundary": (
            "Python reference-validator cost and certificate cardinality "
            "only; not LLVM construction latency, executor throughput, "
            "coverage, solver time, bug yield, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        benchmark(
            object_bytes=args.object_bytes,
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
