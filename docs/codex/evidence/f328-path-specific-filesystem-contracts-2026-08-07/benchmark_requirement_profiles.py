#!/usr/bin/env python3
"""Verify and compare the F328 path-specific filesystem contracts."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import distributed_state as state  # noqa: E402


PROFILES = (
    ("full", state.FULL_SHARED_FILESYSTEM_REQUIREMENTS),
    ("lease", state.LEASE_SHARED_FILESYSTEM_REQUIREMENTS),
    ("coverage", state.COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS),
)
PROBE_PREFIXES = (
    ".symcc-fs-probe-",
    ".symcc-fs-publication-probe-",
)


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize(samples: list[float]) -> dict[str, object]:
    return {
        "runs": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "maximum_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def residue(root: str) -> list[str]:
    return sorted(
        os.path.relpath(os.path.join(directory, name), root)
        for directory, subdirectories, files in os.walk(root)
        for name in (*subdirectories, *files)
        if name.startswith(PROBE_PREFIXES)
    )


def valid_snapshot(name: str, snapshot: dict[str, object]) -> bool:
    required = set(snapshot.get("required_operations", ()))
    if name == "full":
        required = set(state.FULL_SHARED_FILESYSTEM_REQUIREMENTS.required_operations)
        if snapshot.get("schema") != "symcc-shared-filesystem-capabilities-v1":
            return False
    else:
        profile = dict(PROFILES)[name]
        if (
            snapshot.get("schema")
            != "symcc-shared-filesystem-capabilities-v2"
            or snapshot.get("requirement_profile") != profile.name
            or required != set(profile.required_operations)
            or set(snapshot.get("unverified_operations", ()))
            != set(profile.unverified_operations)
        ):
            return False
    return bool(
        snapshot.get("probe_scope") == "same-host-subprocess-v1"
        and snapshot.get("cluster_lock_verified") is False
        and all(snapshot.get(operation) is True for operation in required)
        and all(
            snapshot.get(operation) is None
            for operation in state.FULL_SHARED_FILESYSTEM_REQUIREMENTS.required_operations
            if operation not in required
        )
    )


def instrument_profile(
    root: str,
    profile: state.SharedFilesystemRequirementProfile,
) -> tuple[dict[str, object], dict[str, int]]:
    counters = {
        "probe_file_writes": 0,
        "durable_replace": 0,
        "durable_link": 0,
        "durable_unlink": 0,
        "lock_children": 0,
    }

    def counted(name: str, implementation):
        def wrapper(*args, **kwargs):
            counters[name] += 1
            return implementation(*args, **kwargs)
        return wrapper

    with mock.patch.object(
        state,
        "_write_probe_file",
        side_effect=counted("probe_file_writes", state._write_probe_file),
    ), mock.patch.object(
        state,
        "durable_replace",
        side_effect=counted("durable_replace", state.durable_replace),
    ), mock.patch.object(
        state,
        "durable_link",
        side_effect=counted("durable_link", state.durable_link),
    ), mock.patch.object(
        state,
        "durable_unlink",
        side_effect=counted("durable_unlink", state.durable_unlink),
    ), mock.patch.object(
        state,
        "_run_lock_probe_child",
        side_effect=counted("lock_children", state._run_lock_probe_child),
    ):
        snapshot = state.probe_shared_state_filesystem(
            root,
            timeout=5.0,
            requirements=profile,
        ).snapshot()
    return snapshot, counters


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=2)
    args = parser.parse_args()
    if args.rounds < 5 or args.warmup < 0:
        parser.error("rounds must be >= 5 and warmup must be non-negative")

    samples = {name: [] for name, _profile in PROFILES}
    snapshots: dict[str, dict[str, object]] = {}
    operation_counts: dict[str, dict[str, int]] = {}

    with tempfile.TemporaryDirectory(prefix="symcc-f328-") as temporary:
        for name, profile in PROFILES:
            snapshot, counts = instrument_profile(temporary, profile)
            snapshots[name] = snapshot
            operation_counts[name] = counts

        for _ in range(args.warmup):
            for _name, profile in PROFILES:
                state.probe_shared_state_filesystem(
                    temporary, timeout=5.0, requirements=profile)

        for round_index in range(args.rounds):
            offset = round_index % len(PROFILES)
            order = PROFILES[offset:] + PROFILES[:offset]
            for name, profile in order:
                started = time.perf_counter_ns()
                state.probe_shared_state_filesystem(
                    temporary, timeout=5.0, requirements=profile)
                samples[name].append(
                    (time.perf_counter_ns() - started) / 1_000_000)

        remaining = residue(temporary)

    timing = {name: summarize(values) for name, values in samples.items()}
    full_median = float(timing["full"]["median_ms"])
    for name in ("lease", "coverage"):
        median = float(timing[name]["median_ms"])
        timing[name]["median_delta_vs_full_ms"] = round(
            median - full_median, 3)
        timing[name]["median_ratio_vs_full"] = round(
            median / full_median, 4)

    invalid = [
        name for name, snapshot in snapshots.items()
        if not valid_snapshot(name, snapshot)
    ]
    expected_counts = {
        "full": {
            "probe_file_writes": 4,
            "durable_replace": 3,
            "durable_link": 1,
            "durable_unlink": 1,
            "lock_children": 2,
        },
        "lease": {
            "probe_file_writes": 3,
            "durable_replace": 1,
            "durable_link": 0,
            "durable_unlink": 1,
            "lock_children": 2,
        },
        "coverage": {
            "probe_file_writes": 2,
            "durable_replace": 1,
            "durable_link": 0,
            "durable_unlink": 0,
            "lock_children": 2,
        },
    }
    count_mismatches = [
        name for name in expected_counts
        if operation_counts[name] != expected_counts[name]
    ]
    output = {
        "schema": "symcc-f328-path-specific-filesystem-evidence-v1",
        "scope": (
            "same-host startup qualification on one local filesystem; "
            "not DSE throughput, remote-client locking, or crash recovery"
        ),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "configuration": {
            "interleaved_rounds_per_profile": args.rounds,
            "warmup_rounds_per_profile": args.warmup,
            "order": "balanced three-way rotation",
        },
        "snapshots": snapshots,
        "operation_counts": operation_counts,
        "expected_operation_counts": expected_counts,
        "timing": timing,
        "validation": {
            "invalid_snapshots": invalid,
            "operation_count_mismatches": count_mismatches,
            "probe_residue": remaining,
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if not invalid and not count_mismatches and not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
