#!/usr/bin/env python3
"""Independent finite oracle for the selective concolic MDP slice."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


DOMAIN = range(16)
GAMMA = 0.82
FUTURE_WEIGHT = 0.22


def full_formula(x: int, y: int, z: int, w: int) -> bool:
    return (
        y == ((w + 2) & 0xF)
        and y == z
        and z == ((w + w) & 0xF)
        and ((x * x * x) & 0xF) > y
    )


def partial_formula(y: int, z: int, w: int) -> bool:
    return (
        y == ((w + 2) & 0xF)
        and y == z
        and z == ((w + w) & 0xF)
    )


def deduplicated_completions(witness: int) -> list[int]:
    return list(dict.fromkeys((witness, 0, 15, 7)))


def selective_candidate(witness: int, partial: tuple[int, int, int]) -> int | None:
    y, z, w = partial
    return next(
        (
            x
            for x in deduplicated_completions(witness)
            if full_formula(x, y, z, w)
        ),
        None,
    )


def value_iteration(tolerance: float = 1e-12) -> tuple[float, int, float]:
    value = 0.0
    residual = math.inf
    iterations = 0
    for iteration in range(128):
        updated = 0.27 + 0.17 + FUTURE_WEIGHT * GAMMA * value
        residual = abs(updated - value)
        value = updated
        iterations = iteration + 1
        if residual <= tolerance:
            break
    return value, iterations, residual


def run_oracle() -> dict[str, Any]:
    full_models = [
        (x, y, z, w)
        for x in DOMAIN
        for y in DOMAIN
        for z in DOMAIN
        for w in DOMAIN
        if full_formula(x, y, z, w)
    ]
    partial_models = [
        (y, z, w)
        for y in DOMAIN
        for z in DOMAIN
        for w in DOMAIN
        if partial_formula(y, z, w)
    ]
    assert partial_models == [(4, 4, 2)]
    emitted = [
        selective_candidate(witness, partial_models[0])
        for witness in DOMAIN
    ]
    assert all(candidate is not None for candidate in emitted)
    assert all(
        full_formula(int(candidate), *partial_models[0])
        for candidate in emitted
        if candidate is not None
    )
    direct_x = sorted({model[0] for model in full_models})
    assert set(int(candidate) for candidate in emitted if candidate is not None) <= set(
        direct_x
    )

    value, iterations, residual = value_iteration()
    closed_form = 0.44 / (1.0 - FUTURE_WEIGHT * GAMMA)
    assert math.isclose(value, closed_form, rel_tol=0.0, abs_tol=1e-11)
    laplace_observed = 10.0 / 11.0
    laplace_open = 1.0 / 11.0
    assert math.isclose(laplace_observed + laplace_open, 1.0)

    return {
        "schema": "symcc-selective-concolic-mdp-finite-oracle-v1",
        "all_passed": True,
        "domain_size": len(DOMAIN),
        "configurations": len(DOMAIN),
        "full_models": len(full_models),
        "full_model_x_values": direct_x,
        "partial_models": len(partial_models),
        "candidate_hits": sum(candidate is not None for candidate in emitted),
        "witness_first_hits": sum(
            candidate == witness for witness, candidate in zip(DOMAIN, emitted)
        ),
        "fallback_required": 0,
        "false_sat": 0,
        "false_unsat": 0,
        "relation_edges": 4,
        "cut_weight": 1,
        "smt_assertions": 3,
        "random_assertions": 1,
        "shared_variables": 1,
        "random_only_variables": 1,
        "laplace_probabilities": [laplace_observed, laplace_open],
        "cycle_value": value,
        "cycle_closed_form": closed_form,
        "value_iterations": iterations,
        "value_residual": residual,
        "claim_boundary": (
            "Finite four-bit source model and analytic two-state MDP; not an "
            "FM 2026 KLEE/JFS/METIS/SVM reproduction or public coverage result"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(run_oracle(), sort_keys=True, separators=(",", ":"))
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="ascii")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
