#!/usr/bin/env python3
"""Exercise runtime qualification over real MPI with synthetic host names."""

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

sys.path.insert(0, os.environ["SYMCC_F330_UTIL"])
import mpi_concolic_execution as runner
import mpi_filesystem_qualification as qualification

# Test-only topology injection. Production never accepts a processor override.
MPI.Get_processor_name = lambda: (
    f"synthetic-node-{MPI.COMM_WORLD.Get_rank()}"
)

failure_generation = int(os.environ.get(
    "SYMCC_F330_INJECT_FAILURE_GENERATION", "0"))
original_qualification = runner.qualify_mpi_cluster_advisory_lock


def injected_qualification(*args, **kwargs):
    generation = int(kwargs.get("qualification_generation", 0))
    if failure_generation and generation >= failure_generation:
        qualification._try_exclusive_lock = lambda descriptor: (
            "acquired", "")
    return original_qualification(*args, **kwargs)


runner.qualify_mpi_cluster_advisory_lock = injected_qualification
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


def _run_case(
    temporary: Path,
    wrapper: Path,
    *,
    name: str,
    failure_generation: int,
) -> tuple[dict, str]:
    input_dir = temporary / f"{name}-input"
    output_dir = temporary / f"{name}-output"
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
        "3",
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
        "SYMCC_F330_UTIL": str(REPO / "util"),
        "SYMCC_F330_INJECT_FAILURE_GENERATION": str(failure_generation),
        "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT": "0.5",
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0.2",
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
    result = {
        "name": name,
        "command": command,
        "failure_generation": failure_generation,
        "returncode": completed.returncode,
        "capability": _capability(completed.stdout),
        "renewal_metrics": metrics,
    }
    return result, completed.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symcc-f330-runtime-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "runtime_mpi_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        success, success_log = _run_case(
            temporary,
            wrapper,
            name="success",
            failure_generation=0,
        )
        failure, failure_log = _run_case(
            temporary,
            wrapper,
            name="semantic-drift",
            failure_generation=2,
        )

    success_metrics = success["renewal_metrics"]
    failure_metrics = failure["renewal_metrics"]
    checks = {
        "success_exit_zero": success["returncode"] == 0,
        "startup_enters_synthetic_cluster_scope": (
            success["capability"].get("cluster_lock_verified") is True
            and success["capability"].get("probe_scope")
            == "cross-host-mpi-lock-v1"
        ),
        "both_masters_report_successful_renewals": (
            len(success_metrics) == 2
            and all(item.get("attempts", 0) >= 2 for item in success_metrics)
            and all(item.get("attempts") == item.get("successes")
                    for item in success_metrics)
            and all(item.get("failures") == 0 for item in success_metrics)
        ),
        "semantic_drift_exits_nonzero": failure["returncode"] != 0,
        "semantic_drift_is_reported": (
            "runtime cluster lock renewal 2 failed:" in failure_log
            and "remote master acquired a held cluster lock" in failure_log
        ),
        "both_masters_record_failed_generation": (
            len(failure_metrics) == 2
            and all(item.get("generation") == 2 for item in failure_metrics)
            and all(item.get("failures") == 1 for item in failure_metrics)
        ),
    }
    result = {
        "schema": "symcc-f330-synthetic-runtime-mpi-v1",
        "synthetic_topology": True,
        "deployment_evidence": False,
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "proof_boundary": (
            "Processor names are injected on one physical host. This proves "
            "runtime control-path behavior, not remote filesystem semantics."
        ),
        "success": success,
        "semantic_drift": failure,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== success case ===")
    print(success_log, end="")
    print("=== semantic drift case ===")
    print(failure_log, end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
