#!/usr/bin/env python3
"""Independent exhaustive bit-vector oracles for live CGS predicates."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from benchmark_live_cgs import comparison_program  # noqa: E402
from live_state_search import LiveProgramGraph  # noqa: E402


COMPARISONS = (
    "eq", "ne", "ult", "ule", "ugt", "uge",
    "slt", "sle", "sgt", "sge",
)


def signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def reference(operator: str, value: int, constant: int, bits: int) -> bool:
    mask = (1 << bits) - 1
    left, right = value & mask, constant & mask
    if operator.startswith("s"):
        left, right = signed(left, bits), signed(right, bits)
    relation = operator[-2:]
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "le": left <= right,
        "gt": left > right,
        "ge": left >= right,
    }[relation]


def main() -> int:
    cases = []
    evaluations = 0
    for bits in range(1, 9):
        values = 1 << bits
        constant = (values - 1) // 3
        for operator in COMPARISONS:
            graph = LiveProgramGraph(
                comparison_program(operator, bits, constant),
                cgs_enabled=True,
            )
            token = graph.cgs_store_identity("main", "entry", 0)
            if token is None or not graph.cgs_has_target(300):
                raise AssertionError("CGS oracle target was not admitted")
            true_values = 0
            for value in range(values):
                outcome = reference(operator, value, constant, bits)
                true_values += int(outcome)
                for desired in (False, True):
                    guidance = graph.cgs_guidance(
                        "main:no", {token: value}, ((300, desired),),
                    )
                    if guidance is None:
                        raise AssertionError("CGS unexpectedly disabled")
                    if (guidance.priority == 2) != (outcome == desired):
                        raise AssertionError(
                            f"oracle mismatch: {operator}/{bits}/{value}/{desired}"
                        )
                    evaluations += 1
            cases.append({
                "operator": operator,
                "bits": bits,
                "values": values,
                "true_values": true_values,
            })
    print(json.dumps({
        "schema": "symcc-live-cgs-oracle-v1",
        "cases": len(cases),
        "predicate_evaluations": evaluations,
        "bit_widths": [1, 2, 3, 4, 5, 6, 7, 8],
        "operators": list(COMPARISONS),
        "all_passed": True,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
