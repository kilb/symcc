#!/usr/bin/env python3
"""Exercise F343 synthetic mutation generation through real Open MPI."""

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
WORK_EPOCH = "f343" * 16


def _corpus(output: Path) -> tuple[dict[str, bytes], list[str]]:
    valid: dict[str, bytes] = {}
    invalid = []
    for path in output.iterdir():
        if re.fullmatch(r"[0-9a-f]{64}", path.name) is None:
            continue
        try:
            metadata = path.lstat()
            content = path.read_bytes()
        except OSError:
            invalid.append(path.name)
            continue
        if (not stat.S_ISREG(metadata.st_mode)
                or hashlib.sha256(content).hexdigest() != path.name):
            invalid.append(path.name)
            continue
        valid[path.name] = content
    return valid, sorted(invalid)


def _staging_residue(output: Path) -> list[str]:
    return sorted(
        str(path.relative_to(output))
        for path in output.rglob("*")
        if "staging" in path.parts and (path.is_file() or path.is_symlink())
    )


def _statistic(output: str, label: str) -> int:
    match = re.search(rf"{re.escape(label)}:\s+(\d+)", output)
    return int(match.group(1)) if match else -1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f343-mpi-") as tmp:
        root = Path(tmp)
        inputs = root / "input"
        output = root / "output"
        observer = root / "observed-inputs.txt"
        inputs.mkdir()
        seed = b"\x00"
        (inputs / "seed").write_bytes(seed)
        target = root / "simulation-gate.py"
        target.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "with open(os.environ['F343_OBSERVER'], 'a', encoding='ascii') as f:\n"
            "    f.write(data.hex() + '\\n')\n"
            "if data != b'\\x00':\n"
            "    output = Path(os.environ['SYMCC_OUTPUT_DIR'])\n"
            "    output.mkdir(parents=True, exist_ok=True)\n"
            "    (output / 'echo').write_bytes(data)\n",
            encoding="ascii",
        )
        target.chmod(0o755)
        command = [
            "mpirun",
            "--oversubscribe",
            "-np", "2",
            "python3",
            str(REPO / "util" / "mpi_concolic_execution.py"),
            "-i", str(inputs),
            "-o", str(output),
            "-t", "1",
            "--max-idle", "1",
            "--wall-timeout", "30",
            "--simulate",
            "--",
            str(target),
        ]
        environment = dict(os.environ)
        environment.update({
            "F343_OBSERVER": str(observer),
            "OMPI_ALLOW_RUN_AS_ROOT": "1",
            "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SYMCC_STANDALONE_INPUT_MAX_BYTES": "5",
            "SYMCC_STANDALONE_RESULT_MAX_BYTES": "5",
            "SYMCC_STANDALONE_RESULT_MAX_OBJECTS": "5",
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
        log = completed.stdout
        valid, invalid = _corpus(output)
        observed_hex = (
            observer.read_text(encoding="ascii").splitlines()
            if observer.exists() else []
        )
        observed = [bytes.fromhex(value) for value in observed_hex]
        staged = _staging_residue(output)
        active = sorted(
            path.name for path in output.iterdir()
            if path.name.startswith(".standalone-work-")
        )
        retired = sorted(
            path.name for path in output.iterdir()
            if path.name.startswith(".retired-standalone-work-")
        )

    public_contents = sorted(content.hex() for content in valid.values())
    observed_contents = sorted(content.hex() for content in observed)
    generated = _statistic(log, "Total test cases generated")
    interesting = _statistic(log, "New interesting test cases")
    analyses = _statistic(log, "Total analysis observations")
    checks = {
        "clean_exit": completed.returncode == 0,
        "actual_single_worker_mpi": (
            "Mode:          single-master (1 workers)" in log
        ),
        "all_workers_acknowledged": "Worker shutdown: acked=1/1" in log,
        "simulation_mode_configured": "--simulate" in command,
        "exact_result_budget_reported": (
            "Result budget: 5 objects, 5 bytes per parent" in log
        ),
        "seed_and_generated_children_are_public": (
            len(valid) > 1 and seed.hex() in public_contents and not invalid
        ),
        "all_public_objects_are_one_byte": all(
            len(content) == 1 for content in valid.values()
        ),
        "each_public_object_is_executed_once": (
            observed_contents == public_contents
            and len(observed_contents) == len(set(observed_contents))
        ),
        "generation_and_publication_are_observed": (
            generated >= 5 and interesting == len(valid) - 1
        ),
        "analysis_count_matches_public_corpus": analyses == len(valid),
        "no_staging_residue": not staged,
        "successful_epoch_retired_once": not active and len(retired) == 1,
        "bounded_runtime": 0.0 < elapsed < 15.0,
    }
    result = {
        "schema": "symcc-f343-bounded-streaming-simulation-mpi-v1",
        "actual_mpi_transport": True,
        "actual_multi_host": False,
        "synthetic_observer_target": True,
        "configuration": {
            "ranks": 2,
            "masters": 1,
            "workers": 1,
            "seed_bytes": 1,
            "simulation_outputs_per_seed": 5,
            "result_max_objects": 5,
            "result_max_bytes": 5,
            "input_max_bytes": 5,
            "work_epoch": WORK_EPOCH,
        },
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "generated_observations": generated,
        "new_interesting_objects": interesting,
        "analysis_observations": analyses,
        "public_object_hashes": sorted(valid),
        "public_contents_hex": public_contents,
        "invalid_public_objects": invalid,
        "observed_contents_hex": observed_contents,
        "staging_residue": staged,
        "active_epochs": active,
        "retired_epochs": retired,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "output": log,
        "proof_boundary": (
            "One real two-rank Open MPI run on one host and local storage "
            "exercises the production --simulate worker path, private result "
            "discovery, staging, fenced publication, deduplication, and "
            "acknowledged shutdown. The one-byte observer target deliberately "
            "echoes non-seed children to stop recursive simulation and does not "
            "invoke a symbolic solver. This is not multi-host, DSE coverage, "
            "campaign throughput, bug-discovery, or LAVA-M uplift evidence."
        ),
    }
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("=== MPI output ===")
    print(log, end="")
    print("=== F343 result ===")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
