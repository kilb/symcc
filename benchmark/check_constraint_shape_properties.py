#!/usr/bin/env python3
"""Deterministic property checks for F399 alpha-normalized shapes."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import random
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from constraint_shape import alpha_normalized_constraint_shape  # noqa: E402


def equality_roots(
    offsets: list[int], values: list[int]
) -> tuple[list[dict], list[int]]:
    nodes: list[dict] = []
    roots: list[int] = []
    for offset, value in zip(offsets, values, strict=True):
        read_id = len(nodes)
        nodes.append(
            {
                "id": read_id,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": offset},
            }
        )
        constant_id = len(nodes)
        nodes.append(
            {
                "id": constant_id,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{value:02x}"},
            }
        )
        nodes.append(
            {
                "id": len(nodes),
                "op": "equal",
                "bits": 1,
                "children": [read_id, constant_id],
                "attrs": {},
            }
        )
        roots.append(len(nodes) - 1)
    return nodes, roots


def alias_pair(
    first: int, second: int, *, shared: bool
) -> tuple[list[dict], list[int]]:
    nodes = [
        {"id": 0, "op": "read", "bits": 8, "children": [], "attrs": {"index": first}}
    ]
    if not shared:
        nodes.append(
            {
                "id": 1,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": second},
            }
        )
    right = 0 if shared else 1
    nodes.append(
        {
            "id": len(nodes),
            "op": "equal",
            "bits": 1,
            "children": [0, right],
            "attrs": {},
        }
    )
    return nodes, [len(nodes) - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=399)
    args = parser.parse_args()
    runs = max(1, min(args.runs, 100_000))
    generator = random.Random(args.seed)
    counters = {
        "alpha_renaming_equivalent": 0,
        "constant_change_separated": 0,
        "read_alias_change_separated": 0,
    }

    for _ in range(runs):
        width = generator.randint(1, 16)
        values = [generator.randrange(256) for _ in range(width)]
        offsets = generator.sample(range(0, 1_000_000), width)
        renamed_offsets = generator.sample(range(2_000_000, 3_000_000), width)
        nodes, roots = equality_roots(offsets, values)
        renamed_nodes, renamed_roots = equality_roots(renamed_offsets, values)
        baseline = alpha_normalized_constraint_shape(nodes, roots)
        renamed = alpha_normalized_constraint_shape(renamed_nodes, renamed_roots)
        if baseline.shape_hash != renamed.shape_hash:
            raise AssertionError("alpha renaming changed the constraint shape")
        counters["alpha_renaming_equivalent"] += 1

        changed = copy.deepcopy(renamed_nodes)
        changed[-2]["attrs"]["value_hex"] = f"{values[-1] ^ 0xFF:02x}"
        if (
            baseline.shape_hash
            == alpha_normalized_constraint_shape(changed, renamed_roots).shape_hash
        ):
            raise AssertionError("constant change was merged")
        counters["constant_change_separated"] += 1

        first, second = generator.sample(range(0, 1_000_000), 2)
        shared_nodes, shared_roots = alias_pair(first, second, shared=True)
        distinct_nodes, distinct_roots = alias_pair(first, second, shared=False)
        if (
            alpha_normalized_constraint_shape(shared_nodes, shared_roots).shape_hash
            == alpha_normalized_constraint_shape(
                distinct_nodes, distinct_roots
            ).shape_hash
        ):
            raise AssertionError("read alias change was merged")
        counters["read_alias_change_separated"] += 1

    print(
        json.dumps(
            {
                "schema": "symcc-constraint-shape-property-check-v1",
                "runs": runs,
                "seed": args.seed,
                "checks": counters,
                "status": "PASS",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
