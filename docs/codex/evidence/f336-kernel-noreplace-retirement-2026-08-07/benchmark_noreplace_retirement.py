#!/usr/bin/env python3
"""Measure scoped F336 rename and startup-probe mechanism costs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import statistics
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import distributed_state as state  # noqa: E402
import mpi_concolic_execution as runner  # noqa: E402


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(len(ordered) - 1, lower + 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _measure_rename(root: Path, *, noreplace: bool, index: int) -> float:
    source = root / f"source-{noreplace}-{index}"
    destination = root / f"destination-{noreplace}-{index}"
    source.mkdir()
    started = time.perf_counter_ns()
    if noreplace:
        state.durable_rename_noreplace(str(source), str(destination))
    else:
        state.durable_replace(str(source), str(destination))
    elapsed_us = (time.perf_counter_ns() - started) / 1000.0
    shutil.rmtree(destination)
    return elapsed_us


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "minimum_us": min(samples),
        "median_us": statistics.median(samples),
        "mean_us": statistics.fmean(samples),
        "p95_us": _percentile(samples, 0.95),
        "maximum_us": max(samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        raise SystemExit("samples must be positive and warmups non-negative")

    replace_samples: list[float] = []
    noreplace_samples: list[float] = []
    probe_samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f336-cost-") as tmp:
        root = Path(tmp)
        sequence = 0
        for _ in range(args.warmups):
            _measure_rename(root, noreplace=False, index=sequence)
            sequence += 1
            _measure_rename(root, noreplace=True, index=sequence)
            sequence += 1
            runner._probe_retirement_noreplace(str(root))
        for sample in range(args.samples):
            order = (False, True) if sample % 2 == 0 else (True, False)
            for noreplace in order:
                elapsed = _measure_rename(
                    root, noreplace=noreplace, index=sequence)
                sequence += 1
                (noreplace_samples if noreplace else replace_samples).append(
                    elapsed)
            started = time.perf_counter_ns()
            runner._probe_retirement_noreplace(str(root))
            probe_samples.append((time.perf_counter_ns() - started) / 1000.0)

    replace_summary = _summary(replace_samples)
    noreplace_summary = _summary(noreplace_samples)
    probe_summary = _summary(probe_samples)
    result = {
        "schema": "symcc-f336-kernel-noreplace-cost-v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "renameat2_symbol_available": state._RENAMEAT2 is not None,
        "configuration": {
            "samples_per_mechanism": args.samples,
            "warmups_per_mechanism": args.warmups,
            "interleaved": True,
            "timed_region": (
                "rename syscall plus destination-parent fsync; source creation "
                "and post-sample tree removal are excluded"
            ),
        },
        "replace": {
            "samples_us": replace_samples,
            **replace_summary,
        },
        "noreplace": {
            "samples_us": noreplace_samples,
            **noreplace_summary,
        },
        "startup_probe": {
            "samples_us": probe_samples,
            **probe_summary,
            "timed_region": (
                "owned probe-root creation, success rename, collision "
                "rejection, and durable recursive cleanup"
            ),
        },
        "comparison": {
            "median_overhead_us": (
                noreplace_summary["median_us"] - replace_summary["median_us"]
            ),
            "median_ratio": (
                noreplace_summary["median_us"] / replace_summary["median_us"]
            ),
        },
        "proof_boundary": (
            "Local mechanism timing on one overlay filesystem. It is not a "
            "DSE throughput, solver, coverage, remote-filesystem, or LAVA-M "
            "measurement."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
