#!/usr/bin/env python3
"""Exercise F335 post-rename uncertainty and bounded startup reclamation."""

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
WORK_EPOCH = "f335" * 16
ACTIVE_NAME = f".standalone-work-{WORK_EPOCH}"
RETIRED_PREFIX = f".retired-standalone-work-{WORK_EPOCH}-"
FAILURE_MARKER = "Durable completed-epoch retirement failed: "
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F335_UTIL"])
import mpi_concolic_execution as runner

if (MPI.COMM_WORLD.Get_rank() == 0
        and os.environ.get("SYMCC_F335_INJECT_RETIRE_FSYNC_FAILURE") == "1"):
    real_replace = runner.durable_rename_noreplace

    def visible_retirement_then_fail(source, destination):
        if (os.path.basename(source).startswith(".standalone-work-")
                and os.path.basename(destination).startswith(
                    ".retired-standalone-work-")):
            os.replace(source, destination)
            raise OSError("injected post-rename directory fsync failure")
        return real_replace(source, destination)

    runner.durable_rename_noreplace = visible_retirement_then_fail

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
        "SYMCC_F335_UTIL": str(REPO / "util"),
        "SYMCC_F335_INJECT_RETIRE_FSYNC_FAILURE": (
            "1" if inject_failure else "0"
        ),
        "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
        "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
        "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "1",
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


def _retired_roots(output_dir: Path) -> list[Path]:
    return sorted(
        path for path in output_dir.iterdir()
        if path.name.startswith(RETIRED_PREFIX) and path.is_dir()
    )


def _relative_files(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f335-retire-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "atomic_retirement_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"A"
        (input_dir / "seed").write_bytes(seed)
        command = _command(wrapper, input_dir, output_dir)
        active = output_dir / ACTIVE_NAME

        injected = _run(command, inject_failure=True)
        retired_after_failure = _retired_roots(output_dir)
        failure_files = (
            _relative_files(retired_after_failure[0])
            if len(retired_after_failure) == 1 else []
        )
        active_after_failure = active.exists()

        recovered = _run(command, inject_failure=False)
        retired_after_recovery = _retired_roots(output_dir)
        active_after_recovery = active.exists()
        corpus_digest = hashlib.sha256(seed).hexdigest()
        corpus_intact = (
            (output_dir / corpus_digest).read_bytes() == seed
            if (output_dir / corpus_digest).is_file() else False
        )

    failure_output = injected["stdout"]
    recovered_output = recovered["stdout"]
    failure_position = failure_output.find(FAILURE_MARKER)
    first_retired_names = [path.name for path in retired_after_failure]
    recovered_retired_names = [path.name for path in retired_after_recovery]
    checks = {
        "post_rename_uncertainty_aborts_with_72": (
            injected["returncode"] == 72
            and failure_position >= 0
            and "injected post-rename directory fsync failure"
            in failure_output
            and "MPI_ABORT was invoked" in failure_output
        ),
        "all_workers_acknowledge_before_retirement_failure": (
            0 <= failure_output.find("Worker shutdown: acked=3/3")
            < failure_position
            and 0 <= failure_output.find("Worker shutdown: acked=2/2")
            < failure_position
        ),
        "uncertain_retirement_has_no_active_name_and_one_retired_root": (
            not active_after_failure
            and len(first_retired_names) == 1
            and "state.json" in failure_files
        ),
        "matching_restart_reclaims_old_root_and_retires_new_epoch": (
            recovered["returncode"] == 0
            and not active_after_recovery
            and len(recovered_retired_names) == 1
            and first_retired_names[0] not in recovered_retired_names
            and "Retired GC:    1 root(s) reclaimed" in recovered_output
            and "Worker shutdown: acked=3/3" in recovered_output
            and "Worker shutdown: acked=2/2" in recovered_output
        ),
        "content_addressed_seed_remains_intact": corpus_intact,
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
        "schema": "symcc-f335-atomic-epoch-retirement-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "post_rename_fsync_failure_injected": True,
        "deployment_evidence": False,
        "proof_boundary": (
            "One physical host and a test-only rank-0 post-rename/pre-fsync "
            "fault prove production Abort(72), ACK-before-retirement order, "
            "reserved-root retention, startup GC, and same-epoch convergence. "
            "This does not prove remote-filesystem crash persistence, "
            "cross-job lock semantics, rank repair, DSE throughput, coverage, "
            "solver, or LAVA-M uplift."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
            "retired_gc_limit": 1,
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
            "active_exists_after_failed_retirement": active_after_failure,
            "retired_roots_after_failed_retirement": first_retired_names,
            "retired_files_after_failed_retirement": failure_files,
            "active_exists_after_matching_recovery": active_after_recovery,
            "retired_roots_after_matching_recovery": recovered_retired_names,
            "corpus_seed_digest": corpus_digest,
            "corpus_seed_intact": corpus_intact,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== injected post-rename fsync failure stdout ===")
    print(failure_output, end="")
    print("=== matching recovery stdout ===")
    print(recovered_output, end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
