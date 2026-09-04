#!/usr/bin/env python3
"""Fresh-process legacy versus F344 hybrid result collection benchmark."""

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

import mpi_fuzzing_helper as runner  # noqa: E402


OBJECT_BYTES = 1024 * 1024
SCALES = (32, 128)
MECHANISMS = ("legacy", "bounded")
RANDOM_SEED = 0xF344


def _create_fixture(directory: Path, objects: int) -> None:
    directory.mkdir()
    for index in range(objects):
        path = directory / f"case-{index:06d}"
        with open(path, "wb") as stream:
            stream.seek(OBJECT_BYTES - 1)
            stream.write(bytes((index & 0xFF,)))


def _vector(records: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content_digest in sorted(records):
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        digest.update(content_digest)
    return digest.hexdigest()


def _legacy_collect(directory: Path) -> tuple[int, int, str]:
    entries = list(os.scandir(directory))
    seen: set[bytes] = set()
    retained: list[tuple[str, bytes, bytes]] = []
    for entry in entries:
        if (entry.name.startswith(".") or entry.name.endswith(".hints")
                or not entry.is_file()):
            continue
        with open(entry.path, "rb") as stream:
            content = stream.read()
        content_digest = hashlib.blake2b(content, digest_size=16).digest()
        if content_digest in seen:
            continue
        seen.add(content_digest)
        retained.append((entry.name, content_digest, content))
    records = [(name, content_digest)
               for name, content_digest, _content in retained]
    return len(retained), sum(len(content) for _, _, content in retained), _vector(records)


def _bounded_collect(directory: Path, objects: int) -> tuple[int, int, str]:
    candidates, hints = runner._scan_worker_output_candidates(
        str(directory),
        max_objects=objects,
        max_bytes=objects * OBJECT_BYTES,
        max_object_bytes=OBJECT_BYTES,
    )
    if hints:
        raise RuntimeError("unexpected hint candidates")
    seen: set[bytes] = set()
    retained: list[tuple[runner._WorkerOutputCandidate, bytes]] = []
    for candidate in candidates:
        content = runner._read_worker_output_snapshot(
            candidate, max_bytes=OBJECT_BYTES)
        if content is None:
            raise RuntimeError("unstable first-pass candidate")
        content_digest = hashlib.blake2b(content, digest_size=16).digest()
        if content_digest not in seen:
            seen.add(content_digest)
            retained.append((candidate, content_digest))
        del content

    records: list[tuple[str, bytes]] = []
    total_bytes = 0
    for candidate, expected_digest in retained:
        content = runner._read_worker_output_snapshot(
            candidate, max_bytes=OBJECT_BYTES)
        if content is None:
            raise RuntimeError("unstable second-pass candidate")
        observed_digest = hashlib.blake2b(content, digest_size=16).digest()
        if observed_digest != expected_digest:
            raise RuntimeError("candidate digest drift")
        records.append((candidate.name, observed_digest))
        total_bytes += len(content)
        del content
    return len(retained), total_bytes, _vector(records)


def _sample(mechanism: str, objects: int) -> dict[str, int | float | str]:
    with tempfile.TemporaryDirectory(prefix="symcc-f344-cost-") as tmp:
        output = Path(tmp) / "output"
        _create_fixture(output, objects)
        tracemalloc.start()
        started = time.perf_counter_ns()
        try:
            if mechanism == "legacy":
                count, output_bytes, vector = _legacy_collect(output)
            else:
                count, output_bytes, vector = _bounded_collect(output, objects)
            elapsed_us = (time.perf_counter_ns() - started) / 1000.0
            _current, traced_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return {
            "mechanism": mechanism,
            "objects": objects,
            "object_bytes": OBJECT_BYTES,
            "output_bytes": output_bytes,
            "output_vector_sha256": vector,
            "elapsed_us": elapsed_us,
            "traced_peak_bytes": traced_peak,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "retained_objects": count,
        }


def _run_sample_subprocess(mechanism: str, objects: int) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--sample",
        mechanism,
        str(objects),
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=REPO,
        env=environment,
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
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _summary(samples: list[dict]) -> dict[str, float]:
    result: dict[str, float] = {}
    for field in ("elapsed_us", "traced_peak_bytes", "max_rss_kib"):
        values = [float(sample[field]) for sample in samples]
        result[f"{field}_median"] = statistics.median(values)
        result[f"{field}_p95"] = _p95(values)
    return result


def _benchmark(warmups: int, repetitions: int) -> dict:
    raw: dict[str, dict[str, list[dict]]] = {
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
    checks = {
        "sample_counts_exact": True,
        "all_cardinalities_and_bytes_exact": True,
        "all_output_vectors_equivalent": True,
    }
    correctness = {}
    for scale in SCALES:
        key = str(scale)
        old = summaries[key]["legacy"]
        new = summaries[key]["bounded"]
        ratios[key] = {
            "traced_peak_old_over_new": (
                old["traced_peak_bytes_median"]
                / new["traced_peak_bytes_median"]
            ),
            "max_rss_old_over_new": (
                old["max_rss_kib_median"] / new["max_rss_kib_median"]
            ),
            "elapsed_new_over_old": (
                new["elapsed_us_median"] / old["elapsed_us_median"]
            ),
        }
        vectors = {
            mechanism: sorted({sample["output_vector_sha256"]
                               for sample in samples})
            for mechanism, samples in raw[key].items()
        }
        exact = all(
            len(samples) == repetitions
            and all(
                sample["objects"] == scale
                and sample["retained_objects"] == scale
                and sample["object_bytes"] == OBJECT_BYTES
                and sample["output_bytes"] == scale * OBJECT_BYTES
                for sample in samples
            )
            for samples in raw[key].values()
        )
        equivalent = (
            len(vectors["legacy"]) == 1
            and vectors["legacy"] == vectors["bounded"]
        )
        checks["sample_counts_exact"] &= all(
            len(samples) == repetitions for samples in raw[key].values())
        checks["all_cardinalities_and_bytes_exact"] &= exact
        checks["all_output_vectors_equivalent"] &= equivalent
        correctness[key] = {
            "exact_cardinalities_and_bytes": exact,
            "exact_vector_equivalence": equivalent,
            "vectors": vectors,
        }

    return {
        "schema": "symcc-f344-bounded-hybrid-worker-result-cost-v1",
        "configuration": {
            "filesystem_type": _filesystem_type(REPO),
            "object_bytes": OBJECT_BYTES,
            "objects_per_scale": list(SCALES),
            "process_model": "one fresh Python subprocess per sample",
            "random_seed": RANDOM_SEED,
            "samples_per_mechanism_per_scale": repetitions,
            "schedule": "deterministically interleaved by retained round",
            "source": "sparse regular worker output files",
            "warmups_per_mechanism_per_scale": warmups,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "correctness": correctness,
        "raw_samples": raw,
        "summaries": summaries,
        "ratios": ratios,
        "proof_boundary": (
            "This fresh-process mechanism benchmark compares the former "
            "list(scandir)+read-all retained-content collector with F344's "
            "production preflight and stable two-pass per-object primitives. "
            "All outputs are regular sparse files and modeled as coverage "
            "redundant after the second read. It does not invoke MPI transport, "
            "afl-showmap, a symbolic solver, or a fuzzing campaign, and makes no "
            "coverage, throughput, bug-discovery, or LAVA-M uplift claim."
        ),
    }


def _filesystem_type(path: Path) -> str:
    try:
        output = subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return output or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", nargs=2, metavar=("MECHANISM", "OBJECTS"))
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sample:
        mechanism, raw_objects = args.sample
        if mechanism not in MECHANISMS:
            parser.error("invalid mechanism")
        print(json.dumps(_sample(mechanism, int(raw_objects)), sort_keys=True))
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
