#!/usr/bin/env python3
"""Mechanism benchmark for bounded TopSeed campaign selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from topseed_selector import TopSeedSelector  # noqa: E402


def identity(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def build_selector(candidates: int, groups: int) -> tuple[TopSeedSelector, list[str]]:
    selector = TopSeedSelector(
        program_context=identity("topseed-mechanism-benchmark"),
        seed=404,
        explore_ratio=1.0,
        learn_interval=20,
        max_candidates=candidates + 16,
        max_runs=candidates + 16,
        max_features=1024,
    )
    paths: list[str] = []
    for index in range(candidates):
        group = index % groups
        path = f"/synthetic/seed-{index:06d}"
        paths.append(path)
        coverage = [
            group * 32 + offset
            for offset in range(4 + group % 9)
        ]
        selector.admit(
            identity(f"candidate-{index}"),
            path,
            coverage,
            path_condition=range(index % 17),
            triggers_bug=(group % 997 == 0),
        )
    return selector, paths


def timed(callable_, repeats: int) -> list[int]:
    values = []
    for _repeat in range(repeats):
        start = time.perf_counter_ns()
        callable_()
        values.append(time.perf_counter_ns() - start)
    return values


def summarize(values: list[int]) -> dict[str, int]:
    return {
        "minimum_ns": min(values),
        "median_ns": int(statistics.median(values)),
        "maximum_ns": max(values),
    }


def run(candidates: int, groups: int, repeats: int) -> dict[str, object]:
    explore_times: list[int] = []
    for repeat in range(repeats):
        selector, paths = build_selector(candidates, groups)
        start = time.perf_counter_ns()
        proposal = selector.propose(paths)
        explore_times.append(time.perf_counter_ns() - start)
        if proposal is None or proposal.mode != "explore":
            raise AssertionError(f"explore proposal missing at repeat {repeat}")

    selector, paths = build_selector(candidates, groups)
    history_count = min(512, candidates, groups)
    for index in range(history_count):
        proposal = selector.propose([paths[index]])
        if proposal is None:
            raise AssertionError(
                f"history proposal missing at candidate {index}"
            )
        token = selector.commit(proposal.token)
        accepted = selector.observe(
            token,
            sorted({index % 31, (index * 17) % 4093, 10_000 + index}),
            path_condition=range(index % 17),
        )
        if not accepted:
            raise AssertionError(
                f"history observation rejected at candidate {index}"
            )
    selector.explore_ratio = 0.0
    historical = list(selector.historical_paths(limit=min(512, candidates)))
    exploit_times = timed(lambda: selector.propose(historical), repeats)

    snapshot = selector.snapshot()
    snapshot_times = timed(selector.snapshot, repeats)
    restore_times = timed(
        lambda: TopSeedSelector.from_snapshot(snapshot), repeats
    )
    encoded_bytes = len(json.dumps(
        snapshot, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))

    target_selector = TopSeedSelector(
        program_context=identity("target-latency"),
        seed=1,
        max_candidates=1024,
        max_runs=1024,
        max_features=1024,
    )
    target_paths = []
    target_id = ""
    distractors = 500
    for index in range(distractors):
        path = f"/target/distractor-{index:03d}"
        target_paths.append(path)
        target_selector.admit(
            identity(path), path,
            [index * 8 + offset for offset in range(4)],
        )
    target_path = "/target/high-value"
    target_paths.append(target_path)
    target_id = identity(target_path)
    target_selector.admit(target_id, target_path, range(20_000, 20_064))
    selected = target_selector._explore_candidate(
        set(target_selector.candidates),
        (1.0, 0.0, 1.0, 0.0, 0.0),
        "long",
    )
    if selected is None or selected[0] != target_id:
        raise AssertionError("fixed-weight target group was not selected")
    fifo_dispatches = len(target_paths)
    topseed_dispatches = 1

    return {
        "schema": "symcc-topseed-mechanism-benchmark-v1",
        "parameters": {
            "candidates": candidates,
            "coverage_groups": groups,
            "repeats": repeats,
            "exploit_candidates": history_count,
            "target_distractors": distractors,
        },
        "explore_proposal_cost": summarize(explore_times),
        "exploit_proposal_cost": summarize(exploit_times),
        "snapshot_cost": summarize(snapshot_times),
        "restore_cost": summarize(restore_times),
        "snapshot_bytes": encoded_bytes,
        "fixed_weight_target_latency": {
            "fifo_dispatches": fifo_dispatches,
            "topseed_dispatches": topseed_dispatches,
            "dispatch_reduction": 1.0 - topseed_dispatches / fifo_dispatches,
            "same_candidate_set": True,
        },
        "claim_boundary": (
            "synthetic selector cost and fixed-weight ordering only; no public "
            "coverage, solver-time, bug-yield, or end-to-end speedup claim"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=int, default=10_000)
    parser.add_argument("--groups", type=int, default=2_500)
    parser.add_argument("--repeats", type=int, default=11)
    args = parser.parse_args()
    if not 2 <= args.groups <= args.candidates <= 100_000:
        raise SystemExit("require 2 <= groups <= candidates <= 100000")
    if not 1 <= args.repeats <= 101:
        raise SystemExit("require 1 <= repeats <= 101")
    print(json.dumps(
        run(args.candidates, args.groups, args.repeats),
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
