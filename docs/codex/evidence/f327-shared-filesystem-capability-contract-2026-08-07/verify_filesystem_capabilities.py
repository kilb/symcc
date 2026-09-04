#!/usr/bin/env python3
"""Measure and stress the F327 executable shared-filesystem contract."""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import statistics
import sys
import tempfile
import time
import traceback


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import probe_shared_state_filesystem  # noqa: E402


CAPABILITY_FLAGS = (
    "file_fsync",
    "directory_fsync",
    "same_directory_replace",
    "cross_directory_replace",
    "publication_replace",
    "hard_link",
    "durable_unlink",
    "advisory_lock_exclusion",
    "advisory_lock_release",
)
PROBE_PREFIXES = (
    ".symcc-fs-probe-",
    ".symcc-fs-publication-probe-",
)


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _worker(
    state_root: str,
    publication_root: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait()
    started = time.monotonic_ns()
    try:
        capability = probe_shared_state_filesystem(
            state_root,
            publication_root=publication_root,
            timeout=5.0,
        ).snapshot()
        results.put({
            "ok": True,
            "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
            "capability": capability,
        })
    except BaseException as error:  # Evidence must report child failures.
        results.put({
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        })
        raise


def _residue(root: str) -> list[str]:
    found: list[str] = []
    for directory, subdirectories, files in os.walk(root):
        for name in (*subdirectories, *files):
            if name.startswith(PROBE_PREFIXES):
                found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequential", type=int, default=20)
    parser.add_argument("--waves", type=int, default=5)
    parser.add_argument("--processes", type=int, default=8)
    args = parser.parse_args()
    if min(args.sequential, args.waves, args.processes) < 1:
        parser.error("all counts must be positive")

    context = multiprocessing.get_context("spawn")
    sequential_ms: list[float] = []
    concurrent_ms: list[float] = []
    snapshots: list[dict[str, object]] = []
    child_exit_codes: list[int] = []
    failures: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="symcc-f327-") as temporary:
        state_root = os.path.join(temporary, "state")
        publication_root = os.path.join(temporary, "corpus")

        for _ in range(args.sequential):
            started = time.monotonic_ns()
            snapshot = probe_shared_state_filesystem(
                state_root,
                publication_root=publication_root,
                timeout=5.0,
            ).snapshot()
            sequential_ms.append(
                (time.monotonic_ns() - started) / 1_000_000)
            snapshots.append(snapshot)

        for _ in range(args.waves):
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_worker,
                    args=(state_root, publication_root, start, results),
                )
                for _ in range(args.processes)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)
                child_exit_codes.append(
                    -999 if process.exitcode is None else process.exitcode)
            for _ in processes:
                try:
                    outcome = results.get(timeout=2.0)
                except queue.Empty:
                    failures.append({"ok": False, "error": "missing result"})
                    continue
                if not outcome.get("ok"):
                    failures.append(outcome)
                    continue
                concurrent_ms.append(float(outcome["duration_ms"]))
                snapshots.append(outcome["capability"])
            results.close()
            results.join_thread()

        residue = _residue(temporary)

    expected_runs = args.sequential + args.waves * args.processes
    invalid_snapshots = [
        snapshot
        for snapshot in snapshots
        if (
            snapshot.get("schema")
            != "symcc-shared-filesystem-capabilities-v1"
            or snapshot.get("probe_scope") != "same-host-subprocess-v1"
            or snapshot.get("cluster_lock_verified") is not False
            or any(snapshot.get(name) is not True for name in CAPABILITY_FLAGS)
        )
    ]
    summary = {
        "schema": "symcc-f327-filesystem-probe-evidence-v1",
        "scope": "same-host subprocess; no power-cut or remote-client proof",
        "configuration": {
            "sequential_runs": args.sequential,
            "concurrent_waves": args.waves,
            "processes_per_wave": args.processes,
            "expected_total_runs": expected_runs,
        },
        "observations": {
            "successful_snapshots": len(snapshots),
            "failed_results": len(failures),
            "nonzero_child_exits": sum(code != 0 for code in child_exit_codes),
            "probe_residue_count": len(residue),
            "invalid_capability_snapshots": len(invalid_snapshots),
        },
        "sequential_startup_cost_ms": {
            "median": round(statistics.median(sequential_ms), 3),
            "p95": round(_percentile(sequential_ms, 0.95), 3),
            "maximum": round(max(sequential_ms), 3),
            "samples": [round(value, 3) for value in sequential_ms],
        },
        "concurrent_process_latency_ms": {
            "median": round(statistics.median(concurrent_ms), 3),
            "p95": round(_percentile(concurrent_ms, 0.95), 3),
            "maximum": round(max(concurrent_ms), 3),
            "samples": [round(value, 3) for value in concurrent_ms],
        },
        "filesystem": snapshots[0] if snapshots else None,
        "child_exit_codes": child_exit_codes,
        "residue": residue,
        "failures": failures,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    valid = (
        len(snapshots) == expected_runs
        and not failures
        and all(code == 0 for code in child_exit_codes)
        and not residue
        and not invalid_snapshots
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
