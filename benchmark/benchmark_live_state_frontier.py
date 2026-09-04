#!/usr/bin/env python3
"""Measure bounded persistent live-state frontier mechanism overhead."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from live_state_frontier import PersistentLiveStateFrontier  # noqa: E402
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
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def measure(count: int, samples: int) -> dict[str, int | float]:
    with tempfile.TemporaryDirectory(prefix="symcc-frontier-benchmark-") as root:
        frontier = PersistentLiveStateFrontier(
            root,
            max_states=count + 10,
            lease_ttl=30.0,
        )
        policy = LiveStateSearchPolicy(("random-state",), seed=17)
        root_checkpoint = "1" * 64
        program_root = "2" * 64
        initial = frontier.initialize(
            root_checkpoint, program_root, policy.snapshot(),
        )
        policy = LiveStateSearchPolicy.from_snapshot(initial.search)
        policy.select_index((
            LiveStateSearchFeatures(root_checkpoint, "main:entry"),
        ))
        lease = frontier.claim(
            root_checkpoint,
            expected_generation=initial.generation,
            search=policy.snapshot(),
            owner="benchmark",
            now=1.0,
        )
        assert lease is not None
        children = [f"{index + 16:064x}" for index in range(count)]
        current = frontier.snapshot()
        completed = frontier.complete(
            lease,
            children,
            expected_generation=current.generation,
            search=current.search,
            now=2.0,
        )
        if completed.status != "completed":
            raise RuntimeError(
                f"frontier benchmark completion failed: {completed.status}"
            )

        snapshot_samples: list[float] = []
        for _sample in range(samples):
            started = time.perf_counter_ns()
            frontier.snapshot()
            snapshot_samples.append((time.perf_counter_ns() - started) / 1e6)

        current = frontier.snapshot()
        policy = LiveStateSearchPolicy.from_snapshot(current.search)
        policy.select_index((
            LiveStateSearchFeatures(current.ready[0], "main:leaf"),
        ))
        started = time.perf_counter_ns()
        lease = frontier.claim(
            current.ready[0],
            expected_generation=current.generation,
            search=policy.snapshot(),
            owner="benchmark",
            now=3.0,
        )
        claim_ms = (time.perf_counter_ns() - started) / 1e6
        assert lease is not None
        started = time.perf_counter_ns()
        abandoned = frontier.abandon(lease, now=4.0)
        abandon_ms = (time.perf_counter_ns() - started) / 1e6
        if abandoned.status != "abandoned":
            raise RuntimeError(
                f"frontier benchmark abandonment failed: {abandoned.status}"
            )
        base_bytes = os.path.getsize(frontier.path)
        journal_bytes = sum(
            path.stat().st_size
            for path in Path(frontier.transition_root).glob("*.json")
        )
        return {
            "states": count + 1,
            "bytes": base_bytes + journal_bytes,
            "base_bytes": base_bytes,
            "journal_bytes": journal_bytes,
            "snapshot_median_ms": round(
                statistics.median(snapshot_samples), 3,
            ),
            "snapshot_p95_ms": round(
                _percentile(snapshot_samples, 0.95), 3,
            ),
            "claim_ms": round(claim_ms, 3),
            "abandon_ms": round(abandon_ms, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--counts", default="1,100,1000,5000",
        help="Comma-separated ready-state counts",
    )
    parser.add_argument("--samples", type=_positive, default=31)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        counts = tuple(_positive(value) for value in args.counts.split(","))
    except (TypeError, ValueError) as error:
        raise ValueError("--counts must contain positive integers") from error
    if not counts or len(counts) > 16 or len(set(counts)) != len(counts):
        raise ValueError("--counts must contain 1..16 unique values")
    result = {
        "schema": "symcc-f461-frontier-microbenchmark-v2",
        "samples": args.samples,
        "clock": "time.perf_counter_ns",
        "durability": "file-fsync+directory-fsync",
        "rows": [measure(count, args.samples) for count in counts],
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="ascii")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
