#!/usr/bin/env python3
"""Fresh-process read-all versus F345 stable input hashing benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
if str(UTIL) not in sys.path:
    sys.path.insert(0, str(UTIL))

from distributed_state import stable_regular_file_snapshot  # noqa: E402


SCALES = (32 * 1024 * 1024, 128 * 1024 * 1024)
MECHANISMS = ("legacy", "stable")
RANDOM_SEED = 0xF345


def _create_sparse_fixture(path: Path, size: int) -> None:
    with open(path, "wb") as stream:
        stream.seek(size - 1)
        stream.write(b"\xa5")


def _sample(mechanism: str, input_bytes: int) -> dict[str, int | float | str]:
    with tempfile.TemporaryDirectory(prefix="symcc-f345-cost-") as tmp:
        source = Path(tmp) / "input"
        _create_sparse_fixture(source, input_bytes)
        tracemalloc.start()
        started = time.perf_counter_ns()
        try:
            if mechanism == "legacy":
                with open(source, "rb") as stream:
                    content = stream.read()
                digest = hashlib.sha256(content).hexdigest()
                observed = len(content)
                del content
            else:
                snapshot = stable_regular_file_snapshot(
                    str(source),
                    max_bytes=input_bytes,
                )
                digest = snapshot.sha256
                observed = snapshot.identity.size
            elapsed_us = (time.perf_counter_ns() - started) / 1000.0
            _current, traced_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return {
            "mechanism": mechanism,
            "input_bytes": input_bytes,
            "observed_bytes": observed,
            "sha256": digest,
            "elapsed_us": elapsed_us,
            "traced_peak_bytes": traced_peak,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }


def _run_sample_subprocess(mechanism: str, input_bytes: int) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--sample",
            mechanism,
            str(input_bytes),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"sample failed ({completed.returncode}): {completed.stderr}")
    return json.loads(completed.stdout)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _summary(samples: list[dict]) -> dict[str, float]:
    result = {}
    for field in ("elapsed_us", "traced_peak_bytes", "max_rss_kib"):
        values = [float(sample[field]) for sample in samples]
        result[f"{field}_median"] = statistics.median(values)
        result[f"{field}_p95"] = _p95(values)
    return result


def _filesystem_type(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _benchmark(warmups: int, repetitions: int) -> dict:
    raw = {
        str(scale): {mechanism: [] for mechanism in MECHANISMS}
        for scale in SCALES
    }
    rng = random.Random(RANDOM_SEED)
    for scale in SCALES:
        for _ in range(warmups):
            order = list(MECHANISMS)
            rng.shuffle(order)
            for mechanism in order:
                _run_sample_subprocess(mechanism, scale)
        for _ in range(repetitions):
            order = list(MECHANISMS)
            rng.shuffle(order)
            for mechanism in order:
                raw[str(scale)][mechanism].append(
                    _run_sample_subprocess(mechanism, scale))

    summaries = {
        scale: {
            mechanism: _summary(samples)
            for mechanism, samples in mechanisms.items()
        }
        for scale, mechanisms in raw.items()
    }
    ratios = {}
    exact = True
    for scale in SCALES:
        key = str(scale)
        legacy = summaries[key]["legacy"]
        stable = summaries[key]["stable"]
        ratios[key] = {
            "traced_peak_old_over_new": (
                legacy["traced_peak_bytes_median"]
                / stable["traced_peak_bytes_median"]
            ),
            "max_rss_old_over_new": (
                legacy["max_rss_kib_median"]
                / stable["max_rss_kib_median"]
            ),
            "elapsed_new_over_old": (
                stable["elapsed_us_median"] / legacy["elapsed_us_median"]
            ),
        }
        vectors = {
            mechanism: {sample["sha256"] for sample in samples}
            for mechanism, samples in raw[key].items()
        }
        exact = exact and all(
            len(samples) == repetitions
            and all(
                sample["input_bytes"] == scale
                and sample["observed_bytes"] == scale
                for sample in samples
            )
            for samples in raw[key].values()
        )
        exact = exact and (
            len(vectors["legacy"]) == 1
            and vectors["legacy"] == vectors["stable"]
        )

    return {
        "schema": "symcc-f345-stable-hybrid-input-cost-v1",
        "configuration": {
            "filesystem_type": _filesystem_type(REPO),
            "input_bytes_per_scale": list(SCALES),
            "process_model": "one fresh Python subprocess per sample",
            "random_seed": RANDOM_SEED,
            "samples_per_mechanism_per_scale": repetitions,
            "schedule": "deterministically interleaved by retained round",
            "source": "one sparse regular input file",
            "warmups_per_mechanism_per_scale": warmups,
        },
        "checks": {
            "sample_counts_sizes_and_hashes_exact": exact,
        },
        "all_checks_passed": exact,
        "raw_samples": raw,
        "summaries": summaries,
        "ratios": ratios,
        "proof_boundary": (
            "This fresh-process mechanism benchmark compares read-all SHA-256 "
            "with the production F345 bounded no-follow stable snapshot. It "
            "does not execute MPI, a target, afl-showmap, a symbolic solver, "
            "or a fuzzing campaign, and makes no coverage, throughput, bug-"
            "discovery, or LAVA-M uplift claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", nargs=2, metavar=("MECHANISM", "BYTES"))
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sample:
        mechanism, raw_size = args.sample
        if mechanism not in MECHANISMS:
            parser.error("invalid mechanism")
        print(json.dumps(_sample(mechanism, int(raw_size)), sort_keys=True))
        return 0
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("warmups must be non-negative and repetitions positive")
    result = _benchmark(args.warmups, args.repetitions)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
