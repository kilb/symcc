#!/usr/bin/env python3
"""Reference-validator mechanism benchmark for F415."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_ordered_multilatch_memoryphi_oracles import (
    OrderedWriterCase,
    make_certificate,
    validate_certificate,
)


def benchmark(
    *,
    object_bytes: int = 64,
    stride: int = 8,
    writer_bytes: int = 8,
    load_bytes: int = 8,
    writers_per_transfer: int = 4,
    repeats: int = 11,
    iterations: int = 1000,
) -> dict[str, object]:
    if (
        not 2 <= object_bytes <= 64
        or not 1 <= stride <= 64
        or not 1 <= writer_bytes <= min(8, stride, object_bytes)
        or not 1 <= load_bytes <= min(8, object_bytes)
        or not 2 <= writers_per_transfer <= 4
        or repeats < 1
        or iterations < 1
    ):
        raise ValueError("benchmark parameters are outside the sealed domain")
    widths = tuple(
        max(1, writer_bytes - ordinal) for ordinal in range(writers_per_transfer)
    )
    load_aliases = object_bytes - load_bytes + 1
    case = OrderedWriterCase(
        object_bytes,
        0,
        stride,
        8,
        64,
        load_bytes,
        tuple(range(load_aliases)),
        (widths, (), tuple(reversed(widths)), ()),
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
    writer_metadata = [
        writer
        for transfer in certificate["transfers"]
        for writer in transfer.get("writers", [])
    ]
    witnesses = certificate["witnesses"]
    return {
        "schema": "symcc-ordered-multilatch-memoryphi-benchmark-v1",
        "all_certificates_valid": True,
        "parameters": {
            "object_bytes": object_bytes,
            "stride": stride,
            "writer_bytes_maximum": writer_bytes,
            "writer_width_sequence": list(widths),
            "writers_per_transfer": writers_per_transfer,
            "load_bytes": load_bytes,
            "load_aliases": load_aliases,
            "transfers": len(case.transfers),
            "writer_transfers": sum(bool(item) for item in case.transfers),
            "ordered_writers": sum(len(item) for item in case.transfers),
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
            "writer_metadata_records": len(writer_metadata),
            "reachable_writer_instances": sum(
                len(writer["reachable_addresses"]) for writer in writer_metadata
            ),
            "potential_writer_bytes": sum(
                len(writer["reachable_addresses"]) * writer["bytes"]
                for writer in writer_metadata
            ),
            "load_aliases": load_aliases,
            "byte_lane_witnesses": len(witnesses),
            "witness_alternatives": sum(
                len(witness["alternatives"]) for witness in witnesses
            ),
            "backedge_transfers": len(case.transfers),
            "memory_phi_incoming_edges": len(case.transfers) + 1,
            "fixed_point_rounds_including_stability": len(
                certificate["fixed_point"]["rounds"]
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
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--writer-bytes", type=int, default=8)
    parser.add_argument("--load-bytes", type=int, default=8)
    parser.add_argument("--writers-per-transfer", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = json.dumps(
        benchmark(
            object_bytes=args.object_bytes,
            stride=args.stride,
            writer_bytes=args.writer_bytes,
            load_bytes=args.load_bytes,
            writers_per_transfer=args.writers_per_transfer,
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
