#!/usr/bin/env python3
"""Measure the local cost of persisting completed-epoch disappearance."""

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


def _make_tree(parent: Path) -> Path:
    tree = parent / (".standalone-work-" + "f334" * 16)
    shard = tree / "016"
    shard.mkdir(parents=True)
    (tree / "state.json").write_bytes(b'{"schema":"benchmark"}\n')
    (shard / ("a" * 64 + ".json")).write_bytes(b"{}\n")
    return tree


def _timed(operation) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1000.0


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[max(0, int(0.95 * len(ordered)) - 1)]
    return {
        "minimum_us": ordered[0],
        "median_us": statistics.median(ordered),
        "p95_us": p95,
        "maximum_us": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmups", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("sample counts must be positive")

    legacy_samples: list[float] = []
    durable_samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f334-cleanup-cost-") as tmp:
        root = Path(tmp)
        for index in range(args.warmups + args.repetitions):
            legacy_parent = root / f"legacy-{index:04d}"
            durable_parent = root / f"durable-{index:04d}"
            legacy_tree = _make_tree(legacy_parent)
            durable_tree = _make_tree(durable_parent)
            operations = (
                (
                    lambda: shutil.rmtree(legacy_tree),
                    lambda: durable_rmtree(str(durable_tree)),
                )
                if index % 2 == 0 else
                (
                    lambda: durable_rmtree(str(durable_tree)),
                    lambda: shutil.rmtree(legacy_tree),
                )
            )
            observations = [_timed(operation) for operation in operations]
            shutil.rmtree(legacy_parent)
            shutil.rmtree(durable_parent)
            if index < args.warmups:
                continue
            if index % 2 == 0:
                legacy_samples.append(observations[0])
                durable_samples.append(observations[1])
            else:
                durable_samples.append(observations[0])
                legacy_samples.append(observations[1])

    legacy = _summary(legacy_samples)
    durable = _summary(durable_samples)
    result = {
        "schema": "symcc-f334-durable-cleanup-cost-v1",
        "host": platform.node(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "mechanism_only": True,
        "proof_boundary": (
            "Interleaved local-overlayfs removal of a two-file synthetic epoch. "
            "The delta measures one parent-directory fsync, not MPI barrier, "
            "DSE throughput, solver time, coverage, remote storage, or LAVA-M."
        ),
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "legacy_unacknowledged_rmtree": {
            "samples_us": legacy_samples,
            **legacy,
        },
        "durable_rmtree": {
            "samples_us": durable_samples,
            **durable,
        },
        "median_added_us": durable["median_us"] - legacy["median_us"],
        "median_ratio": durable["median_us"] / legacy["median_us"],
        "all_samples_completed": (
            len(legacy_samples) == args.repetitions
            and len(durable_samples) == args.repetitions
            and all(value > 0.0 for value in legacy_samples + durable_samples)
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
