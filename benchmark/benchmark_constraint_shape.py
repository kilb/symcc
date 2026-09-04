#!/usr/bin/env python3
"""Microbenchmark F399 alpha-normalized Query IR constraint shapes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from constraint_shape import (  # noqa: E402
    AlphaConstraintShapeIndex,
    alpha_normalized_constraint_shape,
)


def expression_chain(reads: int, *, shift: int = 0, final_value: int = 0x41):
    nodes = []
    roots = []
    for index in range(reads):
        read_id = len(nodes)
        nodes.append(
            {
                "id": read_id,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": shift + index},
            }
        )
        constant_id = len(nodes)
        value = final_value if index == reads - 1 else index & 0xFF
        nodes.append(
            {
                "id": constant_id,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{value:02x}"},
            }
        )
        equal_id = len(nodes)
        nodes.append(
            {
                "id": equal_id,
                "op": "equal",
                "bits": 1,
                "children": [read_id, constant_id],
                "attrs": {},
            }
        )
        roots.append(equal_id)
    return nodes, roots


def latency(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "median_us": statistics.median(ordered),
        "p95_us": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_us": ordered[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--reads", type=int, default=64)
    args = parser.parse_args()
    runs = max(1, min(args.runs, 100_000))
    reads = max(1, min(args.reads, 4096))

    nodes, roots = expression_chain(reads)
    shifted_nodes, shifted_roots = expression_chain(reads, shift=1_000_000)
    changed_nodes, changed_roots = expression_chain(reads, final_value=0x42)
    baseline = alpha_normalized_constraint_shape(nodes, roots)
    shifted = alpha_normalized_constraint_shape(shifted_nodes, shifted_roots)
    changed = alpha_normalized_constraint_shape(changed_nodes, changed_roots)

    cold_samples = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        alpha_normalized_constraint_shape(nodes, roots)
        cold_samples.append((time.perf_counter_ns() - started) / 1000.0)

    index = AlphaConstraintShapeIndex(nodes)
    projection_samples = []
    pipeline_samples = []
    suffix_roots = roots[-8:]
    for _ in range(runs):
        started = time.perf_counter_ns()
        index.shape([roots[-1]])
        index.shape(suffix_roots)
        projection_samples.append((time.perf_counter_ns() - started) / 1000.0)
        started = time.perf_counter_ns()
        fresh_index = AlphaConstraintShapeIndex(nodes)
        fresh_index.shape([roots[-1]])
        fresh_index.shape(suffix_roots)
        pipeline_samples.append((time.perf_counter_ns() - started) / 1000.0)

    output = {
        "schema": "symcc-constraint-shape-benchmark-v1",
        "runs": runs,
        "reads": reads,
        "nodes": len(nodes),
        "equivalent_shifted_offsets": baseline.shape_hash == shifted.shape_hash,
        "different_constant_separated": baseline.shape_hash != changed.shape_hash,
        "cold_full_shape": latency(cold_samples),
        "query_ingest_shape_pipeline": latency(pipeline_samples),
        "reused_target_and_suffix_projection": latency(projection_samples),
        "shape": {
            "schema": baseline.to_mapping()["schema"],
            "shape_hash": baseline.shape_hash,
            "read_count": baseline.read_count,
            "node_count": baseline.node_count,
            "root_count": len(baseline.root_hashes),
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
