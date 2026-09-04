#!/usr/bin/env python3
"""Exercise F342 present-external accounting over actual MPI transport."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time


REPO = Path(__file__).resolve().parents[4]
UTIL = REPO / "util"
sys.path.insert(0, str(UTIL))

import mpi_concolic_execution as runner  # noqa: E402


SCHEMA = "symcc-f342-public-provenance-intersection-mpi-v1"
WORK_EPOCH = "f342" * 16
MASTER_ONE_DELAY_SECONDS = 6.0


def _content_for_owner(owner: int) -> tuple[bytes, str]:
    for index in range(10_000):
        content = f"F342-phantom-external-{index}".encode("ascii")
        work_hash = hashlib.sha256(content).hexdigest()
        if runner._rendezvous_work_owner(work_hash, (0, 1)) == owner:
            return content, work_hash
    raise RuntimeError("could not construct an owner-specific content hash")


def _canonical_regular_objects(directory: Path) -> list[str]:
    result: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not runner._normalize_work_hash(entry.name):
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                result.append(entry.name)
    return sorted(result)


def _write_target(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import os
import sys

content = sys.stdin.buffer.read()
with open(os.environ["F342_OBSERVATIONS"], "a", encoding="ascii") as stream:
    stream.write(hashlib.sha256(content).hexdigest() + "\\n")
""",
        encoding="ascii",
    )
    path.chmod(0o755)


def _write_rank_wrapper(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time

sys.path.insert(0, os.environ["F342_UTIL"])
import mpi_concolic_execution as runner

original_master_loop = runner.master_loop

def delayed_master_loop(*args, **kwargs):
    global_comm = args[0]
    if global_comm.Get_rank() == 1:
        time.sleep(float(os.environ["F342_MASTER_ONE_DELAY_SECONDS"]))
    return original_master_loop(*args, **kwargs)

runner.master_loop = delayed_master_loop
runner.main()
""",
        encoding="ascii",
    )
    path.chmod(0o755)


def _run_case() -> dict[str, object]:
    phantom_content, phantom_hash = _content_for_owner(1)
    generated_content = b"F342-public-generated-object"
    generated_hash = hashlib.sha256(generated_content).hexdigest()

    with tempfile.TemporaryDirectory(prefix="symcc-f342-provenance-") as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = input_dir / "late-external"
        seed.write_bytes(phantom_content)
        (output_dir / generated_hash).write_bytes(generated_content)
        observations = root / "observations.txt"
        target = root / "input-observer.py"
        wrapper = root / "rank-wrapper.py"
        _write_target(target)
        _write_rank_wrapper(wrapper)

        mpirun = shutil.which("mpirun")
        if mpirun is None:
            raise RuntimeError("mpirun is required for F342 MPI evidence")
        command = [
            mpirun,
            "--oversubscribe",
            "-np",
            "7",
            sys.executable,
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
            "25",
            "--workers-per-master",
            "3",
            "--",
            str(target),
        ]
        environment = os.environ.copy()
        environment.update({
            "F342_MASTER_ONE_DELAY_SECONDS": str(MASTER_ONE_DELAY_SECONDS),
            "F342_OBSERVATIONS": str(observations),
            "F342_UTIL": str(UTIL),
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONUNBUFFERED": "1",
            "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL": "0",
            "SYMCC_STANDALONE_INPUT_MAX_BYTES": "1024",
            "SYMCC_STANDALONE_RESULT_MAX_BYTES": "1024",
            "SYMCC_STANDALONE_RESULT_MAX_OBJECTS": "2",
            "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
            "SYMCC_STANDALONE_WORK_LEASE_TTL": "12",
            "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "0",
        })

        output_lines: list[str] = []
        marker_seen = threading.Event()
        seed_removed = threading.Event()
        start = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            text=True,
            bufsize=1,
        )

        def consume_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line)
                if "[Master] Observed 1 initial inputs" not in line:
                    continue
                marker_seen.set()
                try:
                    seed.unlink()
                except FileNotFoundError:
                    pass
                else:
                    seed_removed.set()

        reader = threading.Thread(target=consume_output, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=10)
            output_lines.append("F342 evidence driver killed a timed-out MPI run\n")
        reader.join(timeout=5)
        elapsed = time.monotonic() - start
        output = "".join(output_lines)

        observed = (
            observations.read_text(encoding="ascii").splitlines()
            if observations.exists() else []
        )
        public_objects = _canonical_regular_objects(output_dir)
        active = output_dir / f".standalone-work-{WORK_EPOCH}"
        retired = tuple(output_dir.glob(
            f".retired-standalone-work-{WORK_EPOCH}-*"))
        staged = tuple(output_dir.glob(
            f".standalone-work-{WORK_EPOCH}/staging/**/*"))
        checks = {
            "root_observed_phantom": marker_seen.is_set(),
            "phantom_removed_before_delayed_owner": seed_removed.is_set(),
            "clean_mpi_exit": returncode == 0,
            "only_generated_object_executed": observed == [generated_hash],
            "public_namespace_exact": public_objects == [generated_hash],
            "present_external_is_zero": (
                "External input objects:      0" in output),
            "generated_count_is_one": (
                "New interesting test cases:  1" in output),
            "phantom_hash_never_published": phantom_hash not in public_objects,
            "epoch_retired_cleanly": not active.exists() and len(retired) == 1,
            "no_staging_residue": not staged,
        }
        return {
            "mode": "delayed-owner-phantom-external",
            "command": command,
            "returncode": returncode,
            "elapsed_seconds": elapsed,
            "master_one_delay_seconds": MASTER_ONE_DELAY_SECONDS,
            "phantom_owner": runner._rendezvous_work_owner(
                phantom_hash, (0, 1)),
            "phantom_external_hash": phantom_hash,
            "generated_public_hash": generated_hash,
            "observed_target_hashes": observed,
            "valid_public_objects": public_objects,
            "active_epoch_after_run": active.exists(),
            "retired_epoch_count": len(retired),
            "staged_residue": [str(path.relative_to(output_dir)) for path in staged],
            "checks": checks,
            "all_checks_passed": all(checks.values()),
            "output": output,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    case = _run_case()
    artifact = {
        "schema": SCHEMA,
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_input_observer_target": True,
        "deterministic_rank_delay_injection": True,
        "configuration": {"masters": 2, "ranks": 7, "workers": 5},
        "case": case,
        "all_checks_passed": case["all_checks_passed"],
        "proof_boundary": (
            "Actual Open MPI transport on one physical host with an explicitly "
            "injected rank-1 master delay and a synthetic observer target. The "
            "artifact proves final public/external set-intersection accounting; "
            "it does not invoke SymCC's solver and does not establish campaign, "
            "coverage, throughput, multi-host, storage-failover, or bug-finding uplift."
        ),
    }
    args.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
