#!/usr/bin/env python3
"""Compare the pre-F339 staged verifier with the F339 production path."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest import mock


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_concolic_execution as runner  # noqa: E402


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(len(ordered) - 1, lower + 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "minimum": min(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "p95": _percentile(samples, 0.95),
        "maximum": max(samples),
    }


def _materialized_verify(directory: str, expected: set[str]) -> bool:
    """Reproduce the verifier immediately before F339."""
    try:
        entries = tuple(os.scandir(directory))
    except OSError:
        return False
    observed: set[str] = set()
    for entry in entries:
        work_hash = runner._normalize_work_hash(entry.name)
        try:
            regular = entry.is_file(follow_symlinks=False)
        except OSError:
            return False
        if not work_hash or not regular or work_hash in observed:
            return False
        if runner._file_sha256(entry.path) != work_hash:
            return False
        observed.add(work_hash)
    return observed == expected


def _streaming_verify(directory: str, expected: set[str]) -> bool:
    return runner._verified_staged_output_hashes(directory, expected) == expected


def _time_call(operation, directory: str, expected: set[str], result: bool) -> float:
    started = time.perf_counter_ns()
    observed = operation(directory, expected)
    elapsed = (time.perf_counter_ns() - started) / 1000.0
    if observed is not result:
        raise AssertionError("verification result drifted during timing")
    return elapsed


def _peak_traced_bytes(
    operation,
    directory: str,
    expected: set[str],
) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        observed = operation(directory, expected)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if not observed:
        raise AssertionError("valid namespace failed during memory trial")
    return peak


def _make_valid_namespace(root: Path, count: int) -> tuple[Path, set[str]]:
    directory = root / f"valid-{count}"
    directory.mkdir()
    expected = {
        hashlib.sha256(f"F339-valid-{index}".encode("ascii")).hexdigest()
        for index in range(count)
    }
    for name in reversed(sorted(expected)):
        (directory / name).touch()
    return directory, expected


def _filesystem_type(path: str) -> str:
    completed = subprocess.run(
        ["stat", "-f", "-c", "%T", path],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return completed.stdout.strip()


def _valid_namespace_measurements(
    root: Path,
    *,
    samples: int,
    memory_samples: int,
    warmups: int,
) -> dict[str, object]:
    measurements: dict[str, object] = {}
    operations = {
        "materialized": _materialized_verify,
        "streaming": _streaming_verify,
    }

    def digest_from_name(path: str) -> str:
        return os.path.basename(path)

    with mock.patch.object(runner, "_file_sha256", digest_from_name):
        for count in (128, 1024, 4096):
            directory, expected = _make_valid_namespace(root, count)
            for _ in range(warmups):
                for operation in operations.values():
                    _time_call(operation, str(directory), expected, True)

            timings: dict[str, list[float]] = {
                mechanism: [] for mechanism in operations
            }
            for sample in range(samples):
                order = (
                    ("materialized", "streaming")
                    if sample % 2 == 0
                    else ("streaming", "materialized")
                )
                for mechanism in order:
                    timings[mechanism].append(_time_call(
                        operations[mechanism], str(directory), expected, True))

            peaks: dict[str, list[int]] = {
                mechanism: [] for mechanism in operations
            }
            for sample in range(memory_samples):
                order = (
                    ("materialized", "streaming")
                    if sample % 2 == 0
                    else ("streaming", "materialized")
                )
                for mechanism in order:
                    peaks[mechanism].append(_peak_traced_bytes(
                        operations[mechanism], str(directory), expected))

            materialized_time = _summary(timings["materialized"])
            streaming_time = _summary(timings["streaming"])
            materialized_memory = _summary(
                [float(value) for value in peaks["materialized"]])
            streaming_memory = _summary(
                [float(value) for value in peaks["streaming"]])
            measurements[str(count)] = {
                "expected_objects": count,
                "materialized": {
                    "samples_us": timings["materialized"],
                    "peak_traced_bytes": peaks["materialized"],
                    "timing_us": materialized_time,
                    "memory_bytes": materialized_memory,
                },
                "streaming": {
                    "samples_us": timings["streaming"],
                    "peak_traced_bytes": peaks["streaming"],
                    "timing_us": streaming_time,
                    "memory_bytes": streaming_memory,
                },
                "comparison": {
                    "materialized_to_streaming_median_memory_ratio": (
                        materialized_memory["median"]
                        / streaming_memory["median"]
                    ),
                    "materialized_to_streaming_median_time_ratio": (
                        materialized_time["median"]
                        / streaming_time["median"]
                    ),
                    "streaming_to_materialized_median_time_ratio": (
                        streaming_time["median"]
                        / materialized_time["median"]
                    ),
                },
            }
    return measurements


def _unexpected_object_measurement(
    root: Path,
    *,
    samples: int,
    warmups: int,
    object_bytes: int,
) -> dict[str, object]:
    directory = root / "unexpected-large-object"
    directory.mkdir()
    content = (b"F339-unexpected-content\0" * (
        object_bytes // len(b"F339-unexpected-content\0") + 1
    ))[:object_bytes]
    unexpected_hash = hashlib.sha256(content).hexdigest()
    (directory / unexpected_hash).write_bytes(content)
    expected = {hashlib.sha256(b"F339-expected-absent").hexdigest()}
    original_digest = runner._file_sha256
    operations = {
        "materialized": _materialized_verify,
        "streaming": _streaming_verify,
    }
    timings: dict[str, list[float]] = {
        mechanism: [] for mechanism in operations
    }
    observations = {
        mechanism: {"digest_calls": 0, "content_bytes_requested": 0}
        for mechanism in operations
    }

    for _ in range(warmups):
        for operation in operations.values():
            _time_call(operation, str(directory), expected, False)

    for sample in range(samples):
        order = (
            ("materialized", "streaming")
            if sample % 2 == 0
            else ("streaming", "materialized")
        )
        for mechanism in order:
            def counted_digest(path: str, *, _mechanism=mechanism) -> str:
                observations[_mechanism]["digest_calls"] += 1
                observations[_mechanism]["content_bytes_requested"] += (
                    os.stat(path, follow_symlinks=False).st_size
                )
                return original_digest(path)

            with mock.patch.object(runner, "_file_sha256", counted_digest):
                timings[mechanism].append(_time_call(
                    operations[mechanism], str(directory), expected, False))

    materialized_time = _summary(timings["materialized"])
    streaming_time = _summary(timings["streaming"])
    return {
        "object_bytes": object_bytes,
        "timing_samples_per_mechanism": samples,
        "unexpected_hash": unexpected_hash,
        "expected_absent_hash": next(iter(expected)),
        "materialized": {
            "samples_us": timings["materialized"],
            "timing_us": materialized_time,
            **observations["materialized"],
        },
        "streaming": {
            "samples_us": timings["streaming"],
            "timing_us": streaming_time,
            **observations["streaming"],
        },
        "comparison": {
            "materialized_to_streaming_median_time_ratio": (
                materialized_time["median"] / streaming_time["median"]
            ),
            "digest_calls_avoided": (
                observations["materialized"]["digest_calls"]
                - observations["streaming"]["digest_calls"]
            ),
            "content_bytes_requested_avoided": (
                observations["materialized"]["content_bytes_requested"]
                - observations["streaming"]["content_bytes_requested"]
            ),
        },
    }


def _integrity_checks(root: Path) -> dict[str, bool]:
    content = b"F339 no-follow identity"
    work_hash = hashlib.sha256(content).hexdigest()
    target = root / "outside-object"
    target.write_bytes(content)
    alias = root / "alias"
    alias.symlink_to(target)
    stage = root / "symlink-stage"
    stage.mkdir()
    (stage / work_hash).symlink_to(target)
    return {
        "platform_exposes_o_nofollow": getattr(os, "O_NOFOLLOW", None) is not None,
        "regular_inode_digest_matches": runner._file_sha256(str(target)) == work_hash,
        "final_symlink_digest_is_rejected": runner._file_sha256(str(alias)) == "",
        "staged_symlink_is_rejected": runner._verified_staged_output_hashes(
            str(stage), {work_hash}) is None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--memory-samples", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--unexpected-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    if (
        args.samples < 1
        or args.memory_samples < 1
        or args.warmups < 0
        or args.unexpected_bytes < 1
    ):
        raise SystemExit("sample, warmup, and object sizes must be valid")

    with tempfile.TemporaryDirectory(prefix="symcc-f339-staging-") as tmp:
        root = Path(tmp)
        filesystem = _filesystem_type(tmp)
        valid = _valid_namespace_measurements(
            root,
            samples=args.samples,
            memory_samples=args.memory_samples,
            warmups=args.warmups,
        )
        unexpected = _unexpected_object_measurement(
            root,
            samples=args.samples,
            warmups=args.warmups,
            object_bytes=args.unexpected_bytes,
        )
        checks = _integrity_checks(root)

    result = {
        "schema": "symcc-f339-streaming-nofollow-staging-cost-v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "filesystem": filesystem,
        "configuration": {
            "valid_object_counts": [128, 1024, 4096],
            "timing_samples_per_mechanism_and_size": args.samples,
            "memory_samples_per_mechanism_and_size": args.memory_samples,
            "warmups_per_mechanism_and_case": args.warmups,
            "timing_interleaved_and_alternating": True,
            "memory_interleaved_and_alternating": True,
            "directory_creation_excluded": True,
            "valid_case_content_hashing_stubbed": True,
            "valid_case_stub_semantics": "digest equals basename",
            "unexpected_case_uses_production_descriptor_digest": True,
        },
        "valid_namespace": valid,
        "unexpected_object": unexpected,
        "integrity_checks": checks,
        "all_integrity_checks_passed": all(checks.values()),
        "proof_boundary": (
            "Local Python microbenchmark on one overlayfs host. The valid-set "
            "case isolates scan/control-structure cost by replacing content "
            "hashing with basename identity; it does not measure end-to-end "
            "corpus or solver throughput. The unexpected-object case uses the "
            "production descriptor/no-follow digest but reads a page-cached "
            "local file. tracemalloc excludes RSS, kernel buffers, filesystem "
            "cache, and some native allocations."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
