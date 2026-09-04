#!/usr/bin/env python3
"""Compare F341 cardinality subtraction with F342 streamed intersection."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import stat
import statistics
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_concolic_execution as runner  # noqa: E402


SCHEMA = "symcc-f342-provenance-intersection-cost-v1"


def _legacy_count(directory: str) -> int:
    public = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not runner._normalize_work_hash(entry.name):
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                public += 1
    return public


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(
        len(ordered) - 1,
        math.ceil(fraction * len(ordered)) - 1,
    ))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "median_us": statistics.median(values),
        "p95_us": _percentile(values, 0.95),
        "minimum_us": min(values),
        "maximum_us": max(values),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--objects", type=int, default=4096)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.objects < 2 or args.objects % 2:
        parser.error("--objects must be a positive even value")
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("warmups must be non-negative and repetitions positive")

    with tempfile.TemporaryDirectory(prefix="symcc-f342-cost-") as tmp:
        root = Path(tmp)
        public_hashes = [
            hashlib.sha256(f"public-{index}".encode("ascii")).hexdigest()
            for index in range(args.objects)
        ]
        for work_hash in public_hashes:
            (root / work_hash).touch()
        present_external = set(public_hashes[::2])
        phantom_external = {
            hashlib.sha256(f"phantom-{index}".encode("ascii")).hexdigest()
            for index in range(args.objects // 2)
        }
        external_hashes = present_external | phantom_external
        expected_generated = args.objects // 2

        raw: dict[str, list[float]] = {"legacy": [], "intersection": []}
        legacy_public = 0
        new_counts = None
        randomizer = random.Random(342)
        rounds = args.warmups + args.repetitions
        for round_index in range(rounds):
            order = ["legacy", "intersection"]
            randomizer.shuffle(order)
            for mechanism in order:
                start = time.perf_counter_ns()
                if mechanism == "legacy":
                    legacy_public = _legacy_count(str(root))
                else:
                    new_counts = runner._count_public_corpus_objects(
                        str(root), external_hashes)
                elapsed_us = (time.perf_counter_ns() - start) / 1000.0
                if round_index >= args.warmups:
                    raw[mechanism].append(elapsed_us)

    if new_counts is None:
        raise RuntimeError("intersection mechanism did not execute")
    legacy_generated = max(0, legacy_public - len(external_hashes))
    checks = {
        "legacy_public_exact": legacy_public == args.objects,
        "legacy_subtraction_is_wrong": legacy_generated == 0,
        "intersection_public_exact": new_counts.public == args.objects,
        "intersection_external_exact": (
            new_counts.external == len(present_external)),
        "intersection_generated_exact": (
            new_counts.generated == expected_generated),
        "raw_sample_cardinality_exact": all(
            len(values) == args.repetitions for values in raw.values()),
        "all_samples_positive": all(
            value > 0 for values in raw.values() for value in values),
    }
    summaries = {
        mechanism: _summary(values) for mechanism, values in raw.items()
    }
    artifact = {
        "schema": SCHEMA,
        "configuration": {
            "objects": args.objects,
            "present_external": len(present_external),
            "phantom_external": len(phantom_external),
            "warmups_per_mechanism": args.warmups,
            "retained_samples_per_mechanism": args.repetitions,
            "order": "deterministically shuffled within every round",
            "filesystem": "local temporary directory",
        },
        "cardinalities": {
            "legacy_public": legacy_public,
            "legacy_generated": legacy_generated,
            "intersection_public": new_counts.public,
            "intersection_external": new_counts.external,
            "intersection_generated": new_counts.generated,
        },
        "raw_elapsed_us": raw,
        "summaries": summaries,
        "elapsed_ratio_intersection_over_legacy": (
            summaries["intersection"]["median_us"]
            / summaries["legacy"]["median_us"]
        ),
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "A local metadata-hot mechanism benchmark over empty regular files. "
            "It measures one final namespace scan and demonstrates the F341 "
            "counterfactual's cardinality error; it is not campaign throughput, "
            "storage, solver, coverage, multi-host, or bug-finding evidence."
        ),
    }
    args.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
