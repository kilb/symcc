#!/usr/bin/env python3
"""Bounded mechanism-cost benchmark for the F456 POSE-C heap domain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from pose_symbolic_heap import PoseHeapState, PoseTypeLayout  # noqa: E402


def alias_partition_count(references: int) -> int:
    """Equivalence relations over references plus the distinguished null."""
    values = [0] * (references + 2)
    values[0] = 1
    for index in range(1, references + 2):
        values[index] = sum(
            _binomial(index - 1, part - 1) * values[index - part]
            for part in range(1, index + 1)
        )
    return values[references + 1]


def _binomial(number: int, choose: int) -> int:
    if choose < 0 or choose > number:
        return 0
    choose = min(choose, number - choose)
    result = 1
    for index in range(1, choose + 1):
        result = result * (number - choose + index) // index
    return result


def run_benchmark(
    *,
    repeats: int = 11,
    sizes: Iterable[int] = (1, 2, 4, 8, 16),
) -> dict[str, Any]:
    if repeats < 1 or repeats > 101:
        raise ValueError("repeats must be in [1, 101]")
    rows = []
    for reference_count in sizes:
        if reference_count < 1 or reference_count > 64:
            raise ValueError("reference count must be in [1, 64]")
        elapsed: list[float] = []
        representative: PoseHeapState | None = None
        for _ in range(repeats):
            started = time.perf_counter_ns()
            state = PoseHeapState.create(
                [PoseTypeLayout("Node", 16, 8)],
                [(f"r{index}", "Node") for index in range(reference_count)],
            )
            for index in range(reference_count):
                state, _ = state.load(f"r{index}", 0)
            state.to_mapping()
            elapsed.append((time.perf_counter_ns() - started) / 1000.0)
            representative = state
        assert representative is not None
        snapshot = representative.to_mapping()
        rows.append({
            "alias_partition_models": alias_partition_count(reference_count),
            "cfg_forks": representative.metrics["cfg_forks"],
            "heap_refinements": representative.metrics["heap_refinements"],
            "median_build_load_snapshot_us": statistics.median(elapsed),
            "references": reference_count,
            "snapshot_bytes": len(json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            ).encode("ascii")),
            "terms": len(snapshot["terms"]),
        })
    return {
        "repeats": repeats,
        "rows": rows,
        "schema": "symcc-pose-symbolic-heap-mechanism-benchmark-v1",
        "timing_boundary": (
            "single-process Python mechanism cost; not end-to-end SymCC "
            "throughput and not a lazy-initialization wall-time baseline"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_benchmark(repeats=args.repeats)
    content = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
