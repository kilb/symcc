#!/usr/bin/env python3
"""Reference cost benchmark for guarded heap-union initialization proofs."""

from __future__ import annotations

import argparse
import json
from statistics import median
from time import perf_counter_ns

from check_guarded_heap_union_initialization_oracles import (
    complete_paths,
    guarded_cover,
)


def summarize(samples: list[int]) -> dict[str, int]:
    return {
        "minimum_ns": min(samples),
        "median_ns": int(median(samples)),
        "maximum_ns": max(samples),
    }


def run(depth: int, repeats: int, iterations: int) -> dict:
    paths = complete_paths(depth)
    bases = [64 + 16 * index for index in range(len(paths))]
    selected = dict(zip(paths, bases, strict=True))
    stores = {
        path: (base, 0, 8)
        for path, base in zip(paths, bases, strict=True)
    }
    offsets = {base: 0 for base in bases}
    samples: list[int] = []
    for _repeat in range(repeats):
        started = perf_counter_ns()
        for _iteration in range(iterations):
            accepted, certificate = guarded_cover(
                paths, selected, stores, offsets, 8
            )
            if not accepted or certificate != tuple(bases):
                raise AssertionError("guarded proof benchmark changed result")
        samples.append(perf_counter_ns() - started)
    old_dominance_accepts = all(False for _path in paths)
    if old_dominance_accepts:
        raise AssertionError("branch-local stores unexpectedly dominate the merge")
    return {
        "schema": "symcc-guarded-heap-union-init-benchmark-v1",
        "parameters": {
            "depth": depth,
            "paths": len(paths),
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_proof_batch_cost": summarize(samples),
        "median_ns_per_proof": int(median(samples)) // iterations,
        "certificate_paths": len(paths),
        "certificate_decisions": depth * len(paths),
        "legacy_dominance_rule_accepts": old_dominance_accepts,
        "guard_correlated_rule_accepts": True,
        "analytic_path_states": {
            "fork_per_guard_assignment_reference": len(paths),
            "guarded_memory_phi_formula": 1,
        },
        "claim_boundary": (
            "Python reference-proof cost and analytic state cardinality only; "
            "not production C++ lowering latency, coverage, or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=10_000)
    args = parser.parse_args()
    if (
        not 1 <= args.depth <= 6
        or not 1 <= args.repeats <= 101
        or not 1 <= args.iterations <= 1_000_000
    ):
        raise SystemExit(
            "require depth 1..6, repeats 1..101, iterations 1..1000000"
        )
    print(json.dumps(
        run(args.depth, args.repeats, args.iterations),
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
