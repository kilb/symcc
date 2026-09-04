#!/usr/bin/env python3
"""Mechanism cost for one certified finite-domain heap release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter_ns


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))
sys.path.insert(0, str(ROOT / "util"))

from check_heap_lifetime_union_oracles import build_program  # noqa: E402
from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def summarize(samples: list[int]) -> dict[str, int]:
    return {
        "minimum_ns": min(samples),
        "median_ns": int(median(samples)),
        "maximum_ns": max(samples),
    }


def run(domain: int, repeats: int) -> dict:
    samples: list[int] = []
    steps: set[int] = set()
    forks: set[int] = set()
    for _ in range(repeats):
        with tempfile.TemporaryDirectory() as root:
            store = LiveStateStore(root, page_size=64)
            executor = LiveContinuationExecutor(store)
            checkpoint = executor.create(
                build_program(domain), input_bytes=b"\x00"
            )
            started = perf_counter_ns()
            result = executor.resume(checkpoint, max_steps=4096)
            samples.append(perf_counter_ns() - started)
            steps.add(int(result["steps"]))
            forks.add(int(result["forks"]))
    if len(steps) != 1 or forks != {0}:
        raise AssertionError("heap release benchmark is nondeterministic")
    return {
        "schema": "symcc-heap-lifetime-union-benchmark-v1",
        "parameters": {"domain": domain, "repeats": repeats},
        "resume_cost": summarize(samples),
        "instruction_steps": steps.pop(),
        "continuation_forks": 0,
        "analytic_object_resolution_states": {
            "fork_per_object_reference": domain,
            "conditional_lifetime_update": 1,
        },
        "claim_boundary": (
            "local mechanism cost and analytic state cardinality only; no "
            "public benchmark coverage, throughput, or POSE reproduction claim"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=11)
    args = parser.parse_args()
    if not 1 <= args.domain <= 32 or not 1 <= args.repeats <= 101:
        raise SystemExit("require domain 1..32 and repeats 1..101")
    print(json.dumps(
        run(args.domain, args.repeats),
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
