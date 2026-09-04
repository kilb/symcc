#!/usr/bin/env python3
"""Independent exhaustive oracles for bounded CBC prefix admission."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_live_cbc import sequential_program  # noqa: E402
from live_state_search import LiveProgramGraph, search_decision_token  # noqa: E402


def main() -> int:
    cases = []
    for branch_count in range(2, 9):
        graph = LiveProgramGraph(
            sequential_program(branch_count), cbc_enabled=True,
        )
        accepted = []
        for pattern in itertools.product((False, True), repeat=branch_count):
            guidance = graph.cbc_guidance(tuple(
                search_decision_token(1000 + index, choice)
                for index, choice in enumerate(pattern)
            ))
            if guidance is None:
                raise AssertionError("CBC unexpectedly disabled")
            if guidance.accepted:
                accepted.append(pattern)
        expected = [(False,) * branch_count, (True,) * branch_count]
        if accepted != expected:
            raise AssertionError(
                f"CBC accepted {accepted!r}, expected {expected!r}"
            )
        for index in range(branch_count):
            if {pattern[index] for pattern in accepted} != {False, True}:
                raise AssertionError(f"branch {index} lost an outcome")
        cases.append({
            "branches": branch_count,
            "enumerated_patterns": 1 << branch_count,
            "accepted_patterns": len(accepted),
            "outcomes_preserved": True,
        })
    print(json.dumps({
        "schema": "symcc-live-cbc-oracle-v1",
        "cases": cases,
        "all_passed": True,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
