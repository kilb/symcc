#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F413."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_conditional_loop_memoryphi_byte_lane_oracles import (
    ConditionalInductionCase,
    make_certificate,
    validate_certificate,
)
from check_strided_loop_memoryphi_byte_lane_oracles import (
    reachable_writers,
    writer_aliases,
)


def benchmark(
    *, object_bytes: int = 64, stride: int = 8,
    writer_bytes: int = 8, load_bytes: int = 8,
    repeats: int = 11, iterations: int = 1000,
) -> dict[str, object]:
    if (
        not 2 <= object_bytes <= 64
        or not 1 <= stride <= 64
        or not 1 <= writer_bytes <= min(8, stride, object_bytes)
        or not 1 <= load_bytes <= min(8, object_bytes)
        or repeats < 1
        or iterations < 1
    ):
        raise ValueError("benchmark parameters are outside the sealed domain")
    aliases = object_bytes - load_bytes + 1
    case = ConditionalInductionCase(
        object_bytes, 0, stride, 1, writer_bytes, 8, 64,
        load_bytes, tuple(range(aliases)), "ult", object_bytes, True,
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
    affine = case.affine()
    reachable = reachable_writers(affine)
    return {
        "schema": "symcc-conditional-loop-memoryphi-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "stride": stride,
            "writer_bytes": writer_bytes,
            "load_bytes": load_bytes,
            "load_aliases": aliases,
            "guard_predicate": "ult",
            "writer_when": True,
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
            "writer_aliases": len(writer_aliases(affine)),
            "reachable_writers": len(reachable),
            "potential_writer_bytes": len(reachable) * writer_bytes,
            "load_aliases": aliases,
            "byte_lane_witnesses": aliases * load_bytes,
            "guarded_byte_lane_witnesses": aliases * load_bytes,
            "memory_phi_nodes": 2,
            "memory_phi_incoming_edges": 4,
        },
        "claim_boundary": (
            "Python reference-validator cost and guarded certificate "
            "cardinality only; not LLVM construction latency, executor "
            "throughput, coverage, solver time, bug yield, or end-to-end "
            "speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object-bytes", type=int, default=64)
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--writer-bytes", type=int, default=8)
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(benchmark(
        object_bytes=args.object_bytes,
        stride=args.stride,
        writer_bytes=args.writer_bytes,
        load_bytes=args.load_bytes,
        repeats=args.repeats,
        iterations=args.iterations,
    ), sort_keys=True, separators=(",", ":"))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
