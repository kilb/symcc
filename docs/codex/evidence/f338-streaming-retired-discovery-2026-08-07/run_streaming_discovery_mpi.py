#!/usr/bin/env python3
"""Exercise F338 streaming retired-root discovery through real MPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f338" * 16
RETIRED_PREFIX = ".retired-standalone-work-"
CANDIDATE_COUNT = 17
ROOT_LIMIT = 4


def _retired_name(index: int) -> str:
    return (
        f"{RETIRED_PREFIX}{index:064x}-{index:032x}"
    )


def _command(input_dir: Path, output_dir: Path) -> list[str]:
    return [
        "mpirun",
        "--oversubscribe",
        "-np",
        "7",
        "python3",
        str(REPO / "util" / "mpi_concolic_execution.py"),
        "-i",
        str(input_dir),
        "-o",
        str(output_dir),
        "-t",
        "1",
        "--max-idle",
        "1",
        "--wall-timeout",
        "3",
        "--workers-per-master",
        "3",
        "--simulate",
        "--",
        "/bin/true",
    ]


def _durably_create_candidates(output_dir: Path) -> list[str]:
    names = [_retired_name(index) for index in range(CANDIDATE_COUNT)]
    for name in reversed(names):
        candidate = output_dir / name
        candidate.mkdir()
        descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return names


def _retired_names(output_dir: Path) -> list[str]:
    return sorted(
        path.name for path in output_dir.iterdir()
        if path.name.startswith(RETIRED_PREFIX) and path.is_dir()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(
            prefix="symcc-f338-streaming-mpi-") as tmp:
        temporary = Path(tmp)
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"F338"
        (input_dir / "seed").write_bytes(seed)
        candidates = _durably_create_candidates(output_dir)
        command = _command(input_dir, output_dir)
        environment = dict(os.environ)
        environment.update({
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
            "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
            "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": str(ROOT_LIMIT),
            "SYMCC_RETIRED_WORK_STATE_GC_ENTRY_BUDGET": "32",
            "SYMCC_RETIRED_WORK_STATE_GC_TIME_BUDGET_SECONDS": "1",
        })
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45.0,
            check=False,
        )
        elapsed = time.monotonic() - started
        output = completed.stdout
        after = _retired_names(output_dir)
        seed_digest = hashlib.sha256(seed).hexdigest()
        seed_intact = (
            (output_dir / seed_digest).is_file()
            and (output_dir / seed_digest).read_bytes() == seed
        )

    scan_match = re.search(
        r"Retired scan:\s+entries=(\d+),\s*candidates=(\d+),\s*selected=(\d+)",
        output,
    )
    scan_observation = (
        {
            "scanned_entries": int(scan_match.group(1)),
            "candidate_roots": int(scan_match.group(2)),
            "selected_roots": int(scan_match.group(3)),
        }
        if scan_match is not None
        else None
    )
    expected_removed = candidates[:ROOT_LIMIT]
    retained_fixture = candidates[ROOT_LIMIT:]
    new_epoch_roots = [
        name for name in after
        if name.startswith(f"{RETIRED_PREFIX}{WORK_EPOCH}-")
    ]
    checks = {
        "real_mpi_run_succeeds": completed.returncode == 0,
        "all_workers_ack": (
            "Worker shutdown: acked=3/3" in output
            and "Worker shutdown: acked=2/2" in output
        ),
        "multi_master_frontend_exercised": (
            "Mode:          multi-master (2 masters" in output
        ),
        "streaming_scan_telemetry_is_exact": scan_observation is not None
        and scan_observation["candidate_roots"] == CANDIDATE_COUNT
        and scan_observation["selected_roots"] == ROOT_LIMIT
        and scan_observation["scanned_entries"] >= CANDIDATE_COUNT,
        "lexical_top_k_is_reclaimed": all(
            name not in after for name in expected_removed
        ),
        "unselected_fixture_roots_are_retained": all(
            name in after for name in retained_fixture
        ),
        "current_epoch_is_retired_normally": len(new_epoch_roots) == 1,
        "retired_root_conservation_holds": (
            len(after) == CANDIDATE_COUNT - ROOT_LIMIT + 1
        ),
        "production_gc_summary_is_exact": (
            "Retired GC:    4 root(s) reclaimed, "
            "4 entry(s) removed, stop=root-limit" in output
        ),
        "retirement_primitive_is_qualified": (
            "Retirement:    renameat2(RENAME_NOREPLACE) qualified" in output
        ),
        "content_addressed_seed_is_intact": seed_intact,
        "run_is_bounded": 0.0 < elapsed < 15.0,
    }
    result = {
        "schema": "symcc-f338-streaming-retired-discovery-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "deployment_evidence": False,
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "fixture_candidate_count": CANDIDATE_COUNT,
            "retired_gc_root_limit": ROOT_LIMIT,
            "retired_gc_entry_budget": 32,
            "retired_gc_time_budget_seconds": 1.0,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
        },
        "command": command,
        "returncode": completed.returncode,
        "elapsed": elapsed,
        "scan_observation": scan_observation,
        "expected_reclaimed_roots": expected_removed,
        "retained_fixture_roots": retained_fixture,
        "retired_roots_after_run": after,
        "new_epoch_roots": new_epoch_roots,
        "corpus_seed_digest": seed_digest,
        "corpus_seed_intact": seed_intact,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "One real Open MPI run on one physical host and local overlayfs "
            "proves production scan telemetry, deterministic lexical top-k "
            "selection, exact worker ACKs, and normal retirement. It does not "
            "prove bounded full-scan latency, multi-host shared-storage "
            "behavior, DSE throughput, solver, coverage, bug-discovery, or "
            "LAVA-M uplift."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== MPI output ===")
    print(output, end="")
    print("=== F338 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
