#!/usr/bin/env python3
"""Reference-cost benchmark for a 64-way MemorySSA initialization proof."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from check_memoryssa_aa_heap_initialization_oracles import prove


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=64)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=11)
    args = parser.parse_args()
    if not 2 <= args.paths <= 64:
        raise ValueError("paths must be in [2, 64]")
    if args.iterations <= 0 or args.repeats <= 0:
        raise ValueError("iterations and repeats must be positive")
    bases = {64 + index * 16 for index in range(args.paths)}
    paths = [
        {
            "base": base,
            "load": (base, 0, 1),
            "store": (base, 0, 1),
            "skipped": [64 + args.paths * 16],
        }
        for base in sorted(bases)
    ]
    assert prove(paths, bases)
    batches: list[int] = []
    for _ in range(args.repeats):
        start = time.perf_counter_ns()
        for _ in range(args.iterations):
            if not prove(paths, bases):
                raise AssertionError("reference proof changed during benchmark")
        batches.append(time.perf_counter_ns() - start)
    median_batch = int(statistics.median(batches))
    result = {
        "schema": "symcc-memoryssa-aa-heap-init-benchmark-v1",
        "parameters": {
            "paths": args.paths,
            "iterations_per_repeat": args.iterations,
            "repeats": args.repeats,
        },
        "memoryssa_rule_accepts": True,
        "legacy_dominance_rule_accepts": False,
        "guard_tree_rule_accepts_switch": False,
        "certificate_nodes": args.paths + 1,
        "certificate_edges": args.paths,
        "skipped_noalias_definitions": args.paths,
        "analytic_path_states": {
            "fork_per_memoryphi_incoming_reference": args.paths,
            "memoryssa_formula": 1,
        },
        "reference_proof_batch_cost": {
            "minimum_ns": min(batches),
            "median_ns": median_batch,
            "maximum_ns": max(batches),
        },
        "median_ns_per_proof": median_batch // args.iterations,
        "claim_boundary": (
            "Python reference-proof cost and analytic state cardinality "
            "only; not production LLVM analysis latency, coverage, solver "
            "throughput, bug yield, or end-to-end speedup"
        ),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
