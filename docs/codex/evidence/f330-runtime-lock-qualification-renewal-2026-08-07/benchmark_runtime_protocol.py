#!/usr/bin/env python3
"""Measure generation-fenced renewal overhead on a synthetic topology."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


class _CompletedRequest:
    def Test(self):
        return True


class _MessageBus:
    def __init__(self, size):
        self.size = size
        self.lock = threading.Lock()
        self.queues = defaultdict(deque)

    def communicator(self, rank):
        return _Communicator(self, rank)


class _Communicator:
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
        return _CompletedRequest()

    def iprobe(self, *, source, tag):
        with self.bus.lock:
            return bool(self.bus.queues[(self.rank, source, tag)])

    def recv(self, *, source, tag):
        with self.bus.lock:
            return self.bus.queues[(self.rank, source, tag)].popleft()


def _percentile(values, percentile):
    ordered = sorted(values)
    index = max(0, min(
        len(ordered) - 1,
        int(round((len(ordered) - 1) * percentile)),
    ))
    return ordered[index]


def _run_generation(
    root,
    capability,
    processors,
    epoch,
    generation,
    *,
    controllers=None,
):
    size = len(processors)
    bus = _MessageBus(size)
    communicators = [bus.communicator(rank) for rank in range(size)]
    results = [None] * size
    errors = []
    sends = ()
    if controllers is not None:
        accepted_generation, sends, error = (
            qualification.begin_cluster_lock_renewal(
                communicators[0], controllers[0]))
        if error or accepted_generation != generation:
            raise RuntimeError(error or "renewal generation mismatch")

    started = time.perf_counter()

    def run(rank):
        try:
            current_generation = generation
            if controllers is not None and rank != 0:
                current_generation, error, observed = (
                    qualification.poll_cluster_lock_renewal(
                        communicators[rank], controllers[rank]))
                if error or not observed:
                    raise RuntimeError(error or "renewal request missing")
            result = qualification.qualify_mpi_cluster_advisory_lock(
                communicators[rank],
                capability,
                root=str(root),
                epoch=epoch,
                global_rank=rank,
                expected_master_ranks=tuple(range(size)),
                processor_name=processors[rank],
                qualification_generation=current_generation,
                timeout=3.0,
            )
            results[rank] = result
            if controllers is not None:
                if not controllers[rank].complete(
                    current_generation,
                    result,
                    completed_at=time.monotonic(),
                ):
                    raise RuntimeError(result.error or "renewal failed")
        except BaseException as error:  # surfaced after every thread joins
            errors.append(error)

    threads = [threading.Thread(target=run, args=(rank,))
               for rank in range(size)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("qualification thread remained alive")
    if errors:
        raise errors[0]
    if not all(result is not None and result.verified for result in results):
        raise RuntimeError("qualification did not verify on every master")
    if not all(send.Test() for send in sends):
        raise RuntimeError("renewal control send remained incomplete")
    return elapsed_ms, results[0]


def _measure_topology(size, warmups, repetitions):
    processors = tuple(f"synthetic-node-{rank}" for rank in range(size))
    baseline_epoch = f"{size:02x}" * 32
    renewal_epoch = f"{size + 16:02x}" * 32
    with tempfile.TemporaryDirectory(
            prefix=f"symcc-f330-benchmark-{size}-") as tmp:
        temporary = Path(tmp)
        baseline_root = temporary / "baseline"
        renewal_root = temporary / "renewal"
        baseline_capability = probe_shared_state_filesystem(
            str(baseline_root), timeout=2.0)
        renewal_capability = probe_shared_state_filesystem(
            str(renewal_root), timeout=2.0)
        controllers = [
            qualification.ClusterLockRenewalController(
                epoch=renewal_epoch,
                interval=1.0,
                timeout=3.0,
                completed_at=time.monotonic(),
                # This benchmark isolates the pre-F332 request protocol.
                require_configuration_consensus=False,
            )
            for _ in range(size)
        ]
        baseline_values = []
        renewal_values = []
        proof = None
        total = warmups + repetitions
        for iteration in range(1, total + 1):
            # Alternate order to reduce monotonic thermal/order bias.
            order = ("baseline", "renewal") \
                if iteration % 2 else ("renewal", "baseline")
            measured = {}
            for mode in order:
                if mode == "baseline":
                    elapsed, result = _run_generation(
                        baseline_root,
                        baseline_capability,
                        processors,
                        baseline_epoch,
                        iteration,
                    )
                else:
                    elapsed, result = _run_generation(
                        renewal_root,
                        renewal_capability,
                        processors,
                        renewal_epoch,
                        iteration,
                        controllers=controllers,
                    )
                measured[mode] = elapsed
                proof = {
                    "rounds": result.rounds,
                    "contention_checks": result.contention_checks,
                    "release_checks": result.release_checks,
                }
            if iteration > warmups:
                baseline_values.append(measured["baseline"])
                renewal_values.append(measured["renewal"])

    baseline_median = statistics.median(baseline_values)
    renewal_median = statistics.median(renewal_values)
    return {
        "masters": size,
        "warmups": warmups,
        "repetitions": repetitions,
        "proof": proof,
        "baseline_direct_ms": baseline_values,
        "renewal_control_ms": renewal_values,
        "baseline_median_ms": baseline_median,
        "renewal_median_ms": renewal_median,
        "median_increment_ms": renewal_median - baseline_median,
        "median_increment_percent": (
            (renewal_median / baseline_median - 1.0) * 100.0
        ),
        "renewal_mean_ms": statistics.fmean(renewal_values),
        "renewal_p95_ms": _percentile(renewal_values, 0.95),
        "renewal_max_ms": max(renewal_values),
        "failures": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        raise SystemExit("invalid benchmark iteration count")
    observations = [
        _measure_topology(size, args.warmups, args.repetitions)
        for size in (2, 3, 4)
    ]
    result = {
        "schema": "symcc-f330-runtime-renewal-benchmark-v1",
        "synthetic_topology": True,
        "deployment_evidence": False,
        "dse_throughput_evidence": False,
        "local_operation_probe_timed": False,
        "pre_renewal_lease_heartbeat_timed": False,
        "method": (
            "Interleaved same-host Python-thread MPI bus and overlayfs flock; "
            "baseline is direct generation-qualified F329, renewal adds only "
            "root request, peer validation, and controller accounting."
        ),
        "observations": observations,
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
