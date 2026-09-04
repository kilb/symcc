"""Search policies for executable live symbolic-state frontiers.

The policy is intentionally independent from the continuation store.  An
executor supplies immutable state features, while this module owns only
bounded coverage/subpath counters, deterministic random selection, and CFG
distance analysis.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import math
import os
from typing import Any, Mapping, Sequence

from path_cover import MinimumPathCover, enumerate_minimum_path_covers


SUPPORTED_LIVE_SEARCH_STRATEGIES = frozenset({
    "bfs",
    "dfs",
    "random-state",
    "random-path",
    "nurs:covnew",
    "nurs:md2u",
    "nurs:depth",
    "nurs:icnt",
    "nurs:qc",
    "subpath",
    "target-distance",
    "loop-exit",
    "multi-objective",
    "path-cover",
    "cbc",
    "cgs",
})
_MAX_STRATEGIES = 8
_MAX_COUNTER_ENTRIES = 262_144
_MAX_DISTANCE = 1_000_000
_MAX_U63 = (1 << 63) - 1
_MASK_U64 = (1 << 64) - 1
_SEARCH_SNAPSHOT_SCHEMA_V1 = "symcc-live-state-search-snapshot-v1"
_SEARCH_SNAPSHOT_SCHEMA_V2 = "symcc-live-state-search-snapshot-v2"
_SEARCH_SNAPSHOT_SCHEMA_V3 = "symcc-live-state-search-snapshot-v3"
_SEARCH_SNAPSHOT_SCHEMA_V4 = "symcc-live-state-search-snapshot-v4"
_SEARCH_SNAPSHOT_SCHEMA = "symcc-live-state-search-snapshot-v5"
_OUTCOME_OVERFLOW_KEY = "@overflow"


@dataclass
class _OutcomeStats:
    attempts: int = 0
    completions: int = 0
    coverage_gain: int = 0
    steps: int = 0
    solver_queries: int = 0
    failures: int = 0


class _StableRandom:
    """Small, versioned SplitMix64 stream with portable snapshots."""

    def __init__(self, seed: int) -> None:
        self.state = int(seed) & _MASK_U64
        self.draws = 0

    def _next(self) -> int:
        if self.draws >= _MAX_U63:
            raise ValueError("stable random stream is exhausted")
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK_U64
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_U64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_U64
        self.draws += 1
        return (value ^ (value >> 31)) & _MASK_U64

    def random(self) -> float:
        return (self._next() >> 11) * (1.0 / (1 << 53))

    def randrange(self, stop: int) -> int:
        if isinstance(stop, bool) or not isinstance(stop, int) or stop <= 0:
            raise ValueError("stable random range must be a positive integer")
        limit = (1 << 64) - ((1 << 64) % stop)
        while True:
            value = self._next()
            if value < limit:
                return value % stop

    def snapshot(self) -> dict[str, int | str]:
        return {
            "algorithm": "splitmix64-v1",
            "state": self.state,
            "draws": self.draws,
        }

    @classmethod
    def from_snapshot(cls, raw: Any) -> "_StableRandom":
        if not isinstance(raw, Mapping) or set(raw) != {
            "algorithm", "state", "draws",
        } or raw.get("algorithm") != "splitmix64-v1":
            raise ValueError("live search random snapshot is invalid")
        state = _bounded_integer(
            raw.get("state"), "live search random state",
            minimum=0, maximum=_MASK_U64,
        )
        draws = _bounded_integer(
            raw.get("draws"), "live search random draws",
            minimum=0, maximum=_MAX_U63,
        )
        result = cls(0)
        result.state = state
        result.draws = draws
        return result


def _bounded_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside {minimum}..{maximum}")
    return parsed


def parse_live_search_strategies(raw: Any) -> tuple[str, ...]:
    """Parse one comma-separated interleaved strategy family."""
    if not isinstance(raw, str):
        raise ValueError("live search strategy list must be a string")
    values = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not values or len(values) > _MAX_STRATEGIES:
        raise ValueError(
            f"live search requires 1..{_MAX_STRATEGIES} strategies")
    if len(set(values)) != len(values):
        raise ValueError("live search strategy list contains duplicates")
    unsupported = sorted(set(values) - SUPPORTED_LIVE_SEARCH_STRATEGIES)
    if unsupported:
        raise ValueError(f"unsupported live search strategies: {unsupported}")
    return values


def location_name(function: Any, block: Any) -> str:
    function_name = str(function)
    block_name = str(block)
    if not function_name or not block_name:
        raise ValueError("live search location must name a function and block")
    if len(function_name.encode("utf-8")) > 256 \
            or len(block_name.encode("utf-8")) > 256:
        raise ValueError("live search location exceeds its byte budget")
    return f"{function_name}:{block_name}"


def search_decision_token(site: Any, choice: Any) -> int:
    """Return the stable token persisted in a continuation's branch path."""
    encoded = f"{site!s}\x00{choice!s}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")


def cgs_store_token(function: Any, block: Any, instruction: Any) -> int:
    """Return a stable token for one continuation-IR store definition."""
    encoded = (
        f"{function!s}\x00{block!s}\x00{instruction!s}"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")


@dataclass(frozen=True)
class LiveStateSearchFeatures:
    """Bounded, semantic-free feature view of one pending state."""

    identity: str
    location: str
    branch_path: tuple[int, ...] = ()
    path_depth: int = 0
    instructions: int = 0
    solver_queries: int = 0
    recent_locations: tuple[str, ...] = ()
    target_branch: int = 0
    distance_to_uncovered: int | None = None
    distance_to_target: int | None = None
    exits_cycle: bool = False
    path_cover_score: float | None = None
    cgs_priority: int = 0
    cgs_targets: int = 0
    outcome_key: str = ""


@dataclass(frozen=True)
class LivePathCoverGuidance:
    """Candidate-local Empc signals derived without mutable planner state."""

    compatible_covers: int
    recognized_decisions: int
    support: float
    remaining: float
    current_uncovered: bool
    score: float


@dataclass(frozen=True)
class LivePathCoverCoverageContext:
    """One frontier generation's precomputed uncovered cover suffixes."""

    covered_components: dict[str, frozenset[str]]
    remaining: dict[str, tuple[dict[str, float], ...]]


@dataclass(frozen=True)
class LiveCBCGuidance:
    """CBC prefix classification for one prospective live state."""

    accepted: bool
    recognized_decisions: int
    analyzable_decisions: int
    compatible_groups: int
    inconsistent_groups: int
    unknown_tokens: int
    ambiguous_tokens: int


@dataclass(frozen=True)
class _LiveCBCDecision:
    key: str
    function: str
    source: str
    dependencies: frozenset[str]
    analyzable: bool


@dataclass(frozen=True)
class LiveCGSGuidance:
    """Candidate-local concrete-constraint guidance."""

    priority: int
    active_targets: int
    valid_definitions: int
    pending_definitions: int
    recognized_values: int


@dataclass(frozen=True)
class _LiveCGSTarget:
    site: int
    function: str
    source: str
    operator: str
    constant: int
    bits: int
    transform: str
    transform_constant: int
    stores: tuple[int, ...]


@dataclass(frozen=True)
class _LiveFunctionCoverPlan:
    component_for: dict[str, str]
    component_members: dict[str, tuple[str, ...]]
    covers: tuple[MinimumPathCover, ...]
    suffixes: tuple[dict[str, tuple[str, ...]], ...]


class LiveProgramGraph:
    """Bounded interprocedural CFG facts needed by search policies."""

    def __init__(
        self,
        program: Mapping[str, Any],
        *,
        path_cover_max_covers: int = 8,
        path_cover_max_function_nodes: int = 4096,
        path_cover_enabled: bool = True,
        cbc_max_function_nodes: int = 4096,
        cbc_max_branches: int = 4096,
        cbc_enabled: bool = False,
        cgs_max_function_nodes: int = 4096,
        cgs_max_branches: int = 4096,
        cgs_enabled: bool = False,
    ) -> None:
        if not isinstance(path_cover_enabled, bool):
            raise ValueError("live path-cover enable flag must be boolean")
        if not isinstance(cbc_enabled, bool):
            raise ValueError("live CBC enable flag must be boolean")
        if not isinstance(cgs_enabled, bool):
            raise ValueError("live CGS enable flag must be boolean")
        self.path_cover_enabled = path_cover_enabled
        self.cbc_enabled = cbc_enabled
        self.cgs_enabled = cgs_enabled
        self.path_cover_max_covers = _bounded_integer(
            path_cover_max_covers,
            "live path-cover count",
            minimum=1,
            maximum=256,
        )
        self.path_cover_max_function_nodes = _bounded_integer(
            path_cover_max_function_nodes,
            "live path-cover function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cbc_max_function_nodes = _bounded_integer(
            cbc_max_function_nodes,
            "live CBC function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cbc_max_branches = _bounded_integer(
            cbc_max_branches,
            "live CBC branch count",
            minimum=2,
            maximum=65_536,
        )
        self.cgs_max_function_nodes = _bounded_integer(
            cgs_max_function_nodes,
            "live CGS function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cgs_max_branches = _bounded_integer(
            cgs_max_branches,
            "live CGS branch count",
            minimum=1,
            maximum=65_536,
        )
        functions = program.get("functions")
        if not isinstance(functions, Mapping) or len(functions) > 65_536:
            raise ValueError("live program graph has an invalid function set")
        self.adjacency: dict[str, set[str]] = {}
        self.local_adjacency: dict[str, dict[str, set[str]]] = {}
        self.location_functions: dict[str, str] = {}
        self.target_locations: dict[int, set[str]] = {}
        decision_locations: list[tuple[int, str, str, str]] = []
        cbc_instructions: dict[str, list[tuple[str, int, Mapping[str, Any]]]] = {}
        for function_name, raw_function in functions.items():
            if not isinstance(raw_function, Mapping):
                raise ValueError("live program graph function is malformed")
            blocks = raw_function.get("blocks")
            if not isinstance(blocks, Mapping) or len(blocks) > 262_144:
                raise ValueError("live program graph has an invalid block set")
            local = self.local_adjacency.setdefault(str(function_name), {})
            for block_name in blocks:
                location = location_name(function_name, block_name)
                self.adjacency.setdefault(location, set())
                local.setdefault(location, set())
                self.location_functions[location] = str(function_name)
        for function_name, raw_function in functions.items():
            blocks = raw_function["blocks"]
            local = self.local_adjacency[str(function_name)]
            function_instructions = cbc_instructions.setdefault(
                str(function_name), []
            )

            def add_local_edge(source: str, target: str) -> None:
                if target in local:
                    self.adjacency[source].add(target)
                    local[source].add(target)

            for block_name, raw_instructions in blocks.items():
                if not isinstance(raw_instructions, list):
                    raise ValueError("live program graph block is malformed")
                source = location_name(function_name, block_name)
                for instruction_index, instruction in enumerate(raw_instructions):
                    if not isinstance(instruction, Mapping):
                        raise ValueError("live program graph instruction is malformed")
                    function_instructions.append(
                        (source, instruction_index, instruction)
                    )
                    site = instruction.get("site", 0)
                    try:
                        site_id = int(site or 0)
                    except (TypeError, ValueError, OverflowError):
                        site_id = 0
                    if 0 < site_id <= (1 << 64) - 1:
                        self.target_locations.setdefault(site_id, set()).add(source)
                    op = str(instruction.get("op", ""))
                    if op == "branch":
                        for key, choice in (("true", True), ("false", False)):
                            target = location_name(
                                function_name, instruction.get(key, ""))
                            add_local_edge(source, target)
                            if target in local:
                                decision_locations.append((
                                    search_decision_token(site, choice),
                                    str(function_name), source, target,
                                ))
                    elif op == "jump":
                        target = location_name(
                            function_name, instruction.get("target", ""))
                        add_local_edge(source, target)
                    elif op == "loop_summary_transfer":
                        # The executor chooses one of these edges from an
                        # explicit configuration gate, not from a program
                        # branch.  Keep both destinations reachable for CFG
                        # analyses without manufacturing coverage decisions.
                        for key in ("fallback", "target"):
                            target = location_name(
                                function_name, instruction.get(key, ""))
                            add_local_edge(source, target)
                    elif op == "external_pure" and "normal" in instruction:
                        target = location_name(
                            function_name, instruction.get("normal", ""))
                        add_local_edge(source, target)
                    elif op == "throw_if":
                        for key, choice in (("normal", False), ("unwind", True)):
                            target = location_name(
                                function_name, instruction.get(key, ""))
                            add_local_edge(source, target)
                            if target in local:
                                decision_locations.append((
                                    search_decision_token(site, choice),
                                    str(function_name), source, target,
                                ))
                    elif op == "exception_rethrow":
                        target = location_name(
                            function_name, instruction.get("unwind", ""))
                        add_local_edge(source, target)
                    elif (
                        op == "exception_end_catch"
                        and "normal" in instruction
                    ):
                        target = location_name(
                            function_name, instruction.get("normal", ""))
                        add_local_edge(source, target)
                    elif op == "call":
                        callee = str(instruction.get("function", ""))
                        target_function = functions.get(callee)
                        if isinstance(target_function, Mapping):
                            target = location_name(
                                callee, target_function.get("entry", "entry"))
                            if target in self.adjacency:
                                self.adjacency[source].add(target)
                    elif op == "indirect_call":
                        for target_data in instruction.get("targets", ()):
                            if not isinstance(target_data, Mapping):
                                continue
                            callee = str(target_data.get("function", ""))
                            target_function = functions.get(callee)
                            if isinstance(target_function, Mapping):
                                target = location_name(
                                    callee,
                                    target_function.get("entry", "entry"),
                                )
                                if target in self.adjacency:
                                    self.adjacency[source].add(target)
        (
            self._component,
            self._cyclic_components,
            _global_members,
        ) = self._components(self.adjacency)
        self._path_cover_plans: dict[str, _LiveFunctionCoverPlan] = {}
        self._path_cover_decisions: dict[
            int, set[tuple[str, str, str]]
        ] = {}
        self._path_cover_skipped_functions = 0
        if self.path_cover_enabled:
            self._build_path_covers(decision_locations)
        self._cbc_decisions: dict[str, _LiveCBCDecision] = {}
        self._cbc_tokens: dict[int, set[tuple[str, bool]]] = {}
        self._cbc_functions_admitted = 0
        self._cbc_functions_skipped_oversized = 0
        self._cbc_functions_skipped_branch_limit = 0
        self._cbc_functions_interprocedural_fail_open = 0
        if self.cbc_enabled:
            self._build_cbc(program, cbc_instructions)
        self._cgs_targets: dict[int, _LiveCGSTarget] = {}
        self._cgs_store_locations: dict[int, str] = {}
        self._cgs_store_tokens: dict[
            tuple[str, str, int], int | None
        ] = {}
        self._cgs_store_operands: dict[int, Any] = {}
        self._cgs_functions_admitted = 0
        self._cgs_functions_skipped_oversized = 0
        self._cgs_functions_skipped_branch_limit = 0
        self._cgs_branches_rejected = 0
        self._cgs_ambiguous_sites = 0
        self._cgs_ambiguous_store_tokens = 0
        self._cgs_functions_memory_ambiguous = 0
        if self.cgs_enabled:
            self._build_cgs(cbc_instructions)

    @staticmethod
    def _components(
        adjacency: Mapping[str, set[str]],
    ) -> tuple[dict[str, int], set[int], tuple[tuple[str, ...], ...]]:
        visited: set[str] = set()
        finishing_order: list[str] = []
        for start in sorted(adjacency):
            if start in visited:
                continue
            pending: list[tuple[str, bool]] = [(start, False)]
            while pending:
                node, expanded = pending.pop()
                if expanded:
                    finishing_order.append(node)
                    continue
                if node in visited:
                    continue
                visited.add(node)
                pending.append((node, True))
                pending.extend(
                    (successor, False)
                    for successor in sorted(adjacency.get(node, ()), reverse=True)
                    if successor not in visited
                )

        reverse = {node: set() for node in adjacency}
        for source, targets in adjacency.items():
            for target in targets:
                reverse[target].add(source)
        components: list[tuple[str, ...]] = []
        assigned: set[str] = set()
        for start in reversed(finishing_order):
            if start in assigned:
                continue
            members: list[str] = []
            pending = [start]
            assigned.add(start)
            while pending:
                node = pending.pop()
                members.append(node)
                for predecessor in sorted(reverse[node], reverse=True):
                    if predecessor not in assigned:
                        assigned.add(predecessor)
                        pending.append(predecessor)
            components.append(tuple(sorted(members)))
        component_for = {
            node: component
            for component, members in enumerate(components)
            for node in members
        }
        cyclic = {
            component
            for component, members in enumerate(components)
            if len(members) > 1
            or any(node in adjacency.get(node, ()) for node in members)
        }
        return component_for, cyclic, tuple(components)

    @staticmethod
    def _cbc_operand_variables(value: Any) -> frozenset[str]:
        """Collect explicit SSA references without recursing on the Python stack."""
        result: set[str] = set()
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                if set(current) == {"var"}:
                    name = current.get("var")
                    if isinstance(name, str) and name:
                        result.add(name)
                    continue
                pending.extend(current.values())
            elif isinstance(current, Sequence) and not isinstance(
                current, (str, bytes, bytearray)
            ):
                pending.extend(current)
        return frozenset(result)

    @classmethod
    def _cbc_resolve_definitions(
        cls,
        function: Mapping[str, Any],
        instructions: Sequence[tuple[str, int, Mapping[str, Any]]],
    ) -> dict[str, frozenset[str] | None]:
        """Resolve bounded function-local data provenance; ``None`` is unknown."""
        pure_ops = {
            "binary", "unary", "select", "pointer_offset", "external_pure",
        }
        definitions: dict[
            str, tuple[frozenset[str], frozenset[str]] | None
        ] = {}

        def add_definition(
            name: str,
            value: tuple[frozenset[str], frozenset[str]] | None,
        ) -> None:
            definitions[name] = value if name not in definitions else None

        raw_params = function.get("params", ())
        if isinstance(raw_params, Sequence) and not isinstance(
            raw_params, (str, bytes, bytearray)
        ):
            for parameter in raw_params:
                if isinstance(parameter, str) and parameter:
                    add_definition(parameter, None)

        for source, instruction_index, instruction in instructions:
            raw_name = instruction.get("dst")
            if not isinstance(raw_name, str) or not raw_name:
                continue
            op = str(instruction.get("op", ""))
            if op == "const":
                definition = (frozenset(), frozenset())
            elif op == "input":
                try:
                    offset = int(instruction.get("offset", 0))
                except (TypeError, ValueError, OverflowError):
                    definition = None
                else:
                    definition = (
                        frozenset({f"input:{offset}"}), frozenset(),
                    )
            elif op == "input_size":
                definition = (frozenset({"input-size"}), frozenset())
            elif op == "nondet":
                definition = (
                    frozenset({
                        f"nondet:{source}:{instruction_index}"
                    }),
                    frozenset(),
                )
            elif op in pure_ops:
                operands = {
                    key: value for key, value in instruction.items()
                    if key not in {"dst", "normal", "unwind", "true", "false"}
                }
                definition = (
                    frozenset(), cls._cbc_operand_variables(operands),
                )
            else:
                # Memory, allocation, calls, and exception state require an
                # alias/interprocedural proof before CBC may call them independent.
                definition = None
            add_definition(raw_name, definition)

        resolved: dict[str, frozenset[str] | None] = {}
        remaining: dict[str, set[str]] = {}
        accumulated: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = {}
        ready: deque[str] = deque()
        for name, definition in definitions.items():
            if definition is None:
                resolved[name] = None
                ready.append(name)
                continue
            atoms, references = definition
            if any(reference not in definitions for reference in references):
                resolved[name] = None
                ready.append(name)
                continue
            remaining[name] = set(references)
            accumulated[name] = set(atoms)
            for reference in references:
                reverse.setdefault(reference, set()).add(name)
            if not references:
                resolved[name] = frozenset(atoms)
                ready.append(name)

        while ready:
            dependency = ready.popleft()
            for user in reverse.get(dependency, ()):
                if user in resolved:
                    continue
                remaining[user].discard(dependency)
                value = resolved[dependency]
                if value is None:
                    resolved[user] = None
                    ready.append(user)
                else:
                    accumulated[user].update(value)
                    if not remaining[user]:
                        resolved[user] = frozenset(accumulated[user])
                        ready.append(user)

        # Remaining nodes are cyclic definitions or depend on one.
        for name in definitions:
            resolved.setdefault(name, None)
        return resolved

    @staticmethod
    def _cbc_postdominators(
        adjacency: Mapping[str, set[str]],
    ) -> tuple[dict[str, int], dict[str, int], set[str]]:
        """Compute postdominators as integer bitsets plus exit reachability."""
        nodes = tuple(sorted(adjacency))
        index = {node: position for position, node in enumerate(nodes)}
        exit_bit = 1 << len(nodes)
        terminals = {node for node in nodes if not adjacency.get(node)}
        reverse = {node: set() for node in nodes}
        for source, targets in adjacency.items():
            for target in targets:
                if target in reverse:
                    reverse[target].add(source)
        exit_reachable = set(terminals)
        pending = list(terminals)
        while pending:
            node = pending.pop()
            for predecessor in reverse[node]:
                if predecessor not in exit_reachable:
                    exit_reachable.add(predecessor)
                    pending.append(predecessor)

        reachable_mask = exit_bit
        for node in exit_reachable:
            reachable_mask |= 1 << index[node]
        postdominators: dict[str, int] = {}
        for node in nodes:
            own = 1 << index[node]
            if node in terminals:
                postdominators[node] = own | exit_bit
            elif node in exit_reachable:
                postdominators[node] = reachable_mask
            else:
                postdominators[node] = own

        changed = True
        while changed:
            changed = False
            for node in reversed(nodes):
                if node not in exit_reachable or node in terminals:
                    continue
                successors = adjacency.get(node, set())
                if not successors or any(
                    successor not in exit_reachable for successor in successors
                ):
                    continue
                intersection = reachable_mask
                for successor in successors:
                    intersection &= postdominators[successor]
                updated = (1 << index[node]) | intersection
                if updated != postdominators[node]:
                    postdominators[node] = updated
                    changed = True
        return postdominators, index, exit_reachable

    def _build_cbc(
        self,
        program: Mapping[str, Any],
        instructions_by_function: Mapping[
            str, Sequence[tuple[str, int, Mapping[str, Any]]]
        ],
    ) -> None:
        functions = program["functions"]
        interprocedural_functions: set[str] = set()
        for caller, instructions in instructions_by_function.items():
            for _source, _index, instruction in instructions:
                op = str(instruction.get("op", ""))
                if op == "call":
                    interprocedural_functions.add(caller)
                    interprocedural_functions.add(str(instruction.get("function", "")))
                elif op == "indirect_call":
                    interprocedural_functions.add(caller)
                    for target in instruction.get("targets", ()):
                        if isinstance(target, Mapping):
                            interprocedural_functions.add(
                                str(target.get("function", ""))
                            )
        for function_name, instructions in sorted(instructions_by_function.items()):
            function = functions[function_name]
            adjacency = self.local_adjacency[function_name]
            branches = [
                (source, index, instruction)
                for source, index, instruction in instructions
                if str(instruction.get("op", "")) in {"branch", "throw_if"}
            ]
            if not branches:
                continue
            if len(adjacency) > self.cbc_max_function_nodes:
                self._cbc_functions_skipped_oversized += 1
                continue
            if len(branches) > self.cbc_max_branches:
                self._cbc_functions_skipped_branch_limit += 1
                continue
            self._cbc_functions_admitted += 1
            resolved = self._cbc_resolve_definitions(function, instructions)
            component_for, cyclic_components, _members = self._components(adjacency)
            postdominators, node_index, exit_reachable = (
                self._cbc_postdominators(adjacency)
            )

            records: dict[str, tuple[str, int, Mapping[str, Any]]] = {}
            raw_dependencies: dict[str, frozenset[str] | None] = {}
            source_keys: dict[str, list[str]] = {}
            for source, instruction_index, instruction in branches:
                key = f"{function_name}:{source}:{instruction_index}"
                records[key] = (source, instruction_index, instruction)
                source_keys.setdefault(source, []).append(key)
                references = self._cbc_operand_variables(
                    instruction.get("condition")
                )
                dependencies: set[str] = set()
                analyzable = True
                for reference in references:
                    value = resolved.get(reference)
                    if value is None:
                        analyzable = False
                        break
                    dependencies.update(value)
                if source not in exit_reachable or (
                    component_for.get(source) in cyclic_components
                ) or any(
                    successor not in exit_reachable
                    for successor in adjacency.get(source, ())
                ):
                    analyzable = False
                raw_dependencies[key] = (
                    frozenset(dependencies) if analyzable else None
                )

            correlation_atoms: dict[str, set[str]] = {}
            unknown_assumption = function_name in interprocedural_functions
            if unknown_assumption:
                self._cbc_functions_interprocedural_fail_open += 1
            for source, instruction_index, instruction in instructions:
                if unknown_assumption:
                    break
                if str(instruction.get("op", "")) != "assume":
                    continue
                references = self._cbc_operand_variables(
                    instruction.get("condition")
                )
                dependencies: set[str] = set()
                for reference in references:
                    value = resolved.get(reference)
                    if value is None:
                        unknown_assumption = True
                        break
                    dependencies.update(value)
                if unknown_assumption:
                    break
                atom = f"assume:{source}:{instruction_index}"
                for dependency in dependencies:
                    correlation_atoms.setdefault(dependency, set()).add(atom)
            if unknown_assumption:
                raw_dependencies = {key: None for key in raw_dependencies}

            controllers: dict[str, set[str]] = {key: set() for key in records}
            for controller_key, (source, _index, _instruction) in records.items():
                if raw_dependencies[controller_key] is None:
                    continue
                controlled_mask = 0
                for successor in adjacency.get(source, ()):
                    controlled_mask |= postdominators[successor]
                controlled_mask &= ~postdominators[source]
                for controlled_source, keys in source_keys.items():
                    bit = 1 << node_index[controlled_source]
                    if controlled_mask & bit:
                        for key in keys:
                            if key != controller_key:
                                controllers[key].add(controller_key)

            dependencies_by_key: dict[str, set[str]] = {}
            analyzable_by_key: dict[str, bool] = {}
            for key, dependencies in raw_dependencies.items():
                analyzable_by_key[key] = dependencies is not None
                values = set(dependencies or ())
                values.add(f"branch:{key}")
                for dependency in tuple(values):
                    values.update(correlation_atoms.get(dependency, ()))
                dependencies_by_key[key] = values

            for _round in range(len(records) + 1):
                changed = False
                for key in records:
                    for controller in controllers[key]:
                        if not analyzable_by_key[controller]:
                            if analyzable_by_key[key]:
                                analyzable_by_key[key] = False
                                changed = True
                            continue
                        before = len(dependencies_by_key[key])
                        dependencies_by_key[key].update(
                            dependencies_by_key[controller]
                        )
                        changed |= len(dependencies_by_key[key]) != before
                if not changed:
                    break

            for key, (source, _instruction_index, instruction) in records.items():
                decision = _LiveCBCDecision(
                    key=key,
                    function=function_name,
                    source=source,
                    dependencies=frozenset(dependencies_by_key[key]),
                    analyzable=analyzable_by_key[key],
                )
                self._cbc_decisions[key] = decision
                try:
                    site = int(instruction.get("site", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not 0 < site <= _MASK_U64:
                    continue
                for choice in (False, True):
                    token = search_decision_token(site, choice)
                    self._cbc_tokens.setdefault(token, set()).add((key, choice))

    @staticmethod
    def _cbc_compatible(
        left: _LiveCBCDecision,
        right: _LiveCBCDecision,
    ) -> bool:
        return (
            left.key != right.key
            and left.function == right.function
            and left.analyzable
            and right.analyzable
            and left.dependencies.isdisjoint(right.dependencies)
        )

    def cbc_guidance(
        self,
        branch_path: Sequence[int],
    ) -> LiveCBCGuidance | None:
        """Apply CBC's stable greedy grouping to the latest branch outcomes."""
        if not self.cbc_enabled:
            return None
        order: list[str] = []
        outcomes: dict[str, bool] = {}
        unknown = 0
        ambiguous = 0
        recognized = 0
        for token in branch_path[-256:]:
            choices = self._cbc_tokens.get(token)
            if not choices:
                unknown += 1
                continue
            if len(choices) != 1:
                ambiguous += 1
                continue
            key, choice = next(iter(choices))
            recognized += 1
            if key not in outcomes:
                order.append(key)
            outcomes[key] = choice

        groups: list[list[str]] = []
        for key in order:
            decision = self._cbc_decisions[key]
            for group in groups:
                if all(self._cbc_compatible(
                    decision, self._cbc_decisions[member]
                ) for member in group):
                    group.append(key)
                    break
            else:
                groups.append([key])
        compatible = [group for group in groups if len(group) > 1]
        inconsistent = sum(
            len({outcomes[key] for key in group}) > 1
            for group in compatible
        )
        return LiveCBCGuidance(
            accepted=inconsistent == 0,
            recognized_decisions=recognized,
            analyzable_decisions=sum(
                self._cbc_decisions[key].analyzable for key in order
            ),
            compatible_groups=len(compatible),
            inconsistent_groups=inconsistent,
            unknown_tokens=unknown,
            ambiguous_tokens=ambiguous,
        )

    def cbc_telemetry(self) -> dict[str, int | bool]:
        """Expose static CBC admission facts without treating them as proof logs."""
        return {
            "enabled": self.cbc_enabled,
            "functions_total": len(self.local_adjacency),
            "functions_admitted": self._cbc_functions_admitted,
            "functions_skipped_oversized": self._cbc_functions_skipped_oversized,
            "functions_skipped_branch_limit": (
                self._cbc_functions_skipped_branch_limit
            ),
            "functions_interprocedural_fail_open": (
                self._cbc_functions_interprocedural_fail_open
            ),
            "branches_analyzed": len(self._cbc_decisions),
            "branches_analyzable": sum(
                decision.analyzable for decision in self._cbc_decisions.values()
            ),
            "branches_unanalyzable": sum(
                not decision.analyzable
                for decision in self._cbc_decisions.values()
            ),
            "decision_tokens": len(self._cbc_tokens),
            "ambiguous_decision_tokens": sum(
                len(choices) > 1 for choices in self._cbc_tokens.values()
            ),
        }

    @staticmethod
    def _cgs_constant(value: Any) -> tuple[int, int | None] | None:
        if isinstance(value, bool):
            return int(value), 1
        if isinstance(value, int):
            return value, None
        if not isinstance(value, Mapping) or "const" not in value:
            return None
        try:
            constant = int(value["const"])
            bits = int(value.get("bits", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if bits and not 1 <= bits <= 64:
            return None
        return constant, bits or None

    @staticmethod
    def _cgs_variable(value: Any) -> str | None:
        if not isinstance(value, Mapping) or set(value) != {"var"}:
            return None
        name = value.get("var")
        return name if isinstance(name, str) and name else None

    @classmethod
    def _cgs_extract_target(
        cls,
        instruction: Mapping[str, Any],
        definitions: Mapping[str, Mapping[str, Any] | None],
    ) -> tuple[str, int, int, str, int, int] | None:
        condition_name = cls._cgs_variable(instruction.get("condition"))
        comparison = definitions.get(condition_name or "")
        if not isinstance(comparison, Mapping) or comparison.get("op") != "binary":
            return None
        operator = str(comparison.get("operator", ""))
        comparisons = {
            "eq", "ne", "ult", "ule", "ugt", "uge",
            "slt", "sle", "sgt", "sge",
        }
        if operator not in comparisons:
            return None
        left = comparison.get("left")
        right = comparison.get("right")
        left_constant = cls._cgs_constant(left)
        right_constant = cls._cgs_constant(right)
        if (left_constant is None) == (right_constant is None):
            return None
        if left_constant is not None:
            constant, constant_bits = left_constant
            value = right
            operator = {
                "ult": "ugt", "ule": "uge", "ugt": "ult", "uge": "ule",
                "slt": "sgt", "sle": "sge", "sgt": "slt", "sge": "sle",
            }.get(operator, operator)
        else:
            assert right_constant is not None
            constant, constant_bits = right_constant
            value = left

        transform = "identity"
        transform_constant = 0
        transform_bits: int | None = None
        value_name = cls._cgs_variable(value)
        value_definition = definitions.get(value_name or "")
        if (
            isinstance(value_definition, Mapping)
            and value_definition.get("op") == "binary"
            and str(value_definition.get("operator", "")) in {"and", "or"}
        ):
            transform_operator = str(value_definition["operator"])
            transform_left = value_definition.get("left")
            transform_right = value_definition.get("right")
            left_mask = cls._cgs_constant(transform_left)
            right_mask = cls._cgs_constant(transform_right)
            if (left_mask is None) == (right_mask is None):
                return None
            transform = transform_operator
            if left_mask is not None:
                transform_constant, transform_bits = left_mask
                value = transform_right
            else:
                assert right_mask is not None
                transform_constant, transform_bits = right_mask
                value = transform_left
            value_name = cls._cgs_variable(value)
            value_definition = definitions.get(value_name or "")

        seen: set[str] = set()
        while (
            isinstance(value_definition, Mapping)
            and value_definition.get("op") == "unary"
            and str(value_definition.get("operator", "")) == "identity"
        ):
            if value_name is None or value_name in seen:
                return None
            seen.add(value_name)
            value = value_definition.get("value")
            value_name = cls._cgs_variable(value)
            value_definition = definitions.get(value_name or "")
        if not isinstance(value_definition, Mapping) or value_definition.get("op") != "load":
            return None
        if "guard" in value_definition:
            return None
        address = cls._cgs_constant(value_definition.get("address"))
        try:
            bits = int(value_definition.get("bits", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if address is None or not 1 <= bits <= 64:
            return None
        if constant_bits is not None and constant_bits != bits:
            return None
        if transform_bits is not None and transform_bits != bits:
            return None
        return operator, constant, bits, transform, transform_constant, address[0]

    def _build_cgs(
        self,
        instructions_by_function: Mapping[
            str, Sequence[tuple[str, int, Mapping[str, Any]]]
        ],
    ) -> None:
        admitted_functions: set[str] = set()
        branches_by_function: dict[
            str, list[tuple[str, int, Mapping[str, Any]]]
        ] = {}
        for function, instructions in sorted(instructions_by_function.items()):
            branches = [
                (source, index, instruction)
                for source, index, instruction in instructions
                if str(instruction.get("op", "")) == "branch"
            ]
            branches_by_function[function] = branches
            if not branches:
                continue
            if len(self.local_adjacency[function]) > self.cgs_max_function_nodes:
                self._cgs_functions_skipped_oversized += 1
                continue
            if len(branches) > self.cgs_max_branches:
                self._cgs_functions_skipped_branch_limit += 1
                continue
            self._cgs_functions_admitted += 1
            admitted_functions.add(function)

        store_records: dict[
            tuple[str, str, int], tuple[int, int, int]
        ] = {}
        store_operands: dict[tuple[str, str, int], Any] = {}
        unsafe_store_bytes: dict[str, set[int]] = {}
        store_shapes_by_byte: dict[
            str, dict[int, set[tuple[int, int]]]
        ] = {}
        memory_ambiguous_functions: set[str] = set()
        tokens: dict[int, set[tuple[str, str, int]]] = {}
        for function, instructions in instructions_by_function.items():
            if function not in admitted_functions:
                continue
            for source, index, instruction in instructions:
                op = str(instruction.get("op", ""))
                if op in {
                    "call", "indirect_call", "heap_alloc", "heap_realloc",
                    "heap_free", "exception_alloc",
                }:
                    memory_ambiguous_functions.add(function)
                if op != "store":
                    continue
                address = self._cgs_constant(instruction.get("address"))
                try:
                    bits = int(instruction.get("bits", 0))
                except (TypeError, ValueError, OverflowError):
                    memory_ambiguous_functions.add(function)
                    continue
                if (
                    address is None
                    or not 0 <= address[0] <= _MASK_U64
                    or not 1 <= bits <= 64
                    or address[0] + (bits + 7) // 8 > _MASK_U64 + 1
                ):
                    memory_ambiguous_functions.add(function)
                    continue
                shape = (address[0], bits)
                byte_addresses = range(
                    address[0], address[0] + (bits + 7) // 8,
                )
                if "guard" in instruction:
                    unsafe_store_bytes.setdefault(function, set()).update(
                        byte_addresses
                    )
                    continue
                function_shapes = store_shapes_by_byte.setdefault(function, {})
                for byte_address in byte_addresses:
                    function_shapes.setdefault(byte_address, set()).add(shape)
                key = (function, source, index)
                token = cgs_store_token(function, source, index)
                store_records[key] = (token, address[0], bits)
                store_operands[key] = instruction.get("value")
                tokens.setdefault(token, set()).add(key)
        self._cgs_functions_memory_ambiguous = len(
            memory_ambiguous_functions
        )
        for token, keys in tokens.items():
            if len(keys) != 1:
                self._cgs_ambiguous_store_tokens += 1
                for key in keys:
                    self._cgs_store_tokens[key] = None
                continue
            key = next(iter(keys))
            self._cgs_store_tokens[key] = token
            self._cgs_store_locations[token] = key[1]
            self._cgs_store_operands[token] = store_operands[key]
        exact_store_tokens: dict[tuple[str, int, int], list[int]] = {}
        for key, (token, address, bits) in store_records.items():
            if self._cgs_store_tokens.get(key) == token:
                exact_store_tokens.setdefault(
                    (key[0], address, bits), [],
                ).append(token)

        targets_by_site: dict[int, list[_LiveCGSTarget]] = {}
        for function in sorted(admitted_functions):
            instructions = instructions_by_function[function]
            branches = branches_by_function[function]
            if function in memory_ambiguous_functions:
                self._cgs_branches_rejected += len(branches)
                continue
            definitions: dict[str, Mapping[str, Any] | None] = {}
            for _source, _index, candidate in instructions:
                name = candidate.get("dst")
                if not isinstance(name, str) or not name:
                    continue
                definitions[name] = candidate if name not in definitions else None
            for source, _index, branch in branches:
                try:
                    site = int(branch.get("site", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    self._cgs_branches_rejected += 1
                    continue
                extracted = self._cgs_extract_target(branch, definitions)
                if not 0 < site <= _MASK_U64 or extracted is None:
                    self._cgs_branches_rejected += 1
                    continue
                operator, constant, bits, transform, transform_constant, address = (
                    extracted
                )
                if (
                    not 0 <= address <= _MASK_U64
                    or address + (bits + 7) // 8 > _MASK_U64 + 1
                ):
                    self._cgs_branches_rejected += 1
                    continue
                byte_count = (bits + 7) // 8
                target_end = address + byte_count
                target_shape = (address, bits)
                unsafe_bytes = unsafe_store_bytes.get(function, set())
                function_shapes = store_shapes_by_byte.get(function, {})
                has_unsafe_overlap = any(
                    byte_address in unsafe_bytes
                    or any(
                        shape != target_shape
                        for shape in function_shapes.get(byte_address, ())
                    )
                    for byte_address in range(address, target_end)
                )
                if has_unsafe_overlap:
                    self._cgs_branches_rejected += 1
                    continue
                stores = tuple(sorted(exact_store_tokens.get(
                    (function, address, bits), (),
                )))
                if not stores:
                    self._cgs_branches_rejected += 1
                    continue
                targets_by_site.setdefault(site, []).append(_LiveCGSTarget(
                    site=site,
                    function=function,
                    source=source,
                    operator=operator,
                    constant=constant,
                    bits=bits,
                    transform=transform,
                    transform_constant=transform_constant,
                    stores=stores,
                ))
        for site, targets_for_site in targets_by_site.items():
            if len(targets_for_site) != 1:
                self._cgs_ambiguous_sites += 1
                self._cgs_branches_rejected += len(targets_for_site)
                continue
            self._cgs_targets[site] = targets_for_site[0]

    @staticmethod
    def _cgs_signed(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return value - (1 << bits) if value & sign else value

    @classmethod
    def _cgs_evaluate(cls, target: _LiveCGSTarget, raw_value: int) -> bool:
        mask = (1 << target.bits) - 1
        value = int(raw_value) & mask
        transform_constant = target.transform_constant & mask
        if target.transform == "and":
            value &= transform_constant
        elif target.transform == "or":
            value |= transform_constant
        constant = target.constant & mask
        if target.operator == "eq":
            return value == constant
        if target.operator == "ne":
            return value != constant
        if target.operator.startswith("s"):
            value = cls._cgs_signed(value, target.bits)
            constant = cls._cgs_signed(constant, target.bits)
        if target.operator.endswith("lt"):
            return value < constant
        if target.operator.endswith("le"):
            return value <= constant
        if target.operator.endswith("gt"):
            return value > constant
        if target.operator.endswith("ge"):
            return value >= constant
        raise ValueError("live CGS target has an invalid comparison")

    def cgs_has_target(self, site: int) -> bool:
        return self.cgs_enabled and int(site) in self._cgs_targets

    def cgs_store_identity(
        self, function: str, block: str, instruction: int,
    ) -> int | None:
        source = location_name(function, block)
        return self._cgs_store_tokens.get((function, source, instruction))

    def cgs_store_candidate(
        self, function: str, block: str, instruction: int,
    ) -> tuple[int, Any] | None:
        token = self.cgs_store_identity(function, block, instruction)
        if token is None:
            return None
        return token, self._cgs_store_operands[token]

    def cgs_guidance(
        self,
        _location: str,
        latest_values: Mapping[int, int],
        active_targets: Sequence[tuple[int, bool]],
    ) -> LiveCGSGuidance | None:
        if not self.cgs_enabled:
            return None
        valid = 0
        pending = 0
        recognized_values = 0
        admitted_targets = 0
        for site, uncovered_choice in active_targets:
            target = self._cgs_targets.get(int(site))
            if target is None:
                continue
            admitted_targets += 1
            target_valid = False
            for token in target.stores:
                if token in latest_values:
                    recognized_values += 1
                    if self._cgs_evaluate(target, latest_values[token]) == bool(
                        uncovered_choice
                    ):
                        target_valid = True
            valid += target_valid
        return LiveCGSGuidance(
            priority=2 if valid else 0,
            active_targets=admitted_targets,
            valid_definitions=valid,
            pending_definitions=pending,
            recognized_values=recognized_values,
        )

    def cgs_relevant_store_tokens(
        self, active_targets: Sequence[tuple[int, bool]],
    ) -> frozenset[int]:
        return frozenset(
            token
            for site, _choice in active_targets
            for target in (self._cgs_targets.get(int(site)),)
            if target is not None
            for token in target.stores
        )

    def cgs_telemetry(self) -> dict[str, int | bool]:
        return {
            "enabled": self.cgs_enabled,
            "functions_total": len(self.local_adjacency),
            "functions_admitted": self._cgs_functions_admitted,
            "functions_skipped_oversized": self._cgs_functions_skipped_oversized,
            "functions_skipped_branch_limit": (
                self._cgs_functions_skipped_branch_limit
            ),
            "branches_admitted": len(self._cgs_targets),
            "branches_rejected": self._cgs_branches_rejected,
            "ambiguous_branch_sites": self._cgs_ambiguous_sites,
            "stores_admitted": len(self._cgs_store_locations),
            "ambiguous_store_tokens": self._cgs_ambiguous_store_tokens,
            "functions_memory_ambiguous": (
                self._cgs_functions_memory_ambiguous
            ),
        }

    def _build_path_covers(
        self,
        decision_locations: Sequence[tuple[int, str, str, str]],
    ) -> None:
        for function, adjacency in sorted(self.local_adjacency.items()):
            if not adjacency:
                continue
            if len(adjacency) > self.path_cover_max_function_nodes:
                self._path_cover_skipped_functions += 1
                continue
            component_for_index, _cyclic, members = self._components(adjacency)
            component_names = tuple(
                f"component-{index}" for index in range(len(members))
            )
            component_for = {
                location: component_names[index]
                for location, index in component_for_index.items()
            }
            component_members = {
                component_names[index]: tuple(sorted(component))
                for index, component in enumerate(members)
            }
            dag = {component: set() for component in component_names}
            for source, targets in adjacency.items():
                source_component = component_for[source]
                for target in targets:
                    target_component = component_for[target]
                    if source_component != target_component:
                        dag[source_component].add(target_component)
            covers = enumerate_minimum_path_covers(
                dag, max_covers=self.path_cover_max_covers,
            )
            if covers:
                suffixes = tuple({
                    component: path[index:]
                    for path in cover.paths
                    for index, component in enumerate(path)
                } for cover in covers)
                self._path_cover_plans[function] = _LiveFunctionCoverPlan(
                    component_for=component_for,
                    component_members=component_members,
                    covers=covers,
                    suffixes=suffixes,
                )

        for token, function, source, target in decision_locations:
            plan = self._path_cover_plans.get(function)
            if plan is None:
                continue
            source_component = plan.component_for.get(source)
            target_component = plan.component_for.get(target)
            if source_component is None or target_component is None:
                continue
            self._path_cover_decisions.setdefault(token, set()).add((
                function, source_component, target_component,
            ))

    def path_cover_guidance(
        self,
        source: str,
        branch_path: Sequence[int],
        covered: set[str],
        coverage_context: LivePathCoverCoverageContext | None = None,
    ) -> LivePathCoverGuidance | None:
        """Score one pending state against compatible function-local MPCs."""
        function = self.location_functions.get(source)
        plan = self._path_cover_plans.get(function or "")
        if plan is None or source not in plan.component_for:
            return None
        active = set(range(len(plan.covers)))
        recognized = 0
        support = 0.0
        for raw_token in branch_path[-256:]:
            if isinstance(raw_token, bool) or not isinstance(raw_token, int):
                continue
            choices = {
                (edge_source, edge_target)
                for edge_function, edge_source, edge_target
                in self._path_cover_decisions.get(raw_token, ())
                if edge_function == function
            }
            # A 64-bit token collision or repeated site with different targets
            # is ambiguous. Ignoring it is conservative: no cover is removed.
            if len(choices) != 1:
                continue
            edge = next(iter(choices))
            recognized += 1
            if edge[0] == edge[1]:
                continue
            supporting = {
                index for index, cover in enumerate(plan.covers)
                if edge in cover.edges
            }
            matched = active & supporting
            support = len(matched) / max(1, len(active))
            if matched:
                active = matched

        current_component = plan.component_for[source]
        covered_components = (
            coverage_context.covered_components.get(function, frozenset())
            if coverage_context is not None
            else frozenset(
                component
                for location in covered
                if (component := plan.component_for.get(location)) is not None
            )
        )
        if coverage_context is not None:
            remaining = max(
                coverage_context.remaining[function][index][current_component]
                for index in active
            )
        else:
            remaining = 0.0
            for index in active:
                suffix = plan.suffixes[index][current_component]
                remaining = max(
                    remaining,
                    sum(
                        component not in covered_components
                        for component in suffix
                    ) / max(1, len(suffix)),
                )
        current_uncovered = current_component not in covered_components
        score = (
            0.40 * support
            + 0.45 * remaining
            + 0.15 * float(current_uncovered)
        )
        return LivePathCoverGuidance(
            compatible_covers=len(active),
            recognized_decisions=recognized,
            support=support,
            remaining=remaining,
            current_uncovered=current_uncovered,
            score=max(0.0, min(1.0, score)),
        )

    def path_cover_coverage_context(
        self,
        covered: set[str],
    ) -> LivePathCoverCoverageContext:
        """Project global location coverage onto every admitted local SCC DAG."""
        if not self._path_cover_plans:
            return LivePathCoverCoverageContext({}, {})
        result: dict[str, set[str]] = {
            function: set() for function in self._path_cover_plans
        }
        for location in covered:
            function = self.location_functions.get(location)
            plan = self._path_cover_plans.get(function or "")
            if plan is None:
                continue
            component = plan.component_for.get(location)
            if component is not None:
                result[function].add(component)
        frozen = {
            function: frozenset(components)
            for function, components in result.items()
        }
        remaining: dict[str, tuple[dict[str, float], ...]] = {}
        for function, plan in self._path_cover_plans.items():
            covered_components = frozen[function]
            cover_values: list[dict[str, float]] = []
            for cover in plan.covers:
                values: dict[str, float] = {}
                for path in cover.paths:
                    uncovered = 0
                    for reverse_index, component in enumerate(reversed(path), 1):
                        uncovered += component not in covered_components
                        values[component] = uncovered / reverse_index
                cover_values.append(values)
            remaining[function] = tuple(cover_values)
        return LivePathCoverCoverageContext(frozen, remaining)

    def path_cover_telemetry(self) -> dict[str, int | bool]:
        """Expose bounded plan facts without making them correctness state."""
        return {
            "enabled": self.path_cover_enabled,
            "functions_total": len(self.local_adjacency),
            "functions_admitted": len(self._path_cover_plans),
            "functions_skipped_oversized": self._path_cover_skipped_functions,
            "components": sum(
                len(plan.component_members)
                for plan in self._path_cover_plans.values()
            ),
            "covers": sum(
                len(plan.covers) for plan in self._path_cover_plans.values()
            ),
            "decision_tokens": len(self._path_cover_decisions),
            "ambiguous_decision_tokens": sum(
                len(choices) > 1
                for choices in self._path_cover_decisions.values()
            ),
        }

    def distance(self, source: str, destinations: set[str]) -> int | None:
        if source in destinations:
            return 0
        if source not in self.adjacency or not destinations:
            return None
        pending: deque[tuple[str, int]] = deque([(source, 0)])
        seen = {source}
        while pending:
            current, depth = pending.popleft()
            if depth >= _MAX_DISTANCE:
                break
            for successor in self.adjacency.get(current, ()):
                if successor in destinations:
                    return depth + 1
                if successor not in seen:
                    seen.add(successor)
                    pending.append((successor, depth + 1))
        return None

    def distance_to_uncovered(self, source: str, covered: set[str]) -> int | None:
        return self.distance(source, set(self.adjacency) - covered)

    def distance_to_target(self, source: str, target_branch: int) -> int | None:
        return self.distance(source, self.target_locations.get(target_branch, set()))

    def exits_cycle(self, recent_locations: Sequence[str]) -> bool:
        if len(recent_locations) < 2:
            return False
        previous, current = recent_locations[-2:]
        previous_component = self._component.get(previous)
        current_component = self._component.get(current)
        return (
            previous_component in self._cyclic_components
            and current_component is not None
            and current_component != previous_component
        )


class LiveStateSearchPolicy:
    """Interleaved KLEE-style and path-structure search family."""

    def __init__(
        self,
        strategies: Sequence[str] = ("bfs",),
        *,
        seed: int = 1,
        subpath_length: int = 2,
        counter_limit: int = _MAX_COUNTER_ENTRIES,
        path_cover_max_covers: int = 8,
        path_cover_max_function_nodes: int = 4096,
        cbc_state_threshold: int = 5,
        cbc_max_function_nodes: int = 4096,
        cbc_max_branches: int = 4096,
        cgs_target_limit: int = 10,
        cgs_rotation_instructions: int = 1_000_000,
        cgs_max_function_nodes: int = 4096,
        cgs_max_branches: int = 4096,
    ) -> None:
        self.strategies = parse_live_search_strategies(
            ",".join(str(strategy) for strategy in strategies))
        self.seed = _bounded_integer(
            seed, "live search seed", minimum=0, maximum=(1 << 64) - 1)
        self.subpath_length = _bounded_integer(
            subpath_length, "live subpath length", minimum=1, maximum=8)
        self.counter_limit = _bounded_integer(
            counter_limit, "live counter limit", minimum=16,
            maximum=_MAX_COUNTER_ENTRIES)
        self.path_cover_max_covers = _bounded_integer(
            path_cover_max_covers,
            "live path-cover count",
            minimum=1,
            maximum=256,
        )
        self.path_cover_max_function_nodes = _bounded_integer(
            path_cover_max_function_nodes,
            "live path-cover function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cbc_state_threshold = _bounded_integer(
            cbc_state_threshold,
            "live CBC state threshold",
            minimum=1,
            maximum=100_000,
        )
        self.cbc_max_function_nodes = _bounded_integer(
            cbc_max_function_nodes,
            "live CBC function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cbc_max_branches = _bounded_integer(
            cbc_max_branches,
            "live CBC branch count",
            minimum=2,
            maximum=65_536,
        )
        self.cgs_target_limit = _bounded_integer(
            cgs_target_limit,
            "live CGS target count",
            minimum=1,
            maximum=1024,
        )
        self.cgs_rotation_instructions = _bounded_integer(
            cgs_rotation_instructions,
            "live CGS rotation instructions",
            minimum=1,
            maximum=(1 << 31) - 1,
        )
        self.cgs_max_function_nodes = _bounded_integer(
            cgs_max_function_nodes,
            "live CGS function nodes",
            minimum=16,
            maximum=262_144,
        )
        self.cgs_max_branches = _bounded_integer(
            cgs_max_branches,
            "live CGS branch count",
            minimum=1,
            maximum=65_536,
        )
        self.random = _StableRandom(self.seed)
        self.covered_locations: set[str] = set()
        self.location_counts: Counter[str] = Counter()
        self.subpath_counts: Counter[tuple[str, ...]] = Counter()
        self.selection_counts: Counter[str] = Counter()
        self.selection_round = 0
        self.outcome_stats: dict[str, _OutcomeStats] = {}
        self.cgs_instruction_count = 0
        self.cgs_branch_outcomes: dict[int, int] = {}
        self.cgs_partial_order: list[int] = []
        self.cgs_observations = 0
        self.cgs_dropped_branches = 0

    @staticmethod
    def feature_outcome_key(feature: LiveStateSearchFeatures) -> str:
        value = feature.outcome_key or feature.location
        key = LiveStateSearchPolicy._snapshot_location(value)
        if key == _OUTCOME_OVERFLOW_KEY:
            raise ValueError("live search outcome key is reserved")
        return key

    def _bounded_outcome_key(self, feature: LiveStateSearchFeatures) -> str:
        key = self.feature_outcome_key(feature)
        if key in self.outcome_stats:
            return key
        # Reserve one entry for deterministic aggregation before the table is
        # full; otherwise the first overflow would create limit + 1 entries
        # and make the just-written snapshot impossible to restore.
        if len(self.outcome_stats) < self.counter_limit - 1:
            return key
        return _OUTCOME_OVERFLOW_KEY

    @staticmethod
    def _increment(value: int, amount: int, name: str) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError(f"{name} increment is invalid")
        if value > _MAX_U63 - amount:
            raise ValueError(f"{name} is exhausted")
        return value + amount

    def _record_attempt(self, feature: LiveStateSearchFeatures) -> None:
        key = self._bounded_outcome_key(feature)
        stats = self.outcome_stats.setdefault(key, _OutcomeStats())
        stats.attempts = self._increment(
            stats.attempts, 1, "live search attempt count")

    def observe_outcome(
        self,
        outcome_key: str,
        *,
        coverage_gain: int,
        steps: int,
        solver_queries: int,
        failed: bool = False,
    ) -> None:
        """Merge one completed claim into commutative integer statistics."""
        key = self._snapshot_location(outcome_key)
        if key not in self.outcome_stats:
            if len(self.outcome_stats) >= self.counter_limit:
                key = _OUTCOME_OVERFLOW_KEY
            else:
                raise ValueError("live search outcome has no admitted attempt")
        stats = self.outcome_stats.get(key)
        if stats is None or stats.completions >= stats.attempts:
            raise ValueError("live search outcome has no outstanding attempt")
        values = (coverage_gain, steps, solver_queries)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("live search outcome counters must be integers")
        if coverage_gain < 0 or steps < 0 or solver_queries < 0:
            raise ValueError("live search outcome counters must be non-negative")
        stats.completions = self._increment(
            stats.completions, 1, "live search completion count")
        stats.coverage_gain = self._increment(
            stats.coverage_gain, coverage_gain, "live search coverage gain")
        stats.steps = self._increment(stats.steps, steps, "live search step count")
        stats.solver_queries = self._increment(
            stats.solver_queries, solver_queries, "live search solver count")
        stats.failures = self._increment(
            stats.failures, int(bool(failed)), "live search failure count")

    def observe_cgs_instructions(self, count: int) -> None:
        if "cgs" not in self.strategies:
            raise ValueError("live CGS observations require the CGS strategy")
        self.cgs_instruction_count = self._increment(
            self.cgs_instruction_count,
            count,
            "live CGS instruction count",
        )

    def observe_cgs_branch(self, site: int, choice: bool) -> None:
        if "cgs" not in self.strategies:
            raise ValueError("live CGS observations require the CGS strategy")
        site = _bounded_integer(
            site, "live CGS branch site", minimum=1, maximum=_MASK_U64,
        )
        if not isinstance(choice, bool):
            raise ValueError("live CGS branch choice must be boolean")
        self.cgs_observations = self._increment(
            self.cgs_observations, 1, "live CGS observation count",
        )
        previous = self.cgs_branch_outcomes.get(site, 0)
        if not previous and len(self.cgs_branch_outcomes) >= self.cgs_max_branches:
            self.cgs_dropped_branches = self._increment(
                self.cgs_dropped_branches, 1, "live CGS dropped-branch count",
            )
            return
        updated = previous | (2 if choice else 1)
        if updated == previous:
            return
        self.cgs_branch_outcomes[site] = updated
        if previous == 0 and updated in {1, 2}:
            self.cgs_partial_order.append(site)
        elif updated == 3:
            try:
                self.cgs_partial_order.remove(site)
            except ValueError as error:
                raise ValueError(
                    "live CGS partial order is inconsistent"
                ) from error

    def active_cgs_targets(self) -> tuple[tuple[int, bool], ...]:
        if not self.cgs_partial_order:
            return ()
        count = min(self.cgs_target_limit, len(self.cgs_partial_order))
        epoch = self.cgs_instruction_count // self.cgs_rotation_instructions
        start = (epoch * self.cgs_target_limit) % len(self.cgs_partial_order)
        sites = [
            self.cgs_partial_order[(start + index) % len(self.cgs_partial_order)]
            for index in range(count)
        ]
        return tuple(
            (site, self.cgs_branch_outcomes[site] == 1)
            for site in sites
        )

    @classmethod
    def from_environment(cls) -> "LiveStateSearchPolicy":
        strategies = parse_live_search_strategies(
            os.environ.get("SYMCC_LIVE_SEARCH", "bfs"))
        seed = _bounded_integer(
            os.environ.get("SYMCC_LIVE_SEARCH_SEED", "1"),
            "SYMCC_LIVE_SEARCH_SEED", minimum=0, maximum=(1 << 64) - 1)
        length = _bounded_integer(
            os.environ.get("SYMCC_LIVE_SUBPATH_LENGTH", "2"),
            "SYMCC_LIVE_SUBPATH_LENGTH", minimum=1, maximum=8)
        max_covers = _bounded_integer(
            os.environ.get("SYMCC_LIVE_MPC_COVERS", "8"),
            "SYMCC_LIVE_MPC_COVERS", minimum=1, maximum=256,
        )
        max_nodes = _bounded_integer(
            os.environ.get("SYMCC_LIVE_MPC_FUNCTION_NODES", "4096"),
            "SYMCC_LIVE_MPC_FUNCTION_NODES", minimum=16, maximum=262_144,
        )
        cbc_threshold = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CBC_STATE_THRESHOLD", "5"),
            "SYMCC_LIVE_CBC_STATE_THRESHOLD", minimum=1, maximum=100_000,
        )
        cbc_max_nodes = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CBC_FUNCTION_NODES", "4096"),
            "SYMCC_LIVE_CBC_FUNCTION_NODES", minimum=16, maximum=262_144,
        )
        cbc_max_branches = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CBC_BRANCHES", "4096"),
            "SYMCC_LIVE_CBC_BRANCHES", minimum=2, maximum=65_536,
        )
        cgs_targets = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CGS_TARGETS", "10"),
            "SYMCC_LIVE_CGS_TARGETS", minimum=1, maximum=1024,
        )
        cgs_rotation = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CGS_ROTATION_INSTRUCTIONS", "1000000"),
            "SYMCC_LIVE_CGS_ROTATION_INSTRUCTIONS",
            minimum=1,
            maximum=(1 << 31) - 1,
        )
        cgs_max_nodes = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CGS_FUNCTION_NODES", "4096"),
            "SYMCC_LIVE_CGS_FUNCTION_NODES", minimum=16, maximum=262_144,
        )
        cgs_max_branches = _bounded_integer(
            os.environ.get("SYMCC_LIVE_CGS_BRANCHES", "4096"),
            "SYMCC_LIVE_CGS_BRANCHES", minimum=1, maximum=65_536,
        )
        return cls(
            strategies,
            seed=seed,
            subpath_length=length,
            path_cover_max_covers=max_covers,
            path_cover_max_function_nodes=max_nodes,
            cbc_state_threshold=cbc_threshold,
            cbc_max_function_nodes=cbc_max_nodes,
            cbc_max_branches=cbc_max_branches,
            cgs_target_limit=cgs_targets,
            cgs_rotation_instructions=cgs_rotation,
            cgs_max_function_nodes=cgs_max_nodes,
            cgs_max_branches=cgs_max_branches,
        )

    @staticmethod
    def _trim_counter(counter: Counter[Any], limit: int) -> None:
        if len(counter) <= limit:
            return
        for key, _count in sorted(
                counter.items(), key=lambda item: (item[1], repr(item[0])))[:
                    len(counter) - limit]:
            del counter[key]

    def observe_location(self, recent_locations: Sequence[str]) -> None:
        if not recent_locations:
            return
        location = str(recent_locations[-1])
        if (
            location in self.covered_locations
            or len(self.covered_locations) < self.counter_limit
        ):
            self.covered_locations.add(location)
        self.location_counts[location] += 1
        length = min(self.subpath_length, len(recent_locations))
        subpath = tuple(str(item) for item in recent_locations[-length:])
        self.subpath_counts[subpath] += 1
        self._trim_counter(self.location_counts, self.counter_limit)
        self._trim_counter(self.subpath_counts, self.counter_limit)

    @staticmethod
    def _inverse(value: int | None, *, power: float = 1.0) -> float:
        if value is None:
            return 1e-9
        return 1.0 / math.pow(1.0 + max(0, value), power)

    def weights(
        self,
        features: Sequence[LiveStateSearchFeatures],
        strategy: str,
    ) -> tuple[float, ...]:
        if strategy not in SUPPORTED_LIVE_SEARCH_STRATEGIES:
            raise ValueError(f"unsupported live search strategy {strategy!r}")
        result: list[float] = []
        for feature in features:
            if strategy == "nurs:depth":
                weight = math.pow(2.0, min(30, max(0, feature.path_depth)))
            elif strategy == "nurs:icnt":
                weight = self._inverse(feature.instructions)
            elif strategy == "nurs:qc":
                weight = self._inverse(feature.solver_queries)
            elif strategy == "nurs:md2u":
                weight = self._inverse(feature.distance_to_uncovered, power=2.0)
            elif strategy == "nurs:covnew":
                frequency = self.location_counts.get(feature.location, 0)
                weight = self._inverse(frequency, power=2.0) * (
                    1.0 + self._inverse(feature.distance_to_uncovered))
            elif strategy == "subpath":
                length = min(self.subpath_length, len(feature.recent_locations))
                subpath = feature.recent_locations[-length:]
                weight = self._inverse(self.subpath_counts.get(subpath, 0), power=2.0)
            elif strategy == "target-distance":
                weight = self._inverse(feature.distance_to_target, power=3.0)
            elif strategy == "loop-exit":
                repeats = self.location_counts.get(feature.location, 0)
                weight = (8.0 if feature.exits_cycle else 1.0) \
                    * self._inverse(repeats)
            else:
                weight = 1.0
            result.append(max(1e-12, min(1e30, float(weight))))
        return tuple(result)

    def _multi_objective_score(
        self,
        feature: LiveStateSearchFeatures,
        total_attempts: int,
    ) -> float:
        frequency = self.location_counts.get(feature.location, 0)
        novelty = self._inverse(frequency, power=2.0)
        uncovered = self._inverse(feature.distance_to_uncovered, power=2.0)
        target = (
            self._inverse(feature.distance_to_target, power=2.0)
            if feature.target_branch
            else 0.0
        )
        depth_cost = 1.0 / (1.0 + math.log1p(max(0, feature.path_depth)))
        solver_cost = self._inverse(feature.solver_queries)
        loop_exit = 1.0 if feature.exits_cycle else 0.0

        key = self._bounded_outcome_key(feature)
        stats = self.outcome_stats.get(key, _OutcomeStats())
        completed_cost = stats.steps + 8 * stats.solver_queries
        learned_efficiency = (
            stats.coverage_gain / max(1.0, math.sqrt(completed_cost))
            if stats.completions
            else 0.0
        )
        uncertainty = math.sqrt(
            math.log1p(max(1, total_attempts)) / max(1, stats.attempts)
        )
        failure_penalty = stats.failures / max(1.0, stats.completions)
        return (
            0.26 * novelty
            + 0.20 * uncovered
            + 0.18 * target
            + 0.10 * depth_cost
            + 0.08 * solver_cost
            + 0.08 * loop_exit
            + 0.08 * min(4.0, learned_efficiency)
            + 0.02 * min(4.0, uncertainty)
            - 0.20 * min(1.0, failure_penalty)
        )

    def _weighted_index(self, weights: Sequence[float]) -> int:
        total = math.fsum(weights)
        if not math.isfinite(total) or total <= 0.0:
            return self.random.randrange(len(weights))
        selected = self.random.random() * total
        cumulative = 0.0
        for index, weight in enumerate(weights):
            cumulative += weight
            if selected < cumulative:
                return index
        return len(weights) - 1

    def _random_path_index(
        self, features: Sequence[LiveStateSearchFeatures],
    ) -> int:
        candidates = list(range(len(features)))
        depth = 0
        while len(candidates) > 1:
            groups: dict[int | None, list[int]] = {}
            for index in candidates:
                path = features[index].branch_path
                token = path[depth] if depth < len(path) else None
                groups.setdefault(token, []).append(index)
            if len(groups) == 1:
                depth += 1
                if depth > 256:
                    return candidates[self.random.randrange(len(candidates))]
                continue
            keys = sorted(groups, key=lambda value: (-1 if value is None else value))
            candidates = groups[keys[self.random.randrange(len(keys))]]
            depth += 1
        return candidates[0]

    def select_index(self, features: Sequence[LiveStateSearchFeatures]) -> int:
        if not features:
            raise ValueError("cannot select from an empty live-state frontier")
        strategy = self.strategies[self.selection_round % len(self.strategies)]
        self.selection_round += 1
        self.selection_counts[strategy] += 1
        if strategy in {"bfs", "cbc"}:
            selected = 0
        elif strategy == "dfs":
            selected = len(features) - 1
        elif strategy == "random-state":
            selected = self.random.randrange(len(features))
        elif strategy == "random-path":
            selected = self._random_path_index(features)
        elif strategy == "multi-objective":
            total_attempts = sum(
                value.attempts for value in self.outcome_stats.values()
            )
            selected = min(
                range(len(features)),
                key=lambda index: (
                    -self._multi_objective_score(
                        features[index], total_attempts,
                    ),
                    features[index].identity,
                ),
            )
        elif strategy == "path-cover":
            scores: list[float | None] = []
            for feature in features:
                score = feature.path_cover_score
                if score is not None and (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                    or not 0.0 <= float(score) <= 1.0
                ):
                    raise ValueError("live path-cover score is invalid")
                scores.append(None if score is None else float(score))
            if all(score is None for score in scores):
                selected = 0
            else:
                selected = min(
                    range(len(features)),
                    key=lambda index: (
                        -(scores[index] if scores[index] is not None else -1.0),
                        features[index].identity,
                    ),
                )
        elif strategy == "cgs":
            priorities: list[int] = []
            for feature in features:
                if (
                    isinstance(feature.cgs_priority, bool)
                    or not isinstance(feature.cgs_priority, int)
                    or not 0 <= feature.cgs_priority <= 2
                    or isinstance(feature.cgs_targets, bool)
                    or not isinstance(feature.cgs_targets, int)
                    or not 0 <= feature.cgs_targets <= self.cgs_target_limit
                ):
                    raise ValueError("live CGS feature is invalid")
                priorities.append(feature.cgs_priority)
            highest = max(priorities)
            selected = priorities.index(highest)
        else:
            selected = self._weighted_index(self.weights(features, strategy))
        self._record_attempt(features[selected])
        return selected

    @staticmethod
    def _snapshot_location(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value.encode("utf-8")) > 512
        ):
            raise ValueError("live search snapshot location is invalid")
        return value

    @staticmethod
    def _snapshot_count(value: Any, name: str) -> int:
        return _bounded_integer(
            value, name, minimum=1, maximum=_MAX_U63,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return all search state needed for deterministic crash recovery."""
        return {
            "schema": _SEARCH_SNAPSHOT_SCHEMA,
            "strategies": list(self.strategies),
            "seed": self.seed,
            "subpath_length": self.subpath_length,
            "counter_limit": self.counter_limit,
            "path_cover_max_covers": self.path_cover_max_covers,
            "path_cover_max_function_nodes": self.path_cover_max_function_nodes,
            "cbc_state_threshold": self.cbc_state_threshold,
            "cbc_max_function_nodes": self.cbc_max_function_nodes,
            "cbc_max_branches": self.cbc_max_branches,
            "cgs_target_limit": self.cgs_target_limit,
            "cgs_rotation_instructions": self.cgs_rotation_instructions,
            "cgs_max_function_nodes": self.cgs_max_function_nodes,
            "cgs_max_branches": self.cgs_max_branches,
            "cgs_instruction_count": self.cgs_instruction_count,
            "cgs_branch_outcomes": [
                [site, mask]
                for site, mask in sorted(self.cgs_branch_outcomes.items())
            ],
            "cgs_partial_order": list(self.cgs_partial_order),
            "cgs_observations": self.cgs_observations,
            "cgs_dropped_branches": self.cgs_dropped_branches,
            "random": self.random.snapshot(),
            "covered_locations": sorted(self.covered_locations),
            "location_counts": [
                [location, count]
                for location, count in sorted(self.location_counts.items())
            ],
            "subpath_counts": [
                [list(subpath), count]
                for subpath, count in sorted(self.subpath_counts.items())
            ],
            "selection_counts": [
                [strategy, self.selection_counts.get(strategy, 0)]
                for strategy in self.strategies
            ],
            "selection_round": self.selection_round,
            "outcome_stats": [
                [
                    key,
                    stats.attempts,
                    stats.completions,
                    stats.coverage_gain,
                    stats.steps,
                    stats.solver_queries,
                    stats.failures,
                ]
                for key, stats in sorted(self.outcome_stats.items())
            ],
        }

    @classmethod
    def from_snapshot(cls, raw: Any) -> "LiveStateSearchPolicy":
        """Restore a fail-closed, bounded search-policy checkpoint."""
        expected_v1 = {
            "schema", "strategies", "seed", "subpath_length",
            "counter_limit", "random", "covered_locations",
            "location_counts", "subpath_counts", "selection_counts",
            "selection_round",
        }
        expected_v2 = {*expected_v1, "outcome_stats"}
        expected_v3 = {
            *expected_v2,
            "path_cover_max_covers",
            "path_cover_max_function_nodes",
        }
        expected_v4 = {
            *expected_v3,
            "cbc_state_threshold",
            "cbc_max_function_nodes",
            "cbc_max_branches",
        }
        expected_v5 = {
            *expected_v4,
            "cgs_target_limit",
            "cgs_rotation_instructions",
            "cgs_max_function_nodes",
            "cgs_max_branches",
            "cgs_instruction_count",
            "cgs_branch_outcomes",
            "cgs_partial_order",
            "cgs_observations",
            "cgs_dropped_branches",
        }
        if not isinstance(raw, Mapping) or (
            (set(raw) != expected_v1 or raw.get("schema") != _SEARCH_SNAPSHOT_SCHEMA_V1)
            and (
                set(raw) != expected_v2
                or raw.get("schema") != _SEARCH_SNAPSHOT_SCHEMA_V2
            )
            and (
                set(raw) != expected_v3
                or raw.get("schema") != _SEARCH_SNAPSHOT_SCHEMA_V3
            )
            and (
                set(raw) != expected_v4
                or raw.get("schema") != _SEARCH_SNAPSHOT_SCHEMA_V4
            )
            and (
                set(raw) != expected_v5
                or raw.get("schema") != _SEARCH_SNAPSHOT_SCHEMA
            )
        ):
            raise ValueError("live search snapshot is invalid")
        raw_strategies = raw.get("strategies")
        if not isinstance(raw_strategies, list):
            raise ValueError("live search snapshot strategies are invalid")
        strategies = parse_live_search_strategies(
            ",".join(str(value) for value in raw_strategies)
        )
        if list(strategies) != raw_strategies:
            raise ValueError("live search snapshot strategies are invalid")
        result = cls(
            strategies,
            seed=_bounded_integer(
                raw.get("seed"), "live search snapshot seed",
                minimum=0, maximum=_MASK_U64,
            ),
            subpath_length=_bounded_integer(
                raw.get("subpath_length"),
                "live search snapshot subpath length", minimum=1, maximum=8,
            ),
            counter_limit=_bounded_integer(
                raw.get("counter_limit"), "live search snapshot counter limit",
                minimum=16, maximum=_MAX_COUNTER_ENTRIES,
            ),
            path_cover_max_covers=_bounded_integer(
                raw.get("path_cover_max_covers", 8),
                "live search snapshot path-cover count",
                minimum=1,
                maximum=256,
            ),
            path_cover_max_function_nodes=_bounded_integer(
                raw.get("path_cover_max_function_nodes", 4096),
                "live search snapshot path-cover function nodes",
                minimum=16,
                maximum=262_144,
            ),
            cbc_state_threshold=_bounded_integer(
                raw.get("cbc_state_threshold", 5),
                "live search snapshot CBC state threshold",
                minimum=1,
                maximum=100_000,
            ),
            cbc_max_function_nodes=_bounded_integer(
                raw.get("cbc_max_function_nodes", 4096),
                "live search snapshot CBC function nodes",
                minimum=16,
                maximum=262_144,
            ),
            cbc_max_branches=_bounded_integer(
                raw.get("cbc_max_branches", 4096),
                "live search snapshot CBC branch count",
                minimum=2,
                maximum=65_536,
            ),
            cgs_target_limit=_bounded_integer(
                raw.get("cgs_target_limit", 10),
                "live search snapshot CGS target count",
                minimum=1,
                maximum=1024,
            ),
            cgs_rotation_instructions=_bounded_integer(
                raw.get("cgs_rotation_instructions", 1_000_000),
                "live search snapshot CGS rotation instructions",
                minimum=1,
                maximum=(1 << 31) - 1,
            ),
            cgs_max_function_nodes=_bounded_integer(
                raw.get("cgs_max_function_nodes", 4096),
                "live search snapshot CGS function nodes",
                minimum=16,
                maximum=262_144,
            ),
            cgs_max_branches=_bounded_integer(
                raw.get("cgs_max_branches", 4096),
                "live search snapshot CGS branch count",
                minimum=1,
                maximum=65_536,
            ),
        )
        result.random = _StableRandom.from_snapshot(raw.get("random"))

        covered = raw.get("covered_locations")
        if not isinstance(covered, list) or len(covered) > result.counter_limit:
            raise ValueError("live search covered-location snapshot is invalid")
        parsed_covered = [result._snapshot_location(value) for value in covered]
        if parsed_covered != sorted(set(parsed_covered)):
            raise ValueError("live search covered-location snapshot is invalid")
        result.covered_locations = set(parsed_covered)

        raw_locations = raw.get("location_counts")
        if not isinstance(raw_locations, list) \
                or len(raw_locations) > result.counter_limit:
            raise ValueError("live search location-counter snapshot is invalid")
        locations: Counter[str] = Counter()
        previous_location = ""
        for entry in raw_locations:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("live search location-counter snapshot is invalid")
            location = result._snapshot_location(entry[0])
            if location <= previous_location:
                raise ValueError("live search location-counter snapshot is invalid")
            previous_location = location
            locations[location] = result._snapshot_count(
                entry[1], "live search location count",
            )
        result.location_counts = locations

        raw_subpaths = raw.get("subpath_counts")
        if not isinstance(raw_subpaths, list) \
                or len(raw_subpaths) > result.counter_limit:
            raise ValueError("live search subpath-counter snapshot is invalid")
        subpaths: Counter[tuple[str, ...]] = Counter()
        previous_subpath: tuple[str, ...] = ()
        for entry in raw_subpaths:
            if not isinstance(entry, list) or len(entry) != 2 \
                    or not isinstance(entry[0], list) \
                    or not 1 <= len(entry[0]) <= result.subpath_length:
                raise ValueError("live search subpath-counter snapshot is invalid")
            subpath = tuple(
                result._snapshot_location(value) for value in entry[0]
            )
            if subpath <= previous_subpath:
                raise ValueError("live search subpath-counter snapshot is invalid")
            previous_subpath = subpath
            subpaths[subpath] = result._snapshot_count(
                entry[1], "live search subpath count",
            )
        result.subpath_counts = subpaths

        raw_selections = raw.get("selection_counts")
        if not isinstance(raw_selections, list) \
                or len(raw_selections) != len(strategies):
            raise ValueError("live search selection snapshot is invalid")
        selections: Counter[str] = Counter()
        for index, entry in enumerate(raw_selections):
            if not isinstance(entry, list) or len(entry) != 2 \
                    or entry[0] != strategies[index]:
                raise ValueError("live search selection snapshot is invalid")
            selections[strategies[index]] = _bounded_integer(
                entry[1], "live search selection count",
                minimum=0, maximum=_MAX_U63,
            )
        selection_round = _bounded_integer(
            raw.get("selection_round"), "live search selection round",
            minimum=0, maximum=_MAX_U63,
        )
        if sum(selections.values()) != selection_round:
            raise ValueError("live search selection snapshot is inconsistent")
        result.selection_counts = selections
        result.selection_round = selection_round

        raw_outcomes = raw.get("outcome_stats", [])
        if not isinstance(raw_outcomes, list) \
                or len(raw_outcomes) > result.counter_limit:
            raise ValueError("live search outcome snapshot is invalid")
        outcomes: dict[str, _OutcomeStats] = {}
        previous_key = ""
        for entry in raw_outcomes:
            if not isinstance(entry, list) or len(entry) != 7:
                raise ValueError("live search outcome snapshot is invalid")
            key = result._snapshot_location(entry[0])
            if key <= previous_key:
                raise ValueError("live search outcome snapshot is invalid")
            previous_key = key
            values = [
                _bounded_integer(
                    entry[index], "live search outcome counter",
                    minimum=0, maximum=_MAX_U63,
                )
                for index in range(1, 7)
            ]
            stats = _OutcomeStats(*values)
            if stats.completions > stats.attempts \
                    or stats.failures > stats.completions:
                raise ValueError("live search outcome snapshot is inconsistent")
            outcomes[key] = stats
        result.outcome_stats = outcomes
        result.cgs_instruction_count = _bounded_integer(
            raw.get("cgs_instruction_count", 0),
            "live search snapshot CGS instruction count",
            minimum=0,
            maximum=_MAX_U63,
        )
        raw_cgs_outcomes = raw.get("cgs_branch_outcomes", [])
        if (
            not isinstance(raw_cgs_outcomes, list)
            or len(raw_cgs_outcomes) > result.cgs_max_branches
        ):
            raise ValueError("live search snapshot CGS branch outcomes are invalid")
        cgs_outcomes: dict[int, int] = {}
        previous_site = 0
        for entry in raw_cgs_outcomes:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError(
                    "live search snapshot CGS branch outcomes are invalid"
                )
            site = _bounded_integer(
                entry[0], "live search snapshot CGS branch site",
                minimum=1, maximum=_MASK_U64,
            )
            mask = _bounded_integer(
                entry[1], "live search snapshot CGS branch mask",
                minimum=1, maximum=3,
            )
            if site <= previous_site:
                raise ValueError(
                    "live search snapshot CGS branch outcomes are invalid"
                )
            previous_site = site
            cgs_outcomes[site] = mask
        raw_partial = raw.get("cgs_partial_order", [])
        if (
            not isinstance(raw_partial, list)
            or len(raw_partial) > result.cgs_max_branches
        ):
            raise ValueError("live search snapshot CGS partial order is invalid")
        partial = [
            _bounded_integer(
                site, "live search snapshot CGS partial site",
                minimum=1, maximum=_MASK_U64,
            )
            for site in raw_partial
        ]
        if (
            len(set(partial)) != len(partial)
            or set(partial)
            != {site for site, mask in cgs_outcomes.items() if mask in {1, 2}}
        ):
            raise ValueError("live search snapshot CGS partial order is invalid")
        result.cgs_branch_outcomes = cgs_outcomes
        result.cgs_partial_order = partial
        result.cgs_observations = _bounded_integer(
            raw.get("cgs_observations", 0),
            "live search snapshot CGS observations",
            minimum=0,
            maximum=_MAX_U63,
        )
        result.cgs_dropped_branches = _bounded_integer(
            raw.get("cgs_dropped_branches", 0),
            "live search snapshot CGS dropped branches",
            minimum=0,
            maximum=_MAX_U63,
        )
        minimum_cgs_observations = sum(
            2 if mask == 3 else 1 for mask in cgs_outcomes.values()
        ) + result.cgs_dropped_branches
        if result.cgs_observations < minimum_cgs_observations:
            raise ValueError("live search snapshot CGS observations are inconsistent")
        if "cgs" not in result.strategies and (
            result.cgs_instruction_count
            or result.cgs_branch_outcomes
            or result.cgs_partial_order
            or result.cgs_observations
            or result.cgs_dropped_branches
        ):
            raise ValueError("disabled live CGS snapshot has observations")
        return result

    @staticmethod
    def _configuration(policy: "LiveStateSearchPolicy") -> tuple[Any, ...]:
        return (
            policy.strategies,
            policy.seed,
            policy.subpath_length,
            policy.counter_limit,
            policy.path_cover_max_covers,
            policy.path_cover_max_function_nodes,
            policy.cbc_state_threshold,
            policy.cbc_max_function_nodes,
            policy.cbc_max_branches,
            policy.cgs_target_limit,
            policy.cgs_rotation_instructions,
            policy.cgs_max_function_nodes,
            policy.cgs_max_branches,
        )

    @staticmethod
    def _cgs_state(policy: "LiveStateSearchPolicy") -> tuple[Any, ...]:
        return (
            policy.cgs_instruction_count,
            policy.cgs_branch_outcomes,
            policy.cgs_partial_order,
            policy.cgs_observations,
            policy.cgs_dropped_branches,
        )

    @classmethod
    def validate_selection_transition(
        cls,
        previous_raw: Any,
        updated_raw: Any,
    ) -> None:
        """Prove that an updated snapshot records exactly one selection."""
        previous = cls.from_snapshot(previous_raw)
        updated = cls.from_snapshot(updated_raw)
        if cls._configuration(previous) != cls._configuration(updated):
            raise ValueError("live search selection changes configuration")
        strategy = previous.strategies[
            previous.selection_round % len(previous.strategies)
        ]
        if updated.selection_round != previous.selection_round + 1:
            raise ValueError("live search selection round is not consecutive")
        for candidate in previous.strategies:
            expected = previous.selection_counts.get(candidate, 0) + int(
                candidate == strategy
            )
            if updated.selection_counts.get(candidate, 0) != expected:
                raise ValueError("live search selection counts are inconsistent")
        if (
            updated.covered_locations != previous.covered_locations
            or updated.location_counts != previous.location_counts
            or updated.subpath_counts != previous.subpath_counts
            or cls._cgs_state(updated) != cls._cgs_state(previous)
        ):
            raise ValueError("live search selection changes observations")

        changed_attempts = 0
        for key in set(previous.outcome_stats) | set(updated.outcome_stats):
            before = previous.outcome_stats.get(key, _OutcomeStats())
            after = updated.outcome_stats.get(key, _OutcomeStats())
            if (
                after.completions != before.completions
                or after.coverage_gain != before.coverage_gain
                or after.steps != before.steps
                or after.solver_queries != before.solver_queries
                or after.failures != before.failures
                or after.attempts < before.attempts
                or after.attempts > before.attempts + 1
            ):
                raise ValueError("live search selection outcome stats are invalid")
            changed_attempts += after.attempts - before.attempts
        if changed_attempts != 1:
            raise ValueError("live search selection must admit one outcome attempt")

        draw_delta = updated.random.draws - previous.random.draws
        if strategy in {
            "bfs", "dfs", "multi-objective", "path-cover", "cbc", "cgs",
        }:
            valid_draws = draw_delta == 0
        elif strategy == "random-path":
            valid_draws = 0 <= draw_delta <= 257
        else:
            valid_draws = draw_delta == 1
        if not valid_draws:
            raise ValueError("live search selection random draws are invalid")
        expected_random = _StableRandom.from_snapshot(previous.random.snapshot())
        for _draw in range(draw_delta):
            expected_random._next()
        if expected_random.snapshot() != updated.random.snapshot():
            raise ValueError("live search selection random stream is invalid")

    @classmethod
    def validate_observation_transition(
        cls,
        previous_raw: Any,
        updated_raw: Any,
    ) -> None:
        """Prove that completion merged observations, not search decisions."""
        previous = cls.from_snapshot(previous_raw)
        updated = cls.from_snapshot(updated_raw)
        if cls._configuration(previous) != cls._configuration(updated):
            raise ValueError("live search observation changes configuration")
        if (
            updated.selection_round != previous.selection_round
            or updated.selection_counts != previous.selection_counts
            or updated.random.snapshot() != previous.random.snapshot()
        ):
            raise ValueError("live search observation changes selection state")
        if not previous.covered_locations <= updated.covered_locations:
            raise ValueError("live search observation removes coverage")
        if len(updated.covered_locations) > updated.counter_limit:
            raise ValueError("live search observation exceeds coverage budget")

        def monotone_or_trimmed(
            old: Counter[Any],
            new: Counter[Any],
        ) -> bool:
            for key in set(old).intersection(new):
                if new[key] < old[key]:
                    return False
            removed = set(old) - set(new)
            return not removed or (
                len(old) == previous.counter_limit
                and len(new) == updated.counter_limit
            )

        if not monotone_or_trimmed(
            previous.location_counts, updated.location_counts,
        ) or not monotone_or_trimmed(
            previous.subpath_counts, updated.subpath_counts,
        ):
            raise ValueError("live search observation counters regress")

        if (
            updated.cgs_instruction_count < previous.cgs_instruction_count
            or updated.cgs_observations < previous.cgs_observations
            or updated.cgs_dropped_branches < previous.cgs_dropped_branches
        ):
            raise ValueError("live search CGS observation counters regress")
        new_outcome_bits = 0
        for site, before_mask in previous.cgs_branch_outcomes.items():
            after_mask = updated.cgs_branch_outcomes.get(site)
            if after_mask is None or after_mask | before_mask != after_mask:
                raise ValueError("live search CGS branch outcomes regress")
            new_outcome_bits += (after_mask ^ before_mask).bit_count()
        for site, after_mask in updated.cgs_branch_outcomes.items():
            if site not in previous.cgs_branch_outcomes:
                new_outcome_bits += after_mask.bit_count()
        observation_delta = (
            updated.cgs_observations - previous.cgs_observations
        )
        dropped_delta = (
            updated.cgs_dropped_branches - previous.cgs_dropped_branches
        )
        if observation_delta < new_outcome_bits + dropped_delta:
            raise ValueError("live search CGS observation delta is inconsistent")
        old_remaining = [
            site
            for site in previous.cgs_partial_order
            if updated.cgs_branch_outcomes.get(site) in {1, 2}
        ]
        if updated.cgs_partial_order[:len(old_remaining)] != old_remaining:
            raise ValueError("live search CGS partial order is invalid")
        new_partial = updated.cgs_partial_order[len(old_remaining):]
        if any(site in previous.cgs_branch_outcomes for site in new_partial):
            raise ValueError("live search CGS partial order is invalid")

        completion_delta = 0
        for key in set(previous.outcome_stats) | set(updated.outcome_stats):
            before = previous.outcome_stats.get(key, _OutcomeStats())
            after = updated.outcome_stats.get(key, _OutcomeStats())
            delta = after.completions - before.completions
            if after.attempts != before.attempts or delta not in {0, 1}:
                raise ValueError("live search observation outcome stats are invalid")
            metric_deltas = (
                after.coverage_gain - before.coverage_gain,
                after.steps - before.steps,
                after.solver_queries - before.solver_queries,
                after.failures - before.failures,
            )
            if any(value < 0 for value in metric_deltas) \
                    or metric_deltas[3] not in {0, 1} \
                    or (delta == 0 and any(metric_deltas)):
                raise ValueError("live search observation outcome stats are invalid")
            completion_delta += delta
        if completion_delta > 1:
            raise ValueError("live search observation completes multiple attempts")

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": "symcc-live-state-search-v1",
            "strategies": list(self.strategies),
            "seed": self.seed,
            "subpath_length": self.subpath_length,
            "path_cover_max_covers": self.path_cover_max_covers,
            "path_cover_max_function_nodes": self.path_cover_max_function_nodes,
            "cbc_state_threshold": self.cbc_state_threshold,
            "cbc_max_function_nodes": self.cbc_max_function_nodes,
            "cbc_max_branches": self.cbc_max_branches,
            "cgs_target_limit": self.cgs_target_limit,
            "cgs_rotation_instructions": self.cgs_rotation_instructions,
            "cgs_max_function_nodes": self.cgs_max_function_nodes,
            "cgs_max_branches": self.cgs_max_branches,
            "selection_rounds": self.selection_round,
            "selection_counts": {
                strategy: self.selection_counts.get(strategy, 0)
                for strategy in self.strategies
            },
            "covered_locations": len(self.covered_locations),
            "location_counter_entries": len(self.location_counts),
            "subpath_counter_entries": len(self.subpath_counts),
            "random_draws": self.random.draws,
            "snapshot_schema": _SEARCH_SNAPSHOT_SCHEMA,
            "cgs_instruction_count": self.cgs_instruction_count,
            "cgs_branch_outcomes": len(self.cgs_branch_outcomes),
            "cgs_partial_branches": len(self.cgs_partial_order),
            "cgs_active_targets": len(self.active_cgs_targets()),
            "cgs_observations": self.cgs_observations,
            "cgs_dropped_branches": self.cgs_dropped_branches,
            "outcome_contexts": len(self.outcome_stats),
            "outcome_attempts": sum(
                value.attempts for value in self.outcome_stats.values()),
            "outcome_completions": sum(
                value.completions for value in self.outcome_stats.values()),
            "outcome_coverage_gain": sum(
                value.coverage_gain for value in self.outcome_stats.values()),
            "outcome_steps": sum(
                value.steps for value in self.outcome_stats.values()),
            "outcome_solver_queries": sum(
                value.solver_queries for value in self.outcome_stats.values()),
            "outcome_failures": sum(
                value.failures for value in self.outcome_stats.values()),
        }
