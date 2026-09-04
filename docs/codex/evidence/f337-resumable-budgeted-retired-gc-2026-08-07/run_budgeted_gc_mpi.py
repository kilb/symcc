#!/usr/bin/env python3
"""Exercise F337 partial retired-tree reclamation across real MPI restarts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f337" * 16
OLD_EPOCH = "0" * 64
OLD_RETIREMENT_ID = "7" * 32
OLD_RETIRED_NAME = (
    f".retired-standalone-work-{OLD_EPOCH}-{OLD_RETIREMENT_ID}"
)
RETIRED_PREFIX = ".retired-standalone-work-"


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


def _run(command: list[str]) -> dict[str, object]:
    environment = dict(os.environ)
    environment.update({
        "OMPI_ALLOW_RUN_AS_ROOT": "1",
        "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
        "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
        "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "1",
        "SYMCC_RETIRED_WORK_STATE_GC_ENTRY_BUDGET": "2",
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
    return {
        "returncode": completed.returncode,
        "elapsed": time.monotonic() - started,
        "stdout": completed.stdout,
    }


def _durably_create_old_tree(output_dir: Path) -> Path:
    old = output_dir / OLD_RETIRED_NAME
    old.mkdir()
    for index in range(5):
        path = old / f"old-state-{index}"
        with path.open("xb") as stream:
            stream.write(f"retired-{index}".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    for directory in (old, output_dir):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return old


def _retired_names(output_dir: Path) -> list[str]:
    return sorted(
        path.name for path in output_dir.iterdir()
        if path.name.startswith(RETIRED_PREFIX) and path.is_dir()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f337-budgeted-gc-") as tmp:
        temporary = Path(tmp)
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"A"
        (input_dir / "seed").write_bytes(seed)
        old = _durably_create_old_tree(output_dir)
        command = _command(input_dir, output_dir)

        runs: list[dict[str, object]] = []
        observations: list[dict[str, object]] = []
        for _ in range(3):
            run = _run(command)
            runs.append(run)
            observations.append({
                "old_root_exists": old.is_dir(),
                "old_file_count": (
                    len(tuple(old.iterdir())) if old.is_dir() else 0
                ),
                "retired_roots": _retired_names(output_dir),
            })

        corpus_digest = hashlib.sha256(seed).hexdigest()
        corpus_intact = (
            (output_dir / corpus_digest).read_bytes() == seed
            if (output_dir / corpus_digest).is_file() else False
        )

    outputs = [str(run["stdout"]) for run in runs]
    checks = {
        "all_three_real_mpi_runs_succeed": all(
            run["returncode"] == 0 for run in runs
        ),
        "all_workers_ack_on_every_run": all(
            "Worker shutdown: acked=3/3" in output
            and "Worker shutdown: acked=2/2" in output
            for output in outputs
        ),
        "first_two_runs_make_exact_bounded_partial_progress": (
            [item["old_root_exists"] for item in observations[:2]]
            == [True, True]
            and [item["old_file_count"] for item in observations[:2]]
            == [3, 1]
            and all(
                "Retired GC:    0 root(s) reclaimed, "
                "2 entry(s) removed, stop=entry-budget" in output
                for output in outputs[:2]
            )
        ),
        "third_run_durably_completes_old_root": (
            observations[2]["old_root_exists"] is False
            and observations[2]["old_file_count"] == 0
            and "Retired GC:    1 root(s) reclaimed, "
            "2 entry(s) removed, stop=root-limit" in outputs[2]
        ),
        "each_successful_run_retires_its_own_epoch": (
            len(observations[2]["retired_roots"]) == 3
            and OLD_RETIRED_NAME not in observations[2]["retired_roots"]
        ),
        "retirement_primitive_qualified_every_time": all(
            "Retirement:    renameat2(RENAME_NOREPLACE) qualified" in output
            for output in outputs
        ),
        "content_addressed_seed_remains_intact": corpus_intact,
        "all_runs_are_bounded": all(
            0.0 < float(run["elapsed"]) < 15.0 for run in runs
        ),
        "real_multi_master_frontend_exercised": all(
            "Mode:          multi-master (2 masters" in output
            for output in outputs
        ),
    }
    result = {
        "schema": "symcc-f337-resumable-budgeted-retired-gc-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "deployment_evidence": False,
        "proof_boundary": (
            "Three real Open MPI runs on one physical host and local overlayfs "
            "prove exact two-entry startup progress, partial-tree retention, "
            "cross-restart convergence, worker ACKs, and normal epoch retirement. "
            "They do not prove a hard wall-clock bound around blocking syscalls, "
            "remote-filesystem crash persistence, multi-host failover, DSE "
            "throughput, coverage, solver, or LAVA-M uplift."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "old_epoch": OLD_EPOCH,
            "old_retirement_id": OLD_RETIREMENT_ID,
            "old_tree_files": 5,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
            "retired_gc_root_limit": 1,
            "retired_gc_entry_budget": 2,
            "retired_gc_time_budget_seconds": 1.0,
        },
        "command": command,
        "runs": [
            {
                "returncode": run["returncode"],
                "elapsed": run["elapsed"],
            }
            for run in runs
        ],
        "state_observations": observations,
        "corpus_seed_digest": corpus_digest,
        "corpus_seed_intact": corpus_intact,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for index, output in enumerate(outputs, 1):
        print(f"=== MPI run {index} ===")
        print(output, end="")
    print("=== F337 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
