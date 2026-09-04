#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F414."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_multilatch_loop_memoryphi_oracles import (
    MultiLatchCase,
    make_certificate,
    validate_certificate,
)
from check_strided_loop_memoryphi_byte_lane_oracles import (
    reachable_writers,
    writer_aliases,
)


def benchmark(
    *,
    object_bytes: int = 64,
    stride: int = 8,
    writer_bytes: int = 8,
    load_bytes: int = 8,
    repeats: int = 11,
    iterations: int = 1000,
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
    load_aliases = object_bytes - load_bytes + 1
    case = MultiLatchCase(
        object_bytes, 0, stride, 1, writer_bytes, 8, 64,
        load_bytes, tuple(range(load_aliases)),
        (True, False, True, False),
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
    aliases = writer_aliases(affine)
    reachable = reachable_writers(affine)
    writer_transfers = sum(case.writer_transfers)
    witness_count = load_aliases * load_bytes
    return {
        "schema": "symcc-multilatch-loop-memoryphi-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "stride": stride,
            "writer_bytes": writer_bytes,
            "load_bytes": load_bytes,
            "load_aliases": load_aliases,
            "transfers": len(case.writer_transfers),
            "writer_transfers": writer_transfers,
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
            "writer_aliases_per_transfer": len(aliases),
            "reachable_writers_per_transfer": len(reachable),
            "potential_writer_bytes": (
                len(reachable) * writer_bytes * writer_transfers
            ),
            "load_aliases": load_aliases,
            "byte_lane_witnesses": witness_count,
            "witness_alternatives": witness_count * writer_transfers,
            "decision_blocks": 3,
            "backedge_transfers": 4,
            "memory_phi_nodes": 1,
            "memory_phi_incoming_edges": 5,
            "fixed_point_rounds_including_stability": len(reachable) + 1,
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
