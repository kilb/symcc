#!/usr/bin/env python3
"""Measure native UCSan explicit-object admission and checker outcomes."""

from __future__ import annotations

import argparse
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


EXPECTED = {
    0: None,
    1: "out-of-bounds access",
    2: "out-of-bounds access",
    3: "use-after-free",
    4: "use-after-free",
    5: "double free",
    6: "deallocation of a stack object",
    7: "out-of-bounds access",
    8: "out-of-bounds access",
    9: "out-of-bounds access",
    10: None,
    11: None,
    12: None,
    13: None,
}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = fraction * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def seed_for_mode(path: Path, mode: int) -> None:
    ucsan_seed.write_seed(
        path,
        [
            ucsan_seed.SeedEntry(
                flags=ucsan_seed.ROOT_ENTRY,
                object_id=0,
                lower=0,
                path=(0,),
                data=mode.to_bytes(4, "little"),
            )
        ],
    )


def run_benchmark(executable: Path, samples: int) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    accepted = 0
    with tempfile.TemporaryDirectory(prefix="symcc-ucsan-explicit-") as directory:
        root = Path(directory)
        for mode, expected in EXPECTED.items():
            seed = root / f"mode-{mode}.ucsan"
            seed_for_mode(seed, mode)
            timings: list[float] = []
            matched = 0
            returncodes: set[int] = set()
            for _ in range(samples):
                environment = os.environ.copy()
                environment.update(
                    {
                        "SYMCC_UCSAN_INPUT": str(seed),
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
                timings.append((time.perf_counter_ns() - started) / 1_000_000)
                returncodes.add(completed.returncode)
                stderr = completed.stderr.decode("utf-8", errors="replace")
                if expected is None:
                    outcome_matches = (
                        completed.returncode == 0
                        and "explicit object violation:" not in stderr
                    )
                else:
                    diagnostic = f"explicit object violation: {expected}"
                    outcome_matches = completed.returncode != 0 and diagnostic in stderr
                matched += int(outcome_matches)
            accepted += matched
            rows.append(
                {
                    "mode": mode,
                    "expected": expected or "valid",
                    "samples": samples,
                    "matched": matched,
                    "returncodes": sorted(returncodes),
                    "elapsed_ms": {
                        "median": statistics.median(timings),
                        "p95": percentile(timings, 0.95),
                        "minimum": min(timings),
                        "maximum": max(timings),
                    },
                }
            )
    total = len(EXPECTED) * samples
    return {
        "schema": "symcc-f394-explicit-object-checker-benchmark-v1",
        "samples_per_mode": samples,
        "modes": len(EXPECTED),
        "valid_modes": sum(value is None for value in EXPECTED.values()),
        "violation_modes": sum(value is not None for value in EXPECTED.values()),
        "matched_outcomes": accepted,
        "total_outcomes": total,
        "all_outcomes_matched": accepted == total,
        "rows": rows,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "claim_boundary": (
            "whole-process native checker mechanism outcomes and latency; "
            "not a coverage, vulnerability-yield, solver, or cross-system speedup"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")
    report = run_benchmark(args.executable.resolve(), args.samples)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="ascii")


if __name__ == "__main__":
    main()
