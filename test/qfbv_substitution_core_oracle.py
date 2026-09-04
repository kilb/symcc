#!/usr/bin/env python3
# RUN: python3 %s --cases 256
"""Independent finite-domain oracle for variable-substitution core matching."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import sys
import time
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_substitution_core import (  # noqa: E402
    exact_substitution_match,
    substituted_root_digests,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


class Graph:
    def __init__(self) -> None:
        self.expressions: dict[str, dict] = {}

    def node(
        self,
        op: str,
        bits: int,
        children: Iterable[str] = (),
        attrs: dict | None = None,
    ) -> str:
        body = {
            "schema": "symcc-expr-node-v1",
            "op": op,
            "bits": bits,
            "children": list(children),
            "attrs": {} if attrs is None else attrs,
        }
        digest = _digest(body)
        self.expressions[digest] = body
        return digest

    def read(self, offset: int) -> str:
        return self.node("read", 8, attrs={"index": offset})

    def byte(self, value: int) -> str:
        return self.node("constant", 8, attrs={"value_hex": f"{value:02x}"})


Clause = tuple[str, int, int]


def build_formula(clauses: tuple[Clause, ...]) -> tuple[tuple[str, ...], dict]:
    graph = Graph()
    reads: dict[int, str] = {}

    def read(offset: int) -> str:
        if offset not in reads:
            reads[offset] = graph.read(offset)
        return reads[offset]

    roots: list[str] = []
    for op, left, right in clauses:
        if op in {"equal", "ult"}:
            roots.append(graph.node(op, 1, (read(left), graph.byte(right))))
        elif op == "distinct":
            roots.append(graph.node(op, 1, (read(left), read(right))))
        else:
            raise AssertionError(op)
    return tuple(roots), graph.expressions


def variables(clauses: tuple[Clause, ...]) -> tuple[int, ...]:
    found: set[int] = set()
    for op, left, right in clauses:
        found.add(left)
        if op == "distinct":
            found.add(right)
    return tuple(sorted(found))


def exhaustive_matches(
    source_clauses: tuple[Clause, ...],
    source_roots: tuple[str, ...],
    source_expressions: dict,
    target_roots: tuple[str, ...],
    target_offsets: tuple[int, ...],
) -> list[dict[int, int]]:
    source_offsets = variables(source_clauses)
    target_set = set(target_roots)
    mappings: list[dict[int, int]] = []
    for values in itertools.product(target_offsets, repeat=len(source_offsets)):
        mapping = dict(zip(source_offsets, values, strict=True))
        substituted = substituted_root_digests(
            source_roots, source_expressions, mapping
        )
        if set(substituted) <= target_set:
            mappings.append(mapping)
    return mappings


def _random_clause(rng: random.Random, offsets: tuple[int, ...]) -> Clause:
    op = rng.choice(("equal", "equal", "ult", "distinct"))
    left = rng.choice(offsets)
    right = rng.choice(offsets) if op == "distinct" else rng.randrange(4)
    return op, left, right


def run_oracle(case_count: int, seed: int) -> dict[str, int | str]:
    rng = random.Random(seed)
    started = time.monotonic_ns()
    cases: list[tuple[tuple[Clause, ...], tuple[Clause, ...], tuple[int, ...]]] = [
        (
            (("equal", 0, 0), ("equal", 1, 0), ("distinct", 0, 1)),
            (("equal", 7, 0), ("distinct", 7, 7)),
            (7,),
        ),
        (
            (("equal", 0, 1), ("equal", 0, 2)),
            (("equal", 8, 1), ("equal", 9, 2)),
            (8, 9),
        ),
    ]
    while len(cases) < case_count:
        source_offsets = tuple(range(1 + rng.randrange(3)))
        target_offsets = tuple(range(10, 10 + 1 + rng.randrange(3)))
        source = tuple(
            _random_clause(rng, source_offsets)
            for _ in range(1 + rng.randrange(4))
        )
        target = tuple(
            _random_clause(rng, target_offsets)
            for _ in range(1 + rng.randrange(5))
        )
        # Half the cases contain a guaranteed renamed image; mapping values are
        # sampled independently, so non-injective substitutions occur naturally.
        if len(cases) % 2 == 0:
            mapping = {
                offset: rng.choice(target_offsets)
                for offset in variables(source)
            }
            target = tuple(
                (
                    op,
                    mapping[left],
                    mapping[right] if op == "distinct" else right,
                )
                for op, left, right in source
            ) + target[: rng.randrange(len(target) + 1)]
        cases.append((source, target, target_offsets))

    mismatches = 0
    positive = 0
    non_injective = 0
    for source_clauses, target_clauses, target_offsets in cases:
        source_roots, source_expressions = build_formula(source_clauses)
        target_roots, target_expressions = build_formula(target_clauses)
        expected = exhaustive_matches(
            source_clauses,
            source_roots,
            source_expressions,
            target_roots,
            target_offsets,
        )
        actual = exact_substitution_match(
            source_roots,
            source_expressions,
            target_roots,
            target_expressions,
        )
        if expected:
            positive += 1
            if any(len(set(mapping.values())) < len(mapping) for mapping in expected):
                non_injective += 1
        if (actual is None) != (not expected):
            mismatches += 1
            continue
        if actual is not None and actual.mapping not in expected:
            mismatches += 1
    result: dict[str, int | str] = {
        "schema": "symcc-qfbv-substitution-core-oracle-v1",
        "seed": seed,
        "cases": len(cases),
        "positive": positive,
        "negative": len(cases) - positive,
        "non_injective_positive": non_injective,
        "mismatches": mismatches,
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=256)
    parser.add_argument("--seed", type=int, default=0xF431)
    args = parser.parse_args()
    if not 2 <= args.cases <= 100_000:
        parser.error("--cases must be in [2, 100000]")
    result = run_oracle(args.cases, args.seed)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

