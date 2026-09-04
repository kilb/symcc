#!/usr/bin/env python3
"""Measure bounded live-state search-policy selection overhead."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from live_state_search import (  # noqa: E402
    LiveStateSearchFeatures,
    LiveStateSearchPolicy,
)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _features(count: int) -> tuple[LiveStateSearchFeatures, ...]:
    return tuple(
        LiveStateSearchFeatures(
            identity=f"state-{index:08d}",
            location=f"main:block-{index % 257}",
            path_depth=index % 97,
            instructions=index * 3,
            solver_queries=index % 31,
            target_branch=7001,
            distance_to_uncovered=index % 23,
            distance_to_target=(count - index) % 37,
            exits_cycle=index % 29 == 0,
            outcome_key=f"ctx:{index % 257}",
        )
        for index in range(count)
    )


def _seed_feedback(policy: LiveStateSearchPolicy, count: int) -> None:
    for index in range(min(count, 257)):
        feature = LiveStateSearchFeatures(
            identity=f"seed-{index}",
            location=f"main:block-{index}",
            outcome_key=f"ctx:{index}",
        )
        policy.select_index((feature,))
        policy.observe_outcome(
            f"ctx:{index}",
            coverage_gain=int(index % 7 == 0),
            steps=1 + index % 101,
            solver_queries=index % 13,
            failed=index % 41 == 0,
        )


def measure(strategy: str, count: int, samples: int) -> dict[str, int | float | str]:
    policy = LiveStateSearchPolicy((strategy,), seed=17)
    _seed_feedback(policy, count)
    features = _features(count)
    timings: list[float] = []
    selected = ""
    for _sample in range(samples):
        started = time.perf_counter_ns()
        selected = features[policy.select_index(features)].identity
        timings.append((time.perf_counter_ns() - started) / 1e6)
    snapshot_bytes = len(json.dumps(
        policy.snapshot(), sort_keys=True, separators=(",", ":"),
    ).encode("ascii"))
    return {
        "strategy": strategy,
        "candidates": count,
        "feedback_contexts": policy.telemetry()["outcome_contexts"],
        "snapshot_bytes": snapshot_bytes,
        "last_selected": selected,
        "median_ms": round(statistics.median(timings), 6),
        "p95_ms": round(_percentile(timings, 0.95), 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="64,1024,4096")
    parser.add_argument("--samples", type=_positive, default=31)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    counts = tuple(_positive(value) for value in args.counts.split(","))
    if not counts or len(counts) > 8 or len(set(counts)) != len(counts):
        raise ValueError("--counts must contain 1..8 unique positive integers")
    if any(count > 65_536 for count in counts):
        raise ValueError("candidate count exceeds the production window")
    result = {
        "schema": "symcc-f400-live-state-policy-microbenchmark-v1",
        "samples": args.samples,
        "clock": "time.perf_counter_ns",
        "claim_or_solver_execution_included": False,
        "rows": [
            measure(strategy, count, args.samples)
            for count in counts
            for strategy in ("bfs", "multi-objective")
        ],
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="ascii")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
