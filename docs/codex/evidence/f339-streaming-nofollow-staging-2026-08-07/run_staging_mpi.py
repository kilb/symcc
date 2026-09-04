#!/usr/bin/env python3
"""Exercise F339 staging verification through a real Open MPI run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[4]
WORK_EPOCH = "f339" * 16


def _command(
    input_dir: Path,
    output_dir: Path,
    target: Path,
) -> list[str]:
    return [
        "mpirun",
        "--oversubscribe",
        "-np",
        "4",
        "python3",
        str(REPO / "util" / "mpi_concolic_execution.py"),
        "-i",
        str(input_dir),
        "-o",
        str(output_dir),
        "-t",
        "1",
        "--max-idle",
        "1",
        "--wall-timeout",
        "30",
        "--",
        str(target),
    ]


def _corpus_objects(output_dir: Path) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    invalid: list[str] = []
    for path in output_dir.iterdir():
        if re.fullmatch(r"[0-9a-f]{64}", path.name) is None:
            continue
        try:
            metadata = path.lstat()
        except OSError:
            invalid.append(path.name)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            invalid.append(path.name)
            continue
        try:
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            invalid.append(path.name)
            continue
        (valid if observed == path.name else invalid).append(path.name)
    return sorted(valid), sorted(invalid)


def _staged_residue(output_dir: Path) -> list[str]:
    residue: list[str] = []
    for path in output_dir.rglob("*"):
        if "staging" not in path.parts:
            continue
        if path.is_file() or path.is_symlink():
            residue.append(str(path.relative_to(output_dir)))
    return sorted(residue)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f339-mpi-") as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        seed = b"F339-stage"
        (input_dir / "seed").write_bytes(seed)
        target = root / "output-contract-target.py"
        target.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "if data == b'F339-stage':\n"
            "    output = Path(os.environ['SYMCC_OUTPUT_DIR'])\n"
            "    output.mkdir(parents=True, exist_ok=True)\n"
            "    (output / 'child-0000').write_bytes(b'F339-child')\n",
            encoding="ascii",
        )
        target.chmod(0o755)
        command = _command(input_dir, output_dir, target)
        environment = dict(os.environ)
        environment.update({
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYMCC_STANDALONE_WORK_EPOCH": WORK_EPOCH,
            "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
            "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "0",
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
        output = completed.stdout
        valid_objects, invalid_objects = _corpus_objects(output_dir)
        staged_residue = _staged_residue(output_dir)
        retired_roots = sorted(
            path.name for path in output_dir.iterdir()
            if path.name.startswith(".retired-standalone-work-")
            and path.is_dir()
        )

    generated_match = re.search(
        r"Total test cases generated:\s+(\d+)", output)
    interesting_match = re.search(
        r"New interesting test cases:\s+(\d+)", output)
    generated = int(generated_match.group(1)) if generated_match else -1
    interesting = int(interesting_match.group(1)) if interesting_match else -1
    seed_hash = hashlib.sha256(seed).hexdigest()
    checks = {
        "real_mpi_run_succeeds": completed.returncode == 0,
        "single_master_three_workers_exercised": (
            "Mode:          single-master (3 workers)" in output
        ),
        "all_workers_ack": "Worker shutdown: acked=3/3" in output,
        "worker_staging_generated_objects": generated > 0,
        "master_published_new_objects": interesting > 0,
        "all_public_hash_names_match_real_regular_content": (
            len(valid_objects) > 1 and not invalid_objects
        ),
        "initial_seed_remains_intact": seed_hash in valid_objects,
        "no_staged_object_residue": not staged_residue,
        "completed_epoch_retired_once": len(retired_roots) == 1,
        "run_is_bounded": 0.0 < elapsed < 15.0,
    }
    result = {
        "schema": "symcc-f339-streaming-nofollow-staging-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_topology": False,
        "deployment_evidence": False,
        "configuration": {
            "work_epoch": WORK_EPOCH,
            "ranks": 4,
            "masters": 1,
            "workers": 3,
            "simulation_mode": False,
            "synthetic_output_contract_target": True,
            "wall_timeout_seconds": 30,
        },
        "command": command,
        "returncode": completed.returncode,
        "elapsed": elapsed,
        "generated_observations": generated,
        "new_interesting_objects": interesting,
        "valid_public_objects": valid_objects,
        "invalid_public_objects": invalid_objects,
        "staged_object_residue": staged_residue,
        "retired_roots": retired_roots,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "One real Open MPI run on one physical host and local overlayfs "
            "exercises worker staging, master verification, fenced promotion, "
            "cleanup, and exact shutdown ACKs. A deterministic synthetic target "
            "implements the SymCC output-directory contract but does not invoke "
            "a solver. This is not multi-host shared-storage, DSE coverage, "
            "solver throughput, bug-discovery, or LAVA-M evidence."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== MPI output ===")
    print(output, end="")
    print("=== F339 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
