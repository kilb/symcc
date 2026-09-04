#!/usr/bin/env python3
"""Compare legacy read-all simulation with production F343 streaming."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
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


MUTATIONS = 5
RANDOM_SEED = 0xF343


def _legacy_mutations(
    source: Path,
    output: Path,
    rng: random.Random,
) -> list[Path]:
    with source.open("rb") as stream:
        data = stream.read()
    if not data:
        return []
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(MUTATIONS):
        mutated = bytearray(data)
        mutation_count = rng.randint(1, min(3, len(mutated)))
        for _ in range(mutation_count):
            position = rng.randint(0, len(mutated) - 1)
            mutated[position] = rng.randint(0, 255)
        path = output / f"sim_{index:04d}"
        with path.open("wb") as stream:
            stream.write(bytes(mutated))
        paths.append(path)
    return paths


def _vector_digest(paths: list[Path]) -> str:
    object_hashes = []
    for path in paths:
        digest = runner._file_sha256(str(path))
        if not digest:
            raise RuntimeError(f"invalid benchmark output: {path}")
        object_hashes.append(digest)
    return hashlib.sha256("".join(object_hashes).encode("ascii")).hexdigest()


def _worker(mechanism: str, size_bytes: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="symcc-f343-bench-") as tmp:
        root = Path(tmp)
        source = root / "source"
        with source.open("wb") as stream:
            stream.truncate(size_bytes)
        output = root / "output"

        tracemalloc.start()
        started = time.perf_counter_ns()
        if mechanism == "legacy":
            paths = _legacy_mutations(
                source, output, random.Random(RANDOM_SEED))
        elif mechanism == "streaming":
            paths = [Path(path) for path in runner._simulate_mutations(
                str(source),
                str(output),
                MUTATIONS,
                max_objects=MUTATIONS,
                max_bytes=size_bytes * MUTATIONS,
                _rng=random.Random(RANDOM_SEED),
            )]
        else:
            raise ValueError(f"unknown mechanism: {mechanism}")
        elapsed_ns = time.perf_counter_ns() - started
        _current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        sizes = [path.stat().st_size for path in paths]
        vector_digest = _vector_digest(paths)
        if len(paths) != MUTATIONS or sizes != [size_bytes] * MUTATIONS:
            raise RuntimeError("simulation output set is incomplete")
        return {
            "mechanism": mechanism,
            "size_bytes": size_bytes,
            "mutation_count": MUTATIONS,
            "output_bytes": sum(sizes),
            "output_vector_sha256": vector_digest,
            "elapsed_us": elapsed_ns / 1000.0,
            "traced_peak_bytes": traced_peak,
            "max_rss_kib": max_rss,
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
        timeout=180.0,
        check=True,
    )
    return json.loads(completed.stdout)


def _filesystem_type() -> str:
    try:
        return subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "-T", str(REPO)],
            text=True,
            timeout=5.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _orchestrate(output: Path, warmups: int, repetitions: int) -> None:
    scales = (8 * 1024 * 1024, 32 * 1024 * 1024)
    mechanisms = ("legacy", "streaming")
    raw: dict[str, dict[str, list[dict[str, object]]]] = {}
    summaries: dict[str, dict[str, dict[str, float]]] = {}
    ratios: dict[str, dict[str, float]] = {}
    correctness: dict[str, dict[str, object]] = {}
    for size_bytes in scales:
        key = str(size_bytes)
        raw[key] = {mechanism: [] for mechanism in mechanisms}
        for index in range(warmups + repetitions):
            order = mechanisms if index % 2 == 0 else tuple(reversed(mechanisms))
            for mechanism in order:
                sample = _invoke_worker(mechanism, size_bytes)
                if index >= warmups:
                    raw[key][mechanism].append(sample)
        summaries[key] = {
            mechanism: _summarize(raw[key][mechanism])
            for mechanism in mechanisms
        }
        old = summaries[key]["legacy"]
        new = summaries[key]["streaming"]
        ratios[key] = {
            "traced_peak_old_over_new": (
                old["traced_peak_bytes_median"]
                / new["traced_peak_bytes_median"]
            ),
            "max_rss_old_over_new": (
                old["max_rss_kib_median"] / new["max_rss_kib_median"]
            ),
            "elapsed_old_over_new": (
                old["elapsed_us_median"] / new["elapsed_us_median"]
            ),
        }
        vectors = {
            mechanism: sorted({
                str(sample["output_vector_sha256"])
                for sample in raw[key][mechanism]
            })
            for mechanism in mechanisms
        }
        correctness[key] = {
            "legacy_vectors": vectors["legacy"],
            "streaming_vectors": vectors["streaming"],
            "exact_vector_equivalence": (
                len(vectors["legacy"]) == 1
                and vectors["legacy"] == vectors["streaming"]
            ),
            "all_output_cardinalities_and_sizes_exact": all(
                sample["mutation_count"] == MUTATIONS
                and sample["output_bytes"] == size_bytes * MUTATIONS
                for mechanism in mechanisms
                for sample in raw[key][mechanism]
            ),
        }

    small = str(scales[0])
    large = str(scales[1])
    scaling = {
        mechanism: {
            "input_growth": scales[1] / scales[0],
            "traced_peak_growth": (
                summaries[large][mechanism]["traced_peak_bytes_median"]
                / summaries[small][mechanism]["traced_peak_bytes_median"]
            ),
            "max_rss_growth": (
                summaries[large][mechanism]["max_rss_kib_median"]
                / summaries[small][mechanism]["max_rss_kib_median"]
            ),
        }
        for mechanism in mechanisms
    }
    sample_counts_exact = all(
        len(raw[str(size)][mechanism]) == repetitions
        for size in scales
        for mechanism in mechanisms
    )
    result = {
        "schema": "symcc-f343-bounded-streaming-simulation-cost-v1",
        "configuration": {
            "warmups_per_mechanism_per_scale": warmups,
            "samples_per_mechanism_per_scale": repetitions,
            "scales_bytes": list(scales),
            "mutations_per_input": MUTATIONS,
            "random_seed": RANDOM_SEED,
            "stream_chunk_bytes": runner._RESULT_STREAM_CHUNK_BYTES,
            "output_batch_size": runner._SIMULATION_OUTPUT_BATCH_SIZE,
            "source": "sparse zero-filled regular file",
            "filesystem_type": _filesystem_type(),
            "process_model": "one fresh Python subprocess per sample",
            "schedule": "deterministically interleaved by retained round",
        },
        "raw_samples": raw,
        "summaries": summaries,
        "ratios": ratios,
        "scaling": scaling,
        "correctness": correctness,
        "checks": {
            "sample_counts_exact": sample_counts_exact,
            "all_scales_byte_exact": all(
                bool(value["all_output_cardinalities_and_sizes_exact"])
                for value in correctness.values()
            ),
            "all_scales_vector_equivalent": all(
                bool(value["exact_vector_equivalence"])
                for value in correctness.values()
            ),
        },
        "proof_boundary": (
            "This microbenchmark isolates five synthetic mutation outputs. "
            "Legacy is the former read-all/bytearray/bytes implementation; "
            "F343 invokes the production fixed-window, descriptor-batched, "
            "temporary-first implementation with an identical RNG stream. "
            "tracemalloc measures Python allocator peak, ru_maxrss is fresh-"
            "process peak, and elapsed time reflects this host and filesystem. "
            "The result is mechanism evidence, not MPI scale, solver, coverage, "
            "campaign throughput, bug discovery, or LAVA-M uplift evidence."
        ),
    }
    result["all_checks_passed"] = all(result["checks"].values())
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
