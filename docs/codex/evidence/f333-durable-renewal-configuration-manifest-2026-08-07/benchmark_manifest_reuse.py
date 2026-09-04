#!/usr/bin/env python3
"""Measure F333 exact-read reuse against the pre-fast-path I/O sequence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_concolic_execution as runner  # noqa: E402


def _controller() -> runner.ClusterLockRenewalController:
    controller = runner.ClusterLockRenewalController(
        epoch="f333" * 16,
        interval=60.0,
        timeout=5.0,
        completed_at=0.0,
        jitter_fraction=0.1,
    )
    controller._record_configuration_consensus(
        controller.configuration_fingerprint)
    return controller


def _rewrite_before_compare(
    root: str,
    controller: runner.ClusterLockRenewalController,
) -> None:
    """Reproduce the removed fsync/link/unlink prefix before exact reuse."""
    path = os.path.join(root, runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST)
    temporary = f"{path}.{os.getpid()}.{time.monotonic_ns()}.baseline.tmp"
    content = b"pre-fast-path-counterfactual\n"
    try:
        with open(temporary, "xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    runner._ensure_runtime_lock_configuration_manifest(root, controller)


def _timed(operation) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1000.0


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered)) - 1))
    return {
        "minimum_us": ordered[0],
        "median_us": statistics.median(ordered),
        "p95_us": ordered[p95_index],
        "maximum_us": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmups", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--first-publish-samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1 or args.first_publish_samples < 1:
        parser.error("sample counts must be positive")

    controller = _controller()
    fast: list[float] = []
    baseline: list[float] = []
    first_publish: list[float] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f333-reuse-") as tmp:
        root = str(Path(tmp) / "shared")
        runner._ensure_runtime_lock_configuration_manifest(root, controller)
        for index in range(args.warmups + args.repetitions):
            # Alternate order to avoid assigning monotonic drift to one arm.
            operations = (
                (
                    lambda: runner._ensure_runtime_lock_configuration_manifest(
                        root, controller),
                    lambda: _rewrite_before_compare(root, controller),
                )
                if index % 2 == 0 else
                (
                    lambda: _rewrite_before_compare(root, controller),
                    lambda: runner._ensure_runtime_lock_configuration_manifest(
                        root, controller),
                )
            )
            observations = [_timed(operation) for operation in operations]
            if index < args.warmups:
                continue
            if index % 2 == 0:
                fast.append(observations[0])
                baseline.append(observations[1])
            else:
                baseline.append(observations[0])
                fast.append(observations[1])

        for index in range(args.first_publish_samples):
            fresh = str(Path(tmp) / f"first-{index:04d}")
            first_publish.append(_timed(
                lambda fresh=fresh:
                runner._ensure_runtime_lock_configuration_manifest(
                    fresh, controller)
            ))

        manifest = Path(root) / runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST
        manifest_sha256 = runner._file_sha256(str(manifest))
        temporary_residue = tuple(Path(tmp).rglob("*.tmp"))

    fast_summary = _summary(fast)
    baseline_summary = _summary(baseline)
    publish_summary = _summary(first_publish)
    ratio = baseline_summary["median_us"] / fast_summary["median_us"]
    result = {
        "schema": "symcc-f333-manifest-reuse-cost-v1",
        "host": platform.node(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "mechanism_only": True,
        "proof_boundary": (
            "Interleaved local-filesystem startup-control measurements; the "
            "counterfactual reproduces the removed temp-file fsync/link/unlink "
            "prefix. Results do not measure DSE throughput, solver time, "
            "coverage, remote storage, or LAVA-M."
        ),
        "configuration_fingerprint": controller.configuration_fingerprint,
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "first_publish_samples": args.first_publish_samples,
        "fast_exact_reuse": fast_summary,
        "pre_fast_path_counterfactual": baseline_summary,
        "first_publish": publish_summary,
        "counterfactual_to_fast_median_ratio": ratio,
        "median_saved_us": (
            baseline_summary["median_us"] - fast_summary["median_us"]
        ),
        "manifest_sha256": manifest_sha256,
        "temporary_residue_count": len(temporary_residue),
        "all_samples_completed": (
            len(fast) == args.repetitions
            and len(baseline) == args.repetitions
            and len(first_publish) == args.first_publish_samples
            and not temporary_residue
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_samples_completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
