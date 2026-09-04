#!/usr/bin/env python3
"""Exercise F333 restart-stable renewal configuration commitment."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f333" * 16
MANIFEST_NAME = "renewal-configuration.json"
MANIFEST_MARKER = "Durable runtime lock configuration: "
WRAPPER = r'''#!/usr/bin/env python3
import os
import sys

from mpi4py import MPI

sys.path.insert(0, os.environ["SYMCC_F333_UTIL"])
import mpi_concolic_execution as runner

# Test-only topology injection; production code and manifest I/O are unchanged.
MPI.Get_processor_name = lambda: (
    f"synthetic-node-{MPI.COMM_WORLD.Get_rank()}"
)

runner.main()
'''


def _renewal_metrics(output: str) -> list[dict]:
    marker = "Runtime cluster lock renewal: "
    return [
        ast.literal_eval(line.split(marker, 1)[1])
        for line in output.splitlines()
        if marker in line
    ]


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
        "10",
        "--wall-timeout",
        "3",
        "--workers-per-master",
        "3",
        "--simulate",
        "--",
        "/bin/true",
    ]


def _environment(jitter: float) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "OMPI_ALLOW_RUN_AS_ROOT": "1",
        "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMCC_F333_UTIL": str(REPO / "util"),
        "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
        "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
        "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT": "0.4",
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0.12",
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER": f"{jitter:g}",
    })
    return environment


def _run(
    command: list[str],
    *,
    jitter: float,
) -> dict:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=REPO,
        env=_environment(jitter),
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
        "renewal_metrics": _renewal_metrics(completed.stdout),
    }


def _interrupt_after_manifest(
    command: list[str],
    *,
    timeout: float = 30.0,
) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=REPO,
        env=_environment(0.1),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    observed = False
    try:
        deadline = started + timeout
        while time.monotonic() < deadline and process.poll() is None:
            for key, _ in selector.select(timeout=0.1):
                line = key.fileobj.readline()
                if not line:
                    continue
                output.append(line)
                if MANIFEST_MARKER in line:
                    observed = True
                    os.killpg(process.pid, signal.SIGKILL)
                    break
            if observed:
                break
        if not observed and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10.0)
        output.append(process.stdout.read())
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10.0)
        process.stdout.close()
    return {
        "returncode": process.returncode,
        "elapsed": time.monotonic() - started,
        "manifest_marker_observed": observed,
        "stdout": "".join(output),
    }


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f333-manifest-mpi-") as tmp:
        temporary = Path(tmp)
        wrapper = temporary / "durable_manifest_wrapper.py"
        wrapper.write_text(WRAPPER, encoding="ascii")
        input_dir = temporary / "input"
        output_dir = temporary / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        (input_dir / "seed").write_bytes(b"A")
        command = _command(wrapper, input_dir, output_dir)
        state_dir = output_dir / f".standalone-work-{WORK_EPOCH}"
        manifest_path = state_dir / MANIFEST_NAME

        interrupted = _interrupt_after_manifest(command)
        time.sleep(0.5)
        manifest_exists = manifest_path.is_file()
        manifest = (
            json.loads(manifest_path.read_text(encoding="ascii"))
            if manifest_exists else {}
        )
        initial_manifest_sha256 = (
            _manifest_sha256(manifest_path) if manifest_exists else ""
        )

        changed = _run(command, jitter=0.25)
        changed_manifest_sha256 = (
            _manifest_sha256(manifest_path)
            if manifest_path.is_file() else ""
        )
        state_after_changed = state_dir.is_dir()

        recovered = _run(command, jitter=0.1)
        state_after_recovery = state_dir.exists()

    changed_metrics = changed["renewal_metrics"]
    recovered_metrics = recovered["renewal_metrics"]
    configuration = manifest.get("configuration", {})
    checks = {
        "interrupted_after_manifest_publication": (
            interrupted["manifest_marker_observed"]
            and interrupted["returncode"] != 0
        ),
        "canonical_manifest_persisted_after_crash": (
            manifest_exists
            and manifest.get("schema")
            == "symcc-runtime-lock-configuration-manifest-v1"
            and manifest.get("epoch") == WORK_EPOCH
            and re.fullmatch(
                r"[0-9a-f]{64}", str(manifest.get("fingerprint", ""))
            ) is not None
            and configuration.get("schema")
            == "symcc-cluster-lock-renewal-config-v1"
            and configuration.get("jitter_fraction") == 0.1
            and configuration.get("fingerprint")
            == manifest.get("fingerprint")
        ),
        "changed_live_consensus_rejected_by_durable_manifest": (
            changed["returncode"] == 70
            and changed["stdout"].count(
                "manifest does not match consensus") >= 2
        ),
        "changed_configuration_never_attempts_renewal": (
            len(changed_metrics) == 2
            and all(item.get("generation") == 0 for item in changed_metrics)
            and all(item.get("attempts") == 0 for item in changed_metrics)
        ),
        "changed_run_preserves_original_commitment": (
            state_after_changed
            and initial_manifest_sha256
            == changed_manifest_sha256
        ),
        "changed_run_acknowledges_workers_before_abort": (
            "acked=3/3" in changed["stdout"]
            and "acked=2/2" in changed["stdout"]
        ),
        "matching_restart_reuses_commitment_and_progresses": (
            recovered["returncode"] == 0
            and manifest.get("fingerprint", "") in recovered["stdout"]
            and len(recovered_metrics) == 2
            and all(item.get("generation", 0) >= 2
                    for item in recovered_metrics)
            and all(item.get("failures") == 0 for item in recovered_metrics)
        ),
        "matching_restart_acknowledges_workers_and_cleans_state": (
            "acked=3/3" in recovered["stdout"]
            and "acked=2/2" in recovered["stdout"]
            and not state_after_recovery
        ),
        "restart_decisions_are_bounded": (
            changed["elapsed"] < 10.0
            and recovered["elapsed"] < 10.0
        ),
    }
    result = {
        "schema": "symcc-f333-durable-renewal-configuration-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": True,
        "process_crash_injected": True,
        "deployment_evidence": False,
        "proof_boundary": (
            "One physical host with test-only processor identity and process-"
            "group crash injection proves production restart wiring, exact "
            "durable comparison, worker ACK ordering, and cleanup. It does not "
            "prove remote filesystem behavior, rank repair, DSE throughput, "
            "coverage, or solver uplift."
        ),
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "ranks": 7,
            "masters": 2,
            "workers": 5,
            "interval": 0.12,
            "initial_jitter_fraction": 0.1,
            "changed_jitter_fraction": 0.25,
        },
        "command": command,
        "manifest": manifest,
        "manifest_sha256": initial_manifest_sha256,
        "state_observations": {
            "manifest_exists_after_crash": manifest_exists,
            "manifest_sha256_after_crash": initial_manifest_sha256,
            "manifest_sha256_after_changed_restart": changed_manifest_sha256,
            "state_exists_after_changed_restart": state_after_changed,
            "state_exists_after_matching_recovery": state_after_recovery,
        },
        "interrupted": {
            key: value for key, value in interrupted.items()
            if key != "stdout"
        },
        "changed": {
            key: value for key, value in changed.items()
            if key != "stdout"
        },
        "recovered": {
            key: value for key, value in recovered.items()
            if key != "stdout"
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== interrupted stdout ===")
    print(interrupted["stdout"], end="")
    print("=== changed stdout ===")
    print(changed["stdout"], end="")
    print("=== recovered stdout ===")
    print(recovered["stdout"], end="")
    print("=== result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
