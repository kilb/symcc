#!/usr/bin/env python3
"""Measure F329 protocol mechanics without claiming cross-host evidence."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from distributed_state import probe_shared_state_filesystem  # noqa: E402
from mpi_filesystem_qualification import (  # noqa: E402
    qualify_mpi_cluster_advisory_lock,
)


class _Request:
    def Test(self):
        return True


class _Bus:
    def __init__(self, size):
        self.size = size
        self.lock = threading.Lock()
        self.queues = defaultdict(deque)

    def communicator(self, rank):
        return _Comm(self, rank)


class _Comm:
    def __init__(self, bus, rank):
        self.bus = bus
        self.rank = rank

    def Get_rank(self):
        return self.rank

    def Get_size(self):
        return self.bus.size

    def isend(self, message, *, dest, tag):
        with self.bus.lock:
            self.bus.queues[(dest, self.rank, tag)].append(message)
        return _Request()

    def iprobe(self, *, source, tag):
        with self.bus.lock:
            return bool(self.bus.queues[(self.rank, source, tag)])

    def recv(self, *, source, tag):
        with self.bus.lock:
            return self.bus.queues[(self.rank, source, tag)].popleft()


def percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def run_once(root, capability, masters, epoch):
    bus = _Bus(masters)
    results = [None] * masters
    failures = []

    def run(rank):
        try:
            results[rank] = qualify_mpi_cluster_advisory_lock(
                bus.communicator(rank),
                capability,
                root=root,
                epoch=epoch,
                global_rank=rank,
                expected_master_ranks=tuple(range(masters)),
                # Synthetic identities exercise the state machine only. The
                # production frontend always uses MPI.Get_processor_name().
                processor_name=f"synthetic-node-{rank}",
                timeout=10.0,
            )
        except BaseException as error:
            failures.append(repr(error))

    threads = [threading.Thread(target=run, args=(rank,))
               for rank in range(masters)]
    started = time.perf_counter_ns()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15.0)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("synthetic protocol thread deadline expired")
    if failures or any(result is None or not result.clean for result in results):
        raise RuntimeError(f"synthetic qualification failed: {failures!r}")
    first = results[0]
    expected_contention = masters * (masters - 1)
    if (
        not all(result.verified for result in results)
        or first.rounds != masters
        or first.contention_checks != expected_contention
        or first.release_checks != masters
    ):
        raise RuntimeError("synthetic protocol evidence count mismatch")
    return elapsed_ms, first


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = max(1, args.runs)
    warmups = max(0, args.warmups)
    records = {}
    with tempfile.TemporaryDirectory(prefix="symcc-f329-protocol-") as tmp:
        for masters in (2, 3, 4):
            root = os.path.join(tmp, f"m{masters}")
            capability = probe_shared_state_filesystem(root, timeout=5.0)
            samples = []
            first_result = None
            for iteration in range(warmups + runs):
                elapsed_ms, result = run_once(
                    root, capability, masters, chr(97 + masters) * 64)
                if iteration >= warmups:
                    samples.append(elapsed_ms)
                    first_result = result
            assert first_result is not None
            records[str(masters)] = {
                "masters": masters,
                "synthetic_processors": masters,
                "rounds": first_result.rounds,
                "contention_checks": first_result.contention_checks,
                "release_checks": first_result.release_checks,
                "runs": runs,
                "warmups": warmups,
                "median_ms": statistics.median(samples),
                "mean_ms": statistics.fmean(samples),
                "p95_ms": percentile(samples, 0.95),
                "max_ms": max(samples),
                "min_ms": min(samples),
                "failures": 0,
            }
    output = {
        "schema": "symcc-f329-synthetic-protocol-benchmark-v1",
        "synthetic_topology": True,
        "deployment_evidence": False,
        "timing_scope": (
            "same-host Python-thread control-plane plus overlayfs flock rounds; "
            "local nine-operation probe excluded"
        ),
        "records": records,
    }
    args.output.write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
