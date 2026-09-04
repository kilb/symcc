#!/usr/bin/env python3
"""Compare F337 materialized discovery with F338 streaming top-k."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_concolic_execution as runner  # noqa: E402


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(len(ordered) - 1, lower + 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "minimum": min(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "p95": _percentile(samples, 0.95),
        "maximum": max(samples),
    }


def _materialized_selection(shared: str, limit: int) -> tuple[str, ...]:
    """Reproduce the F337 tuple/list/full-sort discovery algorithm."""
    entries = tuple(os.scandir(shared))
    candidates: list[str] = []
    for entry in entries:
        parsed = runner._parse_retired_work_state_name(entry.name)
        if parsed is None:
            continue
        if not entry.is_dir(follow_symlinks=False):
            raise ValueError("retired work state is not a real directory")
        candidates.append(entry.name)
    return tuple(sorted(candidates)[:limit])


def _streaming_selection(shared: str, limit: int) -> tuple[str, ...]:
    selected, _, _ = runner._select_retired_work_states(shared, limit)
    return selected


def _time_selection(
    operation,
    shared: str,
    limit: int,
    expected: tuple[str, ...],
) -> float:
    started = time.perf_counter_ns()
    observed = operation(shared, limit)
    elapsed = (time.perf_counter_ns() - started) / 1000.0
    if observed != expected:
        raise AssertionError("selection identity drifted during timing")
    return elapsed


def _peak_traced_bytes(
    operation,
    shared: str,
    limit: int,
    expected: tuple[str, ...],
) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        observed = operation(shared, limit)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if observed != expected:
        raise AssertionError("selection identity drifted during memory trial")
    return peak


def _make_namespace(root: Path, candidates: int, unrelated: int) -> tuple[Path, tuple[str, ...]]:
    shared = root / f"namespace-{candidates}"
    shared.mkdir()
    names = [
        runner._retired_work_state_name(
            f"{index:064x}", f"{index:032x}")
        for index in range(candidates)
    ]
    creation_order = names.copy()
    random.Random(338 + candidates).shuffle(creation_order)
    for name in creation_order:
        (shared / name).mkdir()
    for index in range(unrelated):
        (shared / f"corpus-{index:06d}").touch()
    return shared, tuple(sorted(names))


def _filesystem_type(path: str) -> str:
    result = subprocess.run(
        ["stat", "-f", "-c", "%T", path],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--memory-samples", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if (
        args.samples < 1
        or args.memory_samples < 1
        or args.warmups < 0
        or args.limit < 1
    ):
        raise SystemExit("sample, warmup, and limit values must be valid")

    candidate_counts = (128, 1024, 4096)
    unrelated_entries = 16
    measurements: dict[str, object] = {}
    with tempfile.TemporaryDirectory(
            prefix="symcc-f338-streaming-discovery-") as tmp:
        filesystem = _filesystem_type(tmp)
        root = Path(tmp)
        for candidate_count in candidate_counts:
            shared, ordered_names = _make_namespace(
                root, candidate_count, unrelated_entries)
            expected = ordered_names[:args.limit]

            for _ in range(args.warmups):
                _time_selection(
                    _materialized_selection, str(shared), args.limit, expected)
                _time_selection(
                    _streaming_selection, str(shared), args.limit, expected)

            timing: dict[str, list[float]] = {
                "materialized": [],
                "streaming": [],
            }
            operations = {
                "materialized": _materialized_selection,
                "streaming": _streaming_selection,
            }
            for sample in range(args.samples):
                order = (
                    ("materialized", "streaming")
                    if sample % 2 == 0
                    else ("streaming", "materialized")
                )
                for mechanism in order:
                    timing[mechanism].append(_time_selection(
                        operations[mechanism],
                        str(shared),
                        args.limit,
                        expected,
                    ))

            memory: dict[str, list[int]] = {
                "materialized": [],
                "streaming": [],
            }
            for sample in range(args.memory_samples):
                order = (
                    ("materialized", "streaming")
                    if sample % 2 == 0
                    else ("streaming", "materialized")
                )
                for mechanism in order:
                    memory[mechanism].append(_peak_traced_bytes(
                        operations[mechanism],
                        str(shared),
                        args.limit,
                        expected,
                    ))

            materialized_time = _summary(timing["materialized"])
            streaming_time = _summary(timing["streaming"])
            materialized_memory = _summary(
                [float(value) for value in memory["materialized"]])
            streaming_memory = _summary(
                [float(value) for value in memory["streaming"]])
            measurements[str(candidate_count)] = {
                "namespace_entries": candidate_count + unrelated_entries,
                "materialized": {
                    "samples_us": timing["materialized"],
                    "peak_traced_bytes": memory["materialized"],
                    "timing_us": materialized_time,
                    "memory_bytes": materialized_memory,
                },
                "streaming": {
                    "samples_us": timing["streaming"],
                    "peak_traced_bytes": memory["streaming"],
                    "timing_us": streaming_time,
                    "memory_bytes": streaming_memory,
                },
                "comparison": {
                    "streaming_to_materialized_median_time_ratio": (
                        streaming_time["median"]
                        / materialized_time["median"]
                    ),
                    "materialized_to_streaming_median_time_ratio": (
                        materialized_time["median"]
                        / streaming_time["median"]
                    ),
                    "streaming_to_materialized_median_memory_ratio": (
                        streaming_memory["median"]
                        / materialized_memory["median"]
                    ),
                    "materialized_to_streaming_median_memory_ratio": (
                        materialized_memory["median"]
                        / streaming_memory["median"]
                    ),
                },
            }

    result = {
        "schema": "symcc-f338-streaming-retired-discovery-cost-v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "filesystem": filesystem,
        "configuration": {
            "candidate_counts": list(candidate_counts),
            "unrelated_entries_per_namespace": unrelated_entries,
            "selection_limit": args.limit,
            "timing_samples_per_mechanism_and_size": args.samples,
            "memory_samples_per_mechanism_and_size": args.memory_samples,
            "warmups_per_mechanism_and_size": args.warmups,
            "timing_interleaved_and_alternating": True,
            "memory_interleaved_and_alternating": True,
            "tree_creation_excluded": True,
        },
        "measurements": measurements,
        "proof_boundary": (
            "Local discovery-only timing and Python tracemalloc peak on one "
            "filesystem. Both paths still perform a complete namespace scan "
            "and type validation. Tracemalloc does not measure kernel buffers, "
            "filesystem caches, or all native allocations. This is not a hard "
            "startup-time bound, multi-host storage result, DSE throughput, "
            "solver, coverage, bug-discovery, or LAVA-M result."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
