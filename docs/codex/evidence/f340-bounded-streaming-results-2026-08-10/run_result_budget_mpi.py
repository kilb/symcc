#!/usr/bin/env python3
"""Exercise F340 success and admission failures through real Open MPI."""

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


def _command(input_dir: Path, output_dir: Path, target: Path) -> list[str]:
    return [
        "mpirun",
        "--oversubscribe",
        "-np",
        "2",
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


def _public_objects(output_dir: Path) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    invalid: list[str] = []
    for path in output_dir.iterdir():
        if re.fullmatch(r"[0-9a-f]{64}", path.name) is None:
            continue
        try:
            metadata = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            invalid.append(path.name)
            continue
        if stat.S_ISREG(metadata.st_mode) and digest == path.name:
            valid.append(path.name)
        else:
            invalid.append(path.name)
    return sorted(valid), sorted(invalid)


def _stage_residue(output_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if "staging" in path.parts and (path.is_file() or path.is_symlink())
    )


def _run_case(mode: str) -> dict[str, object]:
    seed = b"F340-parent"
    seed_hash = hashlib.sha256(seed).hexdigest()
    max_objects = 2
    max_bytes = 16
    with tempfile.TemporaryDirectory(prefix=f"symcc-f340-{mode}-") as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "seed").write_bytes(seed)
        target = root / "result-contract-target.py"
        target.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "if data != b'F340-parent':\n"
            "    raise SystemExit(0)\n"
            "output = Path(os.environ['SYMCC_OUTPUT_DIR'])\n"
            "output.mkdir(parents=True, exist_ok=True)\n"
            "mode = os.environ['F340_CASE']\n"
            "if mode == 'success':\n"
            "    (output / 'child').write_bytes(b'F340-child')\n"
            "elif mode == 'objects':\n"
            "    for index in range(3):\n"
            "        (output / f'child-{index}').write_bytes(bytes([index]))\n"
            "elif mode == 'bytes':\n"
            "    (output / 'child').write_bytes(b'X' * 17)\n",
            encoding="ascii",
        )
        target.chmod(0o755)
        command = _command(input_dir, output_dir, target)
        epoch = hashlib.sha256(f"F340-{mode}".encode("ascii")).hexdigest()
        environment = dict(os.environ)
        environment.update({
            "F340_CASE": mode,
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "0",
            "SYMCC_STANDALONE_RESULT_MAX_BYTES": str(max_bytes),
            "SYMCC_STANDALONE_RESULT_MAX_OBJECTS": str(max_objects),
            "SYMCC_STANDALONE_WORK_EPOCH": epoch,
            "SYMCC_STANDALONE_WORK_LEASE_TTL": "6",
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
        valid, invalid = _public_objects(output_dir)
        residue = _stage_residue(output_dir)
        active_epoch = output_dir / f".standalone-work-{epoch}"
        retired = tuple(output_dir.glob(f".retired-standalone-work-{epoch}-*"))
        active_epoch_exists = active_epoch.is_dir()
        retired_epoch_count = len(retired)
        output = completed.stdout

    expected_failure = mode != "success"
    expected_resource = "objects" if mode == "objects" else "bytes"
    expected_observed = 3 if mode == "objects" else 17
    checks = {
        "bounded_runtime": 0.0 < elapsed < 15.0,
        "configured_budget_is_reported": (
            "Result budget: 2 objects, 16 bytes per parent" in output
        ),
        "exact_public_objects": (
            len(valid) == (1 if expected_failure else 2) and not invalid
        ),
        "seed_is_intact": seed_hash in valid,
        "no_staged_object_residue": not residue,
        "exit_status_matches_admission": (
            completed.returncode != 0 if expected_failure
            else completed.returncode == 0
        ),
        "failure_is_explicit": (
            (
                f"standalone result {expected_resource} budget exceeded: "
                f"observed={expected_observed}, "
                f"limit={max_objects if mode == 'objects' else max_bytes}"
            ) in output
            if expected_failure else
            "budget exceeded" not in output
        ),
        "epoch_lifecycle_matches_admission": (
            active_epoch_exists and retired_epoch_count == 0
            if expected_failure else
            not active_epoch_exists and retired_epoch_count == 1
        ),
    }
    return {
        "mode": mode,
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "valid_public_objects": valid,
        "invalid_public_objects": invalid,
        "staged_residue": residue,
        "active_epoch_after_run": active_epoch_exists,
        "retired_epoch_count": retired_epoch_count,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [_run_case(mode) for mode in ("success", "objects", "bytes")]
    result = {
        "schema": "symcc-f340-bounded-streaming-result-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_output_contract_target": True,
        "configuration": {
            "ranks": 2,
            "masters": 1,
            "workers": 1,
            "max_objects_per_parent": 2,
            "max_bytes_per_parent": 16,
        },
        "cases": cases,
        "all_checks_passed": all(
            bool(case["all_checks_passed"]) for case in cases),
        "proof_boundary": (
            "Three same-host Open MPI runs on local overlayfs exercise one "
            "successful result and independent object/byte admission failures. "
            "A deterministic output-contract target does not invoke SymCC's "
            "solver; these results do not measure DSE coverage, multi-host "
            "storage, campaign throughput, bug discovery, or LAVA-M."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for case in cases:
        print(f"=== {case['mode']} MPI output ===")
        print(case["output"], end="")
    print("=== F340 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
