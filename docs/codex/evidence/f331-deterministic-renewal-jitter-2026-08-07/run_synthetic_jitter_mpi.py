#!/usr/bin/env python3
"""Exercise F331 jitter wiring over real MPI with synthetic host names."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[4]
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F331_UTIL"])
import mpi_concolic_execution as runner

# Test-only topology injection. Production has no processor-name override.
MPI.Get_processor_name = lambda: (
    f"synthetic-node-{MPI.COMM_WORLD.Get_rank()}"
)

runner.main()
'''


def _renewal_metrics(output: str) -> list[dict]:
    records = []
    marker = "Runtime cluster lock renewal: "
    for line in output.splitlines():
        if marker in line:
            records.append(ast.literal_eval(line.split(marker, 1)[1]))
    return records


def _capability(output: str) -> dict:
    marker = "Shared filesystem capabilities: "
    for line in output.splitlines():
        if marker in line:
            return ast.literal_eval(line.split(marker, 1)[1])
    return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symcc-f331-jitter-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "jitter_mpi_wrapper.py"
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        wrapper.write_text(WRAPPER, encoding="ascii")
        input_dir.mkdir()
        output_dir.mkdir()
        (input_dir / "seed").write_bytes(b"A")
        command = [
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
            "10",
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
            "SYMCC_F331_UTIL": str(REPO / "util"),
            "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT": "0.4",
            "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0.12",
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

    metrics = _renewal_metrics(completed.stdout)
    capability = _capability(completed.stdout)
    same_generation = len({item.get("generation") for item in metrics}) == 1
    same_last_schedule = (
        len({item.get("last_scheduled_interval") for item in metrics}) == 1
    )
    same_next_schedule = (
        len({item.get("next_scheduled_interval") for item in metrics}) == 1
    )
    checks = {
        "exit_zero": completed.returncode == 0,
        "synthetic_cluster_scope_entered": (
            capability.get("cluster_lock_verified") is True
            and capability.get("probe_scope") == "cross-host-mpi-lock-v1"
        ),
        "both_masters_report_v2": (
            len(metrics) == 2
            and all(item.get("schema")
                    == "symcc-cluster-lock-renewal-metrics-v2"
                    for item in metrics)
        ),
        "configured_jitter_reaches_both_masters": (
            len(metrics) == 2
            and all(item.get("interval") == 0.12 for item in metrics)
            and all(item.get("jitter_fraction") == 0.5 for item in metrics)
            and all(item.get("maximum_interval") == 0.18 for item in metrics)
        ),
        "successful_generations_are_consistent": (
            len(metrics) == 2
            and same_generation
            and all(item.get("generation", 0) >= 3 for item in metrics)
            and all(item.get("attempts") == item.get("successes")
                    for item in metrics)
            and all(item.get("failures") == 0 for item in metrics)
        ),
        "deterministic_schedule_matches_across_masters": (
            len(metrics) == 2
            and same_last_schedule
            and same_next_schedule
            and all(0.12 <= item.get("last_scheduled_interval", 0.0) < 0.18
                    for item in metrics)
            and all(0.12 <= item.get("next_scheduled_interval", 0.0) < 0.18
                    for item in metrics)
            and metrics[0].get("last_scheduled_interval")
            != metrics[0].get("next_scheduled_interval")
        ),
        "all_workers_acknowledge_shutdown": (
            "acked=3/3" in completed.stdout
            and "acked=2/2" in completed.stdout
        ),
    }
    result = {
        "schema": "symcc-f331-synthetic-jitter-mpi-v1",
        "synthetic_topology": True,
        "deployment_evidence": False,
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "proof_boundary": (
            "One physical host with injected processor names proves production "
            "configuration and MPI lifecycle wiring, not remote lock semantics."
        ),
        "command": command,
        "returncode": completed.returncode,
        "capability": capability,
        "renewal_metrics": metrics,
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
