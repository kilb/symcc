#!/usr/bin/env python3
"""Measure scoped F337 full-delete and bounded-step mechanism costs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from distributed_state import durable_rmtree, durable_rmtree_step  # noqa: E402


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(len(ordered) - 1, lower + 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "minimum_us": min(samples),
        "median_us": statistics.median(samples),
        "mean_us": statistics.fmean(samples),
        "p95_us": _percentile(samples, 0.95),
        "maximum_us": max(samples),
    }


def _make_tree(root: Path, name: str, files: int) -> Path:
    tree = root / name
    tree.mkdir()
    for index in range(files):
        (tree / f"entry-{index:06d}").touch()
    descriptor = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return tree


def _measure_full(root: Path, files: int, sequence: int) -> float:
    tree = _make_tree(root, f"full-{files}-{sequence}", files)
    started = time.perf_counter_ns()
    durable_rmtree(str(tree))
    return (time.perf_counter_ns() - started) / 1000.0


def _measure_step(
    root: Path,
    files: int,
    sequence: int,
    entry_budget: int,
) -> tuple[float, int, bool, str]:
    tree = _make_tree(root, f"step-{files}-{sequence}", files)
    started = time.perf_counter_ns()
    result = durable_rmtree_step(
        str(tree),
        entry_limit=entry_budget,
        time_limit=10.0,
    )
    elapsed_us = (time.perf_counter_ns() - started) / 1000.0
    shutil.rmtree(tree, ignore_errors=True)
    return (
        elapsed_us,
        result.removed_entries,
        result.complete,
        result.stop_reason,
    )


def _measure_default_convergence(
    root: Path,
    files: int,
    sequence: int,
) -> dict[str, object]:
    tree = _make_tree(root, f"converge-{files}-{sequence}", files)
    samples = []
    started = time.perf_counter_ns()
    while tree.exists():
        step_started = time.perf_counter_ns()
        result = durable_rmtree_step(
            str(tree),
            entry_limit=4096,
            time_limit=0.05,
        )
        samples.append({
            "elapsed_us": (time.perf_counter_ns() - step_started) / 1000.0,
            "removed_entries": result.removed_entries,
            "complete": result.complete,
            "stop_reason": result.stop_reason,
        })
    return {
        "total_elapsed_us": (time.perf_counter_ns() - started) / 1000.0,
        "steps": samples,
        "removed_entries": sum(
            int(sample["removed_entries"]) for sample in samples
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--entry-budget", type=int, default=64)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0 or args.entry_budget < 1:
        raise SystemExit("invalid benchmark sample or budget count")

    sizes = (129, 1025, 4097)
    measurements: dict[str, object] = {}
    sequence = 0
    with tempfile.TemporaryDirectory(prefix="symcc-f337-gc-cost-") as tmp:
        root = Path(tmp)
        for files in sizes:
            for _ in range(args.warmups):
                _measure_full(root, files, sequence)
                sequence += 1
                _measure_step(
                    root, files, sequence, args.entry_budget)
                sequence += 1

            full_samples: list[float] = []
            step_samples: list[float] = []
            step_removed: list[int] = []
            step_complete: list[bool] = []
            step_reasons: list[str] = []
            for sample in range(args.samples):
                order = ("full", "step") if sample % 2 == 0 else (
                    "step", "full")
                for mechanism in order:
                    if mechanism == "full":
                        full_samples.append(
                            _measure_full(root, files, sequence))
                    else:
                        elapsed, removed, complete, reason = _measure_step(
                            root,
                            files,
                            sequence,
                            args.entry_budget,
                        )
                        step_samples.append(elapsed)
                        step_removed.append(removed)
                        step_complete.append(complete)
                        step_reasons.append(reason)
                    sequence += 1

            full_summary = _summary(full_samples)
            step_summary = _summary(step_samples)
            measurements[str(files)] = {
                "full_delete": {
                    "samples_us": full_samples,
                    **full_summary,
                },
                "bounded_step": {
                    "samples_us": step_samples,
                    "removed_entries": step_removed,
                    "complete": step_complete,
                    "stop_reasons": step_reasons,
                    **step_summary,
                },
                "comparison": {
                    "bounded_to_full_median_ratio": (
                        step_summary["median_us"] / full_summary["median_us"]
                    ),
                    "full_to_bounded_median_ratio": (
                        full_summary["median_us"] / step_summary["median_us"]
                    ),
                },
            }

        convergence = _measure_default_convergence(
            root, sizes[-1], sequence)

    result = {
        "schema": "symcc-f337-resumable-budgeted-retired-gc-cost-v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "configuration": {
            "file_counts": list(sizes),
            "tree_entries_including_root": [size + 1 for size in sizes],
            "samples_per_mechanism_and_size": args.samples,
            "warmups_per_mechanism_and_size": args.warmups,
            "bounded_step_entry_budget": args.entry_budget,
            "bounded_step_time_limit_seconds": 10.0,
            "interleaved_and_alternating": True,
            "timed_region": (
                "deletion plus required directory fsync only; tree creation "
                "and bounded-step remainder cleanup are excluded"
            ),
        },
        "measurements": measurements,
        "default_budget_convergence_for_4097_files": convergence,
        "proof_boundary": (
            "Local mechanism timing on one overlay filesystem. The fixed-64 "
            "entry comparison isolates startup work capping; complete deletion "
            "still requires repeated steps. The cooperative time limit cannot "
            "preempt one blocking filesystem syscall. This is not a DSE "
            "throughput, solver, coverage, remote-filesystem, or LAVA-M result."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
