#!/usr/bin/env python3
"""Exercise F336 no-clobber retirement and same-epoch recovery under MPI."""

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
WORK_EPOCH = "f336" * 16
ACTIVE_NAME = f".standalone-work-{WORK_EPOCH}"
COLLISION_ID = "c" * 32
COLLISION_NAME = (
    f".retired-standalone-work-{WORK_EPOCH}-{COLLISION_ID}"
)
RETIRED_PREFIX = f".retired-standalone-work-{WORK_EPOCH}-"
FAILURE_MARKER = "Durable completed-epoch retirement failed: "
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F336_UTIL"])
import mpi_concolic_execution as runner

if (MPI.COMM_WORLD.Get_rank() == 0
        and os.environ.get("SYMCC_F336_INJECT_COLLISION") == "1"):
    real_cleanup = runner._cleanup_completed_work_state
    collision_id = os.environ["SYMCC_F336_COLLISION_ID"]

    def collide_then_retire(work_state, shared_dir, *, remove_shared_dir):
        if remove_shared_dir:
            return real_cleanup(
                work_state,
                shared_dir,
                remove_shared_dir=remove_shared_dir,
            )
        epoch = runner._completed_work_state_epoch(work_state, shared_dir)
        collision = os.path.join(
            shared_dir,
            runner._retired_work_state_name(epoch, collision_id),
        )
        os.mkdir(collision)
        marker = os.path.join(collision, "preexisting-marker")
        with open(marker, "xb") as stream:
            stream.write(b"must-not-clobber")
            stream.flush()
            os.fsync(stream.fileno())
        runner.fsync_directory(collision)
        runner.fsync_directory(shared_dir)

        real_urandom = runner.os.urandom
        runner.os.urandom = lambda size: (
            bytes.fromhex(collision_id)
            if size == runner._RETIREMENT_ID_LENGTH // 2 else
            real_urandom(size)
        )
        try:
            return real_cleanup(
                work_state,
                shared_dir,
                remove_shared_dir=remove_shared_dir,
            )
        finally:
            runner.os.urandom = real_urandom

    runner._cleanup_completed_work_state = collide_then_retire

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


def _run(command: list[str], *, inject_collision: bool) -> dict:
    environment = dict(os.environ)
    environment.update({
        "OMPI_ALLOW_RUN_AS_ROOT": "1",
        "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMCC_F336_UTIL": str(REPO / "util"),
        "SYMCC_F336_INJECT_COLLISION": "1" if inject_collision else "0",
        "SYMCC_F336_COLLISION_ID": COLLISION_ID,
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f336-noreplace-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "noreplace_retirement_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"A"
        (input_dir / "seed").write_bytes(seed)
        command = _command(wrapper, input_dir, output_dir)
        active = output_dir / ACTIVE_NAME
        collision = output_dir / COLLISION_NAME

        injected = _run(command, inject_collision=True)
        active_after_collision = active.is_dir()
        collision_marker = (
            (collision / "preexisting-marker").read_bytes()
            if (collision / "preexisting-marker").is_file() else b""
        )
        retired_after_collision = [
            path.name for path in _retired_roots(output_dir)
        ]

        recovered = _run(command, inject_collision=False)
        active_after_recovery = active.exists()
        retired_after_recovery = [
            path.name for path in _retired_roots(output_dir)
        ]
        corpus_digest = hashlib.sha256(seed).hexdigest()
        corpus_intact = (
            (output_dir / corpus_digest).read_bytes() == seed
            if (output_dir / corpus_digest).is_file() else False
        )

    failure_output = injected["stdout"]
    recovered_output = recovered["stdout"]
    failure_position = failure_output.find(FAILURE_MARKER)
    checks = {
        "collision_aborts_with_72": (
            injected["returncode"] == 72
            and failure_position >= 0
            and "File exists" in failure_output
            and "MPI_ABORT was invoked" in failure_output
        ),
        "all_workers_ack_before_collision_failure": (
            0 <= failure_output.find("Worker shutdown: acked=3/3")
            < failure_position
            and 0 <= failure_output.find("Worker shutdown: acked=2/2")
            < failure_position
        ),
        "collision_preserves_both_names_and_old_bytes": (
            active_after_collision
            and retired_after_collision == [COLLISION_NAME]
            and collision_marker == b"must-not-clobber"
        ),
        "startup_primitive_was_qualified": (
            "Retirement:    renameat2(RENAME_NOREPLACE) qualified"
            in failure_output
            and "Retirement:    renameat2(RENAME_NOREPLACE) qualified"
            in recovered_output
        ),
        "matching_restart_reclaims_collision_and_retires": (
            recovered["returncode"] == 0
            and not active_after_recovery
            and len(retired_after_recovery) == 1
            and retired_after_recovery[0] != COLLISION_NAME
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
        "schema": "symcc-f336-kernel-noreplace-retirement-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "collision_injected": True,
        "deployment_evidence": False,
        "proof_boundary": (
            "One physical host and a test-only durable destination collision "
            "prove ACK-before-Abort(72), kernel no-clobber behavior, active "
            "recovery, startup GC, and same-epoch convergence. This does not "
            "prove remote-filesystem crash persistence, cross-job locks, rank "
            "repair, DSE throughput, coverage, solver, or LAVA-M uplift."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "collision_retirement_id": COLLISION_ID,
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
            "active_exists_after_collision": active_after_collision,
            "retired_roots_after_collision": retired_after_collision,
            "collision_marker_hex": collision_marker.hex(),
            "active_exists_after_matching_recovery": active_after_recovery,
            "retired_roots_after_matching_recovery": retired_after_recovery,
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
    print("=== injected no-clobber collision stdout ===")
    print(failure_output, end="")
    print("=== matching recovery stdout ===")
    print(recovered_output, end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
