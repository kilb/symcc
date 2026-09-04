#!/usr/bin/env python3
"""Reproducible mechanism measurements for interpreter-level ConDPOR."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from condpor_interpreter import (  # noqa: E402
    CONDPOR_PROGRAM_SCHEMA,
    explore_condpor_program,
)


def _writers(count: int) -> dict[str, Any]:
    return {
        "schema": CONDPOR_PROGRAM_SCHEMA,
        "name": f"{count}-writer-coherence-enumeration",
        "bit_width": 8,
        "memory": {"x": 0},
        "threads": {
            str(tid): [
                {"op": "write", "object": "x", "value": tid + 1},
                {"op": "halt"},
            ]
            for tid in range(count)
        },
    }


def _regeneration() -> dict[str, Any]:
    return {
        "schema": CONDPOR_PROGRAM_SCHEMA,
        "name": "read-write-control-regeneration",
        "bit_width": 8,
        "memory": {"x": 0},
        "threads": {
            "0": [
                {"op": "read", "object": "x", "dst": "r"},
                {
                    "op": "branch",
                    "condition": {"op": "eq", "args": ["r", 1]},
                    "then": "failure",
                    "else": "done",
                },
                {"op": "label", "name": "failure"},
                {"op": "assert", "condition": False},
                {"op": "label", "name": "done"},
                {"op": "halt"},
            ],
            "1": [
                {"op": "write", "object": "x", "value": 1},
                {"op": "halt"},
            ],
        },
    }


def _measure(subject: dict[str, Any], samples: int) -> dict[str, Any]:
    durations = []
    certificate = None
    for _ in range(samples):
        started = time.perf_counter_ns()
        current = explore_condpor_program(subject)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        if certificate is None:
            certificate = current
        elif current != certificate:
            raise RuntimeError("ConDPOR exploration is not deterministic")
    assert certificate is not None
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
    return {
        "name": subject["name"],
        "samples": samples,
        "median_ms": statistics.median(durations),
        "p95_ms": ordered[p95_index],
        "minimum_ms": min(durations),
        "maximum_ms": max(durations),
        "certificate_sha256": certificate["certificate_sha256"],
        "status": certificate["status"],
        "statistics": certificate["statistics"],
        "bound_reasons": certificate["bound_reasons"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--max-writers", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.samples < 1 or args.max_writers < 1 or args.max_writers > 7:
        parser.error("samples must be positive and max-writers must be in [1, 7]")
    payload = {
        "schema": "symcc-f392-condpor-mechanism-benchmark-v1",
        "clock": "time.perf_counter_ns",
        "claims": {
            "mechanism_measurement_only": True,
            "coverage_or_bug_finding_improvement_claimed": False,
            "cross_system_performance_claimed": False,
        },
        "cases": [
            *(
                _measure(_writers(count), args.samples)
                for count in range(1, args.max_writers + 1)
            ),
            _measure(_regeneration(), args.samples),
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
