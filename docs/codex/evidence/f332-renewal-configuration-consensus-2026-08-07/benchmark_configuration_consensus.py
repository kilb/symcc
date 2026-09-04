#!/usr/bin/env python3
"""Measure F332 validation cost above the existing bounded MPI exchange model."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import statistics
import sys
import threading
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402


class _CompletedRequest:
    def Test(self):
        return True


class _MessageBus:
    def __init__(self, size: int):
        self.size = size
        self.lock = threading.Lock()
        self.queues = defaultdict(deque)

    def communicator(self, rank: int):
        return _Communicator(self, rank)


class _Communicator:
    def __init__(self, bus: _MessageBus, rank: int):
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


def _sample(size: int, *, consensus: bool) -> tuple[float, bool]:
    epoch = hashlib.sha256(f"f332-size-{size}".encode()).hexdigest()
    controllers = [
        qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        for _ in range(size)
    ]
    bus = _MessageBus(size)
    results = [None] * size
    token = hashlib.sha256(
        b"symcc-cluster-lock-renewal-config-exchange-v1\0"
        + bytes.fromhex(epoch)
    ).hexdigest()

    def run(rank: int):
        if consensus:
            results[rank] = (
                qualification.qualify_cluster_lock_renewal_configuration(
                    bus.communicator(rank),
                    controllers[rank],
                    timeout=5.0,
                )
            )
        else:
            records, error = qualification._bounded_master_exchange(
                bus.communicator(rank),
                controllers[rank].configuration_snapshot(),
                token=token,
                phase="renewal-configuration",
                deadline=time.monotonic() + 5.0,
            )
            results[rank] = (records, error)

    threads = [threading.Thread(target=run, args=(rank,))
               for rank in range(size)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(6.0)
    elapsed = time.perf_counter() - started
    if any(thread.is_alive() for thread in threads):
        return elapsed, False
    if consensus:
        clean = (
            all(result is not None and not result[1] for result in results)
            and len({result[0] for result in results}) == 1
        )
    else:
        clean = all(
            result is not None
            and not result[1]
            and len(result[0]) == size
            for result in results
        )
    return elapsed, clean


def _measure(size: int, warmups: int, repetitions: int) -> dict:
    samples = {"exchange": [], "consensus": []}
    all_clean = True
    for iteration in range(warmups + repetitions):
        order = (False, True) if iteration % 2 == 0 else (True, False)
        for consensus in order:
            elapsed, clean = _sample(size, consensus=consensus)
            all_clean = all_clean and clean
            if iteration >= warmups:
                key = "consensus" if consensus else "exchange"
                samples[key].append(elapsed * 1000.0)
    exchange_median = statistics.median(samples["exchange"])
    consensus_median = statistics.median(samples["consensus"])
    return {
        "masters": size,
        "exchange_sample_ms": samples["exchange"],
        "consensus_sample_ms": samples["consensus"],
        "exchange_median_ms": exchange_median,
        "consensus_median_ms": consensus_median,
        "median_delta_ms": consensus_median - exchange_median,
        "median_ratio": consensus_median / exchange_median,
        "all_samples_clean": all_clean,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        raise SystemExit("invalid benchmark configuration")
    observations = [
        _measure(size, args.warmups, args.repetitions)
        for size in (2, 4, 8)
    ]
    checks = {
        "all_samples_complete_and_consistent": all(
            item["all_samples_clean"] for item in observations
        ),
        "all_sample_counts_exact": all(
            len(item["exchange_sample_ms"]) == args.repetitions
            and len(item["consensus_sample_ms"]) == args.repetitions
            for item in observations
        ),
        "all_timings_positive": all(
            item["exchange_median_ms"] > 0.0
            and item["consensus_median_ms"] > 0.0
            for item in observations
        ),
    }
    result = {
        "schema": "symcc-f332-renewal-configuration-consensus-cost-v1",
        "synthetic_control_plane": True,
        "actual_mpi_transport": False,
        "deployment_evidence": False,
        "dse_throughput_evidence": False,
        "method": (
            "Same-host Python threads and a fake point-to-point MPI bus compare "
            "the existing bounded exchange with F332 record validation. Thread "
            "startup and scheduling are included; results are mechanism cost, "
            "not distributed deployment or DSE throughput."
        ),
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
