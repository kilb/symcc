#!/usr/bin/env python3
"""Compare legacy read-all seed publication with production F341 admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import mpi_concolic_execution as runner  # noqa: E402


def _legacy_publish(source: Path, destination: Path) -> tuple[str, int]:
    with source.open("rb") as stream:
        content = stream.read()
    work_hash = hashlib.sha256(content).hexdigest()
    runner._atomic_write(str(destination / work_hash), content)
    if runner._file_sha256(str(destination / work_hash)) != work_hash:
        raise RuntimeError("legacy input digest mismatch")
    return work_hash, len(content)


def _streaming_publish(source: Path, destination: Path) -> tuple[str, int]:
    snapshot = runner._input_file_snapshot(
        str(source), max_bytes=source.stat().st_size)
    if snapshot is None:
        raise RuntimeError("stable input snapshot failed")
    return runner._stream_publish_input_file(
        str(source),
        str(destination),
        expected_hash=snapshot[0],
        expected_identity=snapshot[2],
        max_bytes=snapshot[1],
    )


def _worker(mechanism: str, size_bytes: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="symcc-f341-bench-") as tmp:
        root = Path(tmp)
        source = root / "source"
        with source.open("wb") as stream:
            stream.truncate(size_bytes)
        destination = root / "corpus"
        destination.mkdir()

        tracemalloc.start()
        started = time.perf_counter_ns()
        if mechanism == "legacy":
            work_hash, observed_size = _legacy_publish(source, destination)
        elif mechanism == "streaming":
            work_hash, observed_size = _streaming_publish(source, destination)
        else:
            raise ValueError(f"unknown mechanism: {mechanism}")
        elapsed_ns = time.perf_counter_ns() - started
        _current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if observed_size != size_bytes or len(work_hash) != 64:
            raise RuntimeError("input admission result is incomplete")
        return {
            "mechanism": mechanism,
            "size_bytes": size_bytes,
            "elapsed_us": elapsed_ns / 1000.0,
            "traced_peak_bytes": traced_peak,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(
        len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _summarize(samples: list[dict[str, object]]) -> dict[str, float]:
    elapsed = [float(sample["elapsed_us"]) for sample in samples]
    traced = [float(sample["traced_peak_bytes"]) for sample in samples]
    rss = [float(sample["max_rss_kib"]) for sample in samples]
    return {
        "elapsed_us_median": statistics.median(elapsed),
        "elapsed_us_p95": _percentile(elapsed, 0.95),
        "traced_peak_bytes_median": statistics.median(traced),
        "traced_peak_bytes_p95": _percentile(traced, 0.95),
        "max_rss_kib_median": statistics.median(rss),
        "max_rss_kib_p95": _percentile(rss, 0.95),
    }


def _invoke_worker(mechanism: str, size_bytes: int) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker", mechanism,
            "--size-bytes", str(size_bytes),
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120.0,
        check=True,
    )
    return json.loads(completed.stdout)


def _orchestrate(output: Path, warmups: int, repetitions: int) -> None:
    scales = (8 * 1024 * 1024, 32 * 1024 * 1024)
    raw: dict[str, dict[str, list[dict[str, object]]]] = {}
    summaries: dict[str, dict[str, dict[str, float]]] = {}
    ratios: dict[str, dict[str, float]] = {}
    for size_bytes in scales:
        key = str(size_bytes)
        raw[key] = {"legacy": [], "streaming": []}
        for index in range(warmups + repetitions):
            order = (
                ("legacy", "streaming") if index % 2 == 0
                else ("streaming", "legacy")
            )
            for mechanism in order:
                sample = _invoke_worker(mechanism, size_bytes)
                if index >= warmups:
                    raw[key][mechanism].append(sample)
        summaries[key] = {
            mechanism: _summarize(raw[key][mechanism])
            for mechanism in ("legacy", "streaming")
        }
        old = summaries[key]["legacy"]
        new = summaries[key]["streaming"]
        ratios[key] = {
            "traced_peak_old_over_new": (
                old["traced_peak_bytes_median"]
                / new["traced_peak_bytes_median"]
            ),
            "max_rss_old_over_new": (
                old["max_rss_kib_median"]
                / new["max_rss_kib_median"]
            ),
            "elapsed_old_over_new": (
                old["elapsed_us_median"] / new["elapsed_us_median"]
            ),
        }
    result = {
        "schema": "symcc-f341-input-admission-cost-v1",
        "configuration": {
            "warmups_per_mechanism_per_scale": warmups,
            "samples_per_mechanism_per_scale": repetitions,
            "scales_bytes": list(scales),
            "source": "sparse zero-filled regular file on local overlayfs",
            "process_model": "one fresh Python subprocess per sample",
        },
        "raw_samples": raw,
        "summaries": summaries,
        "ratios": ratios,
        "all_checks_passed": all(
            len(raw[str(size)][mechanism]) == repetitions
            for size in scales
            for mechanism in ("legacy", "streaming")
        ),
        "proof_boundary": (
            "The benchmark isolates owner-side seed admission. Legacy reads "
            "the complete source into one Python bytes object before atomic "
            "publication. F341 hashes a stable source snapshot, then performs "
            "production fixed-window publication and destination verification. "
            "The extra source pass is part of rendezvous-owner identity safety. "
            "Results describe local mechanism memory and time, not campaign "
            "throughput, solver, coverage, multi-host storage, bug discovery, "
            "or LAVA-M."
        ),
    }
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--worker", choices=("legacy", "streaming"))
    parser.add_argument("--size-bytes", type=int)
    args = parser.parse_args()
    if args.worker:
        if args.size_bytes is None or args.size_bytes < 1:
            parser.error("--worker requires positive --size-bytes")
        print(json.dumps(_worker(args.worker, args.size_bytes), sort_keys=True))
        return
    if args.output is None:
        parser.error("orchestration requires --output")
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("warmups must be non-negative and repetitions positive")
    _orchestrate(args.output, args.warmups, args.repetitions)


if __name__ == "__main__":
    main()
