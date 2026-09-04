#!/usr/bin/env python3
"""Mechanism benchmark for callsite-instantiated F409 effect certificates."""

from __future__ import annotations

import argparse
import json
from statistics import median
from time import perf_counter_ns

from check_interprocedural_heap_effect_oracles import (
    Interval,
    valid_artifact,
    validate_artifact,
)


def benchmark(*, callsites: int, repeats: int, iterations: int) -> dict:
    if not 2 <= callsites <= 64:
        raise ValueError("callsites must be in [2, 64]")
    if repeats < 3 or iterations < 1:
        raise ValueError("repeats must be at least 3 and iterations positive")
    certificates = [
        valid_artifact(
            callsites=callsites,
            callsite=callsite,
            size=8,
            load=Interval(callsite % 8, 1),
            store=Interval(callsite % 8, 1),
        )
        for callsite in range(callsites)
    ]
    samples: list[int] = []
    for _ in range(repeats):
        started = perf_counter_ns()
        for _ in range(iterations):
            if not all(validate_artifact(item) for item in certificates):
                raise AssertionError("reference certificate validation failed")
        samples.append(perf_counter_ns() - started)
    median_batch = int(median(samples))
    proofs = iterations * callsites
    return {
        "schema": "symcc-interprocedural-effect-benchmark-v1",
        "parameters": {
            "callsites": callsites,
            "repeats": repeats,
            "iterations_per_repeat": iterations,
        },
        "reference_validation_batch_cost": {
            "minimum_ns": min(samples),
            "median_ns": median_batch,
            "maximum_ns": max(samples),
        },
        "median_ns_per_certificate": median_batch // proofs,
        "analytic_relation_checks": {
            "context_insensitive_cross_product": callsites * callsites,
            "callsite_instantiated": callsites,
        },
        "certificates_per_campaign": callsites,
        "all_certificates_valid": True,
        "claim_boundary": (
            "Python reference-validator cost and analytic relation-check "
            "cardinality only; not LLVM summary construction latency, "
            "executor throughput, coverage, solver time, bug yield, or "
            "end-to-end speedup"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callsites", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    result = benchmark(
        callsites=args.callsites,
        repeats=args.repeats,
        iterations=args.iterations,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
