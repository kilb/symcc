#!/usr/bin/env python3
"""Independent finite oracles for live path-cover matching and SCC facts."""

from __future__ import annotations

from itertools import combinations
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from live_state_search import LiveProgramGraph  # noqa: E402
from path_cover import MinimumPathCoverPlanner  # noqa: E402


def brute_matching_size(
    nodes: tuple[str, ...],
    adjacency: dict[str, set[str]],
) -> int:
    edges = tuple(
        (source, target)
        for source in nodes
        for target in sorted(adjacency[source])
    )
    best = 0
    for size in range(len(edges) + 1):
        for selected in combinations(edges, size):
            if len({source for source, _target in selected}) == size \
                    and len({target for _source, target in selected}) == size:
                best = size
    return best


def check_matching() -> int:
    checked = 0
    for count in range(1, 6):
        nodes = tuple(str(index) for index in range(count))
        possible = tuple(
            (str(source), str(target))
            for source in range(count)
            for target in range(source + 1, count)
        )
        for mask in range(1 << len(possible)):
            adjacency = {node: set() for node in nodes}
            for index, (source, target) in enumerate(possible):
                if mask & (1 << index):
                    adjacency[source].add(target)
            actual = len(MinimumPathCoverPlanner._maximum_matching(
                nodes, adjacency, frozenset(),
            ))
            expected = brute_matching_size(nodes, adjacency)
            if actual != expected:
                raise AssertionError(
                    f"matching mismatch count={count} mask={mask}: "
                    f"actual={actual} expected={expected}"
                )
            checked += 1
    return checked


def reachable(source: str, adjacency: dict[str, set[str]]) -> set[str]:
    result = {source}
    pending = [source]
    while pending:
        current = pending.pop()
        for successor in adjacency[current]:
            if successor not in result:
                result.add(successor)
                pending.append(successor)
    return result


def check_scc() -> int:
    checked = 0
    for count in range(1, 5):
        nodes = tuple(str(index) for index in range(count))
        possible = tuple((source, target) for source in nodes for target in nodes)
        maximum = 1 << len(possible)
        stride = 1 if count <= 3 else max(1, maximum // 4096)
        for mask in range(0, maximum, stride):
            adjacency = {node: set() for node in nodes}
            for index, (source, target) in enumerate(possible):
                if mask & (1 << index):
                    adjacency[source].add(target)
            components, _cyclic, _members = LiveProgramGraph._components(adjacency)
            closure = {node: reachable(node, adjacency) for node in nodes}
            for left in nodes:
                for right in nodes:
                    expected = (
                        right in closure[left] and left in closure[right]
                    )
                    if (components[left] == components[right]) != expected:
                        raise AssertionError(
                            f"SCC mismatch count={count} mask={mask}: "
                            f"left={left} right={right}"
                        )
            checked += 1
    return checked


def main() -> int:
    result = {
        "schema": "symcc-live-path-cover-finite-oracles-v1",
        "matching_dags_checked": check_matching(),
        "scc_graphs_checked": check_scc(),
        "matching_oracle": "exhaustive edge-subset bipartite matching",
        "scc_oracle": "mutual directed reachability",
        "status": "pass",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
