#!/usr/bin/env python3
"""Exercise F344 through the production SymCC worker wrapper and subprocess."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
if str(UTIL) not in sys.path:
    sys.path.insert(0, str(UTIL))

import mpi_fuzzing_helper as runner  # noqa: E402


TARGET = r'''#!/usr/bin/env python3
import os
from pathlib import Path

output = Path(os.environ["SYMCC_OUTPUT_DIR"])
mode = os.environ["F344_MODE"]
if mode == "success":
    (output / "case-a").write_bytes(b"F344-first")
    (output / "case-a.hints").write_text("0:00:01\n", encoding="ascii")
    (output / "case-b").write_bytes(b"F344-second")
elif mode == "objects":
    for index in range(3):
        (output / f"case-{index}").write_bytes(bytes((index,)))
elif mode == "bytes":
    (output / "case-a").write_bytes(b"abc")
    (output / "case-b").write_bytes(b"def")
elif mode == "hints":
    (output / "case").write_bytes(b"data")
    (output / "case.hints").write_text(
        "0:00:01\n1:01:02\n", encoding="ascii")
else:
    raise SystemExit(2)
'''


def _run_case(root: Path, mode: str) -> dict:
    output = root / f"output-{mode}"
    seed = root / "seed"
    environment = dict(os.environ)
    environment.update({
        "F344_MODE": mode,
        "SYMCC_STRING_SOLVER_ENABLE": "0",
        "SYMCC_MAX_TRANSPORT_INPUT": "64",
    })
    limits = {
        "success": (2, 64, 2),
        "objects": (2, 64, 2),
        "bytes": (2, 5, 2),
        "hints": (1, 64, 1),
    }[mode]
    started = time.monotonic()
    try:
        result = runner.run_symcc_worker(
            [str(root / "target.py")],
            str(seed),
            str(output),
            2,
            False,
            engine_name="symcc",
            base_env=environment,
            result_max_objects=limits[0],
            result_max_bytes=limits[1],
            result_max_hints=limits[2],
        )
    except runner._WorkerResultBudgetExceeded as error:
        return {
            "mode": mode,
            "accepted": False,
            "budget_error": error.payload(),
            "target_returncode": error.retcode,
            "target_elapsed_seconds": error.elapsed,
            "post_elapsed_seconds": error.post_elapsed,
            "wall_elapsed_seconds": time.monotonic() - started,
            "visible_outputs": sorted(
                path.name for path in output.iterdir()
                if not path.name.startswith(".")
            ),
        }
    tests, generated, returncode, elapsed, killed, post_elapsed = result
    return {
        "mode": mode,
        "accepted": True,
        "generated": generated,
        "returned": len(tests),
        "target_returncode": returncode,
        "target_elapsed_seconds": elapsed,
        "post_elapsed_seconds": post_elapsed,
        "wall_elapsed_seconds": time.monotonic() - started,
        "killed": killed,
        "contents_hex": sorted(test["content"].hex() for test in tests),
        "content_hashes": sorted(
            hashlib.sha256(test["content"]).hexdigest() for test in tests),
        "hints": sorted(test.get("hints", []) for test in tests),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symcc-f344-integration-") as tmp:
        root = Path(tmp)
        seed = root / "seed"
        seed.write_bytes(b"seed")
        target = root / "target.py"
        target.write_text(TARGET, encoding="ascii")
        target.chmod(0o755)
        cases = [_run_case(root, mode)
                 for mode in ("success", "objects", "bytes", "hints")]

    by_mode = {case["mode"]: case for case in cases}
    expected_hashes = sorted(
        hashlib.sha256(content).hexdigest()
        for content in (b"F344-first", b"F344-second")
    )
    checks = {
        "actual_subprocess_target": all(
            case["target_returncode"] == 0 for case in cases),
        "success_contents_exact": (
            by_mode["success"]["accepted"] is True
            and by_mode["success"]["generated"] == 2
            and by_mode["success"]["returned"] == 2
            and by_mode["success"]["content_hashes"] == expected_hashes
            and by_mode["success"]["hints"] == [[], [(0, 0, 1)]]
        ),
        "object_overflow_exact": by_mode["objects"]["budget_error"] == {
            "resource": "objects", "observed": 3, "limit": 2,
            "objects": 3,
        },
        "byte_overflow_exact": by_mode["bytes"]["budget_error"] == {
            "resource": "bytes", "observed": 6, "limit": 5,
            "objects": 2,
        },
        "hint_overflow_exact": by_mode["hints"]["budget_error"] == {
            "resource": "hints", "observed": 2, "limit": 1,
            "objects": 1,
        },
        "all_cases_bounded": all(
            0.0 < case["wall_elapsed_seconds"] < 10.0 for case in cases),
    }
    result = {
        "schema": "symcc-f344-bounded-hybrid-worker-integration-v1",
        "configuration": {
            "cases": ["success", "objects", "bytes", "hints"],
            "single_object_transport_limit": 64,
            "target": "synthetic executable Python producer",
            "wrapper": "production SymCCEngine + run_symcc_worker",
        },
        "cases": cases,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local integration executes a real child process through the "
            "production SymCC engine wrapper and worker result admission path. "
            "It does not use MPI transport, afl-showmap, a symbolic solver, or a "
            "fuzzing campaign, and makes no coverage, throughput, bug-discovery, "
            "or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
