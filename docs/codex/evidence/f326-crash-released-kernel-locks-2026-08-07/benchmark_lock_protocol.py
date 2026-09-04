#!/usr/bin/env python3
"""Compare legacy mkdir churn with the crash-released lock protocol."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import distributed_state as state  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def measure(operation, cycles: int) -> float:
    started = time.perf_counter_ns()
    for _ in range(cycles):
        operation()
    return (time.perf_counter_ns() - started) / cycles


def run(cycles: int, rounds: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="symcc-f326-lock-bench-") as tmp:
        kernel_path = str(Path(tmp) / "record.lock")
        legacy_path = str(Path(tmp) / "record.legacy-lock")

        def kernel() -> None:
            with state._bounded_advisory_lock(
                kernel_path,
                timeout=1.0,
                description="benchmark lock",
            ):
                pass

        def legacy() -> None:
            os.mkdir(legacy_path)
            os.rmdir(legacy_path)

        kernel()
        legacy()
        kernel_ns: list[float] = []
        legacy_ns: list[float] = []
        for round_index in range(rounds):
            operations = (legacy, kernel) if round_index % 2 == 0 \
                else (kernel, legacy)
            for operation in operations:
                sample = measure(operation, cycles)
                (kernel_ns if operation is kernel else legacy_ns).append(sample)

        kernel_median = statistics.median(kernel_ns)
        legacy_median = statistics.median(legacy_ns)
        return {
            "schema": "symcc-f326-lock-protocol-benchmark-v1",
            "scope": (
                "uncontended short-lock primitive only; excludes lease I/O, "
                "DSE, coverage, and solver throughput"
            ),
            "cycles_per_round": cycles,
            "rounds": rounds,
            "legacy_namespace_mutations_per_cycle": 2,
            "kernel_namespace_mutations_per_warm_cycle": 0,
            "stable_lock_file_count": int(Path(kernel_path).is_file()),
            "legacy_median_ns_per_cycle": round(legacy_median, 1),
            "legacy_p95_ns_per_cycle": round(
                percentile(legacy_ns, 0.95), 1),
            "kernel_median_ns_per_cycle": round(kernel_median, 1),
            "kernel_p95_ns_per_cycle": round(
                percentile(kernel_ns, 0.95), 1),
            "median_speedup": round(legacy_median / kernel_median, 3),
            "legacy_samples_ns_per_cycle": [
                round(value, 1) for value in legacy_ns
            ],
            "kernel_samples_ns_per_cycle": [
                round(value, 1) for value in kernel_ns
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=10000)
    parser.add_argument("--rounds", type=int, default=9)
    args = parser.parse_args()
    if args.cycles < 1 or args.rounds < 1:
        parser.error("cycles and rounds must be positive")
    print(json.dumps(run(args.cycles, args.rounds), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
