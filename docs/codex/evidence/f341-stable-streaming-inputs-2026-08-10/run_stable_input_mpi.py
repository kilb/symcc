#!/usr/bin/env python3
"""Exercise F341 replacement reimport and input overflow through Open MPI."""

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
        "mpirun", "--oversubscribe", "-np", "2", "python3",
        str(REPO / "util" / "mpi_concolic_execution.py"),
        "-i", str(input_dir), "-o", str(output_dir), "-t", "1",
        "--max-idle", "2", "--wall-timeout", "30", "--", str(target),
    ]


def _valid_public_objects(output_dir: Path) -> list[str]:
    valid: list[str] = []
    for path in output_dir.iterdir():
        if re.fullmatch(r"[0-9a-f]{64}", path.name) is None:
            continue
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest == path.name:
            valid.append(path.name)
    return sorted(valid)


def _stage_residue(output_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if "staging" in path.parts and (path.is_file() or path.is_symlink())
    )


def _target(root: Path) -> tuple[Path, Path]:
    marker = root / "executed-inputs.log"
    target = root / "input-observer.py"
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "marker = Path(os.environ['F341_MARKER'])\n"
        "with marker.open('a', encoding='ascii') as stream:\n"
        "    stream.write(hashlib.sha256(data).hexdigest() + '\\n')\n"
        "    stream.flush()\n"
        "    os.fsync(stream.fileno())\n",
        encoding="ascii",
    )
    target.chmod(0o755)
    return target, marker


def _environment(epoch: str, marker: Path, max_bytes: int) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "F341_MARKER": str(marker),
        "OMPI_ALLOW_RUN_AS_ROOT": "1",
        "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMCC_RETIRED_WORK_STATE_GC_LIMIT": "0",
        "SYMCC_STANDALONE_INPUT_MAX_BYTES": str(max_bytes),
        "SYMCC_STANDALONE_RESULT_MAX_BYTES": str(max_bytes),
        "SYMCC_STANDALONE_RESULT_MAX_OBJECTS": "2",
        "SYMCC_STANDALONE_WORK_EPOCH": epoch,
        "SYMCC_STANDALONE_WORK_LEASE_TTL": "8",
    })
    return environment


def _epoch_state(output_dir: Path, epoch: str) -> tuple[bool, int]:
    active = (output_dir / f".standalone-work-{epoch}").is_dir()
    retired = len(tuple(
        output_dir.glob(f".retired-standalone-work-{epoch}-*")))
    return active, retired


def _replacement_case() -> dict[str, object]:
    first = b"F341-first-external-input"
    second = b"F341-replaced-external-input"
    hashes = sorted(hashlib.sha256(value).hexdigest()
                    for value in (first, second))
    epoch = hashlib.sha256(b"F341-replacement").hexdigest()
    with tempfile.TemporaryDirectory(prefix="symcc-f341-replacement-") as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        seed = input_dir / "seed"
        seed.write_bytes(first)
        (output_dir / "notes").write_bytes(b"non-corpus metadata")
        (output_dir / ("f" * 64)).symlink_to(output_dir / "notes")
        target, marker = _target(root)
        command = _command(input_dir, output_dir, target)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=REPO,
            env=_environment(epoch, marker, 1024),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text(
                    encoding="ascii").splitlines():
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
        replacement = input_dir / ".replacement"
        replacement.write_bytes(second)
        os.replace(replacement, seed)
        output, _ = process.communicate(timeout=45.0)
        elapsed = time.monotonic() - started
        observed = (
            marker.read_text(encoding="ascii").splitlines()
            if marker.exists() else []
        )
        public = _valid_public_objects(output_dir)
        residue = _stage_residue(output_dir)
        active, retired = _epoch_state(output_dir, epoch)

    checks = {
        "bounded_runtime": 0.0 < elapsed < 15.0,
        "clean_exit": process.returncode == 0,
        "same_name_replacement_processed_once_each": sorted(observed) == hashes,
        "two_exact_external_objects_public": public == hashes,
        "metadata_and_symlink_not_counted": (
            "External input objects:      2" in output
            and "New interesting test cases:  0" in output
        ),
        "analysis_count_is_two": (
            "Total analysis observations:       2" in output
        ),
        "no_staging_residue": not residue,
        "successful_epoch_retired": not active and retired == 1,
    }
    return {
        "mode": "same-name-replacement",
        "command": command,
        "returncode": process.returncode,
        "elapsed_seconds": elapsed,
        "expected_external_hashes": hashes,
        "observed_target_hashes": observed,
        "valid_public_objects": public,
        "staged_residue": residue,
        "active_epoch_after_run": active,
        "retired_epoch_count": retired,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "output": output,
    }


def _overflow_case() -> dict[str, object]:
    seed = b"X" * 17
    epoch = hashlib.sha256(b"F341-overflow").hexdigest()
    with tempfile.TemporaryDirectory(prefix="symcc-f341-overflow-") as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "seed").write_bytes(seed)
        target, marker = _target(root)
        command = _command(input_dir, output_dir, target)
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=_environment(epoch, marker, 16),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45.0,
            check=False,
        )
        elapsed = time.monotonic() - started
        public = _valid_public_objects(output_dir)
        residue = _stage_residue(output_dir)
        active, retired = _epoch_state(output_dir, epoch)
        target_executed = marker.exists()
        output = completed.stdout

    checks = {
        "bounded_runtime": 0.0 < elapsed < 15.0,
        "fatal_exit": completed.returncode != 0,
        "overflow_is_exact": (
            "standalone input bytes budget exceeded: "
            "observed=17, limit=16" in output
        ),
        "budget_is_reported": "Input budget:  16 bytes per object" in output,
        "target_not_executed": not target_executed,
        "nothing_public_or_staged": not public and not residue,
        "worker_shutdown_acknowledged": "acked=1/1" in output,
        "failed_epoch_remains_recoverable": active and retired == 0,
    }
    return {
        "mode": "input-overflow",
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "valid_public_objects": public,
        "staged_residue": residue,
        "active_epoch_after_run": active,
        "retired_epoch_count": retired,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [_replacement_case(), _overflow_case()]
    result = {
        "schema": "symcc-f341-stable-streaming-input-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_input_observer_target": True,
        "configuration": {"ranks": 2, "masters": 1, "workers": 1},
        "cases": cases,
        "all_checks_passed": all(
            bool(case["all_checks_passed"]) for case in cases),
        "proof_boundary": (
            "Two same-host Open MPI runs on local overlayfs exercise stable "
            "input replacement reimport and pre-dispatch byte rejection. The "
            "observer target does not invoke SymCC's solver. This is not DSE "
            "coverage, campaign throughput, multi-host storage, bug discovery, "
            "or LAVA-M evidence."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for case in cases:
        print(f"=== {case['mode']} MPI output ===")
        print(case["output"], end="")
    print("=== F341 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
