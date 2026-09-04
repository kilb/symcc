#!/usr/bin/env python3
"""Measure a UCSan native object-graph snapshot/replay mechanism."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "util"))

import ucsan_seed  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = fraction * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def run_benchmark(executable: Path, seed: Path, samples: int) -> dict[str, object]:
    seed_entries = ucsan_seed.read_seed(seed)
    input_bytes = ucsan_seed.serialize_seed(seed_entries)
    timings: list[float] = []
    output_digests: set[str] = set()
    output_sizes: set[int] = set()
    canonical = True
    with tempfile.TemporaryDirectory(prefix="symcc-ucsan-snapshot-") as directory:
        directory_path = Path(directory)
        for sample in range(samples):
            output = directory_path / f"snapshot-{sample}.ucsan"
            environment = os.environ.copy()
            environment.update(
                {
                    "SYMCC_UCSAN_INPUT": str(seed),
                    "SYMCC_UCSAN_DUMP": str(output),
                    "SYMCC_UCSAN_SYMBOLIZE": "0",
                }
            )
            started = time.perf_counter_ns()
            completed = subprocess.run(
                [str(executable)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if completed.returncode != 0:
                raise RuntimeError(
                    f"sample {sample} exited {completed.returncode}: "
                    f"{completed.stderr.decode('utf-8', errors='replace')}"
                )
            output_entries = ucsan_seed.read_seed(output)
            canonical &= output_entries == ucsan_seed.canonicalize(output_entries)
            output_bytes = ucsan_seed.serialize_seed(output_entries)
            output_digests.add(hashlib.sha256(output_bytes).hexdigest())
            output_sizes.add(len(output_bytes))
            timings.append(elapsed_ms)

    return {
        "schema": "symcc-ucsan-snapshot-benchmark-v1",
        "samples": samples,
        "input_bytes": len(input_bytes),
        "output_sizes": sorted(output_sizes),
        "distinct_output_digests": len(output_digests),
        "canonical_outputs": canonical,
        "elapsed_ms": {
            "median": statistics.median(timings),
            "p95": percentile(timings, 0.95),
            "minimum": min(timings),
            "maximum": max(timings),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "claim_boundary": (
            "whole-process native mechanism timing with two durable snapshot "
            "publications per sample; not a coverage, solver, or cross-system speedup"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("seed", type=Path)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")
    report = run_benchmark(args.executable.resolve(), args.seed.resolve(), args.samples)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="ascii")


if __name__ == "__main__":
    main()
