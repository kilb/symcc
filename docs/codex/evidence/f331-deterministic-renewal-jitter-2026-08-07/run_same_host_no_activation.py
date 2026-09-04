#!/usr/bin/env python3
"""Prove jitter never activates without cross-host startup evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[4]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symcc-f331-same-host-") as tmp:
        temporary = Path(tmp)
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"A"
        (input_dir / "seed").write_bytes(seed)
        command = [
            "mpirun",
            "--oversubscribe",
            "-np",
            "7",
            "python3",
            str(REPO / "util/mpi_concolic_execution.py"),
            "-i",
            str(input_dir),
            "-o",
            str(output_dir),
            "-t",
            "1",
            "--max-idle",
            "1",
            "--wall-timeout",
            "4",
            "--workers-per-master",
            "3",
            "--simulate",
            "--",
            "/bin/true",
        ]
        environment = dict(os.environ)
        environment.update({
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0.05",
            "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER": "0.5",
        })
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
        capability_line = next(
            (line for line in completed.stdout.splitlines()
             if "Shared filesystem capabilities:" in line),
            "",
        )
        capability = (
            ast.literal_eval(capability_line.split(": ", 1)[1])
            if capability_line else {}
        )
        hidden = sorted(path.name for path in output_dir.iterdir()
                        if path.name.startswith(".standalone-work-"))
        checks = {
            "exit_zero": completed.returncode == 0,
            "two_masters": (
                "Mode:          multi-master (2 masters" in completed.stdout
            ),
            "all_workers_acknowledged": (
                "acked=3/3" in completed.stdout
                and "acked=2/2" in completed.stdout
            ),
            "cluster_not_claimed": (
                capability.get("cluster_lock_verified") is False
            ),
            "same_host_membership": (
                len({item.get("processor") for item in
                     capability.get("cluster_lock_members", [])}) == 1
            ),
            "zero_cross_host_rounds": (
                capability.get("cluster_lock_rounds") == 0
            ),
            "runtime_jitter_not_activated": (
                "Runtime cluster lock renewal:" not in completed.stdout
            ),
            "epoch_state_cleaned": hidden == [],
            "seed_published": (
                output_dir / hashlib.sha256(seed).hexdigest()
            ).is_file(),
        }
    result = {
        "schema": "symcc-f331-same-host-no-jitter-v1",
        "deployment_evidence": False,
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "configured_interval": 0.05,
        "configured_jitter_fraction": 0.5,
        "command": command,
        "returncode": completed.returncode,
        "capability": capability,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(completed.stdout, end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
