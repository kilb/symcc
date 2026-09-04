#!/usr/bin/env python3
"""Compare scalar and shard-batched durable lease heartbeats."""

from __future__ import annotations

import argparse
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


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def work_ids(records: int, active_shards: int) -> tuple[str, ...]:
    return tuple(
        f"{index % active_shards:08x}{index:056x}"
        for index in range(records)
    )


def run_scenario(
    records: int,
    active_shards: int,
    rounds: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="symcc-f324-bench-") as tmp:
        table = state.FencedWorkLeaseTable(
            tmp, shard_count=64, lease_ttl=3600.0)
        ids = work_ids(records, active_shards)
        leases = {
            work_id: table.claim(
                work_id,
                {"sequence": index},
                owner="benchmark",
                now=10.0,
            )
            for index, work_id in enumerate(ids)
        }
        if not all(leases.values()):
            raise RuntimeError("benchmark lease setup failed")

        timestamp = 20.0

        def scalar() -> None:
            nonlocal timestamp
            timestamp += 1.0
            for work_id, token in leases.items():
                if not table.heartbeat(work_id, token or "", now=timestamp):
                    raise RuntimeError("scalar heartbeat lost a lease")

        def batched() -> None:
            nonlocal timestamp
            timestamp += 1.0
            result = table.heartbeat_many(leases, now=timestamp)
            if len(result.renewed) != records or result.lost:
                raise RuntimeError("batched heartbeat lost a lease")

        scalar()
        batched()

        with mock.patch(
                "distributed_state.fsync_directory",
                wraps=state.fsync_directory) as scalar_sync:
            scalar()
        with mock.patch(
                "distributed_state.fsync_directory",
                wraps=state.fsync_directory) as batch_sync:
            batched()

        scalar_ms: list[float] = []
        batch_ms: list[float] = []
        for round_index in range(rounds):
            operations = (scalar, batched) if round_index % 2 == 0 \
                else (batched, scalar)
            for operation in operations:
                started = time.perf_counter_ns()
                operation()
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
                (scalar_ms if operation is scalar else batch_ms).append(
                    elapsed_ms)

        scalar_median = statistics.median(scalar_ms)
        batch_median = statistics.median(batch_ms)
        return {
            "records": records,
            "active_shards": active_shards,
            "rounds": rounds,
            "scalar_directory_syncs": scalar_sync.call_count,
            "batch_directory_syncs": batch_sync.call_count,
            "directory_sync_reduction_pct": round(
                100.0 * (1.0 - batch_sync.call_count / scalar_sync.call_count),
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
    if args.records < 1 or args.rounds < 1:
        parser.error("records and rounds must be positive")

    report = {
        "schema": "symcc-f324-heartbeat-group-commit-benchmark-v1",
        "clock": "time.perf_counter_ns",
        "scope": "lease heartbeat persistence only; not DSE throughput",
        "scenarios": [
            run_scenario(args.records, active_shards, args.rounds)
            for active_shards in (1, 8)
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
