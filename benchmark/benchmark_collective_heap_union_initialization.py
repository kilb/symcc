#!/usr/bin/env python3
"""Reference proof cost for collective heap-union initialization."""

from __future__ import annotations

import argparse
import json
from statistics import median
from time import perf_counter_ns

from check_collective_heap_union_initialization_oracles import (
    collective_cover,
)


def summarize(samples: list[int]) -> dict[str, int]:
    return {
        "minimum_ns": min(samples),
        "median_ns": int(median(samples)),
        "maximum_ns": max(samples),
    }


def run(domain: int, repeats: int, iterations: int) -> dict:
    bases = [64 + 16 * index for index in range(domain)]
    stores = [(base, 8, True) for base in bases]
    samples: list[int] = []
    for _ in range(repeats):
        started = perf_counter_ns()
        for _iteration in range(iterations):
            accepted, certificate = collective_cover(bases, stores, 8)
            if not accepted or certificate != tuple(bases):
                raise AssertionError("collective proof benchmark changed result")
        samples.append(perf_counter_ns() - started)
    return {
        "schema": "symcc-collective-heap-union-init-benchmark-v1",
        "parameters": {
            "domain": domain,
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_proof_batch_cost": summarize(samples),
        "median_ns_per_proof": int(median(samples)) // iterations,
        "certificate_bases": domain,
        "dominating_store_witnesses": domain,
        "analytic_path_states": {
            "fork_by_victim_and_survivor_reference": domain * (domain - 1),
            "merged_lifetime_and_alias_formula": 1,
        },
        "claim_boundary": (
            "Python reference proof cost and analytic state cardinality only; "
            "not production C++ lowering latency or end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", type=int, default=32)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=10_000)
    args = parser.parse_args()
    if (
        not 2 <= args.domain <= 256
        or not 1 <= args.repeats <= 101
        or not 1 <= args.iterations <= 1_000_000
    ):
        raise SystemExit("require domain 2..256, repeats 1..101, iterations 1..1000000")
    print(
        json.dumps(
            run(args.domain, args.repeats, args.iterations),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
