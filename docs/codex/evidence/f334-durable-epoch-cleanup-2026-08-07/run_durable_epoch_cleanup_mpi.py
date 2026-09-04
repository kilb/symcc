#!/usr/bin/env python3
"""Exercise F334 fail-closed cleanup and matching epoch recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f334" * 16
FAILURE_MARKER = "Durable completed-epoch cleanup failed: "
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F334_UTIL"])
import mpi_concolic_execution as runner

if (MPI.COMM_WORLD.Get_rank() == 0
        and os.environ.get("SYMCC_F334_INJECT_CLEANUP_FAILURE") == "1"):
    real_rmtree = runner.durable_rmtree

    def fail_completed_epoch(path):
        if os.path.basename(path).startswith(".standalone-work-"):
            raise OSError("injected durable cleanup I/O failure")
        return real_rmtree(path)

    runner.durable_rmtree = fail_completed_epoch

runner.main()
'''


def _command(wrapper: Path, input_dir: Path, output_dir: Path) -> list[str]:
    return [
        "mpirun",
        "--oversubscribe",
        "-np",
        "7",
        "python3",
        str(wrapper),
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


def _run(command: list[str], *, inject_failure: bool) -> dict:
    environment = dict(os.environ)
    environment.update({
        "OMPI_ALLOW_RUN_AS_ROOT": "1",
        "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMCC_F334_UTIL": str(REPO / "util"),
        "SYMCC_F334_INJECT_CLEANUP_FAILURE": (
            "1" if inject_failure else "0"
        ),
        "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
        "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f334-cleanup-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "durable_cleanup_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        (input_dir / "seed").write_bytes(b"A")
        command = _command(wrapper, input_dir, output_dir)
        state_dir = output_dir / f".standalone-work-{WORK_EPOCH}"

        injected = _run(command, inject_failure=True)
        state_exists_after_failure = state_dir.is_dir()
        state_files_after_failure = (
            sorted(
                str(path.relative_to(state_dir))
                for path in state_dir.rglob("*")
                if path.is_file()
            )
            if state_exists_after_failure else []
        )

        recovered = _run(command, inject_failure=False)
        state_exists_after_recovery = state_dir.exists()

    failure_output = injected["stdout"]
    recovered_output = recovered["stdout"]
    failure_position = failure_output.find(FAILURE_MARKER)
    checks = {
        "injected_cleanup_uncertainty_aborts_with_72": (
            injected["returncode"] == 72
            and failure_position >= 0
            and "MPI_ABORT was invoked" in failure_output
        ),
        "all_workers_acknowledge_before_cleanup_failure": (
            0 <= failure_output.find("Worker shutdown: acked=3/3")
            < failure_position
            and 0 <= failure_output.find("Worker shutdown: acked=2/2")
            < failure_position
        ),
        "failed_cleanup_retains_recoverable_epoch": (
            state_exists_after_failure
            and "state.json" in state_files_after_failure
        ),
        "matching_restart_completes_and_removes_epoch": (
            recovered["returncode"] == 0
            and not state_exists_after_recovery
            and "Worker shutdown: acked=3/3" in recovered_output
            and "Worker shutdown: acked=2/2" in recovered_output
        ),
        "both_runs_are_bounded": (
            0.0 < injected["elapsed"] < 15.0
            and 0.0 < recovered["elapsed"] < 15.0
        ),
        "real_multi_master_frontend_exercised": (
            "Mode:          multi-master (2 masters" in failure_output
            and "Mode:          multi-master (2 masters" in recovered_output
        ),
    }
    result = {
        "schema": "symcc-f334-durable-epoch-cleanup-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "cleanup_failure_injected": True,
        "deployment_evidence": False,
        "proof_boundary": (
            "One physical host and a test-only rank-0 durable_rmtree fault "
            "prove production Abort(72) wiring, ACK-before-cleanup ordering, "
            "state retention, and matching recovery. This does not prove "
            "remote-filesystem crash persistence, rank repair, DSE throughput, "
            "coverage, solver, or LAVA-M uplift."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
        },
        "command": command,
        "injected": {
            "returncode": injected["returncode"],
            "elapsed": injected["elapsed"],
        },
        "recovered": {
            "returncode": recovered["returncode"],
            "elapsed": recovered["elapsed"],
        },
        "state_observations": {
            "state_exists_after_failed_cleanup": state_exists_after_failure,
            "state_files_after_failed_cleanup": state_files_after_failure,
            "state_exists_after_matching_recovery": state_exists_after_recovery,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== injected cleanup failure stdout ===")
    print(failure_output, end="")
    print("=== matching recovery stdout ===")
    print(recovered_output, end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
