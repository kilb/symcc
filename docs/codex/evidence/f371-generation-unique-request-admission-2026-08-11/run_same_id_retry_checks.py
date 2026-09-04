#!/usr/bin/env python3
"""Executable counterfactuals for F371 same-ID retry generation fencing."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import query_store as query_store_module  # noqa: E402
from query_store import PersistentSubprocessSolver, WorkLease  # noqa: E402


def _lease(
    query_id: str,
    *,
    prefix_path: Path = Path("prefix.smt2"),
) -> WorkLease:
    return WorkLease(
        query_id,
        1,
        Path("query.smt2"),
        "prefix",
        prefix_path,
        Path("target.smt2"),
        1000,
    )


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not path.exists():
        raise RuntimeError(f"helper did not publish {path}")


def _helper(root: Path) -> PersistentSubprocessSolver:
    root.mkdir(parents=True)
    startups = root / "startups"
    ready = root / "duplicate-ready"
    script = (
        "import json,os,pathlib,sys,time;"
        f"\nstartups=pathlib.Path({str(startups)!r})"
        "\ntry: generation=int(startups.read_text())+1"
        "\nexcept (FileNotFoundError,ValueError): generation=1"
        "\nstartups.write_text(str(generation)); seen={}"
        f"\nready=pathlib.Path({str(ready)!r})"
        "\nfor line in sys.stdin:"
        "\n q=line.rstrip('\\n').split('\\t')[0]"
        "\n seen[q]=seen.get(q,0)+1"
        "\n if q == 'repeat' and seen[q] > 1: time.sleep(0.5)"
        "\n print(json.dumps({'request_id':q,'status':'sat',"
        "'assignments':{'0':11},'solver':'f371-helper',"
        "'generation':generation,'helper_pid':os.getpid()}),flush=True)"
        "\n if q == 'repeat' and seen[q] == 1:"
        "\n  time.sleep(0.2)"
        "\n  print(json.dumps({'request_id':q,'status':'sat',"
        "'assignments':{'0':22},'solver':'stale-duplicate',"
        "'generation':generation,'helper_pid':os.getpid()}),flush=True)"
        "\n  ready.write_text(str(generation))"
    )
    return PersistentSubprocessSolver((sys.executable, "-c", script))


def _startup_count(root: Path) -> int:
    return int((root / "startups").read_text(encoding="ascii"))


def _counterfactual(root: Path) -> dict[str, object]:
    solver = _helper(root)
    try:
        with mock.patch.object(
            solver,
            "_admit_generation_request",
            side_effect=lambda process, _query_id: process,
        ):
            first = dict(solver(_lease("repeat")))
            _wait_for(root / "duplicate-ready")
            second = dict(solver(_lease("repeat")))
        return {
            "first_assignment": first["assignments"]["0"],
            "same_generation": first["helper_pid"] == second["helper_pid"],
            "second_assignment": second["assignments"]["0"],
            "stale_duplicate_accepted": second["assignments"]["0"] == 22,
            "startup_count": _startup_count(root),
        }
    finally:
        solver.close()


def _production(root: Path) -> dict[str, object]:
    solver = _helper(root)
    try:
        first = dict(solver(_lease("repeat")))
        _wait_for(root / "duplicate-ready")
        second = dict(solver(_lease("repeat")))
        return {
            "first_assignment": first["assignments"]["0"],
            "fresh_response_admitted": second["assignments"]["0"] == 11,
            "generation_rotated": first["helper_pid"] != second["helper_pid"],
            "second_assignment": second["assignments"]["0"],
            "startup_count": _startup_count(root),
        }
    finally:
        solver.close()


def _compatibility(root: Path) -> dict[str, bool]:
    solver = _helper(root)
    try:
        original_pid = solver.process.pid
        rejected = False
        try:
            solver(_lease("preflight-id", prefix_path=Path("bad\npath.smt2")))
        except ValueError:
            rejected = True
        after_rejection = dict(solver(_lease("preflight-id")))
        distinct = dict(solver(_lease("different-id")))
        return {
            "distinct_ids_reuse_generation": (
                after_rejection["helper_pid"] == distinct["helper_pid"] == original_pid
            ),
            "preflight_does_not_admit_or_rotate": (
                rejected and after_rejection["helper_pid"] == original_pid
            ),
        }
    finally:
        solver.close()


def _bounded_history(root: Path) -> bool:
    solver = _helper(root)
    try:
        first = dict(solver(_lease("first-id")))
        with mock.patch.object(
            query_store_module,
            "_MAX_PERSISTENT_GENERATION_REQUESTS",
            1,
        ):
            second = dict(solver(_lease("second-id")))
        return first["helper_pid"] != second["helper_pid"]
    finally:
        solver.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        counterfactual = _counterfactual(root / "counterfactual")
        production = _production(root / "production")
        compatibility = _compatibility(root / "compatibility")
        bounded_history = _bounded_history(root / "bounded-history")

    checks = [
        counterfactual["first_assignment"] == 11,
        counterfactual["second_assignment"] == 22,
        counterfactual["same_generation"] is True,
        counterfactual["stale_duplicate_accepted"] is True,
        counterfactual["startup_count"] == 1,
        production["first_assignment"] == 11,
        production["second_assignment"] == 11,
        production["fresh_response_admitted"] is True,
        production["generation_rotated"] is True,
        production["startup_count"] == 2,
        all(compatibility.values()),
        bounded_history,
    ]
    output = {
        "all_checks_passed": all(checks),
        "bounded_history_rotates_generation": bounded_history,
        "compatibility": compatibility,
        "counterfactual_without_f371": counterfactual,
        "production_f371": production,
        "schema": "symcc-f371-generation-unique-request-admission-evidence-v1",
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
