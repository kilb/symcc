#!/usr/bin/env python3
"""Exercise F332 configuration consensus over real Open MPI transport."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f332" * 16
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F332_UTIL"])
import mpi_concolic_execution as runner

# Test-only topology and per-rank configuration injection.
MPI.Get_processor_name = lambda: (
    f"synthetic-node-{MPI.COMM_WORLD.Get_rank()}"
)
if (
    os.environ.get("SYMCC_F332_INJECT_DRIFT") == "1"
    and MPI.COMM_WORLD.Get_rank() == 1
):
    os.environ["SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER"] = "0.25"

runner.main()
'''


def _renewal_metrics(output: str) -> list[dict]:
    records = []
    marker = "Runtime cluster lock renewal: "
    for line in output.splitlines():
        if marker in line:
            records.append(ast.literal_eval(line.split(marker, 1)[1]))
    return records


def _consensus_fingerprints(output: str) -> list[str]:
    marker = "Runtime lock renewal configuration consensus: "
    return [
        line.split(marker, 1)[1].strip()
        for line in output.splitlines()
        if marker in line
    ]


def _mismatch_summaries(output: str) -> list[str]:
    pattern = re.compile(
        r"cluster lock renewal configuration mismatch "
        r"\(0:[0-9a-f]{12},1:[0-9a-f]{12}\)"
    )
    return pattern.findall(output)


def _run(
    temporary: Path,
    wrapper: Path,
    *,
    name: str,
    inject_drift: bool,
) -> dict:
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
        "SYMCC_F332_UTIL": str(REPO / "util"),
        "SYMCC_F332_INJECT_DRIFT": "1" if inject_drift else "0",
        "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
        "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT": "0.4",
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0.12",
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER": "0.1",
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
    return {
        "command": command,
        "returncode": completed.returncode,
        "elapsed": elapsed,
        "stdout": completed.stdout,
        "consensus_fingerprints": _consensus_fingerprints(completed.stdout),
        "mismatch_summaries": _mismatch_summaries(completed.stdout),
        "renewal_metrics": _renewal_metrics(completed.stdout),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symcc-f332-config-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "configuration_consensus_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        healthy = _run(
            temporary,
            wrapper,
            name="healthy",
            inject_drift=False,
        )
        drift = _run(
            temporary,
            wrapper,
            name="drift",
            inject_drift=True,
        )

    healthy_metrics = healthy["renewal_metrics"]
    drift_metrics = drift["renewal_metrics"]
    checks = {
        "healthy_exit_zero": healthy["returncode"] == 0,
        "healthy_exact_consensus_fingerprint": (
            len(healthy["consensus_fingerprints"]) == 1
            and re.fullmatch(
                r"[0-9a-f]{64}", healthy["consensus_fingerprints"][0]
            ) is not None
        ),
        "healthy_runtime_renewal_progresses": (
            len(healthy_metrics) == 2
            and all(item.get("generation", 0) >= 2
                    for item in healthy_metrics)
            and all(item.get("attempts") == item.get("successes")
                    for item in healthy_metrics)
            and all(item.get("failures") == 0 for item in healthy_metrics)
        ),
        "healthy_workers_acknowledge": (
            "acked=3/3" in healthy["stdout"]
            and "acked=2/2" in healthy["stdout"]
        ),
        "drift_exits_fail_closed": drift["returncode"] == 70,
        "drift_detected_on_both_masters": (
            len(drift["mismatch_summaries"]) >= 2
            and drift["stdout"].count(
                "runtime cluster lock configuration consensus failed"
            ) >= 2
        ),
        "drift_never_reaches_consensus": (
            drift["consensus_fingerprints"] == []
        ),
        "drift_never_attempts_renewal": (
            len(drift_metrics) == 2
            and all(item.get("generation") == 0 for item in drift_metrics)
            and all(item.get("attempts") == 0 for item in drift_metrics)
        ),
        "drift_workers_acknowledge_before_abort": (
            "acked=3/3" in drift["stdout"]
            and "acked=2/2" in drift["stdout"]
        ),
        "drift_detection_is_bounded": drift["elapsed"] < 10.0,
    }
    result = {
        "schema": "symcc-f332-renewal-configuration-consensus-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": True,
        "deployment_evidence": False,
        "proof_boundary": (
            "One physical host with test-only processor/configuration injection "
            "proves production MPI protocol wiring and fail-closed drift "
            "detection, not remote filesystem semantics."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
            "interval": 0.12,
            "root_jitter_fraction": 0.1,
            "injected_peer_jitter_fraction": 0.25,
        },
        "healthy": {
            key: value for key, value in healthy.items() if key != "stdout"
        },
        "drift": {
            key: value for key, value in drift.items() if key != "stdout"
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== healthy stdout ===")
    print(healthy["stdout"], end="")
    print("=== drift stdout ===")
    print(drift["stdout"], end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
