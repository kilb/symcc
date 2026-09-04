#!/usr/bin/env python3
"""Run a real same-host two-master job and reject false cluster claims."""

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symcc-f329-mpi-") as tmp:
        temporary = Path(tmp)
        input_dir = temporary / "in"
        output_dir = temporary / "out"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = b"A"
        (input_dir / "seed").write_bytes(seed)
        command = [
            "mpirun", "--oversubscribe", "-np", "7",
            "python3", str(REPO / "util/mpi_concolic_execution.py"),
            "-i", str(input_dir), "-o", str(output_dir),
            "-t", "1", "--max-idle", "1", "--wall-timeout", "4",
            "--workers-per-master", "3", "--simulate", "--", "/bin/true",
        ]
        environment = dict(os.environ)
        environment.update({
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
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
        print(completed.stdout, end="")
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
        corpus_digest = hashlib.sha256(seed).hexdigest()
        checks = {
            "exit_zero": completed.returncode == 0,
            "two_masters": "Mode:          multi-master (2 masters" in completed.stdout,
            "all_workers_acknowledged": (
                "acked=3/3" in completed.stdout
                and "acked=2/2" in completed.stdout
            ),
            "same_host_scope": (
                capability.get("probe_scope") == "same-host-subprocess-v1"
            ),
            "cluster_not_claimed": capability.get("cluster_lock_verified") is False,
            "membership_observed": (
                [item.get("rank")
                 for item in capability.get("cluster_lock_members", [])]
                == [0, 1]
                and len({item.get("processor")
                         for item in capability.get("cluster_lock_members", [])})
                == 1
            ),
            "zero_cross_host_rounds": capability.get("cluster_lock_rounds") == 0,
            "epoch_state_cleaned": hidden == [],
            "seed_published": (output_dir / corpus_digest).is_file(),
        }
        result = {
            "schema": "symcc-f329-same-host-mpi-v1",
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
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["all_checks_passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
