#!/usr/bin/env python3
"""Compare durable recursive deletion with durable name retirement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import statistics
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from distributed_state import durable_rmtree  # noqa: E402
from mpi_concolic_execution import (  # noqa: E402
    _cleanup_completed_work_state,
)


BASELINE_EPOCH = "a" * 64
RETIREMENT_EPOCH = "b" * 64


def _make_tree(parent: Path, epoch: str, payload_files: int) -> Path:
    tree = parent / f".standalone-work-{epoch}"
    tree.mkdir(parents=True)
    (tree / "state.json").write_bytes(b'{"schema":"benchmark"}\n')
    for index in range(payload_files):
        shard = tree / f"{index % 16:03d}"
        shard.mkdir(exist_ok=True)
        (shard / f"{index:08x}.state").write_bytes(
            index.to_bytes(8, "big") * 4)
    return tree


def _timed(operation) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1000.0


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "minimum_us": ordered[0],
        "median_us": statistics.median(ordered),
        "p95_us": ordered[max(0, int(0.95 * len(ordered)) - 1)],
        "maximum_us": ordered[-1],
    }


def _measure_scale(
    root: Path,
    *,
    payload_files: int,
    warmups: int,
    repetitions: int,
) -> dict:
    deletion_samples: list[float] = []
    retirement_samples: list[float] = []
    for index in range(warmups + repetitions):
        sample_root = root / f"n{payload_files:04d}-{index:04d}"
        deletion_parent = sample_root / "delete"
        retirement_parent = sample_root / "retire"
        deletion_tree = _make_tree(
            deletion_parent, BASELINE_EPOCH, payload_files)
        retirement_tree = _make_tree(
            retirement_parent, RETIREMENT_EPOCH, payload_files)
        operations = (
            (
                lambda: durable_rmtree(str(deletion_tree)),
                lambda: _cleanup_completed_work_state(
                    str(retirement_tree),
                    str(retirement_parent),
                    remove_shared_dir=False,
                ),
            )
            if index % 2 == 0 else
            (
                lambda: _cleanup_completed_work_state(
                    str(retirement_tree),
                    str(retirement_parent),
                    remove_shared_dir=False,
                ),
                lambda: durable_rmtree(str(deletion_tree)),
            )
        )
        observations = [_timed(operation) for operation in operations]
        shutil.rmtree(sample_root)
        if index < warmups:
            continue
        if index % 2 == 0:
            deletion_samples.append(observations[0])
            retirement_samples.append(observations[1])
        else:
            retirement_samples.append(observations[0])
            deletion_samples.append(observations[1])

    deletion = _summary(deletion_samples)
    retirement = _summary(retirement_samples)
    return {
        "payload_files": payload_files,
        "total_files_per_tree": payload_files + 1,
        "durable_recursive_deletion": {
            "samples_us": deletion_samples,
            **deletion,
        },
        "durable_name_retirement": {
            "samples_us": retirement_samples,
            **retirement,
        },
        "median_saved_us": deletion["median_us"] - retirement["median_us"],
        "median_speedup": deletion["median_us"] / retirement["median_us"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("sample counts must be positive")

    with tempfile.TemporaryDirectory(prefix="symcc-f335-retire-cost-") as tmp:
        root = Path(tmp)
        scales = [
            _measure_scale(
                root,
                payload_files=payload_files,
                warmups=args.warmups,
                repetitions=args.repetitions,
            )
            for payload_files in (2, 128, 1024)
        ]

    samples_complete = all(
        len(scale[mode]["samples_us"]) == args.repetitions
        and all(value > 0.0 for value in scale[mode]["samples_us"])
        for scale in scales
        for mode in ("durable_recursive_deletion", "durable_name_retirement")
    )
    result = {
        "schema": "symcc-f335-atomic-epoch-retirement-cost-v1",
        "host": platform.node(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "mechanism_only": True,
        "proof_boundary": (
            "Interleaved local-filesystem measurements compare recursive "
            "tree deletion plus parent fsync with same-directory rename plus "
            "parent fsync. Tree construction and deferred reclamation are "
            "outside the timed region. Results are not MPI, end-to-end DSE, "
            "remote storage, solver, coverage, or LAVA-M uplift."
        ),
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "scales": scales,
        "all_samples_completed": samples_complete,
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
