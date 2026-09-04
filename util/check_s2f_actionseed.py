#!/usr/bin/env python3
"""Integration check for QSYM's S2F actionseed file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_target(executable: Path, seed: Path, out_dir: Path, telemetry: Path,
               action_file: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "SYMCC_INPUT_FILE": str(seed),
        "SYMCC_OUTPUT_DIR": str(out_dir),
        "SYMCC_TELEMETRY_OUT": str(telemetry),
    })
    if action_file is not None:
        env["SYMCC_S2F_ACTIONS"] = str(action_file)
    else:
        env.pop("SYMCC_S2F_ACTIONS", None)
    subprocess.run([str(executable), str(seed)], env=env, check=False)
    return json.loads(telemetry.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_s2f_actionseed.py EXECUTABLE", file=sys.stderr)
        return 2

    executable = Path(argv[1])
    with tempfile.TemporaryDirectory(prefix="symcc-s2f-actionseed.") as tmp:
        root = Path(tmp)
        seed = root / "seed"
        seed.write_bytes(b"A")

        baseline = run_target(
            executable, seed, root / "baseline-out", root / "baseline.json")
        if baseline.get("open_branches"):
            branch = int(baseline["open_branches"][0])
        elif baseline.get("branch_trace"):
            branch = int(baseline["branch_trace"][0][2])
        else:
            print("baseline did not expose a branch", file=sys.stderr)
            return 1

        skip_actions = root / "skip.actions"
        skip_actions.write_text(f"{branch} skip\n", encoding="ascii")
        skipped = run_target(
            executable, seed, root / "skip-out", root / "skip.json",
            skip_actions)
        if skipped.get("s2f_skip_actions", 0) < 1:
            print("skip action was not reached", file=sys.stderr)
            return 1
        if skipped.get("generated", 0) != 0:
            print("skip action generated a testcase", file=sys.stderr)
            return 1

        solve_actions = root / "solve.actions"
        solve_actions.write_text(f"{branch} solve\n", encoding="ascii")
        solved = run_target(
            executable, seed, root / "solve-out", root / "solve.json",
            solve_actions)
        if solved.get("s2f_solve_actions", 0) < 1:
            print("solve action was not reached", file=sys.stderr)
            return 1
        if solved.get("generated", 0) < 1:
            print("solve action did not generate a testcase", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
