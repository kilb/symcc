#!/usr/bin/env python3
"""Microbenchmark persistent live-state minimum-path-cover guidance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from live_state_search import (  # noqa: E402
    LiveProgramGraph,
    LiveStateSearchFeatures,
    LiveStateSearchPolicy,
    search_decision_token,
)


def diamond_chain(count: int) -> dict[str, object]:
    blocks: dict[str, list[dict[str, object]]] = {}
    for index in range(count):
        branch = f"branch-{index:05d}"
        left = f"left-{index:05d}"
        right = f"right-{index:05d}"
        successor = (
            f"branch-{index + 1:05d}" if index + 1 < count else "exit"
        )
        blocks[branch] = [{
            "op": "branch",
            "condition": 1,
            "true": left,
            "false": right,
            "site": index + 1,
        }]
        blocks[left] = [{"op": "jump", "target": successor}]
        blocks[right] = [{"op": "jump", "target": successor}]
    blocks["exit"] = [{"op": "halt", "value": 0}]
    return {
        "functions": {
            "main": {
                "entry": "branch-00000",
                "blocks": blocks,
            },
        },
    }


def measure(samples: int, operation: Callable[[], object]) -> dict[str, float]:
    values = []
    for _sample in range(samples):
        started = time.perf_counter_ns()
        operation()
        values.append((time.perf_counter_ns() - started) / 1_000_000.0)
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {
        "median_ms": statistics.median(values),
        "p95_ms": p95,
        "minimum_ms": min(values),
        "maximum_ms": max(values),
    }


def benchmark_size(diamonds: int, samples: int) -> dict[str, object]:
    program = diamond_chain(diamonds)
    build_enabled = measure(
        samples,
        lambda: LiveProgramGraph(program, path_cover_max_covers=8),
    )
    build_disabled = measure(
        samples,
        lambda: LiveProgramGraph(program, path_cover_enabled=False),
    )
    graph = LiveProgramGraph(program, path_cover_max_covers=8)
    plan = graph._path_cover_plans["main"]
    covered = {
        location
        for index, location in enumerate(sorted(graph.adjacency))
        if index % 3 == 0
    }
    context = graph.path_cover_coverage_context(covered)
    candidate_count = min(512, diamonds * 2)
    candidates = [
        (
            f"main:{'left' if index % 2 == 0 else 'right'}-{index // 2:05d}",
            search_decision_token(index // 2 + 1, index % 2 == 0),
        )
        for index in range(candidate_count)
    ]

    def score_candidates() -> None:
        for location, token in candidates:
            guidance = graph.path_cover_guidance(
                location, (token,), covered, context,
            )
            if guidance is None:
                raise AssertionError("path-cover guidance unexpectedly unavailable")

    guidance = measure(samples, score_candidates)
    scores = [
        graph.path_cover_guidance(location, (token,), covered, context).score
        for location, token in candidates
    ]
    features = tuple(
        LiveStateSearchFeatures(
            f"state-{index:08d}",
            candidates[index][0],
            path_cover_score=score,
        )
        for index, score in enumerate(scores)
    )

    def select_candidate() -> None:
        policy = LiveStateSearchPolicy(("path-cover",))
        policy.select_index(features)

    return {
        "diamonds": diamonds,
        "cfg_nodes": len(graph.adjacency),
        "cfg_edges": sum(len(targets) for targets in graph.adjacency.values()),
        "covers": len(plan.covers),
        "candidate_count": candidate_count,
        "build_enabled": build_enabled,
        "build_disabled": build_disabled,
        "coverage_context": measure(
            samples,
            lambda: graph.path_cover_coverage_context(covered),
        ),
        "candidate_guidance_batch": guidance,
        "policy_selection": measure(samples, select_candidate),
        "selected_identity": features[
            LiveStateSearchPolicy(("path-cover",)).select_index(features)
        ].identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--diamonds", type=int, nargs="+", default=[16, 64, 256])
    args = parser.parse_args()
    if not 3 <= args.samples <= 101:
        parser.error("--samples must be in 3..101")
    if any(not 1 <= value <= 1024 for value in args.diamonds):
        parser.error("--diamonds values must be in 1..1024")
    result = {
        "schema": "symcc-live-path-cover-benchmark-v1",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "samples": args.samples,
        "results": [
            benchmark_size(diamonds, args.samples)
            for diamonds in args.diamonds
        ],
        "claim_boundary": (
            "In-process graph-construction and policy-overhead microbenchmark; "
            "no target, solver, AFL map, coverage, throughput, or bug-yield claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
