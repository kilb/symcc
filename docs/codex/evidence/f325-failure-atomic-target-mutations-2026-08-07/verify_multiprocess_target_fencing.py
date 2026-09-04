#!/usr/bin/env python3
"""Race overlapping target groups through independent OS processes."""

from __future__ import annotations

import argparse
import json
import multiprocessing
from pathlib import Path
import queue
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import FencedTargetLeaseTable  # noqa: E402


def claimant(
    root: str,
    shards: int,
    name: str,
    targets: tuple[int, ...],
    timestamp: float,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    table = FencedTargetLeaseTable(
        root, shard_count=shards, lease_ttl=3600.0)
    ready.put(name)
    if not start.wait(timeout=10.0):
        results.put((name, "", "start-timeout"))
        return
    try:
        token = table.claim_group(
            targets,
            {"claimant": name},
            owner=name,
            now=timestamp,
        )
        results.put((name, token or "", ""))
    except Exception as error:  # Preserve the child failure for the parent.
        results.put((name, "", f"{type(error).__name__}: {error}"))


def run(rounds: int, shards: int) -> dict[str, object]:
    context = multiprocessing.get_context("fork")
    group_a = tuple(range(1, 33))
    group_b = tuple(range(17, 49))
    groups = {"coordinator-a": group_a, "coordinator-b": group_b}
    wins = {name: 0 for name in groups}
    started = time.perf_counter_ns()

    with tempfile.TemporaryDirectory(prefix="symcc-f325-race-") as tmp:
        parent_table = FencedTargetLeaseTable(
            tmp, shard_count=shards, lease_ttl=3600.0)
        for round_index in range(rounds):
            ready = context.Queue()
            results = context.Queue()
            start = context.Event()
            processes = [
                context.Process(
                    target=claimant,
                    args=(
                        tmp,
                        shards,
                        name,
                        targets,
                        100.0 + round_index,
                        ready,
                        start,
                        results,
                    ),
                )
                for name, targets in groups.items()
            ]
            for process in processes:
                process.start()
            try:
                observed_ready = {ready.get(timeout=10.0) for _ in processes}
            except queue.Empty as error:
                raise RuntimeError("claimant readiness timed out") from error
            if observed_ready != set(groups):
                raise RuntimeError(f"unexpected ready set: {observed_ready}")
            start.set()
            try:
                outcomes = [results.get(timeout=20.0) for _ in processes]
            except queue.Empty as error:
                raise RuntimeError("claimant result timed out") from error
            for process in processes:
                process.join(timeout=10.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)
                    raise RuntimeError("claimant hung while acquiring group locks")
                if process.exitcode != 0:
                    raise RuntimeError(
                        f"claimant exited with status {process.exitcode}")

            errors = [error for _name, _token, error in outcomes if error]
            if errors:
                raise RuntimeError(f"child failures: {errors}")
            winners = [(name, token) for name, token, _error in outcomes if token]
            if len(winners) != 1:
                raise RuntimeError(f"expected one winner, observed {outcomes}")
            winner, token = winners[0]
            wins[winner] += 1

            records = list(Path(tmp).glob("*/*.json"))
            if len(records) != len(groups[winner]):
                raise RuntimeError(
                    f"partial publication: {len(records)} records for {winner}")
            if not parent_table.release_group(groups[winner], token):
                raise RuntimeError("winner group could not be released")
            if list(Path(tmp).glob("*/*.json")):
                raise RuntimeError("release left target records behind")

    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    return {
        "schema": "symcc-f325-multiprocess-target-fencing-v1",
        "process_model": "fork",
        "rounds": rounds,
        "processes_per_round": 2,
        "configured_shards": shards,
        "group_size": len(group_a),
        "overlap_size": len(set(group_a) & set(group_b)),
        "single_winner_rounds": rounds,
        "partial_publications": 0,
        "residual_records_after_release": 0,
        "hangs": 0,
        "wins": wins,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--shards", type=int, default=8)
    args = parser.parse_args()
    if args.rounds < 1 or args.shards < 1:
        parser.error("rounds and shards must be positive")
    print(json.dumps(run(args.rounds, args.shards), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
