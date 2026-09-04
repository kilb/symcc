#!/usr/bin/env python3
"""Measure UCSan byte-initialization propagation and UBI sink outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import benchmark_ucsan_explicit_objects as checker


EXPECTED = {
    0: None,
    1: "use-before-initialization in branch condition",
    2: "use-before-initialization in pointer dereference",
    3: None,
    4: "use-before-initialization in branch condition",
    5: None,
    6: "use-before-initialization in branch condition",
    7: "use-before-initialization in branch condition",
    8: "use-before-initialization in pointer dereference",
    9: "use-before-initialization in branch condition",
    10: None,
    11: "use-before-initialization in memory access extent",
    12: "use-before-initialization in branch condition",
    13: None,
    14: "use-before-initialization in branch condition",
    15: None,
    16: None,
    17: "use-before-initialization in branch condition",
    18: "use-before-initialization in branch condition",
    19: None,
    20: None,
    21: None,
    22: "use-before-initialization in pointer dereference",
    23: "use-after-free",
    24: None,
    25: "use-before-initialization in branch condition",
    26: "use-before-initialization in pointer dereference",
    27: None,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")

    checker.EXPECTED = EXPECTED
    report = checker.run_benchmark(args.executable.resolve(), args.samples)
    report["schema"] = "symcc-f395-byte-initialization-ubi-benchmark-v1"
    report["claim_boundary"] = (
        "whole-process byte-initialization propagation and UBI sink outcomes; "
        "not a coverage, vulnerability-yield, per-instruction-cost, or "
        "cross-system speedup result"
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="ascii")


if __name__ == "__main__":
    main()
