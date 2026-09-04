#!/usr/bin/env python3
"""Run real single- and two-master F327 MPI startup smoke tests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "util" / "mpi_concolic_execution.py"
PROBE_PREFIXES = (
    ".symcc-fs-probe-",
    ".symcc-fs-publication-probe-",
)


def _run(
    mpirun: str,
    input_dir: Path,
    output_dir: Path,
    processes: int,
    workers_per_master: int,
) -> subprocess.CompletedProcess[str]:
    command = [
        mpirun,
        "--oversubscribe",
        "-np",
        str(processes),
        sys.executable,
        str(RUNNER),
        "-i",
        str(input_dir),
        "-o",
        str(output_dir),
        "-t",
        "1",
        "--max-idle",
        "1",
    ]
    if workers_per_master:
        command.extend(["--workers-per-master", str(workers_per_master)])
    command.extend(["--", "/bin/true"])
    environment = os.environ.copy()
    environment.update({
        "SYMCC_SHARED_STATE_FS_PROBE": "1",
        "SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT": "5",
        "SYMCC_SHUTDOWN_GRACE_SEC": "5",
        "SYMCC_FINALIZE_GRACE_SEC": "5",
    })
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=45.0,
        check=False,
    )


def _residue(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.name.startswith(PROBE_PREFIXES)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    mpirun = shutil.which("mpirun")
    if mpirun is None:
        raise RuntimeError("mpirun is required for F327 integration evidence")

    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="symcc-f327-mpi-") as temporary:
        temporary_path = Path(temporary)
        input_dir = temporary_path / "input"
        input_dir.mkdir()
        (input_dir / "seed-a").write_bytes(b"seed-a\n")
        (input_dir / "seed-b").write_bytes(b"seed-b\n")

        cases = (
            ("single-master", 3, 0, 2),
            ("two-master", 8, 3, 3),
        )
        for name, processes, workers_per_master, expected_acks in cases:
            output = temporary_path / name
            result = _run(
                mpirun,
                input_dir,
                output,
                processes,
                workers_per_master,
            )
            (args.output_dir / f"{name}.log").write_text(
                result.stdout, encoding="utf-8")
            capability_reported = all(marker in result.stdout for marker in (
                "Shared filesystem capabilities:",
                "'probe_scope': 'same-host-subprocess-v1'",
                "'cluster_lock_verified': False",
                "'publication_replace': True",
                "'advisory_lock_exclusion': True",
                "'advisory_lock_release': True",
            ))
            ack_marker = f"acked={expected_acks}/{expected_acks}"
            hidden_epochs = sorted(
                path.name for path in output.glob(".standalone-work-*")
            ) if output.exists() else []
            residue = _residue(output) if output.exists() else []
            record = {
                "name": name,
                "processes": processes,
                "workers_per_master": workers_per_master,
                "returncode": result.returncode,
                "capability_snapshot_reported": capability_reported,
                "shutdown_ack_marker": ack_marker,
                "shutdown_ack_reported": ack_marker in result.stdout,
                "global_quiescence_reported": (
                    "Global quiescence committed" in result.stdout
                    if name == "two-master" else True
                ),
                "probe_residue": residue,
                "hidden_epoch_state_after_finalize": hidden_epochs,
            }
            records.append(record)

    summary = {
        "schema": "symcc-f327-mpi-preflight-evidence-v1",
        "scope": "/bin/true orchestration smoke; not DSE throughput",
        "cases": records,
    }
    summary_path = args.output_dir / "mpi-smoke-summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    valid = all(
        record["returncode"] == 0
        and record["capability_snapshot_reported"]
        and record["shutdown_ack_reported"]
        and record["global_quiescence_reported"]
        and not record["probe_residue"]
        and not record["hidden_epoch_state_after_finalize"]
        for record in records
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
