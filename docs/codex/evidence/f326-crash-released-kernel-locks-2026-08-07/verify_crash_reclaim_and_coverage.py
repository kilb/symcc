#!/usr/bin/env python3
"""Exercise SIGKILL lock recovery and concurrent coverage OR commits."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import queue
import select
import signal
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import (  # noqa: E402
    CoverageOwnerShardGossip,
    FencedWorkLeaseTable,
)


def wait_for_lock_marker(read_fd: int, child: int) -> None:
    readable, _writable, _errors = select.select([read_fd], [], [], 10.0)
    if readable != [read_fd] or os.read(read_fd, 1) != b"L":
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
        raise RuntimeError("child failed to publish a held-lock marker")


def kill_work_holder(root: str, work_id: str) -> None:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            table = FencedWorkLeaseTable(
                root, shard_count=8, lock_acquire_timeout=1.0)
            with table._locked(work_id):
                os.write(write_fd, b"L")
                while True:
                    signal.pause()
        except BaseException:
            os._exit(2)
    os.close(write_fd)
    try:
        wait_for_lock_marker(read_fd, child)
        os.kill(child, signal.SIGKILL)
        waited, status = os.waitpid(child, 0)
        if waited != child or not os.WIFSIGNALED(status):
            raise RuntimeError("work lock holder did not die by signal")
    finally:
        os.close(read_fd)


def kill_coverage_holder(root: str, shard: int) -> None:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            gossip = CoverageOwnerShardGossip(
                root,
                shard_count=8,
                coordinator_id="crash-holder",
                lock_acquire_timeout=1.0,
            )
            with gossip._locked(shard):
                os.write(write_fd, b"L")
                while True:
                    signal.pause()
        except BaseException:
            os._exit(2)
    os.close(write_fd)
    try:
        wait_for_lock_marker(read_fd, child)
        os.kill(child, signal.SIGKILL)
        waited, status = os.waitpid(child, 0)
        if waited != child or not os.WIFSIGNALED(status):
            raise RuntimeError("coverage lock holder did not die by signal")
    finally:
        os.close(read_fd)


def coverage_writer(
    root: str,
    rank: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    try:
        gossip = CoverageOwnerShardGossip(
            root,
            shard_count=8,
            coordinator_id=f"writer-{rank}",
            coordinator_index=rank,
            coordinator_count=8,
            lock_acquire_timeout=5.0,
        )
        ready.put(rank)
        if not start.wait(timeout=10.0):
            results.put((rank, -1, "start timeout"))
            return
        bit = 1 << rank
        novel = gossip.claim(
            [(index, bit) for index in range(256)], now=100.0)
        results.put((rank, novel, ""))
    except Exception as error:
        results.put((rank, -1, f"{type(error).__name__}: {error}"))


def run_crash_reclaim(rounds: int) -> dict[str, object]:
    work_latencies: list[float] = []
    coverage_latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f326-crash-") as tmp:
        work_root = str(Path(tmp) / "work")
        coverage_root = str(Path(tmp) / "coverage")
        work = FencedWorkLeaseTable(
            work_root, shard_count=8, lock_acquire_timeout=0.5)
        coverage = CoverageOwnerShardGossip(
            coverage_root,
            shard_count=8,
            coordinator_id="reclaimer",
            lock_acquire_timeout=0.5,
        )
        for round_index in range(rounds):
            work_id = f"{round_index + 1:064x}"
            kill_work_holder(work_root, work_id)
            started = time.perf_counter_ns()
            token = work.claim(
                work_id,
                {"round": round_index},
                owner="reclaimer",
                now=10.0 + round_index,
            )
            work_latencies.append(
                (time.perf_counter_ns() - started) / 1_000_000.0)
            if not token:
                raise RuntimeError("work lock was not reclaimed after SIGKILL")

            index = round_index
            shard = coverage.shard_for_index(index)
            kill_coverage_holder(coverage_root, shard)
            started = time.perf_counter_ns()
            novel = coverage.claim([(index, 1)], now=10.0 + round_index)
            coverage_latencies.append(
                (time.perf_counter_ns() - started) / 1_000_000.0)
            if novel != 1:
                raise RuntimeError(
                    "coverage lock was not reclaimed after SIGKILL")

        return {
            "rounds": rounds,
            "killed_processes": rounds * 2,
            "work_reclaims": rounds,
            "coverage_reclaims": rounds,
            "timeouts": 0,
            "work_max_reclaim_ms": round(max(work_latencies), 3),
            "coverage_max_reclaim_ms": round(max(coverage_latencies), 3),
            "persistent_work_lock_files": len(list(
                Path(work_root).glob("*/*.lock"))),
            "persistent_coverage_lock_files": len(list(
                Path(coverage_root).glob("state/*.lock"))),
        }


def run_coverage_stress(campaigns: int) -> dict[str, object]:
    context = multiprocessing.get_context("fork")
    complete = 0
    with tempfile.TemporaryDirectory(prefix="symcc-f326-coverage-") as tmp:
        for campaign in range(campaigns):
            root = str(Path(tmp) / f"campaign-{campaign}")
            CoverageOwnerShardGossip(
                root,
                shard_count=8,
                coordinator_id="initializer",
                coordinator_index=0,
                coordinator_count=8,
            )
            ready = context.Queue()
            results = context.Queue()
            start = context.Event()
            processes = [
                context.Process(
                    target=coverage_writer,
                    args=(root, rank, ready, start, results),
                )
                for rank in range(8)
            ]
            for process in processes:
                process.start()
            try:
                observed = {ready.get(timeout=10.0) for _ in processes}
            except queue.Empty as error:
                raise RuntimeError("coverage writers did not become ready") from error
            if observed != set(range(8)):
                raise RuntimeError(f"unexpected ready writers: {observed}")
            start.set()
            try:
                outcomes = [results.get(timeout=30.0) for _ in processes]
            except queue.Empty as error:
                raise RuntimeError("coverage writer result timed out") from error
            for process in processes:
                process.join(timeout=10.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)
                    raise RuntimeError("coverage writer hung")
                if process.exitcode != 0:
                    raise RuntimeError(
                        f"coverage writer exited with {process.exitcode}")
            errors = [error for _rank, _novel, error in outcomes if error]
            if errors:
                raise RuntimeError(f"coverage writer failures: {errors}")
            if sum(novel for _rank, novel, _error in outcomes) != 2048:
                raise RuntimeError(f"novelty accounting mismatch: {outcomes}")

            reader = CoverageOwnerShardGossip(
                root, shard_count=8, coordinator_id="reader")
            merged = dict(reader.pull())
            if len(merged) != 256 or set(merged.values()) != {0xFF}:
                raise RuntimeError("concurrent OR commit lost coverage bits")
            complete += 1
    return {
        "campaigns": campaigns,
        "processes_per_campaign": 8,
        "indices_per_writer": 256,
        "expected_novel_bits_per_campaign": 2048,
        "complete_campaigns": complete,
        "lost_update_campaigns": 0,
        "timeouts": 0,
        "hangs": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-rounds", type=int, default=20)
    parser.add_argument("--coverage-campaigns", type=int, default=10)
    args = parser.parse_args()
    if args.crash_rounds < 1 or args.coverage_campaigns < 1:
        parser.error("round and campaign counts must be positive")
    started = time.perf_counter_ns()
    report = {
        "schema": "symcc-f326-crash-reclaim-coverage-stress-v1",
        "process_model": "fork+SIGKILL",
        "scope": "shared-state coordination correctness; not DSE throughput",
        "crash_reclaim": run_crash_reclaim(args.crash_rounds),
        "coverage_stress": run_coverage_stress(args.coverage_campaigns),
    }
    report["elapsed_ms"] = round(
        (time.perf_counter_ns() - started) / 1_000_000.0, 3)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
