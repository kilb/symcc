#!/usr/bin/env python3
"""Compare scalar and shard-batched durable target-group mutations."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import distributed_state as state  # noqa: E402


class ScalarDirectorySync:
    """Reference path with one directory barrier per record mutation."""

    @property
    def pending_count(self) -> int:
        return 0

    def replace(self, source: str, destination: str) -> None:
        state.durable_replace(source, destination)

    def unlink(self, path: str) -> None:
        state.durable_unlink(path)

    def flush(self) -> int:
        return 0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def run_scenario(records: int, shards: int, rounds: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="symcc-f325-bench-") as tmp:
        table = state.FencedTargetLeaseTable(
            tmp, shard_count=shards, lease_ttl=3600.0)
        targets = tuple(range(1, records + 1))
        target_ids = table._target_ids(targets)
        active_directories = {
            table.shard_dir(table.shard_for_work(target_id))
            for _target, target_id in target_ids
        }
        for directory in active_directories:
            Path(directory).mkdir(parents=True, exist_ok=True)

        timestamp = 10.0

        def cycle(*, scalar: bool) -> None:
            nonlocal timestamp
            timestamp += 1.0
            context = mock.patch.object(
                state, "_DirectorySyncBatch", ScalarDirectorySync,
            ) if scalar else nullcontext()
            with context:
                token = table.claim_group(
                    targets,
                    {"benchmark": "target-group-mutation"},
                    owner="benchmark",
                    now=timestamp,
                )
                if not token:
                    raise RuntimeError("target-group claim failed")
                if not table.release_group(targets, token):
                    raise RuntimeError("target-group release failed")

        cycle(scalar=True)
        cycle(scalar=False)

        with mock.patch(
                "distributed_state.fsync_directory",
                wraps=state.fsync_directory) as scalar_sync:
            cycle(scalar=True)
        with mock.patch(
                "distributed_state.fsync_directory",
                wraps=state.fsync_directory) as batch_sync:
            cycle(scalar=False)

        scalar_ms: list[float] = []
        batch_ms: list[float] = []
        for round_index in range(rounds):
            order = (True, False) if round_index % 2 == 0 else (False, True)
            for scalar in order:
                started = time.perf_counter_ns()
                cycle(scalar=scalar)
                elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
                (scalar_ms if scalar else batch_ms).append(elapsed)

        scalar_median = statistics.median(scalar_ms)
        batch_median = statistics.median(batch_ms)
        return {
            "records": records,
            "configured_shards": shards,
            "active_shards": len(active_directories),
            "rounds": rounds,
            "mutations_per_cycle": records * 2,
            "scalar_directory_syncs": scalar_sync.call_count,
            "batch_directory_syncs": batch_sync.call_count,
            "directory_sync_reduction_pct": round(
                100.0 * (
                    1.0 - batch_sync.call_count / scalar_sync.call_count
                ),
                3,
            ),
            "scalar_median_ms": round(scalar_median, 3),
            "scalar_p95_ms": round(percentile(scalar_ms, 0.95), 3),
            "batch_median_ms": round(batch_median, 3),
            "batch_p95_ms": round(percentile(batch_ms, 0.95), 3),
            "median_speedup": round(scalar_median / batch_median, 3),
            "scalar_samples_ms": [round(value, 3) for value in scalar_ms],
            "batch_samples_ms": [round(value, 3) for value in batch_ms],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.records <= state.FencedTargetLeaseTable.MAX_TARGETS:
        parser.error("records must be between 1 and MAX_TARGETS")
    if args.rounds < 1:
        parser.error("rounds must be positive")

    report = {
        "schema": "symcc-f325-target-mutation-benchmark-v1",
        "clock": "time.perf_counter_ns",
        "scope": (
            "target lease claim+release persistence only; "
            "not DSE, coverage, or solver throughput"
        ),
        "scenarios": [
            run_scenario(args.records, shards, args.rounds)
            for shards in (1, 8)
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
