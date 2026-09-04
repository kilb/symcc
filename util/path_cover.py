"""Interprocedural path-cover guidance for concolic exploration.

Empc formulates path prioritization as a minimum path-cover problem.  SymCC is
an offline concolic executor rather than KLEE's in-memory state executor, so the
integration here maps prefix-sensitive branch targets to edges in multiple
minimum covers and feeds that signal into the persistent PrefixDAG.

Loops are collapsed into SCCs before matching.  Calls are handled by computing
function-local covers over the compiler's interprocedural graph and retaining
the call graph as structural-task context.  Multiple maximum matchings are
enumerated with bounded edge-exclusion search, producing multiple minimum path
covers without enumerating program paths.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import os
from typing import Any

from structural_tasks import ProgramTaskGraph


def _uint(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


@dataclass(frozen=True)
class MinimumPathCover:
    paths: tuple[tuple[str, ...], ...]
    edges: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class BranchCoverChoice:
    site_id: int
    outcome: int
    function: str
    source: str
    target: str

    @property
    def edge(self) -> tuple[str, str]:
        return self.source, self.target


@dataclass
class FunctionCoverPlan:
    function: str
    components: tuple[str, ...]
    dag_edges: frozenset[tuple[str, str]]
    covers: tuple[MinimumPathCover, ...]
    predecessor_sites: dict[str, tuple[int, ...]] = field(default_factory=dict)


class MinimumPathCoverPlanner:
    """Bounded multiple-MPC planner with dependence-guided infeasibility repair."""

    def __init__(
        self,
        graph: ProgramTaskGraph,
        *,
        max_covers: int = 8,
        max_function_nodes: int = 4096,
    ) -> None:
        self.graph = graph
        self.max_covers = max(1, int(max_covers))
        self.max_function_nodes = max(16, int(max_function_nodes))
        self.plans: dict[str, FunctionCoverPlan] = {}
        self.choices: dict[tuple[int, int], BranchCoverChoice] = {}
        self.branch_choices: dict[int, BranchCoverChoice] = {}
        self.site_branch_ids: dict[int, set[int]] = {}
        self.covered_components: dict[str, set[str]] = {}
        self.cover_attempts: dict[str, list[int]] = {}
        self.cover_rewards: dict[str, list[float]] = {}
        self.cover_failures: dict[str, list[int]] = {}
        self.path_active_covers: dict[str, dict[str, set[int]]] = {}
        self.choice_failures: dict[int, int] = {}
        self.fallback_boost: dict[int, float] = {}
        self.observations = 0
        self.infeasible_paths = 0
        self._build()

    @property
    def enabled(self) -> bool:
        return bool(self.plans and self.choices)

    @classmethod
    def from_environment(
        cls, graph: ProgramTaskGraph,
    ) -> "MinimumPathCoverPlanner":
        try:
            max_covers = int(os.environ.get("SYMCC_MPC_COVERS", "8"))
        except ValueError:
            max_covers = 8
        try:
            max_nodes = int(os.environ.get("SYMCC_MPC_FUNCTION_NODES", "4096"))
        except ValueError:
            max_nodes = 4096
        return cls(graph, max_covers=max_covers,
                   max_function_nodes=max_nodes)

    def _build(self) -> None:
        blocks_by_function: dict[str, set[str]] = {}
        for block, function in self.graph.block_functions.items():
            blocks_by_function.setdefault(function, set()).add(block)
        sites_by_block: dict[str, set[int]] = {}
        for site, block in self.graph.site_blocks.items():
            sites_by_block.setdefault(block, set()).add(site)

        component_for_block: dict[str, str] = {}
        for function, block_set in sorted(blocks_by_function.items()):
            if not block_set or len(block_set) > self.max_function_nodes:
                continue
            adjacency = {block: set() for block in block_set}
            for source, target in self.graph.cfg_edges:
                if source in block_set and target in block_set:
                    adjacency[source].add(target)
            sccs = ProgramTaskGraph._strongly_connected_components(adjacency)
            components: list[str] = []
            for index, members in enumerate(sccs):
                component = f"{function}#{index}"
                components.append(component)
                for block in members:
                    component_for_block[block] = component
            dag_adjacency = {component: set() for component in components}
            for source, targets in adjacency.items():
                source_component = component_for_block[source]
                for target in targets:
                    target_component = component_for_block[target]
                    if source_component != target_component:
                        dag_adjacency[source_component].add(target_component)
            covers = self._enumerate_covers(
                dag_adjacency, self.max_covers)
            if not covers:
                continue

            sites_by_component: dict[str, set[int]] = {
                component: set() for component in components
            }
            for block in block_set:
                sites_by_component[component_for_block[block]].update(
                    sites_by_block.get(block, ()))
            reverse = {component: set() for component in components}
            for source, targets in dag_adjacency.items():
                for target in targets:
                    reverse[target].add(source)
            predecessor_sites = {
                component: self._predecessor_sites(
                    component, reverse, sites_by_component)
                for component in components
            }
            plan = FunctionCoverPlan(
                function=function,
                components=tuple(sorted(components)),
                dag_edges=frozenset(
                    (source, target)
                    for source, targets in dag_adjacency.items()
                    for target in targets),
                covers=tuple(covers),
                predecessor_sites=predecessor_sites,
            )
            self.plans[function] = plan
            self.covered_components[function] = set()
            self.cover_attempts[function] = [0] * len(covers)
            self.cover_rewards[function] = [0.0] * len(covers)
            self.cover_failures[function] = [0] * len(covers)

        for site, successors in self.graph.branch_successors.items():
            source_block = self.graph.site_blocks.get(site)
            function = self.graph.block_functions.get(source_block or "")
            if not function or function not in self.plans or not source_block:
                continue
            source_component = component_for_block.get(source_block)
            if not source_component:
                continue
            for outcome, target_block in ((1, successors[0]), (0, successors[1])):
                target_component = component_for_block.get(target_block)
                if target_component:
                    self.choices[(site, outcome)] = BranchCoverChoice(
                        site, outcome, function,
                        source_component, target_component)

    @staticmethod
    def _predecessor_sites(
        component: str,
        reverse: dict[str, set[str]],
        sites_by_component: dict[str, set[int]],
    ) -> tuple[int, ...]:
        result: set[int] = set()
        queue: deque[tuple[str, int]] = deque([(component, 0)])
        seen = {component}
        while queue:
            current, depth = queue.popleft()
            if depth >= 8:
                continue
            for predecessor in sorted(reverse.get(current, ())):
                if predecessor in seen:
                    continue
                seen.add(predecessor)
                result.update(sites_by_component.get(predecessor, ()))
                queue.append((predecessor, depth + 1))
        return tuple(sorted(result))[:256]

    @classmethod
    def _enumerate_covers(
        cls,
        adjacency: dict[str, set[str]],
        max_covers: int | None = None,
    ) -> list[MinimumPathCover]:
        nodes = tuple(sorted(adjacency))
        if not nodes:
            return []
        cap = max(1, max_covers or 8)
        base = cls._maximum_matching(nodes, adjacency, frozenset())
        maximum_size = len(base)
        pending: deque[frozenset[tuple[str, str]]] = deque([frozenset()])
        seen_forbidden: set[frozenset[tuple[str, str]]] = set()
        seen_covers: set[frozenset[tuple[str, str]]] = set()
        covers: list[MinimumPathCover] = []
        # Each exclusion requires a fresh maximum matching. Keep that work
        # independent of function size; otherwise a long graph with a unique
        # cover performs O(V) redundant matching passes during construction.
        search_budget = max(32, cap * 16)

        while pending and len(covers) < cap and len(seen_forbidden) < search_budget:
            forbidden = pending.popleft()
            if forbidden in seen_forbidden:
                continue
            seen_forbidden.add(forbidden)
            matching = cls._maximum_matching(nodes, adjacency, forbidden)
            if len(matching) != maximum_size:
                continue
            edge_set = frozenset(matching.items())
            if edge_set not in seen_covers:
                seen_covers.add(edge_set)
                covers.append(cls._matching_to_cover(nodes, matching))
            for edge in sorted(edge_set):
                candidate = forbidden | {edge}
                if candidate not in seen_forbidden:
                    pending.append(candidate)
        return covers

    @staticmethod
    def _maximum_matching(
        nodes: tuple[str, ...],
        adjacency: dict[str, set[str]],
        forbidden: frozenset[tuple[str, str]],
    ) -> dict[str, str]:
        """Maximum bipartite matching for a DAG's split-node representation."""
        pair_right: dict[str, str] = {}
        pair_left: dict[str, str] = {}

        for root in nodes:
            if root in pair_left:
                continue
            pending = deque([root])
            seen_left = {root}
            seen_right: set[str] = set()
            parent_right: dict[str, str] = {}
            via_right: dict[str, str] = {}
            terminal: str | None = None
            while pending and terminal is None:
                left = pending.popleft()
                for right in sorted(adjacency.get(left, ())):
                    if (left, right) in forbidden or right in seen_right:
                        continue
                    seen_right.add(right)
                    parent_right[right] = left
                    previous = pair_right.get(right)
                    if previous is None:
                        terminal = right
                        break
                    if previous not in seen_left:
                        seen_left.add(previous)
                        via_right[previous] = right
                        pending.append(previous)
            if terminal is None:
                continue
            right = terminal
            while True:
                left = parent_right[right]
                old_right = pair_left.get(left)
                pair_right[right] = left
                pair_left[left] = right
                if old_right is None:
                    break
                right = via_right[left]
        return {left: right for right, left in pair_right.items()}

    @staticmethod
    def _matching_to_cover(
        nodes: tuple[str, ...],
        matching: dict[str, str],
    ) -> MinimumPathCover:
        predecessor = {target: source for source, target in matching.items()}
        starts = [node for node in nodes if node not in predecessor]
        paths = []
        visited: set[str] = set()
        for start in starts:
            path = []
            current = start
            while current not in visited:
                visited.add(current)
                path.append(current)
                if current not in matching:
                    break
                current = matching[current]
            paths.append(tuple(path))
        for node in nodes:
            if node not in visited:
                paths.append((node,))
        return MinimumPathCover(
            paths=tuple(sorted(paths)),
            edges=frozenset(matching.items()),
        )

    def _cover_support(
        self,
        choice: BranchCoverChoice,
    ) -> set[int]:
        plan = self.plans.get(choice.function)
        if plan is None:
            return set()
        return {
            index for index, cover in enumerate(plan.covers)
            if choice.edge in cover.edges
        }

    def _active_for_path(self, path: str, function: str) -> set[int]:
        plan = self.plans[function]
        by_function = self.path_active_covers.setdefault(path, {})
        return by_function.setdefault(function, set(range(len(plan.covers))))

    def observe(
        self,
        path: str,
        telemetry: Any,
        *,
        reward: float,
        coverage_delta: int,
    ) -> None:
        if not self.enabled or telemetry is None:
            return
        self.observations += 1
        for branch in list(self.fallback_boost):
            self.fallback_boost[branch] *= 0.94
            if self.fallback_boost[branch] < 0.01:
                self.fallback_boost.pop(branch, None)

        for entry in getattr(telemetry, "branch_trace", ()) or ():
            if not isinstance(entry, (list, tuple)) or len(entry) < 5:
                continue
            actual_id = _uint(entry[1])
            opposite_id = _uint(entry[2])
            site = _uint(entry[3])
            outcome = int(bool(entry[4]))
            actual = self.choices.get((site, outcome))
            opposite = self.choices.get((site, 1 - outcome))
            self.site_branch_ids.setdefault(site, set()).update(
                branch for branch in (actual_id, opposite_id) if branch)
            if actual_id and actual:
                self.branch_choices[actual_id] = actual
            if opposite_id and opposite:
                self.branch_choices[opposite_id] = opposite
            if actual is None:
                continue
            self.covered_components[actual.function].update(
                (actual.source, actual.target))
            support = self._cover_support(actual)
            active = self._active_for_path(path, actual.function)
            matched = active & support
            if matched:
                active.intersection_update(matched)
            for index in support:
                self.cover_attempts[actual.function][index] += 1
                previous = self.cover_rewards[actual.function][index]
                self.cover_rewards[actual.function][index] = (
                    reward if self.cover_attempts[actual.function][index] == 1
                    else 0.82 * previous + 0.18 * reward)

        target = _uint(getattr(telemetry, "target_branch", 0))
        if not target:
            return
        target_choice = self.branch_choices.get(target)
        if target_choice is None:
            return
        reached = bool(getattr(telemetry, "target_reached", False))
        generated = _uint(getattr(telemetry, "generated", 0))
        sat = _uint(getattr(telemetry, "solver_sat", 0))
        unsat = _uint(getattr(telemetry, "solver_unsat", 0))
        unknown = _uint(getattr(telemetry, "solver_unknown", 0))
        timeouts = _uint(getattr(telemetry, "z3_timeouts", 0))
        failed = (not reached) or (
            generated == 0 and sat == 0 and (unsat or unknown or timeouts))
        if not failed:
            self.choice_failures[target] = 0
            return

        self.infeasible_paths += 1
        self.choice_failures[target] = self.choice_failures.get(target, 0) + 1
        support = self._cover_support(target_choice)
        for index in support:
            self.cover_failures[target_choice.function][index] += 1
        active = self._active_for_path(path, target_choice.function)
        remaining = active - support
        if remaining:
            active.intersection_update(remaining)
        else:
            all_indices = set(range(len(self.plans[target_choice.function].covers)))
            replacement = all_indices - support
            active.clear()
            active.update(replacement or all_indices)

        plan = self.plans[target_choice.function]
        for site in plan.predecessor_sites.get(target_choice.source, ()):
            for branch_id in self.site_branch_ids.get(site, ()):
                if branch_id != target:
                    self.fallback_boost[branch_id] = min(
                        1.0, self.fallback_boost.get(branch_id, 0.0) + 0.35)

    def branch_priority(self, branch_id: int, path: str | None = None) -> float:
        choice = self.branch_choices.get(_uint(branch_id))
        if choice is None:
            return 0.0
        plan = self.plans.get(choice.function)
        if plan is None:
            return 0.0
        support = self._cover_support(choice)
        active = (
            self._active_for_path(path, choice.function)
            if path else set(range(len(plan.covers))))
        matched = support & active
        if matched:
            support_ratio = len(matched) / max(1, len(active))
            quality = max(
                (1.0 + self.cover_rewards[choice.function][index])
                / (1.0
                   + math.log1p(self.cover_attempts[choice.function][index])
                   + self.cover_failures[choice.function][index])
                for index in matched)
            quality = min(1.0, quality)
        else:
            support_ratio = 0.08 if choice.source == choice.target else 0.02
            quality = 0.10
        uncovered = (
            1.0 if choice.target not in self.covered_components[choice.function]
            else 0.15)
        fallback = self.fallback_boost.get(_uint(branch_id), 0.0)
        failures = self.choice_failures.get(_uint(branch_id), 0)
        failure_penalty = 1.0 / (1.0 + failures)
        score = (
            0.42 * support_ratio
            + 0.25 * quality
            + 0.23 * uncovered
            + 0.10 * fallback
        ) * failure_penalty
        return max(0.0, min(1.0, score))

    def apply_to_prefix_dag(self, dag: Any) -> None:
        for branch_id, node in getattr(dag, "nodes", {}).items():
            paths = getattr(node, "seed_paths", ()) or ()
            if paths:
                score = max(
                    self.branch_priority(branch_id, path)
                    for path in paths)
            else:
                score = self.branch_priority(branch_id)
            node.path_cover_reward = score

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "functions": len(self.plans),
            "covers": sum(len(plan.covers) for plan in self.plans.values()),
            "observations": self.observations,
            "infeasible_paths": self.infeasible_paths,
            "covered_components": {
                function: sorted(components)
                for function, components in self.covered_components.items()
            },
            "cover_attempts": self.cover_attempts,
            "cover_rewards": self.cover_rewards,
            "cover_failures": self.cover_failures,
            "branch_choices": [
                [branch, choice.site_id, choice.outcome]
                for branch, choice in list(self.branch_choices.items())[-32768:]
            ],
            "site_branch_ids": {
                str(site): sorted(branches)
                for site, branches in self.site_branch_ids.items()
            },
            "path_active_covers": [
                [path, function, *sorted(indices)]
                for path, functions in list(
                    self.path_active_covers.items())[-8192:]
                for function, indices in functions.items()
            ],
            "choice_failures": self.choice_failures,
            "fallback_boost": self.fallback_boost,
        }

    def restore(self, raw: Any) -> None:
        if not self.enabled or not isinstance(raw, dict):
            return
        for function, components in raw.get(
                "covered_components", {}).items():
            if function in self.plans and isinstance(components, list):
                valid = set(self.plans[function].components)
                self.covered_components[function] = {
                    str(component) for component in components
                    if str(component) in valid
                }
        for key, destination, converter in (
            ("cover_attempts", self.cover_attempts, _uint),
            ("cover_rewards", self.cover_rewards, _bounded),
            ("cover_failures", self.cover_failures, _uint),
        ):
            source = raw.get(key, {})
            if not isinstance(source, dict):
                continue
            for function, values in source.items():
                if function not in self.plans or not isinstance(values, list):
                    continue
                expected = len(self.plans[function].covers)
                if len(values) == expected:
                    destination[function] = [converter(value) for value in values]
        for item in raw.get("branch_choices", ())[-32768:]:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            branch, site, outcome = _uint(item[0]), _uint(item[1]), int(bool(item[2]))
            choice = self.choices.get((site, outcome))
            if branch and choice:
                self.branch_choices[branch] = choice
        site_branches = raw.get("site_branch_ids", {})
        if isinstance(site_branches, dict):
            for site, branches in site_branches.items():
                site_id = _uint(site)
                if site_id and isinstance(branches, list):
                    self.site_branch_ids[site_id] = {
                        branch for branch in map(_uint, branches) if branch
                    }
        for item in raw.get("path_active_covers", ())[-32768:]:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            path, function = str(item[0]), str(item[1])
            if function not in self.plans:
                continue
            maximum = len(self.plans[function].covers)
            indices = {
                index for index in map(_uint, item[2:]) if index < maximum
            }
            if indices:
                self.path_active_covers.setdefault(path, {})[function] = indices
        failures = raw.get("choice_failures", {})
        if isinstance(failures, dict):
            self.choice_failures = {
                branch: _uint(value)
                for key, value in failures.items()
                if (branch := _uint(key)) in self.branch_choices
            }
        boosts = raw.get("fallback_boost", {})
        if isinstance(boosts, dict):
            self.fallback_boost = {
                branch: _bounded(value)
                for key, value in boosts.items()
                if (branch := _uint(key)) in self.branch_choices
            }
        self.observations = _uint(raw.get("observations"))
        self.infeasible_paths = _uint(raw.get("infeasible_paths"))


def enumerate_minimum_path_covers(
    adjacency: dict[str, set[str]],
    *,
    max_covers: int = 8,
) -> tuple[MinimumPathCover, ...]:
    """Enumerate a bounded, deterministic family of minimum path covers.

    Callers must supply an acyclic graph. The public wrapper keeps the matching
    implementation shared by seed-level PrefixDAG guidance and live-state
    scheduling without sharing either subsystem's mutable feedback state.
    """
    if isinstance(max_covers, bool) or not isinstance(max_covers, int) \
            or not 1 <= max_covers <= 256:
        raise ValueError("minimum path-cover count must be in 1..256")
    nodes = set(adjacency)
    if any(
        not isinstance(source, str)
        or not source
        or not isinstance(targets, set)
        or any(not isinstance(target, str) or target not in nodes for target in targets)
        for source, targets in adjacency.items()
    ):
        raise ValueError("minimum path-cover graph is invalid")
    indegree = {node: 0 for node in nodes}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    pending = deque(sorted(
        node for node, degree in indegree.items() if degree == 0
    ))
    visited = 0
    while pending:
        source = pending.popleft()
        visited += 1
        for target in sorted(adjacency[source]):
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(nodes):
        raise ValueError("minimum path-cover graph must be acyclic")
    return tuple(MinimumPathCoverPlanner._enumerate_covers(
        adjacency, max_covers,
    ))
