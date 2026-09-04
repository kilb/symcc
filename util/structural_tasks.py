"""Call-graph structural tasks for adaptive parallel concolic execution.

The compiler's coloration sidecar is intentionally richer than a distance map:
it contains basic-block, call, entry/exit, and branch-site records.  This module
turns those records into deterministic call-graph regions and maintains
feedback-driven worker ownership over the regions.

There is no MPI dependency here.  The coordinator only asks which pending work
item best matches a worker's current ownership, which keeps the policy unit
testable and permits ordinary work stealing when structural information is
missing or the queue is heavily backlogged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import os
import time
from typing import Any, Iterable, Sequence


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _function_key(block: str, function: str) -> str:
    module = block.split(":", 1)[0] if ":" in block else "module"
    return f"{module}::{function}"


def _stable_region_id(functions: Iterable[str]) -> int:
    payload = "\0".join(sorted(functions)).encode("utf-8", errors="replace")
    value = int.from_bytes(
        hashlib.blake2b(payload, digest_size=8).digest(), "little")
    return value or 1


@dataclass(frozen=True)
class StructuralRegion:
    region_id: int
    functions: tuple[str, ...]
    sites: tuple[int, ...]
    predecessors: tuple[int, ...] = ()
    successors: tuple[int, ...] = ()


@dataclass
class ProgramTaskGraph:
    """Whole-program structural graph reconstructed from compiler summaries."""

    source_path: str = ""
    regions: dict[int, StructuralRegion] = field(default_factory=dict)
    site_regions: dict[int, int] = field(default_factory=dict)
    function_regions: dict[str, int] = field(default_factory=dict)
    call_edges: set[tuple[str, str]] = field(default_factory=set)
    block_functions: dict[str, str] = field(default_factory=dict)
    cfg_edges: set[tuple[str, str]] = field(default_factory=set)
    site_blocks: dict[int, str] = field(default_factory=dict)
    site_opcodes: dict[int, str] = field(default_factory=dict)
    site_locations: dict[int, str] = field(default_factory=dict)
    branch_successors: dict[int, tuple[str, str]] = field(default_factory=dict)
    switch_successors: dict[
        int, tuple[tuple[str, str], ...]
    ] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.regions and self.site_regions)

    @classmethod
    def load(cls, path: str | None) -> "ProgramTaskGraph":
        if not path:
            return cls()
        try:
            with open(path, encoding="utf-8", errors="ignore") as stream:
                graph = cls.from_lines(stream)
        except OSError:
            return cls(source_path=path)
        graph.source_path = path
        return graph

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "ProgramTaskGraph":
        block_functions_raw: dict[str, str] = {}
        cfg_edges: set[tuple[str, str]] = set()
        site_blocks: dict[int, str] = {}
        site_opcodes: dict[int, str] = {}
        site_locations: dict[int, str] = {}
        branch_successors: dict[int, tuple[str, str]] = {}
        switch_successors: dict[int, tuple[tuple[str, str], ...]] = {}
        direct_calls: list[tuple[str, str]] = []
        indirect_calls: list[tuple[str, str]] = []
        entries_by_name: dict[str, list[str]] = {}
        entries_by_signature: dict[str, list[str]] = {}

        for raw in lines:
            fields = raw.strip().split()
            if not fields or not fields[0].startswith("#"):
                continue
            tag = fields[0]
            if tag == "#N" and len(fields) >= 3:
                block_functions_raw[fields[1]] = fields[2]
            elif tag == "#E" and len(fields) >= 3:
                cfg_edges.add((fields[1], fields[2]))
            elif tag == "#ENTRY" and len(fields) >= 4:
                function, signature, block = fields[1], fields[2], fields[3]
                block_functions_raw.setdefault(block, function)
                entries_by_name.setdefault(function, []).append(block)
                entries_by_signature.setdefault(signature, []).append(block)
            elif tag == "#ADDR" and len(fields) >= 4:
                function, signature, block = fields[1], fields[2], fields[3]
                block_functions_raw.setdefault(block, function)
                entries_by_signature.setdefault(signature, []).append(block)
            elif tag == "#SITE" and len(fields) >= 4:
                site = _positive_int(fields[1])
                if site:
                    site_blocks.setdefault(site, fields[2])
                    block_functions_raw.setdefault(fields[2], fields[3])
                    if len(fields) >= 5:
                        site_opcodes.setdefault(site, fields[4])
                    if len(fields) >= 6:
                        site_locations.setdefault(site, fields[5])
            elif tag == "#BRANCH" and len(fields) >= 5:
                site = _positive_int(fields[1])
                if site:
                    branch_successors[site] = (fields[3], fields[4])
            elif tag == "#SWITCH" and len(fields) >= 5:
                site = _positive_int(fields[1])
                if not site:
                    continue
                alternatives: list[tuple[str, str]] = [
                    ("default", fields[3])
                ]
                for item in fields[5:]:
                    value, separator, successor = item.partition(":")
                    if separator and value and successor:
                        alternatives.append((value, successor))
                switch_successors[site] = tuple(alternatives)
            elif tag == "#X" and len(fields) >= 3:
                direct_calls.append((fields[1], fields[2]))
            elif tag == "#IX" and len(fields) >= 3:
                indirect_calls.append((fields[1], fields[2]))

        for caller, callee_name in direct_calls:
            for entry in entries_by_name.get(callee_name, ()):
                cfg_edges.add((caller, entry))
        for caller, signature in indirect_calls:
            for entry in entries_by_signature.get(signature, ()):
                cfg_edges.add((caller, entry))

        block_functions = {
            block: _function_key(block, function)
            for block, function in block_functions_raw.items()
        }
        functions = set(block_functions.values())
        call_edges: set[tuple[str, str]] = set()
        for source, target in cfg_edges:
            source_function = block_functions.get(source)
            target_function = block_functions.get(target)
            if (source_function and target_function
                    and source_function != target_function):
                call_edges.add((source_function, target_function))

        adjacency = {function: set() for function in functions}
        for source, target in call_edges:
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set())
        components = cls._strongly_connected_components(adjacency)

        function_regions: dict[str, int] = {}
        functions_by_region: dict[int, tuple[str, ...]] = {}
        for component in components:
            region_id = _stable_region_id(component)
            functions_by_region[region_id] = tuple(sorted(component))
            for function in component:
                function_regions[function] = region_id

        sites_by_region: dict[int, set[int]] = {
            region_id: set() for region_id in functions_by_region
        }
        site_regions: dict[int, int] = {}
        for site, block in site_blocks.items():
            function = block_functions.get(block)
            region_id = function_regions.get(function or "")
            if region_id is None:
                continue
            sites_by_region[region_id].add(site)
            site_regions[site] = region_id

        region_edges: set[tuple[int, int]] = set()
        for source, target in call_edges:
            source_region = function_regions.get(source)
            target_region = function_regions.get(target)
            if (source_region and target_region and source_region != target_region):
                region_edges.add((source_region, target_region))
        predecessors: dict[int, set[int]] = {
            region_id: set() for region_id in functions_by_region
        }
        successors: dict[int, set[int]] = {
            region_id: set() for region_id in functions_by_region
        }
        for source, target in region_edges:
            successors[source].add(target)
            predecessors[target].add(source)

        regions = {
            region_id: StructuralRegion(
                region_id=region_id,
                functions=functions_by_region[region_id],
                sites=tuple(sorted(sites_by_region[region_id])),
                predecessors=tuple(sorted(predecessors[region_id])),
                successors=tuple(sorted(successors[region_id])),
            )
            for region_id in functions_by_region
            if sites_by_region[region_id]
        }
        # Drop mappings to call-only regions. They remain represented indirectly
        # by edges between the branch-bearing regions reached through them.
        active_regions = set(regions)
        site_regions = {
            site: region for site, region in site_regions.items()
            if region in active_regions
        }
        return cls(
            regions=regions,
            site_regions=site_regions,
            function_regions=function_regions,
            call_edges=call_edges,
            block_functions=block_functions,
            cfg_edges=cfg_edges,
            site_blocks=site_blocks,
            site_opcodes=site_opcodes,
            site_locations=site_locations,
            branch_successors=branch_successors,
            switch_successors=switch_successors,
        )

    @staticmethod
    def _strongly_connected_components(
        adjacency: dict[str, set[str]],
    ) -> list[tuple[str, ...]]:
        index = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[tuple[str, ...]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlink[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for successor in sorted(adjacency.get(node, ())):
                if successor not in indices:
                    visit(successor)
                    lowlink[node] = min(lowlink[node], lowlink[successor])
                elif successor in on_stack:
                    lowlink[node] = min(lowlink[node], indices[successor])
            if lowlink[node] != indices[node]:
                return
            component = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(tuple(sorted(component)))

        for node in sorted(adjacency):
            if node not in indices:
                visit(node)
        return sorted(components)


@dataclass
class RegionFeedback:
    visits: int = 0
    productive_runs: int = 0
    coverage_gain: float = 0.0
    reward_ema: float = 0.0
    cost_ema: float = 0.0
    last_seen: float = 0.0
    last_gain: float = 0.0
    seen_sites: set[int] = field(default_factory=set)
    corpus: tuple[str, ...] = ()


class DynamicStructuralTaskAllocator:
    """DynamiQ-style region ownership with online feedback reallocation."""

    def __init__(
        self,
        graph: ProgramTaskGraph,
        *,
        rebalance_interval: float = 30.0,
        corpus_per_region: int = 8,
    ) -> None:
        self.graph = graph
        self.rebalance_interval = max(1.0, float(rebalance_interval))
        self.corpus_per_region = max(1, int(corpus_per_region))
        self.feedback = {
            region_id: RegionFeedback() for region_id in graph.regions
        }
        self.path_regions: dict[str, tuple[int, ...]] = {}
        self.branch_regions: dict[int, int] = {}
        self.worker_regions: dict[int, tuple[int, ...]] = {}
        self.last_rebalance = 0.0
        self.epoch = 0
        self.observations = 0
        self.rebalances = 0

    @property
    def enabled(self) -> bool:
        return self.graph.enabled

    @classmethod
    def from_environment(cls) -> "DynamicStructuralTaskAllocator":
        graph_path = (
            os.environ.get("SYMCC_TASK_GRAPH")
            or os.environ.get("SYMCC_DIRECTED_DISTANCE"))
        graph = ProgramTaskGraph.load(graph_path)
        try:
            interval = float(os.environ.get(
                "SYMCC_TASK_REBALANCE_INTERVAL", "30"))
        except ValueError:
            interval = 30.0
        try:
            corpus = int(os.environ.get("SYMCC_TASK_CORPUS_PER_REGION", "8"))
        except ValueError:
            corpus = 8
        return cls(graph, rebalance_interval=interval,
                   corpus_per_region=corpus)

    def observe(
        self,
        path: str,
        telemetry: Any,
        *,
        reward: float,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        now: float,
    ) -> tuple[int, ...]:
        if not self.enabled or telemetry is None:
            return ()
        visited: set[int] = set()
        sites_by_region: dict[int, set[int]] = {}
        for entry in getattr(telemetry, "branch_trace", ()) or ():
            if not isinstance(entry, (list, tuple)) or len(entry) < 5:
                continue
            site = _positive_int(entry[3])
            region_id = self.graph.site_regions.get(site)
            if region_id is None:
                continue
            visited.add(region_id)
            sites_by_region.setdefault(region_id, set()).add(site)
            actual = _positive_int(entry[1])
            opposite = _positive_int(entry[2])
            if actual:
                self.branch_regions[actual] = region_id
            if opposite:
                self.branch_regions[opposite] = region_id
        if not visited:
            return ()

        ordered = tuple(sorted(visited))
        self.path_regions[path] = ordered
        self.observations += 1
        share = 1.0 / len(ordered)
        productive = coverage_delta > 0 or interesting_cases > 0
        for region_id in ordered:
            state = self.feedback.setdefault(region_id, RegionFeedback())
            state.visits += 1
            state.reward_ema = (
                reward if state.visits == 1
                else 0.82 * state.reward_ema + 0.18 * reward)
            normalized_cost = max(0.0, elapsed) * share
            state.cost_ema = (
                normalized_cost if state.visits == 1
                else 0.85 * state.cost_ema + 0.15 * normalized_cost)
            state.coverage_gain += max(0, coverage_delta) * share
            state.last_seen = now
            state.seen_sites.update(sites_by_region.get(region_id, ()))
            if productive:
                state.productive_runs += 1
                state.last_gain = now
                if path not in state.corpus:
                    state.corpus = (
                        state.corpus + (path,))[-self.corpus_per_region:]
        return ordered

    def _priority(self, region_id: int, now: float) -> float:
        region = self.graph.regions[region_id]
        state = self.feedback.setdefault(region_id, RegionFeedback())
        total = max(1, self.observations)
        exploration = math.sqrt(math.log1p(total + 1) / (state.visits + 1))
        unseen = 1.0 - len(state.seen_sites) / max(1, len(region.sites))
        yield_rate = state.productive_runs / max(1, state.visits)
        efficiency = state.reward_ema / (1.0 + math.log1p(state.cost_ema))
        age = (
            1.0 if state.last_seen == 0.0
            else min(1.0, max(0.0, now - state.last_seen)
                     / (4.0 * self.rebalance_interval)))
        stalled = (
            state.visits >= 4
            and state.last_gain > 0.0
            and now - state.last_gain >= 3.0 * self.rebalance_interval)
        structural_frontier = min(
            1.0, (len(region.predecessors) + len(region.successors)) / 8.0)
        score = (
            0.30 * unseen
            + 0.22 * min(1.0, exploration)
            + 0.20 * min(1.0, efficiency)
            + 0.12 * yield_rate
            + 0.08 * age
            + 0.08 * structural_frontier
        )
        if stalled and unseen < 0.25:
            score *= 0.55
        return max(0.01, score)

    def rebalance(
        self,
        workers: Sequence[int],
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic() if now is None else now
        worker_list = sorted({_positive_int(worker) for worker in workers
                              if _positive_int(worker)})
        if not worker_list:
            self.worker_regions.clear()
            return False
        if (not force and self.worker_regions
                and now - self.last_rebalance < self.rebalance_interval):
            return False

        priorities = {
            region_id: self._priority(region_id, now)
            for region_id in self.graph.regions
        }
        region_order = sorted(
            priorities, key=lambda region_id: (
                priorities[region_id], -region_id), reverse=True)
        rotated_workers = worker_list[:]
        offset = self.epoch % len(rotated_workers)
        rotated_workers = rotated_workers[offset:] + rotated_workers[:offset]
        ownership: dict[int, list[int]] = {
            worker: [] for worker in worker_list
        }

        if len(region_order) >= len(worker_list):
            loads = {worker: 0.0 for worker in worker_list}
            for region_id in region_order:
                worker = min(
                    rotated_workers,
                    key=lambda candidate: (
                        loads[candidate],
                        len(ownership[candidate]),
                        candidate,
                    ),
                )
                ownership[worker].append(region_id)
                loads[worker] += priorities[region_id]
        else:
            owner_counts = {region_id: 0 for region_id in region_order}
            for worker, region_id in zip(rotated_workers, region_order):
                ownership[worker].append(region_id)
                owner_counts[region_id] += 1
            for worker in rotated_workers[len(region_order):]:
                region_id = max(
                    region_order,
                    key=lambda candidate: (
                        priorities[candidate] / (owner_counts[candidate] + 1),
                        -owner_counts[candidate],
                        -candidate,
                    ),
                )
                ownership[worker].append(region_id)
                owner_counts[region_id] += 1

        self.worker_regions = {
            worker: tuple(sorted(regions))
            for worker, regions in ownership.items()
        }
        self.epoch += 1
        self.rebalances += 1
        self.last_rebalance = now
        return True

    def regions_for_task(self, path: str, target_branch: int = 0) -> tuple[int, ...]:
        if target_branch:
            region_id = self.branch_regions.get(_positive_int(target_branch))
            if region_id is not None:
                return (region_id,)
        return self.path_regions.get(path, ())

    def accepts(self, worker: int, path: str, target_branch: int = 0) -> bool:
        task_regions = self.regions_for_task(path, target_branch)
        owned = self.worker_regions.get(_positive_int(worker), ())
        if not task_regions or not owned:
            return True
        return not set(task_regions).isdisjoint(owned)

    def select_index(
        self,
        worker: int,
        items: Sequence[tuple],
        start: int,
        *,
        active_worker_count: int,
    ) -> int | None:
        if start >= len(items):
            return None
        if not self.enabled or not self.worker_regions:
            return start
        for index in range(start, len(items)):
            path = items[index][0]
            target = items[index][2]
            if self.accepts(worker, path, target):
                return index
        # Heavy backlogs are allowed to steal across structural ownership. This
        # prevents a slow or failed owner from turning partitioning into a global
        # barrier while preserving exclusivity in the normal case.
        if len(items) - start > max(4, 2 * max(1, active_worker_count)):
            return start
        return None

    def task_region(self, path: str, target_branch: int = 0) -> int:
        regions = self.regions_for_task(path, target_branch)
        if not regions:
            return 0
        now = time.monotonic()
        return max(regions, key=lambda region_id: self._priority(region_id, now))

    def path_priority(self, path: str) -> float:
        regions = self.path_regions.get(path, ())
        if not regions:
            return 0.0
        now = time.monotonic()
        return min(1.0, max(self._priority(region_id, now)
                            for region_id in regions))

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "source_path": self.graph.source_path,
            "regions": len(self.graph.regions),
            "sites": len(self.graph.site_regions),
            "call_edges": len(self.graph.call_edges),
            "observations": self.observations,
            "rebalances": self.rebalances,
            "epoch": self.epoch,
            "feedback": {
                str(region_id): {
                    "visits": state.visits,
                    "productive_runs": state.productive_runs,
                    "coverage_gain": state.coverage_gain,
                    "reward_ema": state.reward_ema,
                    "cost_ema": state.cost_ema,
                    "last_seen": state.last_seen,
                    "last_gain": state.last_gain,
                    "seen_sites": sorted(state.seen_sites),
                    "corpus": list(state.corpus),
                }
                for region_id, state in self.feedback.items()
            },
            "path_regions": [
                [path, *regions]
                for path, regions in list(self.path_regions.items())[-8192:]
            ],
            "branch_regions": [
                [branch, region]
                for branch, region in list(self.branch_regions.items())[-32768:]
            ],
            "worker_regions": {
                str(worker): list(regions)
                for worker, regions in self.worker_regions.items()
            },
        }

    def restore(self, raw: Any) -> None:
        if not self.enabled or not isinstance(raw, dict):
            return
        active_regions = set(self.graph.regions)
        now = time.monotonic()
        feedback = raw.get("feedback", {})
        if isinstance(feedback, dict):
            for key, item in feedback.items():
                region_id = _positive_int(key)
                if region_id not in active_regions or not isinstance(item, dict):
                    continue
                last_seen = max(0.0, _finite_float(item.get("last_seen")))
                last_gain = max(0.0, _finite_float(item.get("last_gain")))
                if last_seen > now + 3600.0:
                    last_seen = now
                if last_gain > now + 3600.0:
                    last_gain = now
                sites = {
                    _positive_int(site) for site in item.get("seen_sites", ())
                    if _positive_int(site) in self.graph.site_regions
                }
                corpus = item.get("corpus", ())
                if not isinstance(corpus, (list, tuple)):
                    corpus = ()
                self.feedback[region_id] = RegionFeedback(
                    visits=_positive_int(item.get("visits")),
                    productive_runs=_positive_int(item.get("productive_runs")),
                    coverage_gain=max(
                        0.0, _finite_float(item.get("coverage_gain"))),
                    reward_ema=max(
                        0.0, _finite_float(item.get("reward_ema"))),
                    cost_ema=max(0.0, _finite_float(item.get("cost_ema"))),
                    last_seen=last_seen,
                    last_gain=last_gain,
                    seen_sites=sites,
                    corpus=tuple(str(path) for path in
                                 corpus[-self.corpus_per_region:]),
                )
        for item in raw.get("path_regions", ())[-8192:]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            regions = tuple(
                region for region in (_positive_int(value) for value in item[1:])
                if region in active_regions)
            if regions:
                self.path_regions[str(item[0])] = regions
        for item in raw.get("branch_regions", ())[-32768:]:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            branch, region = _positive_int(item[0]), _positive_int(item[1])
            if branch and region in active_regions:
                self.branch_regions[branch] = region
        self.observations = _positive_int(raw.get("observations"))
        self.rebalances = _positive_int(raw.get("rebalances"))
        self.epoch = _positive_int(raw.get("epoch"))
