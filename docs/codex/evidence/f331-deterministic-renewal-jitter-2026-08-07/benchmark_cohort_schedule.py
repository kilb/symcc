#!/usr/bin/env python3
"""Measure deterministic renewal dispersion without filesystem I/O."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402


def _epoch(job: int) -> str:
    return hashlib.sha256(f"symcc-f331-job-{job}".encode()).hexdigest()


def _bucket(value: float, width: float) -> int:
    return int(math.floor((value + 1e-12) / width))


def _schedule(
    *,
    jobs: int,
    generations: int,
    interval: float,
    jitter: float,
    bucket_width: float,
) -> dict:
    epochs = tuple(_epoch(job) for job in range(jobs))
    clocks = [0.0] * jobs
    all_buckets: Counter[int] = Counter()
    per_generation_peak = []
    delays = []
    for generation in range(1, generations + 1):
        generation_buckets: Counter[int] = Counter()
        for job, epoch in enumerate(epochs):
            delay = qualification._scheduled_renewal_interval(
                epoch,
                generation,
                interval,
                jitter,
            )
            delays.append(delay)
            clocks[job] += delay
            bucket = _bucket(clocks[job], bucket_width)
            generation_buckets[bucket] += 1
            all_buckets[bucket] += 1
        per_generation_peak.append(max(generation_buckets.values()))
    return {
        "total_events": len(delays),
        "minimum_delay": min(delays),
        "maximum_delay": max(delays),
        "minimum_event_time": min(clocks),
        "maximum_event_time": max(clocks),
        "peak_events_per_bucket": max(all_buckets.values()),
        "nonempty_buckets": len(all_buckets),
        "per_generation_peak": per_generation_peak,
        "bucket_counts": [[bucket, all_buckets[bucket]]
                          for bucket in sorted(all_buckets)],
    }


def _timing_samples(
    *,
    jobs: int,
    generations: int,
    interval: float,
    jitter: float,
    warmups: int,
    repetitions: int,
) -> dict:
    epochs = tuple(_epoch(job) for job in range(jobs))
    fixed_samples = []
    jittered_samples = []
    total = warmups + repetitions
    for iteration in range(total):
        order = (0.0, jitter) if iteration % 2 == 0 else (jitter, 0.0)
        measured = {}
        for fraction in order:
            started = time.perf_counter()
            checksum = 0.0
            for generation in range(1, generations + 1):
                for epoch in epochs:
                    checksum += qualification._scheduled_renewal_interval(
                        epoch,
                        generation,
                        interval,
                        fraction,
                    )
            elapsed = time.perf_counter() - started
            if checksum <= 0.0:
                raise RuntimeError("invalid timing checksum")
            measured[fraction] = elapsed
        if iteration >= warmups:
            fixed_samples.append(measured[0.0])
            jittered_samples.append(measured[jitter])
    decisions = jobs * generations
    return {
        "warmups": warmups,
        "repetitions": repetitions,
        "decisions_per_sample": decisions,
        "fixed_sample_ms": [sample * 1000.0 for sample in fixed_samples],
        "jittered_sample_ms": [sample * 1000.0
                               for sample in jittered_samples],
        "fixed_median_us_per_decision": (
            statistics.median(fixed_samples) * 1e6 / decisions
        ),
        "jittered_median_us_per_decision": (
            statistics.median(jittered_samples) * 1e6 / decisions
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=1024)
    parser.add_argument("--generations", type=int, default=12)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--jitter", type=float, default=0.1)
    parser.add_argument("--bucket-width", type=float, default=0.1)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if (
        args.jobs < 1
        or args.generations < 1
        or not math.isfinite(args.interval)
        or args.interval <= 0.0
        or not math.isfinite(args.jitter)
        or args.jitter <= 0.0
        or args.jitter > 0.5
        or not math.isfinite(args.bucket_width)
        or args.bucket_width <= 0.0
        or args.warmups < 0
        or args.repetitions < 1
    ):
        raise SystemExit("invalid benchmark configuration")

    fixed = _schedule(
        jobs=args.jobs,
        generations=args.generations,
        interval=args.interval,
        jitter=0.0,
        bucket_width=args.bucket_width,
    )
    jittered = _schedule(
        jobs=args.jobs,
        generations=args.generations,
        interval=args.interval,
        jitter=args.jitter,
        bucket_width=args.bucket_width,
    )
    timing = _timing_samples(
        jobs=args.jobs,
        generations=args.generations,
        interval=args.interval,
        jitter=args.jitter,
        warmups=args.warmups,
        repetitions=args.repetitions,
    )
    total_events = args.jobs * args.generations
    checks = {
        "event_conservation": (
            fixed["total_events"] == total_events
            and jittered["total_events"] == total_events
        ),
        "fixed_cohort_is_synchronized": (
            fixed["peak_events_per_bucket"] == args.jobs
        ),
        "delay_only_bound_holds": (
            jittered["minimum_delay"] >= args.interval
            and jittered["maximum_delay"]
            < args.interval * (1.0 + args.jitter)
        ),
        "jitter_peak_is_below_ten_percent": (
            jittered["peak_events_per_bucket"] < args.jobs * 0.1
        ),
        "every_generation_is_dispersed": all(
            peak < args.jobs * 0.1
            for peak in jittered["per_generation_peak"]
        ),
    }
    result = {
        "schema": "symcc-f331-renewal-cohort-schedule-v1",
        "synthetic_schedule": True,
        "deployment_evidence": False,
        "actual_mpi_transport": False,
        "filesystem_io_measured": False,
        "dse_throughput_evidence": False,
        "configuration": {
            "jobs": args.jobs,
            "generations": args.generations,
            "interval": args.interval,
            "jitter_fraction": args.jitter,
            "bucket_width": args.bucket_width,
        },
        "method": (
            "All jobs start together. Fixed and deterministic-jitter schedules "
            "conserve the same renewals; 100 ms buckets measure only planned "
            "arrival concentration, without MPI, storage, or qualification I/O."
        ),
        "fixed": fixed,
        "jittered": jittered,
        "comparison": {
            "peak_reduction_percent": (
                100.0 * (1.0 - jittered["peak_events_per_bucket"]
                         / fixed["peak_events_per_bucket"])
            ),
            "nonempty_bucket_multiplier": (
                jittered["nonempty_buckets"] / fixed["nonempty_buckets"]
            ),
        },
        "timing": timing,
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
