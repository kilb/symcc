#!/usr/bin/env python3
"""Independent finite oracles for the bounded TopSeed selector."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import random
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from topseed_selector import TopSeedSelector  # noqa: E402


def identity(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def reference_choice(
    selector: TopSeedSelector,
    available: set[str],
    weights: tuple[float, ...],
    policy: str,
) -> tuple[str, str] | None:
    groups: dict[str, list[str]] = {}
    coverage: dict[str, set[int]] = {}
    for candidate_id in available:
        candidate = selector.candidates[candidate_id]
        group = selector._group_key(candidate.coverage)
        if group in selector.used_groups:
            continue
        groups.setdefault(group, []).append(candidate_id)
        coverage[group] = set(candidate.coverage)
    if not groups:
        return None
    frequency: Counter[int] = Counter()
    selected_coverage: set[int] = set()
    for candidate in selector.candidates.values():
        frequency.update(candidate.coverage)
        if selector._group_key(candidate.coverage) in selector.used_groups:
            selected_coverage.update(candidate.coverage)

    scores: dict[str, float] = {}
    for group, members in groups.items():
        branches = coverage[group]
        features = (
            len(branches),
            math.fsum(1.0 / frequency[branch] for branch in branches),
            len(branches - selected_coverage),
            sum(selector.candidates[item].triggers_bug for item in members),
            len(members),
        )
        scores[group] = math.fsum(
            float(feature) * weight
            for feature, weight in zip(features, weights)
        )
    group = min(groups, key=lambda item: (-scores[item], item))
    members = sorted(groups[group])
    if policy == "long":
        candidate = min(
            members,
            key=lambda item: (
                -len(selector.candidates[item].path_condition), item,
            ),
        )
    elif policy == "short":
        candidate = min(
            members,
            key=lambda item: (
                len(selector.candidates[item].path_condition), item,
            ),
        )
    else:
        unique: dict[str, int] = {}
        conditions = {
            item: set(selector.candidates[item].path_condition)
            for item in members
        }
        for item in members:
            other = set()
            for sibling in members:
                if sibling != item:
                    other.update(conditions[sibling])
            unique[item] = len(conditions[item] - other)
        candidate = min(members, key=lambda item: (-unique[item], item))
    return candidate, group


def reference_high_cluster(scores: list[float]) -> tuple[int, ...]:
    if len(scores) < 2 or math.isclose(min(scores), max(scores)):
        return tuple(range(len(scores)))
    ranked = sorted(range(len(scores)), key=lambda index: (scores[index], index))
    best: tuple[float, int] | None = None
    for cut in range(1, len(ranked)):
        low = [scores[index] for index in ranked[:cut]]
        high = [scores[index] for index in ranked[cut:]]
        low_mean = math.fsum(low) / len(low)
        high_mean = math.fsum(high) / len(high)
        error = math.fsum((value - low_mean) ** 2 for value in low)
        error += math.fsum((value - high_mean) ** 2 for value in high)
        candidate = (error, cut)
        if best is None or candidate < best:
            best = candidate
    assert best is not None
    return tuple(sorted(ranked[best[1] :]))


def run(seed: int) -> dict[str, object]:
    rng = random.Random(seed)
    group_evaluations = 0
    for case in range(80):
        selector = TopSeedSelector(
            program_context=identity(f"program-{case}"),
            seed=case,
            max_candidates=64,
            max_runs=64,
            max_features=128,
        )
        groups: list[tuple[int, ...]] = []
        for group_index in range(7):
            branches = tuple(sorted(rng.sample(range(1, 40), rng.randint(1, 7))))
            groups.append(branches)
            for member in range(rng.randint(1, 3)):
                name = f"{case}-{group_index}-{member}"
                selector.admit(
                    identity(name),
                    f"/seed/{name}",
                    branches,
                    path_condition=rng.sample(range(80), rng.randint(0, 9)),
                    triggers_bug=rng.random() < 0.15,
                )
        if case % 2:
            used = selector._group_key(groups[0])
            selector.used_groups.add(used)
        available = set(selector.candidates)
        for _weight_index in range(16):
            weights = tuple(rng.choice((-1.0, -0.5, 0.0, 0.5, 1.0)) for _ in range(5))
            for policy in ("unique", "long", "short"):
                expected = reference_choice(
                    selector, available, weights, policy
                )
                actual = selector._explore_candidate(
                    available, weights, policy
                )
                if actual != expected:
                    raise AssertionError(
                        f"group oracle mismatch: {case=} {weights=} {policy=} "
                        f"{expected=} {actual=}"
                    )
                group_evaluations += 1

    rarity_evaluations = 0
    cluster_evaluations = 0
    for case in range(120):
        coverage_sets = [
            set(rng.sample(range(1, 50), rng.randint(1, 9)))
            for _ in range(rng.randint(2, 12))
        ]
        frequency = Counter(
            branch for coverage in coverage_sets for branch in coverage
        )
        expected = [
            math.fsum(1.0 / frequency[branch] for branch in coverage)
            for coverage in coverage_sets
        ]
        actual = TopSeedSelector._rarity_scores(coverage_sets)
        if actual != expected:
            raise AssertionError(f"rarity oracle mismatch: {case=}")
        rarity_evaluations += sum(len(coverage) for coverage in coverage_sets)

        distinct_scores = [
            float(index * index + rng.random() / 1000.0)
            for index in range(rng.randint(2, 16))
        ]
        expected_cluster = reference_high_cluster(distinct_scores)
        actual_cluster = TopSeedSelector._high_cluster(distinct_scores)
        if actual_cluster != expected_cluster:
            raise AssertionError(
                f"cluster oracle mismatch: {case=} "
                f"{expected_cluster=} {actual_cluster=}"
            )
        cluster_evaluations += len(distinct_scores)

    bitmap_evaluations = 0
    for value in range(256):
        expected = tuple(bit for bit in range(8) if value & (1 << bit))
        actual = TopSeedSelector.coverage_features_from_bitmap(bytes([value]))
        if actual != expected:
            raise AssertionError(f"bitmap oracle mismatch: {value=}")
        bitmap_evaluations += 8

    replay_evaluations = 0
    for case in range(64):
        selector = TopSeedSelector(
            program_context=identity(f"restart-{case}"),
            seed=case,
            max_candidates=16,
            max_runs=16,
            max_features=32,
        )
        paths = []
        for index in range(6):
            path = f"/seed/{case}/{index}"
            paths.append(path)
            selector.admit(
                identity(f"restart-{case}-{index}"), path,
                [index + 1, (index + 1) * 10],
                path_condition=range(index),
            )
        restored = TopSeedSelector.from_snapshot(selector.snapshot())
        if restored.propose(paths) != selector.propose(paths):
            raise AssertionError(f"restart proposal mismatch: {case=}")
        replay_evaluations += 1

    total = (
        group_evaluations
        + rarity_evaluations
        + cluster_evaluations
        + bitmap_evaluations
        + replay_evaluations
    )
    return {
        "schema": "symcc-topseed-independent-oracles-v1",
        "seed": seed,
        "group_policy_evaluations": group_evaluations,
        "rarity_feature_evaluations": rarity_evaluations,
        "cluster_score_evaluations": cluster_evaluations,
        "bitmap_bit_evaluations": bitmap_evaluations,
        "restart_evaluations": replay_evaluations,
        "total_evaluations": total,
        "all_passed": True,
        "oracle_independence": (
            "reference formulas do not call selector scoring, rarity, "
            "clustering, or snapshot-transition helpers"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=404)
    args = parser.parse_args()
    print(json.dumps(run(args.seed), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
