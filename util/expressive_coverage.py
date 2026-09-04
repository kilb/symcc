"""Engine-neutral expressive coverage tree for concolic scheduling.

The tree joins prefix-sensitive runtime branch traces with compiler-emitted
source and call-graph metadata.  It deliberately stores summaries rather than
solver expressions: every backend can emit the existing telemetry schema, and
the coordinator can persist or export the result without depending on Z3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import time
from typing import Any, Iterable

from structural_tasks import ProgramTaskGraph


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0


def _stable_id(parts: Iterable[Any]) -> str:
    payload = "\0".join(str(part) for part in parts).encode(
        "utf-8", errors="replace")
    return hashlib.blake2b(payload, digest_size=12).hexdigest()


@dataclass
class ExpressiveCoverageNode:
    node_id: str
    branch_id: int
    parent_branch: int
    site_id: int
    outcome: int
    status: str
    function: str = ""
    location: str = ""
    branch_type: str = "branch"
    context: tuple[str, ...] = ()
    depth: int = 0
    visits: int = 0
    attempts: int = 0
    productive_runs: int = 0
    generated: int = 0
    solver_sat: int = 0
    solver_unsat: int = 0
    solver_unknown: int = 0
    solver_time_us: int = 0
    dependency_count: int = 0
    dependency_span: int = 0
    dependency_lo: int = 0
    dependency_hi: int = 0
    data_quality: float = 0.0
    target_distance: float = 0.0
    candidate_quality: float = 0.0
    reward_ema: float = 0.0
    loop_iterations: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    seed_paths: tuple[str, ...] = ()
    children: set[str] = field(default_factory=set)

    @property
    def shape(self) -> tuple[int, int, int, int, str]:
        """Constraint shape with absolute byte offsets intentionally removed."""
        return (
            self.site_id,
            self.outcome,
            self.dependency_count,
            self.dependency_span,
            self.branch_type,
        )


class ExpressiveCoverageTree:
    """Bounded Cottontail-style ECT augmented with solver/data feedback."""

    ACTIVE = {"open", "timeout"}

    def __init__(
        self,
        graph: ProgramTaskGraph | None = None,
        *,
        max_nodes: int = 16384,
        trace_cap: int = 512,
        context_depth: int = 8,
        seeds_per_node: int = 4,
        export_path: str = "",
    ) -> None:
        self.graph = graph or ProgramTaskGraph()
        self.max_nodes = max(128, int(max_nodes))
        self.trace_cap = max(8, int(trace_cap))
        self.context_depth = max(1, int(context_depth))
        self.seeds_per_node = max(1, int(seeds_per_node))
        self.export_path = export_path
        self.nodes: dict[str, ExpressiveCoverageNode] = {}
        self.branch_nodes: dict[int, str] = {}
        self.shape_visits: dict[tuple[int, int, int, int, str], int] = {}
        self.site_outcomes: dict[int, set[int]] = {}
        self.path_nodes: dict[str, tuple[str, ...]] = {}
        self.observations = 0
        self.filtered_duplicates = 0
        self.loop_compressions = 0

    @classmethod
    def from_environment(
        cls,
        graph: ProgramTaskGraph | None = None,
    ) -> "ExpressiveCoverageTree":
        try:
            max_nodes = int(os.environ.get("SYMCC_ECT_NODES", "16384"))
        except ValueError:
            max_nodes = 16384
        try:
            trace_cap = int(os.environ.get("SYMCC_ECT_TRACE", "512"))
        except ValueError:
            trace_cap = 512
        try:
            context_depth = int(os.environ.get("SYMCC_ECT_CONTEXT", "8"))
        except ValueError:
            context_depth = 8
        return cls(
            graph,
            max_nodes=max_nodes,
            trace_cap=trace_cap,
            context_depth=context_depth,
            export_path=os.environ.get("SYMCC_ECT_OUT", ""),
        )

    def _site_metadata(self, site: int) -> tuple[str, str, str]:
        block = self.graph.site_blocks.get(site, "")
        function = self.graph.block_functions.get(block, "")
        location = self.graph.site_locations.get(site, "")
        branch_type = self.graph.site_opcodes.get(site, "branch")
        if site in self.graph.switch_successors:
            branch_type = "switch"
        elif site in self.graph.branch_successors:
            branch_type = "branch"
        return function, location, branch_type

    @staticmethod
    def _taint_by_branch(telemetry: Any) -> dict[int, tuple[int, int, int]]:
        result: dict[int, tuple[int, int, int]] = {}
        for item in getattr(telemetry, "comparison_taints", ()) or ():
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                continue
            branch = _positive_int(item[1])
            count = _positive_int(item[2])
            lower = _positive_int(item[3])
            upper = _positive_int(item[4])
            if branch and count and lower <= upper:
                result[branch] = (count, lower, upper)
        return result

    @staticmethod
    def _add_seed(
        node: ExpressiveCoverageNode,
        path: str,
        limit: int,
    ) -> None:
        if path in node.seed_paths:
            return
        node.seed_paths = (node.seed_paths + (path,))[-limit:]

    def _node(
        self,
        *,
        branch: int,
        parent: int,
        site: int,
        outcome: int,
        status: str,
        context: tuple[str, ...],
        depth: int,
        dependency: tuple[int, int, int] | None,
        now: float,
    ) -> ExpressiveCoverageNode:
        function, location, branch_type = self._site_metadata(site)
        node_id = _stable_id((
            parent, branch, site, outcome, *context[-self.context_depth:]))
        node = self.nodes.get(node_id)
        if node is None:
            count, lower, upper = dependency or (0, 0, 0)
            node = ExpressiveCoverageNode(
                node_id=node_id,
                branch_id=branch,
                parent_branch=parent,
                site_id=site,
                outcome=outcome,
                status=status,
                function=function,
                location=location,
                branch_type=branch_type,
                context=context[-self.context_depth:],
                depth=depth,
                dependency_count=count,
                dependency_span=(upper - lower + 1) if count else 0,
                dependency_lo=lower,
                dependency_hi=upper,
                first_seen=now,
                last_seen=now,
            )
            self.nodes[node_id] = node
        else:
            node.status = status if status in self.ACTIVE else node.status
            node.last_seen = now
            if dependency:
                count, lower, upper = dependency
                had_dependency = node.dependency_count > 0
                node.dependency_count = max(node.dependency_count, count)
                node.dependency_span = max(
                    node.dependency_span, upper - lower + 1)
                node.dependency_lo = (
                    min(node.dependency_lo, lower)
                    if had_dependency else lower)
                node.dependency_hi = (
                    max(node.dependency_hi, upper)
                    if had_dependency else upper)
        self.branch_nodes[branch] = node_id
        self.site_outcomes.setdefault(site, set()).add(outcome)
        return node

    def observe(
        self,
        path: str,
        telemetry: Any,
        *,
        reward: float,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        killed: bool,
        now: float | None = None,
    ) -> int:
        trace = tuple(
            item for item in (getattr(telemetry, "branch_trace", ()) or ())
            if isinstance(item, (list, tuple)) and len(item) == 6
        )[:self.trace_cap]
        if not trace:
            return 0
        now = time.monotonic() if now is None else now
        taints = self._taint_by_branch(telemetry)
        open_branches = set(getattr(telemetry, "open_branches", ()) or ())
        function_context: list[str] = []
        previous_node: ExpressiveCoverageNode | None = None
        run_nodes: list[str] = []
        run_shapes: set[tuple[int, int, int, int, str]] = set()
        loop_counts: dict[tuple[int, int, str], int] = {}
        duplicates = 0

        for depth, item in enumerate(trace):
            parent, actual, opposite, site, taken, _interesting = (
                _positive_int(value) for value in item)
            function, _location, branch_type = self._site_metadata(site)
            if function and (not function_context
                             or function_context[-1] != function):
                function_context.append(function)
                if len(function_context) > self.context_depth:
                    function_context.pop(0)
            context = tuple(function_context)
            dependency = taints.get(actual) or taints.get(opposite)
            actual_node = self._node(
                branch=actual,
                parent=parent,
                site=site,
                outcome=int(bool(taken)),
                status="observed",
                context=context,
                depth=depth,
                dependency=dependency,
                now=now,
            )
            actual_node.status = "observed"
            actual_node.visits += 1
            actual_node.generated += _positive_int(
                getattr(telemetry, "generated", 0))
            actual_node.solver_sat += _positive_int(
                getattr(telemetry, "solver_sat", 0))
            actual_node.solver_unsat += _positive_int(
                getattr(telemetry, "solver_unsat", 0))
            actual_node.solver_unknown += _positive_int(
                getattr(telemetry, "solver_unknown", 0))
            actual_node.solver_time_us += _positive_int(
                getattr(telemetry, "solver_time_us", 0))
            actual_node.data_quality = max(
                0.78 * actual_node.data_quality,
                _clamp01(_finite(getattr(telemetry, "data_quality", 0.0))))
            actual_node.reward_ema = (
                reward if actual_node.visits == 1
                else 0.82 * actual_node.reward_ema + 0.18 * reward)
            actual_node.candidate_quality = _clamp01(
                0.45 * actual_node.reward_ema
                + 0.20 * actual_node.data_quality
                + 0.20 * min(1.0, coverage_delta / 4.0)
                + 0.15 * min(1.0, interesting_cases / 2.0))
            if coverage_delta > 0 or interesting_cases > 0:
                actual_node.productive_runs += 1
            self._add_seed(actual_node, path, self.seeds_per_node)
            if previous_node is not None:
                previous_node.children.add(actual_node.node_id)
            run_nodes.append(actual_node.node_id)

            loop_key = (site, int(bool(taken)), function)
            loop_counts[loop_key] = loop_counts.get(loop_key, 0) + 1
            if loop_counts[loop_key] > 1:
                actual_node.loop_iterations = max(
                    actual_node.loop_iterations, loop_counts[loop_key])
                self.loop_compressions += 1

            shape = actual_node.shape
            if shape in run_shapes:
                duplicates += 1
            else:
                run_shapes.add(shape)
            self.shape_visits[shape] = self.shape_visits.get(shape, 0) + 1

            if opposite in open_branches:
                open_node = self._node(
                    branch=opposite,
                    parent=parent,
                    site=site,
                    outcome=int(not bool(taken)),
                status="open",
                    context=context,
                    depth=depth,
                    dependency=dependency,
                    now=now,
                )
                open_node.target_distance = max(
                    open_node.target_distance,
                    1.0 - math.exp(-depth / 16.0),
                )
                self._add_seed(open_node, path, self.seeds_per_node)
                if previous_node is not None:
                    previous_node.children.add(open_node.node_id)
            previous_node = actual_node

        target = _positive_int(getattr(telemetry, "target_branch", 0))
        if target:
            node_id = self.branch_nodes.get(target)
            node = self.nodes.get(node_id or "")
            if node is not None:
                node.attempts += 1
                if bool(getattr(telemetry, "target_reached", False)):
                    node.status = (
                        "sat" if _positive_int(
                            getattr(telemetry, "solver_sat", 0))
                        or _positive_int(getattr(telemetry, "generated", 0))
                        else "reached")
                elif killed or _positive_int(
                        getattr(telemetry, "z3_timeouts", 0)):
                    node.status = "timeout"
                else:
                    node.status = "stale"
        self.path_nodes[path] = tuple(run_nodes)
        self.observations += 1
        self.filtered_duplicates += duplicates
        self._prune()
        return duplicates

    def priority(self, branch_id: int) -> float:
        node_id = self.branch_nodes.get(_positive_int(branch_id))
        node = self.nodes.get(node_id or "")
        if node is None:
            return 0.5
        shape_visits = self.shape_visits.get(node.shape, 0)
        rarity = 1.0 / math.sqrt(1.0 + shape_visits)
        untaken = 1.0 if node.status in self.ACTIVE else 0.0
        depth = 1.0 - math.exp(-node.depth / 16.0)
        dependency = (
            min(1.0, node.dependency_count / max(1, node.dependency_span))
            if node.dependency_count else 0.0)
        solver_penalty = min(
            0.45,
            0.08 * node.solver_unknown
            + 0.06 * math.log1p(node.solver_time_us / 1000.0),
        )
        return _clamp01(
            0.32 * untaken
            + 0.20 * rarity
            + 0.16 * depth
            + 0.12 * dependency
            + 0.12 * node.candidate_quality
            + 0.08 * node.target_distance
            - solver_penalty)

    def _prune(self) -> None:
        overflow = len(self.nodes) - self.max_nodes
        if overflow <= 0:
            return
        victims = sorted(
            (
                node for node in self.nodes.values()
                if node.status not in self.ACTIVE
            ),
            key=lambda node: (
                node.candidate_quality,
                node.last_seen,
                node.visits,
            ),
        )
        for node in victims[:overflow]:
            self.nodes.pop(node.node_id, None)
            if self.branch_nodes.get(node.branch_id) == node.node_id:
                self.branch_nodes.pop(node.branch_id, None)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "kind": "symcc-expressive-coverage-tree",
            "observations": self.observations,
            "filtered_duplicates": self.filtered_duplicates,
            "loop_compressions": self.loop_compressions,
            "nodes": [
                {
                    "id": node.node_id,
                    "branch": node.branch_id,
                    "parent_branch": node.parent_branch,
                    "site": node.site_id,
                    "outcome": node.outcome,
                    "status": node.status,
                    "function": node.function,
                    "location": node.location,
                    "branch_type": node.branch_type,
                    "context": list(node.context),
                    "depth": node.depth,
                    "visits": node.visits,
                    "attempts": node.attempts,
                    "productive_runs": node.productive_runs,
                    "generated": node.generated,
                    "solver": {
                        "sat": node.solver_sat,
                        "unsat": node.solver_unsat,
                        "unknown": node.solver_unknown,
                        "time_us": node.solver_time_us,
                    },
                    "dependency": {
                        "count": node.dependency_count,
                        "span": node.dependency_span,
                        "lo": node.dependency_lo,
                        "hi": node.dependency_hi,
                    },
                    "data_quality": node.data_quality,
                    "target_distance": node.target_distance,
                    "candidate_quality": node.candidate_quality,
                    "reward_ema": node.reward_ema,
                    "loop_iterations": node.loop_iterations,
                    "first_seen": node.first_seen,
                    "last_seen": node.last_seen,
                    "seed_paths": list(node.seed_paths),
                    "children": sorted(node.children),
                }
                for node in self.nodes.values()
            ],
            "paths": [
                [path, *nodes]
                for path, nodes in list(self.path_nodes.items())[-8192:]
            ],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return
        self.observations = _positive_int(raw.get("observations"))
        self.filtered_duplicates = _positive_int(
            raw.get("filtered_duplicates"))
        self.loop_compressions = _positive_int(raw.get("loop_compressions"))
        for item in raw.get("nodes", ())[-self.max_nodes:]:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id", ""))
            branch = _positive_int(item.get("branch"))
            site = _positive_int(item.get("site"))
            if not node_id or not branch or not site:
                continue
            solver = item.get("solver", {})
            dependency = item.get("dependency", {})
            if not isinstance(solver, dict):
                solver = {}
            if not isinstance(dependency, dict):
                dependency = {}
            context = item.get("context", ())
            seeds = item.get("seed_paths", ())
            children = item.get("children", ())
            node = ExpressiveCoverageNode(
                node_id=node_id,
                branch_id=branch,
                parent_branch=_positive_int(item.get("parent_branch")),
                site_id=site,
                outcome=int(bool(_positive_int(item.get("outcome")))),
                status=str(item.get("status", "observed")),
                function=str(item.get("function", "")),
                location=str(item.get("location", "")),
                branch_type=str(item.get("branch_type", "branch")),
                context=tuple(str(value) for value in context)[
                    -self.context_depth:],
                depth=_positive_int(item.get("depth")),
                visits=_positive_int(item.get("visits")),
                attempts=_positive_int(item.get("attempts")),
                productive_runs=_positive_int(item.get("productive_runs")),
                generated=_positive_int(item.get("generated")),
                solver_sat=_positive_int(solver.get("sat")),
                solver_unsat=_positive_int(solver.get("unsat")),
                solver_unknown=_positive_int(solver.get("unknown")),
                solver_time_us=_positive_int(solver.get("time_us")),
                dependency_count=_positive_int(dependency.get("count")),
                dependency_span=_positive_int(dependency.get("span")),
                dependency_lo=_positive_int(dependency.get("lo")),
                dependency_hi=_positive_int(dependency.get("hi")),
                data_quality=_clamp01(_finite(item.get("data_quality"))),
                target_distance=_clamp01(
                    _finite(item.get("target_distance"))),
                candidate_quality=_clamp01(
                    _finite(item.get("candidate_quality"))),
                reward_ema=_clamp01(_finite(item.get("reward_ema"))),
                loop_iterations=_positive_int(item.get("loop_iterations")),
                first_seen=max(0.0, _finite(item.get("first_seen"))),
                last_seen=max(0.0, _finite(item.get("last_seen"))),
                seed_paths=tuple(str(value) for value in seeds)[
                    -self.seeds_per_node:],
                children={str(value) for value in children},
            )
            self.nodes[node_id] = node
            self.branch_nodes[branch] = node_id
            self.shape_visits[node.shape] = (
                self.shape_visits.get(node.shape, 0) + node.visits)
            self.site_outcomes.setdefault(site, set()).add(node.outcome)
        for item in raw.get("paths", ())[-8192:]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            nodes = tuple(
                str(value) for value in item[1:] if str(value) in self.nodes)
            if nodes:
                self.path_nodes[str(item[0])] = nodes

    def export(self) -> bool:
        if not self.export_path:
            return False
        temporary = self.export_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(
                    self.to_mapping(), stream, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(temporary, self.export_path)
            return True
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
