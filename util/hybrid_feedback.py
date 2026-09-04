"""Adaptive feedback and scheduling primitives for hybrid concolic fuzzing.

The module has no MPI or AFL dependency.  It keeps policy decisions testable and
allows the orchestration layer to treat concolic engines as telemetry-producing
workers rather than embedding another heuristic directly in its event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import heapq
from itertools import islice
import json
import math
import os
import stat
import time
from typing import Any

from structural_tasks import (
    DynamicStructuralTaskAllocator,
    ProgramTaskGraph,
)
from expressive_coverage import ExpressiveCoverageTree
from path_cover import MinimumPathCoverPlanner


_MAX_ADAPTIVE_STATE_ENTRIES = 1_000_000
_DEFAULT_GUIDANCE_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_TELEMETRY_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_ADAPTIVE_STATE_MAX_BYTES = 256 * 1024 * 1024
_MAX_ADAPTIVE_STATE_MAX_BYTES = 1024 * 1024 * 1024


def _read_bounded_regular_utf8(path: str, maximum: int) -> str | None:
    """Read one regular UTF-8 artifact without crossing its byte budget."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NONBLOCK", 0
    )
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            chunks = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    if len(encoded) > maximum:
        return None
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _bounded_mapping_set(
    mapping: dict[Any, Any],
    key: Any,
    value: Any,
    maximum: int,
) -> None:
    """Refresh one insertion-ordered entry and evict the oldest if needed."""
    if key in mapping:
        mapping.pop(key)
    elif len(mapping) >= maximum:
        mapping.pop(next(iter(mapping)))
    mapping[key] = value


def _trim_set(values: set[Any], maximum: int) -> None:
    while len(values) > maximum:
        values.pop()


def _sequence_tail(value: Any, maximum: int) -> tuple[Any, ...] | list[Any]:
    if not isinstance(value, (list, tuple)):
        return ()
    return value[-maximum:]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, parsed) if math.isfinite(parsed) else 0.0


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _parse_int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item, 0)
        except ValueError:
            continue
        if value > 0:
            out.add(value)
    return out


def load_directed_distance_map(
    path: str | None,
    *,
    max_entries: int = 1_000_000,
    max_bytes: int = _DEFAULT_GUIDANCE_MAX_BYTES,
) -> dict[int, float]:
    if not path:
        return {}
    max_entries = max(
        1, min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_entries))
    )
    max_bytes = max(
        1, min(_MAX_ADAPTIVE_STATE_MAX_BYTES, _nonnegative_int(max_bytes))
    )
    text = _read_bounded_regular_utf8(path, max_bytes)
    if text is None:
        return {}
    distances: dict[int, float] = {}

    def add(site_value: Any, distance_value: Any) -> None:
        try:
            site = int(str(site_value), 0)
            distance = float(distance_value)
        except (TypeError, ValueError, OverflowError):
            return
        if site > 0 and math.isfinite(distance) and distance >= 0.0:
            if site not in distances and len(distances) >= max_entries:
                return
            distances[site] = min(distance, distances.get(site, distance))

    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if isinstance(raw, dict):
        items = raw.get("distances", raw)
        if isinstance(items, dict):
            for site, distance in items.items():
                add(site, distance)
            return distances
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add(item.get("site", item.get("id")), item.get("distance"))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    add(item[0], item[1])
            return distances
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                add(item.get("site", item.get("id")), item.get("distance"))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                add(item[0], item[1])
        return distances

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(":", " ").replace(",", " ").split()
        if len(parts) >= 2:
            add(parts[0], parts[1])
    return distances


def load_static_dependency_map(
    path: str | None,
    *,
    max_sites: int = 65536,
    max_intervals: int = 256,
    max_bytes: int = _DEFAULT_GUIDANCE_MAX_BYTES,
) -> dict[int, tuple[tuple[int, int], ...]]:
    """Load compiler-emitted branch-to-input byte intervals."""
    if not path:
        return {}
    max_sites = max(
        1, min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_sites))
    )
    max_intervals = max(1, min(65536, _nonnegative_int(max_intervals)))
    max_bytes = max(
        1, min(_MAX_ADAPTIVE_STATE_MAX_BYTES, _nonnegative_int(max_bytes))
    )
    text = _read_bounded_regular_utf8(path, max_bytes)
    if text is None:
        return {}
    result: dict[int, list[tuple[int, int]]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            site = int(fields[0], 0)
            lower = int(fields[1], 0)
            upper = int(fields[2], 0)
        except ValueError:
            continue
        if site <= 0 or lower < 0 or upper < lower:
            continue
        if site not in result:
            if len(result) >= max_sites:
                continue
            result[site] = []
        interval = (lower, upper)
        if (interval not in result[site]
                and len(result[site]) < max_intervals):
            result[site].append(interval)
    return {
        site: tuple(sorted(intervals))
        for site, intervals in result.items()
    }


@dataclass(frozen=True)
class SolverTelemetry:
    """One concolic execution's engine-independent solver summary."""

    schema: int = 1
    engine: str = "symcc"
    solver_algorithm: str = ""
    capabilities: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    return_code: int = 0
    killed: bool = False
    input_bytes: int = 0
    symbolic_branches: int = 0
    interesting_branches: int = 0
    skipped_branches: int = 0
    directed_pruned_branches: int = 0
    unique_sites: int = 0
    path_hash: int = 0
    solver_queries: int = 0
    solver_sat: int = 0
    solver_unsat: int = 0
    solver_unknown: int = 0
    solver_time_us: int = 0
    fast_solves: int = 0
    z3_solves: int = 0
    z3_timeouts: int = 0
    backsolver_targets: int = 0
    backsolver_attempts: int = 0
    backsolver_sat: int = 0
    backsolver_constraints_kept: int = 0
    backsolver_constraints_dropped: int = 0
    backsolver_direct_attempts: int = 0
    backsolver_direct_sat: int = 0
    backsolver_validations: int = 0
    backsolver_validation_failures: int = 0
    backsolver_z3_fallbacks: int = 0
    poly_cache_hits: int = 0
    poly_cache_entries: int = 0
    poly_samples: int = 0
    poly_template_constraints: int = 0
    poly_dense_walks: int = 0
    poly_john_steps: int = 0
    poly_dense_fallbacks: int = 0
    prefix_context_hits: int = 0
    prefix_context_entries: int = 0
    unsat_core_hits: int = 0
    unsat_core_entries: int = 0
    unsat_core_clauses: int = 0
    unsat_core_minimized: int = 0
    unsat_core_unification_hits: int = 0
    linear_subsumption_prunes: int = 0
    generated: int = 0
    relevant_input_bytes: int = 0
    dependency_bytes_sum: int = 0
    max_dependency_bytes: int = 0
    query_exports: int = 0
    query_export_failures: int = 0
    query_deferred: int = 0
    query_ir_nodes: int = 0
    query_ir_input_bytes: int = 0
    query_ir_max_bits: int = 0
    query_ir_comparison_ops: int = 0
    query_ir_nonlinear_ops: int = 0
    query_ir_bitwise_ops: int = 0
    query_ir_structural_ops: int = 0
    elapsed_us: int = 0
    target_branch: int = 0
    target_reached: bool = False
    target_status: str = ""
    s2f_action_branches: int = 0
    s2f_action_reached: int = 0
    s2f_solve_actions: int = 0
    s2f_sample_actions: int = 0
    s2f_skip_actions: int = 0
    open_branches: tuple[int, ...] = ()
    branch_trace: tuple[tuple[int, int, int, int, int, int], ...] = ()
    data_comparisons: int = 0
    data_coverage_map_updates: int = 0
    empirical_domain_profiles_loaded: int = 0
    empirical_domain_context_skips: int = 0
    empirical_domain_parse_failures: int = 0
    empirical_domain_attempts: int = 0
    empirical_domain_prefilter_rejects: int = 0
    empirical_domain_solver_queries: int = 0
    empirical_domain_solver_time_us: int = 0
    empirical_domain_sat: int = 0
    empirical_domain_validated: int = 0
    empirical_domain_validation_failures: int = 0
    empirical_domain_unsat_fallbacks: int = 0
    empirical_domain_unknown_fallbacks: int = 0
    data_features: tuple[tuple[int, int, int], ...] = ()
    empirical_value_profiles: tuple[
        tuple[int, int, int, int, tuple[tuple[int, int], ...]], ...
    ] = ()
    empirical_domain_feedback: tuple[
        tuple[
            int, int, tuple[int, ...], int, int, int, int, int, int, int, int,
            int
        ], ...
    ] = ()
    empirical_value_profile_context: str = ""
    static_data_regions: int = 0
    static_data_objects: int = 0
    static_data_segments: int = 0
    static_data_accesses: int = 0
    data_switches: int = 0
    data_switch_probes: int = 0
    string_records_loaded: int = 0
    string_solver_queries: int = 0
    string_solver_verified: int = 0
    string_dual_view_verified: int = 0
    static_data_features: tuple[tuple[int, int, int, int, int], ...] = ()
    comparison_taints: tuple[tuple[int, int, int, int, int, int, int], ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "SolverTelemetry":
        fields = cls.__dataclass_fields__
        non_integer_fields = {
            "engine", "solver_algorithm", "capabilities", "missing_fields",
            "return_code", "target_reached", "target_status", "killed",
            "open_branches",
            "branch_trace", "data_features", "static_data_features",
            "comparison_taints", "empirical_value_profiles",
            "empirical_domain_feedback",
            "empirical_value_profile_context",
        }
        values = {
            name: _nonnegative_int(raw.get(name, 0))
            for name in fields
            if name not in non_integer_fields
        }
        values["schema"] = _nonnegative_int(raw.get("schema", 1)) or 1
        engine = str(raw.get("engine", "symcc")).strip().lower()
        values["engine"] = (
            engine if engine in {"symcc", "symsan"} else "unknown")
        values["solver_algorithm"] = str(
            raw.get("solver_algorithm", ""))[:64]
        values["return_code"] = _int_value(raw.get("return_code", 0))
        capabilities_are_explicit = "capabilities" in raw
        raw_capabilities = raw.get("capabilities", ())
        if not isinstance(raw_capabilities, (list, tuple, set)):
            raw_capabilities = ()
        capabilities = {
            str(value)[:64] for value in raw_capabilities if str(value)
        }
        capabilities.add("execution")
        # A partial observation has no explicit capability contract, so infer
        # capabilities from the fields it actually reports.  A materialized
        # dataclass mapping contains every field, including zero-valued ones;
        # re-inferring from presence there would falsely advertise every
        # optional capability after an MPI/JSON round trip.
        if not capabilities_are_explicit:
            if any(name in raw for name in (
                    "solver_queries", "solver_sat", "solver_unsat",
                    "solver_unknown", "solver_time_us")):
                capabilities.add("solver")
            if "branch_trace" in raw:
                capabilities.add("branch_trace")
            if "comparison_taints" in raw:
                capabilities.add("comparison_taint")
            if "data_features" in raw:
                capabilities.add("data_coverage")
            if ("empirical_value_profile_context" in raw
                    or "empirical_value_profiles" in raw):
                capabilities.add("empirical_value_profile")
            if any(name in raw for name in (
                    "empirical_domain_profiles_loaded",
                    "empirical_domain_attempts",
                    "empirical_domain_validated",
                    "empirical_domain_feedback")):
                capabilities.add("empirical_domain_solver")
            if "static_data_features" in raw:
                capabilities.add("static_data_coverage")
            if any(name in raw for name in (
                    "backsolver_targets", "backsolver_attempts",
                    "backsolver_sat")):
                capabilities.add("backsolver")
            if any(name in raw for name in (
                    "query_ir_nodes", "query_ir_input_bytes",
                    "query_ir_max_bits")):
                capabilities.add("query_ir_structure")
        values["capabilities"] = tuple(sorted(capabilities))
        raw_missing = raw.get("missing_fields", ())
        if not isinstance(raw_missing, (list, tuple, set)):
            raw_missing = ()
        values["missing_fields"] = tuple(sorted({
            str(value)[:64] for value in raw_missing if str(value)
        }))
        values["target_reached"] = bool(raw.get("target_reached", False))
        target_status = str(raw.get("target_status", "")).strip().lower()
        values["target_status"] = (
            target_status
            if target_status in {"none", "sat", "unsat", "unknown"}
            else ""
        )
        values["killed"] = bool(raw.get("killed", False))
        branches = raw.get("open_branches", ())
        if not isinstance(branches, (list, tuple)):
            branches = ()
        values["open_branches"] = tuple(dict.fromkeys(
            branch for branch in (_nonnegative_int(value) for value in branches)
            if branch != 0
        ))[:256]
        trace = raw.get("branch_trace", ())
        parsed_trace = []
        if isinstance(trace, (list, tuple)):
            for entry in trace[:512]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 6:
                    continue
                parsed = tuple(_nonnegative_int(value) for value in entry)
                if parsed[1] and parsed[2]:
                    parsed_trace.append(parsed)
        values["branch_trace"] = tuple(parsed_trace)
        features = raw.get("data_features", ())
        parsed_features = []
        if isinstance(features, (list, tuple)):
            for entry in features[:512]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                    continue
                feature_id, matched, width = (
                    _nonnegative_int(value) for value in entry)
                width = min(64, width)
                matched = min(width, matched)
                if feature_id and width:
                    parsed_features.append((feature_id, matched, width))
        values["data_features"] = tuple(parsed_features)
        profiles = raw.get("empirical_value_profiles", ())
        parsed_profiles = []
        if isinstance(profiles, (list, tuple)):
            for entry in profiles[:512]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 5:
                    continue
                site = _nonnegative_int(entry[0])
                bits = min(64, _nonnegative_int(entry[1]))
                observations = _nonnegative_int(entry[2])
                saturated = _nonnegative_int(entry[3])
                raw_values = entry[4]
                if (not site or not bits or not observations
                        or saturated not in {0, 1}
                        or not isinstance(raw_values, (list, tuple))
                        or len(raw_values) > 8):
                    continue
                counts = []
                seen_values = set()
                maximum = (1 << bits) - 1 if bits < 64 else (1 << 64) - 1
                invalid_values = False
                for pair in raw_values:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        invalid_values = True
                        break
                    value = _nonnegative_int(pair[0])
                    count = _nonnegative_int(pair[1])
                    if value > maximum or value in seen_values or not count:
                        invalid_values = True
                        break
                    seen_values.add(value)
                    counts.append((value, count))
                recorded = sum(count for _, count in counts)
                if (not invalid_values and counts
                        and (not saturated and recorded == observations
                             or saturated and recorded < observations)):
                    parsed_profiles.append((
                        site, bits, observations, saturated, tuple(counts)))
        values["empirical_value_profiles"] = tuple(parsed_profiles)
        domain_feedback = raw.get("empirical_domain_feedback", ())
        parsed_domain_feedback = []
        seen_domain_feedback = set()
        if isinstance(domain_feedback, (list, tuple)):
            for entry in domain_feedback[:512]:
                if (not isinstance(entry, (list, tuple))
                        or len(entry) not in {11, 12}):
                    continue
                site = _nonnegative_int(entry[0])
                bits = min(64, _nonnegative_int(entry[1]))
                raw_values = entry[2]
                if (not site or not bits
                        or not isinstance(raw_values, (list, tuple))
                        or not 0 < len(raw_values) <= 8):
                    continue
                maximum = (1 << bits) - 1
                values_list = tuple(sorted({
                    _nonnegative_int(value) for value in raw_values
                }))
                if (len(values_list) != len(raw_values)
                        or any(value > maximum for value in values_list)):
                    continue
                counts = tuple(
                    _nonnegative_int(value) for value in entry[3:11])
                (attempts, prefilter_rejects, solver_queries, sat, validated,
                 validation_failures, solver_unsat, unknown) = counts
                if (not attempts
                        or prefilter_rejects + solver_queries != attempts
                        or sat + solver_unsat + unknown != solver_queries
                        or validated + validation_failures != sat):
                    continue
                key = site, bits, values_list
                if key in seen_domain_feedback:
                    continue
                seen_domain_feedback.add(key)
                solver_time_us = (
                    _nonnegative_int(entry[11]) if len(entry) == 12 else 0)
                parsed_domain_feedback.append((
                    site, bits, values_list, *counts, solver_time_us,
                ))
        values["empirical_domain_feedback"] = tuple(parsed_domain_feedback)
        profile_context = str(
            raw.get("empirical_value_profile_context", ""))
        values["empirical_value_profile_context"] = (
            profile_context
            if len(profile_context) == 64
            and all(byte in "0123456789abcdef" for byte in profile_context)
            else ""
        )
        static_features = raw.get("static_data_features", ())
        parsed_static_features = []
        if isinstance(static_features, (list, tuple)):
            for entry in static_features[:2048]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 5:
                    continue
                object_id, offset, matched, width, kind = (
                    _nonnegative_int(value) for value in entry)
                width = min((1 << 32) - 1, width)
                matched = min(width, matched)
                kind = min(2, kind)
                if object_id and width and matched:
                    parsed_static_features.append(
                        (object_id, offset, matched, width, kind))
        values["static_data_features"] = tuple(parsed_static_features)
        taints = raw.get("comparison_taints", ())
        parsed_taints = []
        if isinstance(taints, (list, tuple)):
            for entry in taints[:512]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 7:
                    continue
                site, branch, count, lo, hi, taken, interesting = (
                    _nonnegative_int(value) for value in entry)
                if site and branch and count and lo <= hi:
                    parsed_taints.append((
                        site, branch, min(4096, count), lo, hi,
                        int(bool(taken)), int(bool(interesting))))
        values["comparison_taints"] = tuple(parsed_taints)
        return cls(**values)

    @classmethod
    def from_observation(
        cls,
        raw: dict[str, Any] | None,
        *,
        engine: str,
        input_bytes: int,
        generated: int,
        elapsed: float,
        return_code: int,
        killed: bool,
        solver_algorithm: str = "",
    ) -> "SolverTelemetry":
        """Normalize full or partial engine reports without inventing metrics."""
        mapping = dict(raw) if isinstance(raw, dict) else {}
        mapping.setdefault("engine", engine)
        mapping.setdefault("input_bytes", max(0, int(input_bytes)))
        mapping.setdefault("generated", max(0, int(generated)))
        mapping.setdefault(
            "elapsed_us", max(0, int(max(0.0, elapsed) * 1_000_000)))
        mapping.setdefault("return_code", int(return_code))
        mapping.setdefault("killed", bool(killed))
        if solver_algorithm:
            mapping.setdefault("solver_algorithm", solver_algorithm)

        required = {
            "input_bytes", "generated", "elapsed_us",
            "solver_queries", "solver_sat", "solver_unsat",
            "solver_unknown", "solver_time_us", "branch_trace",
        }
        raw_missing = mapping.get("missing_fields", ())
        missing = (
            {str(value) for value in raw_missing}
            if isinstance(raw_missing, (list, tuple, set))
            else set()
        )
        missing.update(required - set(mapping))
        mapping["missing_fields"] = sorted(missing)
        return cls.from_mapping(mapping)

    @classmethod
    def load(
        cls,
        path: str,
        max_bytes: int = _DEFAULT_TELEMETRY_MAX_BYTES,
    ) -> "SolverTelemetry | None":
        text = _read_bounded_regular_utf8(
            path,
            max(
                1,
                min(
                    _MAX_ADAPTIVE_STATE_MAX_BYTES,
                    _nonnegative_int(max_bytes),
                ),
            ),
        )
        if text is None:
            return None
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        return cls.from_mapping(raw)

    def has_capability(self, name: str) -> bool:
        return str(name) in self.capabilities

    @property
    def solve_yield(self) -> float:
        attempts = max(1, self.solver_queries + self.fast_solves)
        return min(1.0, self.generated / attempts)

    @property
    def timeout_ratio(self) -> float:
        return min(1.0, self.z3_timeouts / max(1, self.z3_solves))

    @property
    def backsolver_yield(self) -> float:
        return min(
            1.0, self.backsolver_sat / max(1, self.backsolver_attempts))

    @property
    def backsolver_direct_yield(self) -> float:
        return min(
            1.0,
            self.backsolver_direct_sat / max(1, self.backsolver_attempts),
        )

    @property
    def backsolver_validation_failure_ratio(self) -> float:
        return min(
            1.0,
            self.backsolver_validation_failures / max(1, self.backsolver_validations),
        )

    @property
    def difficulty(self) -> float:
        """Bounded estimate of a path's resistance to ordinary mutation/solving."""
        branch_pressure = self.interesting_branches / max(1, self.symbolic_branches)
        dependency_pressure = self.max_dependency_bytes / max(1, self.input_bytes)
        solver_pressure = math.log1p(self.solver_time_us / 1000.0) / 10.0
        pruning_pressure = len(self.open_branches) / max(1, self.symbolic_branches)
        data_pressure = 1.0 - self.data_quality if self.data_features else 0.0
        implicit_flow_pressure = (
            self.backsolver_targets / max(1, self.symbolic_branches))
        return min(
            1.0,
            0.23 * branch_pressure
            + 0.18 * min(1.0, dependency_pressure)
            + 0.18 * min(1.0, solver_pressure)
            + 0.14 * self.timeout_ratio
            + 0.09 * min(1.0, pruning_pressure)
            + 0.08 * data_pressure
            + 0.10 * min(1.0, implicit_flow_pressure),
        )

    @property
    def data_quality(self) -> float:
        if not self.data_features:
            return 0.0
        return sum(matched / width for _, matched, width in self.data_features) / len(
            self.data_features)

    @property
    def comparison_taint_locality(self) -> float:
        if not self.comparison_taints:
            return 0.0
        score = 0.0
        for _site, _branch, count, lo, hi, _taken, interesting in (
                self.comparison_taints):
            span = max(1, hi - lo + 1)
            density = min(1.0, count / span)
            score += 0.75 * density + 0.25 * interesting
        return min(1.0, score / len(self.comparison_taints))


@dataclass(frozen=True)
class CandidateContext:
    path: str
    seed_type: str
    generation: int
    vector: tuple[float, ...]
    prior_score: float


@dataclass
class ReplayRecord:
    path: str
    reward: float
    difficulty: float
    visits: int
    last_seen: float
    last_replay: float = 0.0
    open_branches: tuple[int, ...] = ()
    target_cursor: int = 0
    comparison_locality: float = 0.0


@dataclass(frozen=True)
class ReplayJob:
    path: str
    target_branch: int = 0
    actions: tuple[tuple[int, str], ...] = ()


@dataclass
class CSTGTransition:
    source: int
    target: int
    branch_id: int
    site_id: int
    observations: int = 0
    attempts: int = 0
    arrivals: int = 0
    divergences: int = 0
    reward: float = 0.0
    cost: float = 0.0
    difficulty: float = 0.0
    status: str = "open"
    last_seen: float = 0.0
    last_scheduled: float = 0.0
    seeds: tuple[str, ...] = ()

    @property
    def arrival_probability(self) -> float:
        return (self.arrivals + 1.0) / (self.attempts + 2.0)

    @property
    def divergence_probability(self) -> float:
        return (self.divergences + 1.0) / (self.attempts + 2.0)


class ConcolicStateTransitionGraph:
    """Global Marco-style transition graph with asynchronous target work."""

    ACTIVE = {"open", "diverged", "timeout"}
    TERMINAL = {"sat", "unsat"}

    def __init__(self, max_transitions: int = 8192, action_cap: int = 16) -> None:
        self.max_transitions = max(
            128,
            min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_transitions)),
        )
        self.max_node_visits = min(
            _MAX_ADAPTIVE_STATE_ENTRIES,
            self.max_transitions * 2,
        )
        self.action_cap = max(1, min(64, int(action_cap)))
        self.transitions: dict[int, CSTGTransition] = {}
        self.node_visits: dict[int, int] = {}
        self.scheduled = 0
        self.divergence_updates = 0

    @staticmethod
    def _remember_seed(
        transition: CSTGTransition,
        path: str,
    ) -> None:
        if not path:
            return
        transition.seeds = tuple(dict.fromkeys(
            transition.seeds + (path,)))[-4:]

    def observe(
        self,
        path: str,
        telemetry: SolverTelemetry,
        *,
        reward: float,
        elapsed: float,
        killed: bool,
        now: float,
    ) -> None:
        targeted = telemetry.target_branch
        observed_targets: set[int] = set()
        for parent, actual, alternate, site, _taken, _interesting in (
                telemetry.branch_trace):
            if actual:
                visits = self.node_visits.pop(actual, 0) + 1
                _bounded_mapping_set(
                    self.node_visits,
                    actual,
                    visits,
                    self.max_node_visits,
                )
            if not alternate:
                continue
            observed_targets.add(alternate)
            transition = self.transitions.get(alternate)
            if transition is None:
                transition = CSTGTransition(
                    source=parent,
                    target=alternate,
                    branch_id=alternate,
                    site_id=site,
                )
                self.transitions[alternate] = transition
            else:
                transition.source = parent
                transition.site_id = site
            transition.observations += 1
            transition.last_seen = now
            transition.difficulty = max(
                0.75 * transition.difficulty, telemetry.difficulty)
            self._remember_seed(transition, path)

            if targeted == alternate:
                transition.attempts += 1
                transition.cost = (
                    elapsed if transition.attempts == 1
                    else 0.75 * transition.cost + 0.25 * elapsed)
                transition.reward = _clamp01(
                    0.72 * transition.reward + 0.28 * reward)
                if telemetry.target_reached:
                    transition.arrivals += 1
                    if telemetry.target_status in {"sat", "unsat"}:
                        transition.status = telemetry.target_status
                    elif telemetry.target_status == "unknown":
                        transition.status = "timeout"
                    else:
                        transition.divergences += 1
                        transition.status = "diverged"
                        self.divergence_updates += 1
                elif killed or telemetry.z3_timeouts > 0:
                    transition.divergences += 1
                    transition.status = "timeout"
                    self.divergence_updates += 1
                else:
                    transition.divergences += 1
                    transition.status = "diverged"
                    self.divergence_updates += 1

        if targeted and targeted not in observed_targets:
            transition = self.transitions.get(targeted)
            if transition is None and telemetry.target_reached:
                transition = CSTGTransition(
                    source=0,
                    target=targeted,
                    branch_id=targeted,
                    site_id=0,
                )
                self.transitions[targeted] = transition
                self._remember_seed(transition, path)
            if transition is not None:
                transition.attempts += 1
                transition.last_seen = now
                transition.cost = (
                    elapsed if transition.attempts == 1
                    else 0.75 * transition.cost + 0.25 * elapsed)
                transition.reward = _clamp01(
                    0.72 * transition.reward + 0.28 * reward)
                if telemetry.target_reached:
                    transition.arrivals += 1
                    if telemetry.target_status in {"sat", "unsat"}:
                        transition.status = telemetry.target_status
                    elif telemetry.target_status == "unknown":
                        transition.status = "timeout"
                    else:
                        transition.status = "diverged"
                        transition.divergences += 1
                        self.divergence_updates += 1
                else:
                    transition.divergences += 1
                    transition.status = "timeout" if killed else "diverged"
                    self.divergence_updates += 1
        self._prune()

    def _prune(self) -> None:
        overflow = len(self.transitions) - self.max_transitions
        if overflow <= 0:
            return
        victims = heapq.nsmallest(
            overflow,
            self.transitions.values(),
            key=lambda transition: (
                transition.status in self.ACTIVE,
                transition.reward,
                transition.last_seen,
                transition.observations,
            ),
        )
        for transition in victims:
            self.transitions.pop(transition.branch_id, None)

    def _score(self, transition: CSTGTransition, now: float,
               cooldown: float) -> float:
        source_visits = self.node_visits.get(transition.source, 0)
        target_visits = self.node_visits.get(transition.target, 0)
        reachability = (
            (source_visits + 1.0)
            / (source_visits + target_visits + 2.0))
        novelty = 1.0 / math.sqrt(1.0 + target_visits)
        cost = transition.cost / max(1, transition.attempts)
        age = min(
            1.0,
            max(0.0, now - transition.last_scheduled)
            / max(1.0, cooldown),
        )
        return (
            0.28 * transition.arrival_probability
            + 0.24 * reachability
            + 0.18 * novelty
            + 0.15 * transition.reward
            + 0.10 * transition.difficulty
            + 0.05 * age
            - 0.18 * transition.divergence_probability
            - 0.08 * min(1.0, math.log1p(max(0.0, cost)) / 4.0)
        )

    def select(
        self,
        limit: int,
        *,
        cooldown: float,
        now: float,
        exclude_targets: set[int] | None = None,
        commit: bool = True,
        proposal_limit: int | None = None,
    ) -> list[ReplayJob]:
        if limit <= 0:
            return []
        candidates: list[tuple[float, CSTGTransition, str]] = []
        blocked_targets = exclude_targets or set()
        for transition in self.transitions.values():
            if transition.status not in self.ACTIVE:
                continue
            if transition.branch_id in blocked_targets:
                continue
            if now - transition.last_scheduled < cooldown:
                continue
            seed = next((
                candidate for candidate in reversed(transition.seeds)
                if os.path.isfile(candidate)
            ), None)
            if seed is None:
                continue
            candidates.append((self._score(
                transition, now, cooldown), transition, seed))
        candidates.sort(key=lambda item: item[0], reverse=True)

        output_limit = max(limit, proposal_limit or limit)
        selected: list[ReplayJob] = []
        selected_seeds: set[str] = set()
        candidates_by_seed: dict[
            str, list[tuple[float, CSTGTransition, str]]
        ] = {}
        for candidate in candidates:
            candidates_by_seed.setdefault(candidate[2], []).append(candidate)
        for _score, primary, seed in candidates:
            if seed in selected_seeds:
                continue
            actions: list[tuple[int, str]] = []
            for _candidate_score, transition, _candidate_seed in (
                candidates_by_seed[seed]
            ):
                if len(actions) >= self.action_cap:
                    break
                action = (
                    "sample"
                    if transition.attempts > 0
                    and transition.difficulty >= 0.55
                    and transition.divergence_probability < 0.70
                    else "solve"
                )
                actions.append((transition.branch_id, action))
            if not actions:
                continue
            selected.append(ReplayJob(
                seed, primary.branch_id, tuple(actions)))
            selected_seeds.add(seed)
            if len(selected) >= output_limit:
                break
        if commit:
            self.commit_jobs(selected, now)
        return selected

    def commit_jobs(self, jobs: list[ReplayJob], now: float) -> None:
        for job in jobs:
            for branch_id, action in job.actions:
                if action == "skip":
                    continue
                transition = self.transitions.get(branch_id)
                if transition is not None:
                    transition.last_scheduled = now
        self.scheduled += len(jobs)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "max_transitions": self.max_transitions,
            "action_cap": self.action_cap,
            "scheduled": self.scheduled,
            "divergence_updates": self.divergence_updates,
            "node_visits": [
                [node, visits]
                for node, visits in list(self.node_visits.items())[-16384:]
            ],
            "transitions": [
                {
                    "source": item.source,
                    "target": item.target,
                    "branch_id": item.branch_id,
                    "site_id": item.site_id,
                    "observations": item.observations,
                    "attempts": item.attempts,
                    "arrivals": item.arrivals,
                    "divergences": item.divergences,
                    "reward": item.reward,
                    "cost": item.cost,
                    "difficulty": item.difficulty,
                    "status": item.status,
                    "last_seen": item.last_seen,
                    "last_scheduled": item.last_scheduled,
                    "seeds": list(item.seeds),
                }
                for item in self.transitions.values()
            ],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.scheduled = _nonnegative_int(raw.get("scheduled"))
        self.divergence_updates = _nonnegative_int(
            raw.get("divergence_updates"))
        node_visits = raw.get("node_visits", ())
        if isinstance(node_visits, list):
            for item in node_visits[-16384:]:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                node = _nonnegative_int(item[0])
                visits = _nonnegative_int(item[1])
                if node and visits:
                    self.node_visits[node] = visits
        transitions = raw.get("transitions", ())
        if not isinstance(transitions, list):
            return
        for item in transitions[-self.max_transitions:]:
            if not isinstance(item, dict):
                continue
            branch = _nonnegative_int(item.get("branch_id"))
            if not branch:
                continue
            status = str(item.get("status", "open"))
            if status == "stale":
                status = "diverged"
            if status not in self.ACTIVE | self.TERMINAL:
                status = "open"
            seeds = item.get("seeds", ())
            if not isinstance(seeds, (list, tuple)):
                seeds = ()
            try:
                self.transitions[branch] = CSTGTransition(
                    source=_nonnegative_int(item.get("source")),
                    target=_nonnegative_int(item.get("target", branch)),
                    branch_id=branch,
                    site_id=_nonnegative_int(item.get("site_id")),
                    observations=_nonnegative_int(item.get("observations")),
                    attempts=_nonnegative_int(item.get("attempts")),
                    arrivals=_nonnegative_int(item.get("arrivals")),
                    divergences=_nonnegative_int(item.get("divergences")),
                    reward=_clamp01(float(item.get("reward", 0.0))),
                    cost=max(0.0, float(item.get("cost", 0.0))),
                    difficulty=_clamp01(float(item.get("difficulty", 0.0))),
                    status=status,
                    last_seen=max(0.0, float(item.get("last_seen", 0.0))),
                    last_scheduled=max(
                        0.0, float(item.get("last_scheduled", 0.0))),
                    seeds=tuple(str(seed) for seed in seeds[-4:]),
                )
            except (TypeError, ValueError, OverflowError):
                continue


@dataclass
class EdgeDependenceBranch:
    branch_id: int
    site_id: int = 0
    traced: int = 0
    scheduled: int = 0
    last_seen: float = 0.0
    last_scheduled: float = 0.0
    last_gain: float = 0.0
    corpus: tuple[str, ...] = ()
    target_branches: tuple[int, ...] = ()
    target_cursor: int = 0
    reward_ema: float = 0.0
    cost_ema: float = 0.0
    terminal_failures: int = 0
    successful_targets: int = 0
    target_distance: float = 0.0


@dataclass
class ConcurrencyReplayRecord:
    path: str
    best_distance: float
    reward: float
    visits: int
    last_seen: float
    last_scheduled: float = 0.0
    open_branches: tuple[int, ...] = ()
    target_cursor: int = 0


@dataclass
class S2FActionState:
    """Per-prefix state for one S2F executor action."""

    attempts: int = 0
    successes: int = 0
    reward_sum: float = 0.0
    cost_sum: float = 0.0
    consecutive_failures: int = 0
    last_status: str = "untried"

    def score(self, total_attempts: int) -> float:
        if self.attempts == 0:
            return 1.0
        mean_reward = self.reward_sum / self.attempts
        mean_cost = self.cost_sum / self.attempts
        exploration = math.sqrt(
            math.log1p(max(1, total_attempts)) / self.attempts)
        failure_penalty = min(0.45, 0.09 * self.consecutive_failures)
        return (mean_reward / math.sqrt(max(0.05, mean_cost))
                + 0.30 * exploration - failure_penalty)


def _new_s2f_actions() -> dict[str, S2FActionState]:
    return {
        "exact": S2FActionState(),
        "tailored": S2FActionState(),
        "sampling": S2FActionState(),
    }


@dataclass
class PrefixNode:
    branch_id: int
    parent_id: int
    site_id: int
    outcome: int
    status: str
    visits: int = 0
    attempts: int = 0
    reward: float = 0.0
    difficulty: float = 0.0
    data_reward: float = 0.0
    backsolver_reward: float = 0.0
    path_cover_reward: float = 0.0
    solver_cost: float = 0.0
    timeout_penalty: float = 0.0
    path_difficulty: float = 0.0
    target_distance: float = 0.0
    target_path_reward: float = 0.0
    target_path_visits: int = 0
    taco_generation_bonus: float = 0.0
    color_feasibility: float = 0.0
    mdp_value: float = 0.0
    mdp_cost: float = 0.0
    mdp_transition_probability: float = 0.0
    mdp_novelty_reward: float = 0.0
    infeasible_streak: int = 0
    last_update: float = 0.0
    last_scheduled: float = 0.0
    seed_paths: tuple[str, ...] = ()
    actions: dict[str, S2FActionState] = field(
        default_factory=_new_s2f_actions)


@dataclass
class ConstraintSummary:
    branch_id: int
    status: str
    observations: int
    solver_time_us: int
    next_retry: float = 0.0


class ConstraintSummaryCache:
    """Cross-execution cache for terminal and expensive prefix outcomes."""

    TERMINAL = {"sat", "unsat"}

    def __init__(self, max_entries: int = 8192) -> None:
        self.max_entries = max(
            128,
            min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_entries)),
        )
        self.entries: dict[int, ConstraintSummary] = {}

    def observe(self, telemetry: SolverTelemetry, now: float) -> None:
        branch_id = telemetry.target_branch
        if not branch_id:
            return
        if not telemetry.target_reached:
            status = "diverged"
        elif telemetry.target_status in self.TERMINAL:
            status = telemetry.target_status
        elif telemetry.target_status == "unknown":
            status = "timeout"
        else:
            status = "diverged"
        previous = self.entries.get(branch_id)
        observations = previous.observations + 1 if previous else 1
        # Transient failures use bounded exponential backoff; exact SAT/UNSAT
        # summaries are the only outcomes that permanently retire a target.
        retry_base = 30.0 if status == "timeout" else 5.0
        retry = (
            now + min(3600.0, retry_base * (2 ** min(7, observations - 1)))
            if status in {"timeout", "diverged"} else 0.0
        )
        _bounded_mapping_set(
            self.entries,
            branch_id,
            ConstraintSummary(
                branch_id,
                status,
                observations,
                telemetry.solver_time_us,
                retry,
            ),
            self.max_entries,
        )

    def allows(self, branch_id: int, now: float) -> bool:
        summary = self.entries.get(branch_id)
        if summary is None:
            return True
        if summary.status in self.TERMINAL:
            return False
        return now >= summary.next_retry

    def to_mapping(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                **vars(entry),
                "clock": "relative",
                "retry_after": max(0.0, entry.next_retry - now),
            }
            for entry in self.entries.values()
        ]

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, list):
            return
        now = time.monotonic()
        for item in raw[-self.max_entries:]:
            if not isinstance(item, dict):
                continue
            branch_id = _nonnegative_int(item.get("branch_id"))
            status = str(item.get("status", ""))
            if status == "stale":
                status = "diverged"
            if not branch_id or status not in {
                    "sat", "unsat", "timeout", "diverged"}:
                continue
            if item.get("clock") == "relative":
                retry_after = _nonnegative_float(item.get("retry_after"))
                next_retry = now + min(3600.0, retry_after)
            else:
                try:
                    next_retry = max(
                        0.0, float(item.get("next_retry", 0.0)))
                except (TypeError, ValueError, OverflowError):
                    next_retry = 0.0
                if not math.isfinite(next_retry) or next_retry > now + 3600.0:
                    next_retry = now
            _bounded_mapping_set(
                self.entries,
                branch_id,
                ConstraintSummary(
                    branch_id=branch_id,
                    status=status,
                    observations=max(
                        1, _nonnegative_int(item.get("observations"))),
                    solver_time_us=_nonnegative_int(
                        item.get("solver_time_us")),
                    next_retry=next_retry,
                ),
                self.max_entries,
            )


class EdgeDependenceCoverage:
    """SYMCTS-style concolic-local edge-dependence metric and scheduler."""

    def __init__(
        self,
        max_branches: int = 4096,
        max_cells: int = 262144,
        trace_cap: int = 128,
        corpus_per_branch: int = 4,
        directed_distances: dict[int, float] | None = None,
        target_lease_seconds: float = 120.0,
    ) -> None:
        self.max_branches = max(128, max_branches)
        self.max_cells = max(1024, max_cells)
        self.trace_cap = max(16, trace_cap)
        self.corpus_per_branch = max(1, corpus_per_branch)
        self.directed_distances: dict[int, float] = {}
        for site, distance in (directed_distances or {}).items():
            try:
                site_id = int(site)
                value = float(distance)
            except (TypeError, ValueError, OverflowError):
                continue
            if site_id > 0 and math.isfinite(value) and value >= 0.0:
                self.directed_distances[site_id] = min(
                    value, self.directed_distances.get(site_id, value))
        self.rows: dict[int, EdgeDependenceBranch] = {}
        self.cells: dict[tuple[int, int], tuple[int, int]] = {}
        self.row_cells: dict[int, int] = {}
        self.target_lease_seconds = max(
            1.0, _nonnegative_float(target_lease_seconds))
        # WU-UCT-style unobserved-work accounting for expensive target solves.
        # Dead workers cannot hold a target forever because every group expires.
        self.target_leases: dict[int, float] = {}
        self.target_lease_groups: dict[int, tuple[float, tuple[int, ...]]] = {}
        self.lease_reservations = 0
        self.lease_suppressions = 0

    @staticmethod
    def _edge_id(site: int, taken: int) -> int:
        mixed = ((site & 0xFFFFFFFFFFFFFFFF) * 11400714819323198485)
        mixed ^= 0x9E3779B97F4A7C15 if taken else 0xD1B54A32D192ED03
        return (mixed & 0xFFFFFFFFFFFFFFFF) or 1

    @staticmethod
    def _add_corpus(row: EdgeDependenceBranch, path: str,
                    limit: int) -> None:
        if path in row.corpus:
            return
        row.corpus = (row.corpus + (path,))[-limit:]

    @staticmethod
    def _add_target(row: EdgeDependenceBranch, target_branch: int) -> None:
        if target_branch <= 0 or target_branch in row.target_branches:
            return
        targets = row.target_branches
        if len(targets) >= 64:
            targets = targets[1:]
            row.target_cursor = max(0, row.target_cursor - 1)
        row.target_branches = targets + (target_branch,)
        row.target_cursor %= len(row.target_branches)

    @staticmethod
    def _retire_target(row: EdgeDependenceBranch, target_branch: int) -> None:
        if target_branch <= 0 or target_branch not in row.target_branches:
            return
        retired_index = row.target_branches.index(target_branch)
        row.target_branches = tuple(
            branch for branch in row.target_branches
            if branch != target_branch
        )
        if row.target_branches:
            if retired_index < row.target_cursor:
                row.target_cursor -= 1
            row.target_cursor %= len(row.target_branches)
        else:
            row.target_cursor = 0

    def _expire_target_leases(self, now: float) -> None:
        for target, deadline in tuple(self.target_leases.items()):
            if now >= deadline:
                self.target_leases.pop(target, None)
        for primary, (deadline, _targets) in tuple(
                self.target_lease_groups.items()):
            if now >= deadline:
                self.target_lease_groups.pop(primary, None)

    def leased_targets(self, now: float) -> set[int]:
        self._expire_target_leases(now)
        return set(self.target_leases)

    def reserve_targets(
        self,
        primary: int,
        targets: tuple[int, ...],
        *,
        cooldown: float,
        now: float,
    ) -> bool:
        self._expire_target_leases(now)
        unique = tuple(dict.fromkeys(
            target for target in targets if target > 0))
        if not unique:
            return True
        conflicts = [target for target in unique if target in self.target_leases]
        if conflicts:
            self.lease_suppressions += len(conflicts)
            return False
        deadline = now + max(
            _nonnegative_float(cooldown), self.target_lease_seconds)
        for target in unique:
            self.target_leases[target] = deadline
        group_key = primary if primary > 0 else unique[0]
        self.target_lease_groups[group_key] = (deadline, unique)
        self.lease_reservations += len(unique)
        return True

    def reserve_job(
        self,
        job: ReplayJob,
        *,
        cooldown: float,
        now: float,
    ) -> bool:
        targets = tuple(dict.fromkeys(
            (job.target_branch,) + tuple(
                branch for branch, action in job.actions
                if action != "skip"
            )
        ))
        return self.reserve_targets(
            job.target_branch, targets, cooldown=cooldown, now=now)

    def release_job(self, job: ReplayJob) -> None:
        targets = tuple(dict.fromkeys(
            (job.target_branch,) + tuple(
                branch for branch, action in job.actions
                if action != "skip"
            )
        ))
        unique = tuple(target for target in targets if target > 0)
        if unique:
            self.release_target(
                job.target_branch if job.target_branch > 0 else unique[0])

    def release_target(self, target_branch: int) -> None:
        if target_branch <= 0:
            return
        group = self.target_lease_groups.pop(target_branch, None)
        if group is None:
            self.target_leases.pop(target_branch, None)
            return
        deadline, targets = group
        for target in targets:
            if self.target_leases.get(target) == deadline:
                self.target_leases.pop(target, None)

    @staticmethod
    def _next_target(
        row: EdgeDependenceBranch,
        blocked: set[int],
    ) -> tuple[int, int] | None:
        if not row.target_branches:
            return (0, -1)
        for offset in range(len(row.target_branches)):
            index = (row.target_cursor + offset) % len(row.target_branches)
            target = row.target_branches[index]
            if target not in blocked:
                return (target, index)
        return None

    def _branch_observations(
        self,
        telemetry: SolverTelemetry,
    ) -> tuple[tuple[int, int, int], ...]:
        observations: list[tuple[int, int, int]] = []
        for entry in telemetry.branch_trace[:self.trace_cap]:
            site = entry[3]
            if not site:
                continue
            branch_id = self._edge_id(site, entry[4])
            observations.append((branch_id, site, entry[2]))
        return tuple(observations)

    def _branch_counts(
        self,
        observations: tuple[tuple[int, int, int], ...],
    ) -> dict[int, int]:
        counts: dict[int, int] = {}
        for branch_id, _site, _target_branch in observations:
            counts[branch_id] = min(255, counts.get(branch_id, 0) + 1)
        return counts

    @staticmethod
    def _edge_reward(
        *,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        killed: bool,
        telemetry: SolverTelemetry,
    ) -> float:
        if killed:
            return 0.0
        coverage = 1.0 - math.exp(-max(0, coverage_delta) / 4.0)
        corpus = 1.0 - math.exp(-max(0, interesting_cases) / 2.0)
        reached = 1.0 if telemetry.target_reached else 0.0
        solver = max(telemetry.solve_yield, telemetry.backsolver_yield)
        data = 1.0 - math.exp(-telemetry.data_coverage_map_updates / 64.0)
        quality = (
            0.46 * coverage
            + 0.18 * corpus
            + 0.16 * reached
            + 0.12 * solver
            + 0.08 * data
        )
        cost = (
            1.0
            + 0.35 * math.log1p(max(0.0, elapsed))
            + 0.25 * telemetry.timeout_ratio
        )
        return _clamp01(quality / cost)

    def observe(
        self,
        path: str,
        telemetry: SolverTelemetry,
        now: float,
        *,
        coverage_delta: int = 0,
        interesting_cases: int = 0,
        elapsed: float = 0.0,
        killed: bool = False,
    ) -> int:
        attempted_target = telemetry.target_branch
        if attempted_target:
            self.release_target(attempted_target)
        terminal_target = (
            attempted_target
            if telemetry.target_reached
            and telemetry.target_status in {"sat", "unsat"}
            else 0
        )
        observations = self._branch_observations(telemetry)
        counts = self._branch_counts(observations)
        if not counts:
            if terminal_target:
                for row in self.rows.values():
                    self._retire_target(row, terminal_target)
            return 0
        branches = tuple(counts)
        metadata: dict[int, tuple[int, int]] = {}
        for branch_id, site, target_branch in observations:
            metadata.setdefault(branch_id, (site, target_branch))
        local_reward = self._edge_reward(
            coverage_delta=coverage_delta,
            interesting_cases=interesting_cases,
            elapsed=elapsed,
            killed=killed or telemetry.killed,
            telemetry=telemetry,
        )
        productive = (
            coverage_delta > 0
            or interesting_cases > 0
            or telemetry.target_reached
            or telemetry.generated > 0
        )
        failed_target = (
            attempted_target
            if attempted_target
            and (
                killed
                or telemetry.killed
                or telemetry.target_status == "unknown"
                or not telemetry.target_reached
            )
            else 0
        )
        changed_rows: set[int] = set()
        corpus_rows: set[int] = set()
        novelty = 0
        for branch_id in branches:
            row = self.rows.get(branch_id)
            if row is None:
                site, _target_branch = metadata.get(branch_id, (0, 0))
                row = EdgeDependenceBranch(branch_id, site_id=site)
                self.rows[branch_id] = row
            site, target_branch = metadata.get(branch_id, (0, 0))
            if site and not row.site_id:
                row.site_id = site
            if site in self.directed_distances:
                row.target_distance = self.directed_distances[site]
            if target_branch and target_branch != terminal_target:
                self._add_target(row, target_branch)
                corpus_rows.add(branch_id)
            if terminal_target and terminal_target in row.target_branches:
                row.successful_targets += int(telemetry.target_status == "sat")
            target_failed_here = (
                failed_target and failed_target in row.target_branches
            )
            untargeted_terminal = (
                (killed or telemetry.killed) and not telemetry.target_branch
            )
            if target_failed_here or untargeted_terminal:
                row.terminal_failures += 1
            alpha = 0.30 if row.traced == 0 else 0.18
            row.reward_ema = (
                (1.0 - alpha) * row.reward_ema + alpha * local_reward)
            if elapsed > 0.0:
                row.cost_ema = (
                    (1.0 - alpha) * row.cost_ema
                    + alpha * max(0.0, elapsed)
                )
            if productive:
                row.last_gain = now
            row.traced += 1
            row.last_seen = now

        for source in branches:
            for dest in branches:
                key = (source, dest)
                count = counts[dest]
                previous = self.cells.get(key)
                if previous is None:
                    self.cells[key] = (count, count)
                    self.row_cells[source] = self.row_cells.get(source, 0) + 1
                    changed_rows.add(source)
                    novelty += 1
                    continue
                lo, hi = previous
                new_lo, new_hi = min(lo, count), max(hi, count)
                if new_lo != lo or new_hi != hi:
                    self.cells[key] = (new_lo, new_hi)
                    changed_rows.add(source)
                    novelty += 1

        for branch_id in changed_rows:
            self._add_corpus(
                self.rows[branch_id], path, self.corpus_per_branch)
        for branch_id in corpus_rows - changed_rows:
            self._add_corpus(
                self.rows[branch_id], path, self.corpus_per_branch)
        if terminal_target:
            for row in self.rows.values():
                self._retire_target(row, terminal_target)
        self._prune()
        return novelty

    def _prune(self) -> None:
        branch_overflow = len(self.rows) - self.max_branches
        if branch_overflow > 0:
            victims = heapq.nsmallest(
                branch_overflow,
                self.rows.values(),
                key=lambda row: (
                    bool(row.corpus),
                    bool(row.target_branches),
                    row.reward_ema,
                    row.last_seen,
                    row.traced + 100 * row.scheduled,
                ),
            )
            for row in victims:
                self.rows.pop(row.branch_id, None)
            retained = set(self.rows)
            self.cells = {
                key: value for key, value in self.cells.items()
                if key[0] in retained and key[1] in retained
            }
            self._rebuild_row_cells()

        cell_overflow = len(self.cells) - self.max_cells
        if cell_overflow <= 0:
            return
        victims = heapq.nsmallest(
            cell_overflow,
            self.cells.items(),
            key=lambda item: (item[1][1] - item[1][0], item[1][1], item[0]),
        )
        for key, _value in victims:
            self.cells.pop(key, None)
            source = key[0]
            if source in self.row_cells:
                self.row_cells[source] = max(0, self.row_cells[source] - 1)

    def _rebuild_row_cells(self) -> None:
        self.row_cells.clear()
        for source, _dest in self.cells:
            self.row_cells[source] = self.row_cells.get(source, 0) + 1

    def path_bonus(self, path: str) -> float:
        branch_scores = [
            self.row_cells.get(row.branch_id, 0)
            + 2 * len(row.target_branches)
            + 4.0 * row.reward_ema
            for row in self.rows.values()
            if path in row.corpus
        ]
        if not branch_scores:
            return 0.0
        return min(1.0, math.log1p(sum(branch_scores)) / 12.0)

    def select(
        self,
        limit: int,
        cooldown: float,
        now: float,
        exclude_paths: set[str] | None = None,
        *,
        admit: Callable[[ReplayJob], bool] | None = None,
    ) -> list[ReplayJob]:
        if limit <= 0:
            return []
        self._expire_target_leases(now)
        excluded = exclude_paths or set()
        candidates: list[tuple[float, EdgeDependenceBranch, str]] = []
        leased_targets = set(self.target_leases)
        for row in self.rows.values():
            if now - row.last_scheduled < cooldown:
                continue
            paths = [
                path for path in reversed(row.corpus)
                if path not in excluded and os.path.isfile(path)
            ]
            if not paths:
                continue
            if self._next_target(row, leased_targets) is None:
                self.lease_suppressions += 1
                continue
            # SYMCTS prioritizes lower traced+scheduled rows; recent directed
            # work also gives an explicit branch target and utility feedback.
            under_explored = 1.0 / (1.0 + row.traced + 20.0 * row.scheduled)
            structural = min(1.0, math.log1p(
                self.row_cells.get(row.branch_id, 0)) / 8.0)
            age = min(1.0, (now - row.last_scheduled) / max(1.0, cooldown))
            distance_bonus = 0.0
            if row.site_id in self.directed_distances:
                distance_bonus = 1.0 / (
                    1.0 + self.directed_distances[row.site_id])
            target_bonus = 1.0 if row.target_branches else 0.0
            utility = _clamp01(
                row.reward_ema / math.sqrt(max(0.05, row.cost_ema)))
            success_bonus = min(1.0, row.successful_targets / 4.0)
            failure_penalty = min(0.45, 0.08 * row.terminal_failures)
            score = (
                0.35 * under_explored
                + 0.18 * structural
                + 0.16 * distance_bonus
                + 0.14 * utility
                + 0.10 * target_bonus
                + 0.04 * success_bonus
                + 0.03 * age
                - failure_penalty
            )
            selected_path = paths[row.scheduled % len(paths)]
            candidates.append((score, row, selected_path))
        selected = []
        selected_paths = set(excluded)
        selected_targets = set(leased_targets)
        for _score, row, path in sorted(candidates, key=lambda item: item[0],
                                       reverse=True):
            if path in selected_paths:
                continue
            target_choice = self._next_target(row, selected_targets)
            if target_choice is None:
                self.lease_suppressions += 1
                continue
            target, target_index = target_choice
            job = ReplayJob(path, target)
            if target:
                accepted = (
                    admit(job) if admit is not None
                    else self.reserve_job(job, cooldown=cooldown, now=now)
                )
                if not accepted:
                    continue
                selected_targets.add(target)
                row.target_cursor = (
                    target_index + 1) % len(row.target_branches)
            row.scheduled += 1
            row.last_scheduled = now
            selected.append(job)
            selected_paths.add(path)
            if len(selected) >= limit:
                break
        return selected

    def to_mapping(self) -> dict[str, Any]:
        return {
            "max_branches": self.max_branches,
            "max_cells": self.max_cells,
            "trace_cap": self.trace_cap,
            "corpus_per_branch": self.corpus_per_branch,
            "lease_reservations": self.lease_reservations,
            "lease_suppressions": self.lease_suppressions,
            "rows": [
                {
                    "branch_id": row.branch_id,
                    "site_id": row.site_id,
                    "traced": row.traced,
                    "scheduled": row.scheduled,
                    "last_seen": row.last_seen,
                    "last_scheduled": row.last_scheduled,
                    "last_gain": row.last_gain,
                    "corpus": list(row.corpus),
                    "target_branches": list(row.target_branches),
                    "target_cursor": row.target_cursor,
                    "reward_ema": row.reward_ema,
                    "cost_ema": row.cost_ema,
                    "terminal_failures": row.terminal_failures,
                    "successful_targets": row.successful_targets,
                    "target_distance": row.target_distance,
                }
                for row in self.rows.values()
            ],
            "cells": [
                [source, dest, bounds[0], bounds[1]]
                for (source, dest), bounds in self.cells.items()
            ],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.rows.clear()
        self.cells.clear()
        self.row_cells.clear()
        self.target_leases.clear()
        self.target_lease_groups.clear()
        self.lease_reservations = _nonnegative_int(
            raw.get("lease_reservations"))
        self.lease_suppressions = _nonnegative_int(
            raw.get("lease_suppressions"))
        now = time.monotonic()
        rows = raw.get("rows", ())
        if isinstance(rows, list):
            for item in rows[-self.max_branches:]:
                if not isinstance(item, dict):
                    continue
                branch_id = _nonnegative_int(item.get("branch_id"))
                if not branch_id:
                    continue
                paths = item.get("corpus", ())
                if not isinstance(paths, (list, tuple)):
                    paths = ()
                targets_raw = item.get("target_branches", ())
                if not isinstance(targets_raw, (list, tuple)):
                    targets_raw = ()
                targets = tuple(dict.fromkeys(
                    target for target in (
                        _nonnegative_int(value) for value in targets_raw)
                    if target
                ))[:64]
                try:
                    last_seen = max(0.0, float(item.get("last_seen", 0.0)))
                    last_scheduled = max(
                        0.0, float(item.get("last_scheduled", 0.0)))
                    last_gain = max(
                        0.0, float(item.get("last_gain", 0.0)))
                    if last_seen > now + 3600.0:
                        last_seen = now
                    if last_scheduled > now + 3600.0:
                        last_scheduled = 0.0
                    if last_gain > now + 3600.0:
                        last_gain = 0.0
                    target_cursor = _nonnegative_int(
                        item.get("target_cursor"))
                    if targets:
                        target_cursor %= len(targets)
                    else:
                        target_cursor = 0
                    self.rows[branch_id] = EdgeDependenceBranch(
                        branch_id=branch_id,
                        site_id=_nonnegative_int(item.get("site_id")),
                        traced=_nonnegative_int(item.get("traced")),
                        scheduled=_nonnegative_int(item.get("scheduled")),
                        last_seen=last_seen,
                        last_scheduled=last_scheduled,
                        last_gain=last_gain,
                        corpus=tuple(str(path) for path in
                                     paths[-self.corpus_per_branch:]),
                        target_branches=targets,
                        target_cursor=target_cursor,
                        reward_ema=_clamp01(
                            _nonnegative_float(item.get("reward_ema"))),
                        cost_ema=_nonnegative_float(item.get("cost_ema")),
                        terminal_failures=_nonnegative_int(
                            item.get("terminal_failures")),
                        successful_targets=_nonnegative_int(
                            item.get("successful_targets")),
                        target_distance=_nonnegative_float(
                            item.get("target_distance")),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
        cells = raw.get("cells", ())
        if isinstance(cells, list):
            for item in cells[-self.max_cells:]:
                if not isinstance(item, (list, tuple)) or len(item) != 4:
                    continue
                source, dest, lo, hi = (_nonnegative_int(value)
                                        for value in item)
                if (source in self.rows and dest in self.rows and lo <= hi):
                    self.cells[(source, dest)] = (lo, hi)
        self._rebuild_row_cells()


class HierarchicalConcurrencyGuidance:
    """Distance-layered replay around concurrency-relevant branch frontiers."""

    def __init__(
        self,
        distance_path: str | None,
        max_records: int = 4096,
    ) -> None:
        self.distances = load_directed_distance_map(distance_path)
        self.max_records = max(
            128,
            min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_records)),
        )
        self.records: dict[str, ConcurrencyReplayRecord] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.distances)

    @staticmethod
    def _level(distance: float) -> int:
        if distance <= 0.0:
            return 0
        if distance <= 2.0:
            return 1
        if distance <= 8.0:
            return 2
        return 3

    def observe(
        self,
        path: str,
        telemetry: SolverTelemetry,
        reward: float,
        now: float,
    ) -> int:
        if not self.enabled:
            return 0
        matches: list[tuple[float, int]] = []
        for _parent, _actual, opposite, site, _taken, _interesting in (
                telemetry.branch_trace):
            distance = self.distances.get(site)
            if distance is not None:
                matches.append((distance, opposite))
        if not matches:
            return 0
        best_distance = min(distance for distance, _opposite in matches)
        open_branches = tuple(dict.fromkeys(
            branch for _distance, branch in matches if branch))
        record = self.records.get(path)
        if record is None:
            _bounded_mapping_set(
                self.records,
                path,
                ConcurrencyReplayRecord(
                    path=path,
                    best_distance=best_distance,
                    reward=reward,
                    visits=1,
                    last_seen=now,
                    open_branches=open_branches[:64],
                ),
                self.max_records,
            )
        else:
            record.best_distance = min(record.best_distance, best_distance)
            record.reward = 0.72 * record.reward + 0.28 * reward
            record.visits += 1
            record.last_seen = now
            record.open_branches = tuple(dict.fromkeys(
                record.open_branches + open_branches))[:64]
            if record.open_branches:
                record.target_cursor %= len(record.open_branches)
            self.records.pop(path)
            self.records[path] = record
        return len(matches)

    def path_bonus(self, path: str) -> float:
        record = self.records.get(path)
        if record is None:
            return 0.0
        return min(1.0, 1.0 / (1.0 + record.best_distance) + 0.25 * record.reward)

    def select(
        self,
        limit: int,
        cooldown: float,
        now: float,
        exclude_paths: set[str],
        exclude_targets: set[int] | None = None,
        *,
        commit: bool = True,
        proposal_limit: int | None = None,
    ) -> list[ReplayJob]:
        if not self.enabled or limit <= 0:
            return []
        blocked_targets = exclude_targets or set()
        candidates: list[
            tuple[tuple[int, float], ConcurrencyReplayRecord, int, int]
        ] = []
        for record in self.records.values():
            if record.path in exclude_paths:
                continue
            if not os.path.isfile(record.path):
                continue
            if now - record.last_scheduled < cooldown:
                continue
            target = 0
            target_index = -1
            if record.open_branches:
                for offset in range(len(record.open_branches)):
                    index = (record.target_cursor + offset) % len(
                        record.open_branches)
                    candidate_target = record.open_branches[index]
                    if candidate_target not in blocked_targets:
                        target = candidate_target
                        target_index = index
                        break
                if target_index < 0:
                    continue
            frontier = 1.0 / (1.0 + record.best_distance)
            novelty = 1.0 / math.sqrt(max(1, record.visits))
            open_bonus = 0.20 if record.open_branches else 0.0
            score = 0.58 * frontier + 0.25 * record.reward + 0.12 * novelty \
                + open_bonus
            candidates.append(((self._level(record.best_distance), -score),
                               record, target, target_index))
        candidates.sort(key=lambda item: item[0])
        jobs: list[ReplayJob] = []
        output_limit = max(limit, proposal_limit or limit)
        for _key, record, target, target_index in candidates[:output_limit]:
            jobs.append(ReplayJob(record.path, target))
        if commit:
            self.commit_jobs(jobs, now)
        return jobs

    def commit_jobs(self, jobs: list[ReplayJob], now: float) -> None:
        for job in jobs:
            record = self.records.get(job.path)
            if record is None:
                continue
            record.last_scheduled = now
            if job.target_branch in record.open_branches:
                record.target_cursor = (
                    record.open_branches.index(job.target_branch) + 1
                ) % len(record.open_branches)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "distance_sites": len(self.distances),
            "records": [vars(record) for record in self.records.values()],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        records = raw.get("records")
        if not isinstance(records, list):
            return
        now = time.monotonic()
        for item in records[-self.max_records:]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", ""))
            if not path:
                continue
            branches = item.get("open_branches", ())
            if not isinstance(branches, (list, tuple)):
                branches = ()
            try:
                last_seen = max(0.0, float(item.get("last_seen", 0.0)))
                last_scheduled = max(
                    0.0, float(item.get("last_scheduled", 0.0)))
                if last_seen > now + 3600.0:
                    last_seen = now
                if last_scheduled > now + 3600.0:
                    last_scheduled = 0.0
                _bounded_mapping_set(
                    self.records,
                    path,
                    ConcurrencyReplayRecord(
                        path=path,
                        best_distance=_nonnegative_float(
                            item.get("best_distance")),
                        reward=_clamp01(float(item.get("reward", 0.0))),
                        visits=max(1, _nonnegative_int(item.get("visits"))),
                        last_seen=last_seen,
                        last_scheduled=last_scheduled,
                        open_branches=tuple(dict.fromkeys(
                            branch for branch in
                            (_nonnegative_int(value) for value in branches)
                            if branch))[:64],
                        target_cursor=_nonnegative_int(
                            item.get("target_cursor")),
                    ),
                    self.max_records,
                )
            except (TypeError, ValueError, OverflowError):
                continue


class PrefixDAG:
    """Bounded, persistent execution DAG keyed by prefix-sensitive branch IDs."""

    ACTIVE = {"open", "timeout", "diverged"}

    def __init__(self, max_nodes: int = 4096) -> None:
        self.max_nodes = max(
            128,
            min(65536, _nonnegative_int(max_nodes)),
        )
        self.nodes: dict[int, PrefixNode] = {}
        self.constraints = ConstraintSummaryCache(self.max_nodes * 2)
        self.directed_sites = _parse_int_set(os.environ.get("SYMCC_DIRECTED_SITES"))
        self.directed_distances = load_directed_distance_map(
            os.environ.get("SYMCC_DIRECTED_DISTANCE"))
        self.concurrency_distances = load_directed_distance_map(
            os.environ.get("SYMCC_CONCURRENCY_GUIDANCE"))
        self.dynamic_coloration_enabled = (
            os.environ.get("SYMCC_DYNAMIC_COLORATION", "1") != "0")
        try:
            self.mdp_gamma = _clamp01(float(
                os.environ.get("SYMCC_COLORGO_GAMMA", "0.82")))
        except (TypeError, ValueError, OverflowError):
            self.mdp_gamma = 0.82
        if self.mdp_gamma <= 0.0:
            self.mdp_gamma = 0.82
        self.selective_mdp_iterations = max(
            1,
            min(
                128,
                _nonnegative_int(
                    os.environ.get("SYMCC_SELECTIVE_MDP_ITERATIONS", 32)
                ),
            ),
        )
        try:
            self.selective_mdp_tolerance = float(
                os.environ.get("SYMCC_SELECTIVE_MDP_TOLERANCE", "0.000001")
            )
        except (TypeError, ValueError, OverflowError):
            self.selective_mdp_tolerance = 0.000001
        if not math.isfinite(self.selective_mdp_tolerance):
            self.selective_mdp_tolerance = 0.000001
        self.selective_mdp_tolerance = max(
            1e-12, min(0.1, self.selective_mdp_tolerance)
        )
        self.selective_mdp_last_iterations = 0
        self.selective_mdp_last_residual = 0.0
        self.selective_mdp_transition_groups = 0
        self.selective_mdp_refresh_interval = max(
            1,
            min(
                1024,
                _nonnegative_int(
                    os.environ.get("SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL", 8)
                ),
            ),
        )
        self.selective_mdp_small_graph_nodes = max(
            0,
            min(
                self.max_nodes,
                _nonnegative_int(
                    os.environ.get("SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES", 512)
                ),
            ),
        )
        self.selective_mdp_refreshes = 0
        self.selective_mdp_skipped_refreshes = 0
        self._mdp_refresh_observations = 0
        try:
            self.min_feasibility = _clamp01(float(
                os.environ.get("SYMCC_COLORGO_MIN_FEASIBILITY", "0.04")))
        except (TypeError, ValueError, OverflowError):
            self.min_feasibility = 0.04
        self.sampling_budget = max(
            0, _nonnegative_int(os.environ.get("SYMCC_S2F_SAMPLING_BUDGET", 2)))
        try:
            self.high_queue_fraction = _clamp01(float(
                os.environ.get("SYMCC_S2F_HIGH_QUEUE_FRACTION", "0.75")))
        except (TypeError, ValueError, OverflowError):
            self.high_queue_fraction = 0.75
        self.action_cap = max(
            1,
            min(
                64,
                _nonnegative_int(
                    os.environ.get("SYMCC_S2F_ACTIONS_PER_SEED", 16)),
            ),
        )
        self.taco_enabled = os.environ.get("SYMCC_TACO", "1") != "0"
        self.taco_extended_conditions = (
            os.environ.get("SYMCC_TACO_EXTENDED_CONDITIONS", "1") != "0")
        self.multigo_enabled = os.environ.get("SYMCC_MULTIGO", "1") != "0"
        try:
            self.multigo_poisson_scale = max(0.05, float(
                os.environ.get("SYMCC_MULTIGO_POISSON_SCALE", "3.0")))
        except (TypeError, ValueError, OverflowError):
            self.multigo_poisson_scale = 3.0
        try:
            self.multigo_explore_fraction = _clamp01(float(
                os.environ.get("SYMCC_MULTIGO_EXPLORE_FRACTION", "0.35")))
        except (TypeError, ValueError, OverflowError):
            self.multigo_explore_fraction = 0.35
        self.site_frequency: dict[int, int] = {}
        self.site_frequency_cap = min(
            65536,
            max(4096, self.max_nodes * 4),
        )
        self._site_frequency_heap: list[tuple[int, int]] = []
        self.total_site_frequency = 0
        self.queue_epoch = 0

    def _record_site_frequency(self, site: int) -> None:
        """Update a bounded Space-Saving sketch for MultiGo probabilities."""
        if site <= 0:
            return
        count = self.site_frequency.get(site)
        if count is not None:
            count += 1
            self.site_frequency[site] = count
            heapq.heappush(self._site_frequency_heap, (count, site))
        elif len(self.site_frequency) < self.site_frequency_cap:
            self.site_frequency[site] = 1
            heapq.heappush(self._site_frequency_heap, (1, site))
        else:
            while self._site_frequency_heap:
                victim_count, victim = heapq.heappop(
                    self._site_frequency_heap)
                if self.site_frequency.get(victim) == victim_count:
                    break
            else:
                victim, victim_count = min(
                    self.site_frequency.items(),
                    key=lambda item: (item[1], item[0]),
                )
            self.site_frequency.pop(victim, None)
            replacement_count = victim_count + 1
            self.site_frequency[site] = replacement_count
            heapq.heappush(
                self._site_frequency_heap,
                (replacement_count, site),
            )
        self.total_site_frequency += 1
        if len(self._site_frequency_heap) > self.site_frequency_cap * 4:
            self._site_frequency_heap = [
                (frequency, observed_site)
                for observed_site, frequency in self.site_frequency.items()
            ]
            heapq.heapify(self._site_frequency_heap)

    def _directed_weight(self, site_id: int) -> float:
        if site_id in self.directed_sites:
            return 1.0
        distance = self.directed_distances.get(site_id)
        if distance is None:
            return 0.0
        return 1.0 / (1.0 + distance)

    def _concurrency_weight(self, site_id: int) -> float:
        distance = self.concurrency_distances.get(site_id)
        if distance is None:
            return 0.0
        return 1.0 / (1.0 + distance)

    def _color_score(self, node: PrefixNode) -> float:
        static = max(self._directed_weight(node.site_id),
                     self._concurrency_weight(node.site_id))
        if not self.dynamic_coloration_enabled:
            return static
        dynamic = max(node.color_feasibility, node.mdp_value)
        if node.infeasible_streak:
            dynamic *= max(0.0, 1.0 - 0.12 * node.infeasible_streak)
        return _clamp01(max(static, dynamic))

    def _site_probability(self, site_id: int) -> float:
        if self.total_site_frequency <= 0:
            return 0.5
        frequency = self.site_frequency.get(site_id, 0)
        lam = self.multigo_poisson_scale * frequency / self.total_site_frequency
        return _clamp01(1.0 - math.exp(-max(1e-6, lam)))

    def _path_difficulty(self, sites: list[int]) -> float:
        if not sites or not self.multigo_enabled:
            return 0.0
        unique_sites = list(dict.fromkeys(site for site in sites if site))
        if not unique_sites:
            return 0.0
        difficulty = 0.0
        for site in unique_sites:
            difficulty += -math.log(max(1e-6, self._site_probability(site)))
        return _clamp01(difficulty / (8.0 + len(unique_sites)))

    def _target_score(self, site_id: int) -> float:
        return max(self._directed_weight(site_id),
                   self._concurrency_weight(site_id))

    def _target_underexplored(self, node: PrefixNode) -> float:
        return 1.0 / math.sqrt(1.0 + node.target_path_visits + node.attempts)

    def _target_path_score(self, node: PrefixNode) -> float:
        if not (self.taco_enabled or self.multigo_enabled):
            return 0.0
        if (node.target_path_visits == 0 and node.target_distance <= 0.0
                and node.target_path_reward <= 0.0
                and self._target_score(node.site_id) <= 0.0):
            return 0.0
        exploit = max(node.target_distance, node.target_path_reward,
                      self._color_score(node))
        explore = max(node.path_difficulty, self._target_underexplored(node))
        if self.queue_epoch % 100 < int(100 * self.multigo_explore_fraction):
            multigo = 0.62 * explore + 0.38 * exploit
        else:
            multigo = 0.58 * exploit + 0.42 * explore
        taco = (
            0.45 * node.target_distance
            + 0.30 * self._target_underexplored(node)
            + 0.25 * node.target_path_reward
        )
        return _clamp01(max(multigo if self.multigo_enabled else 0.0,
                            taco if self.taco_enabled else 0.0))

    def _update_target_path_metrics(
        self,
        node: PrefixNode,
        path_difficulty: float,
        reward: float,
    ) -> None:
        distance = self._target_score(node.site_id)
        node.path_difficulty = _clamp01(
            0.80 * node.path_difficulty + 0.20 * path_difficulty)
        node.target_distance = max(
            0.86 * node.target_distance,
            distance,
        )
        node.target_path_reward = _clamp01(
            0.74 * node.target_path_reward + 0.26 * reward)
        node.target_path_visits += 1
        node.taco_generation_bonus = _clamp01(
            0.48 * node.target_distance
            + 0.34 * self._target_underexplored(node)
            + 0.18 * node.target_path_reward
        )

    def _children_by_parent(self) -> dict[int, list[PrefixNode]]:
        children: dict[int, list[PrefixNode]] = {}
        for node in self.nodes.values():
            if node.parent_id:
                children.setdefault(node.parent_id, []).append(node)
        return children

    def _depth(self, node: PrefixNode) -> int:
        depth = 0
        seen = {node.branch_id}
        parent = self.nodes.get(node.parent_id)
        while parent is not None and parent.branch_id not in seen and depth < 256:
            seen.add(parent.branch_id)
            depth += 1
            parent = self.nodes.get(parent.parent_id)
        return depth

    def _refresh_mdp_values(self) -> None:
        if not self.dynamic_coloration_enabled or not self.nodes:
            return
        self.selective_mdp_refreshes += 1
        children = self._children_by_parent()
        sibling_groups: dict[tuple[int, int], list[PrefixNode]] = {}
        for node in self.nodes.values():
            key = (node.parent_id, node.site_id)
            sibling_groups.setdefault(key, []).append(node)
        self.selective_mdp_transition_groups = len(sibling_groups)

        def transition_probability(node: PrefixNode) -> float:
            siblings = sibling_groups[(node.parent_id, node.site_id)]
            total = sum(
                sibling.visits + 0.5 * sibling.attempts
                for sibling in siblings
            )
            return (
                node.visits + 0.5 * node.attempts + 1.0
            ) / (total + len(siblings))

        child_groups: dict[int, dict[int, list[PrefixNode]]] = {}
        for parent_id, child_nodes in children.items():
            for child in child_nodes:
                child_groups.setdefault(parent_id, {}).setdefault(
                    child.site_id, []
                ).append(child)

        values = {
            branch_id: _clamp01(node.mdp_value)
            for branch_id, node in self.nodes.items()
        }
        residual = 0.0
        iterations = 0
        for iteration in range(self.selective_mdp_iterations):
            updated: dict[int, float] = {}
            residual = 0.0
            for branch_id, node in self.nodes.items():
                static = max(
                    self._directed_weight(node.site_id),
                    self._concurrency_weight(node.site_id),
                )
                feasibility = max(node.color_feasibility, static)
                if static > 0.0:
                    feasibility = max(feasibility, 0.35 + 0.65 * static)
                if node.status == "unsat":
                    feasibility *= 0.35

                future = 0.0
                for alternatives in child_groups.get(branch_id, {}).values():
                    expected = sum(
                        transition_probability(child)
                        * values.get(child.branch_id, 0.0)
                        for child in alternatives
                    )
                    future = max(future, expected)

                local_reward = max(
                    node.reward,
                    0.55 * node.data_reward,
                    0.55 * node.backsolver_reward,
                    0.55 * node.path_cover_reward,
                )
                novelty = (
                    1.0
                    if node.status in self.ACTIVE and node.visits == 0
                    else (0.35 if node.status in self.ACTIVE else 0.0)
                )
                node.mdp_transition_probability = transition_probability(node)
                node.mdp_novelty_reward = novelty
                cost = _clamp01(
                    max(
                        node.mdp_cost,
                        0.55 * node.solver_cost
                        + 0.45 * node.timeout_penalty,
                    )
                )
                infeasible_penalty = min(
                    0.55, 0.11 * node.infeasible_streak
                )
                value = _clamp01(
                    0.27 * novelty
                    + 0.20 * local_reward
                    + 0.18 * feasibility
                    + 0.17 * node.mdp_transition_probability
                    + 0.22 * self.mdp_gamma * future
                    - 0.24 * cost
                    - infeasible_penalty
                )
                updated[branch_id] = value
                residual = max(
                    residual, abs(value - values.get(branch_id, 0.0))
                )
            values = updated
            iterations = iteration + 1
            if residual <= self.selective_mdp_tolerance:
                break

        for branch_id, value in values.items():
            node = self.nodes[branch_id]
            node.mdp_value = value
            observed_cost = _clamp01(
                0.55 * node.solver_cost + 0.45 * node.timeout_penalty
            )
            node.mdp_cost = max(node.mdp_cost, observed_cost)
        self.selective_mdp_last_iterations = iterations
        self.selective_mdp_last_residual = residual

    def _should_refresh_mdp_values(self, *, force: bool = False) -> bool:
        if force:
            return True
        if len(self.nodes) <= self.selective_mdp_small_graph_nodes:
            return True
        if self.selective_mdp_refresh_interval <= 1:
            return True
        return (
            self._mdp_refresh_observations
            % self.selective_mdp_refresh_interval
        ) == 0

    def _update_dynamic_coloration(
        self,
        actual_path: list[PrefixNode],
        telemetry: SolverTelemetry,
        reward: float,
        killed: bool,
    ) -> None:
        if not self.dynamic_coloration_enabled:
            return
        reached = bool(not killed and telemetry.target_reached)
        self._mdp_refresh_observations += 1
        path_static = max(
            (
                max(
                    self._directed_weight(node.site_id),
                    self._concurrency_weight(node.site_id),
                )
                for node in actual_path
            ),
            default=0.0,
        )
        path_signal = max(path_static, 1.0 if reached else 0.0, reward)
        for depth, node in enumerate(reversed(actual_path)):
            static = max(self._directed_weight(node.site_id),
                         self._concurrency_weight(node.site_id))
            signal = max(static, path_signal * (self.mdp_gamma ** depth))
            if signal > 0.0:
                node.color_feasibility = _clamp01(
                    0.74 * node.color_feasibility + 0.26 * signal)
                if reached or static > 0.0:
                    node.infeasible_streak = 0
            elif telemetry.directed_pruned_branches:
                node.color_feasibility *= 0.94

        for branch_id in telemetry.open_branches:
            node = self.nodes.get(branch_id)
            if node is None:
                continue
            static = max(self._directed_weight(node.site_id),
                         self._concurrency_weight(node.site_id))
            if static > 0.0:
                node.color_feasibility = max(
                    node.color_feasibility,
                    _clamp01(0.64 * node.color_feasibility + 0.36 * static),
                )

        if telemetry.target_branch:
            target = self.nodes.get(telemetry.target_branch)
            if target is not None:
                if reached:
                    target.color_feasibility = _clamp01(
                        0.55 * target.color_feasibility + 0.45)
                    target.infeasible_streak = 0
                else:
                    expensive = (
                        telemetry.solver_unsat
                        or telemetry.solver_unknown
                        or telemetry.z3_timeouts
                        or telemetry.directed_pruned_branches
                    )
                    decay = 0.58 if expensive else 0.78
                    target.color_feasibility *= decay
                    target.infeasible_streak += 1
                    if telemetry.z3_timeouts or telemetry.solver_unknown:
                        target.mdp_cost = max(target.mdp_cost, 0.75)
        if self._should_refresh_mdp_values(force=(reached or path_static > 0.0)):
            self._refresh_mdp_values()
        else:
            self.selective_mdp_skipped_refreshes += 1

    @staticmethod
    def _update_node_pressure(
        node: PrefixNode,
        telemetry: SolverTelemetry,
    ) -> None:
        map_progress = (
            1.0 - math.exp(-telemetry.data_coverage_map_updates / 64.0)
            if telemetry.data_coverage_map_updates else 0.0)
        data_reward = max(telemetry.data_quality, map_progress)
        solver_cost = min(
            1.0, math.log1p(telemetry.solver_time_us / 1000.0) / 10.0)
        unknown_pressure = (
            telemetry.solver_unknown / max(1, telemetry.solver_queries)
            if telemetry.solver_unknown else 0.0)
        timeout_penalty = max(telemetry.timeout_ratio, min(1.0, unknown_pressure))
        mdp_cost = min(1.0, 0.55 * solver_cost + 0.45 * timeout_penalty)
        node.data_reward = 0.78 * node.data_reward + 0.22 * data_reward
        node.backsolver_reward = (
            0.78 * node.backsolver_reward
            + 0.22 * max(telemetry.backsolver_yield,
                         telemetry.backsolver_direct_yield))
        node.solver_cost = 0.80 * node.solver_cost + 0.20 * solver_cost
        node.timeout_penalty = 0.70 * node.timeout_penalty + 0.30 * timeout_penalty
        node.mdp_cost = 0.82 * node.mdp_cost + 0.18 * mdp_cost

    def _inherited_signal(self, node: PrefixNode, attr: str) -> float:
        value = _clamp01(float(getattr(node, attr, 0.0)))
        parent = self.nodes.get(node.parent_id)
        if parent is None:
            return value
        return max(value, _clamp01(float(getattr(parent, attr, 0.0))) * 0.92)

    @staticmethod
    def _add_seed(node: PrefixNode, path: str) -> None:
        if path in node.seed_paths:
            return
        node.seed_paths = (node.seed_paths + (path,))[-4:]

    def ingest(
        self,
        path: str,
        telemetry: SolverTelemetry,
        reward: float,
        now: float,
        strategy: int | None = None,
        elapsed: float = 0.0,
        killed: bool = False,
        actions: tuple[tuple[int, str], ...] = (),
    ) -> None:
        open_ids = set(telemetry.open_branches)
        trace_sites = [
            site for _parent, _actual, _opposite, site, _taken, _interesting
            in telemetry.branch_trace if site
        ]
        for site in trace_sites:
            self._record_site_frequency(site)
        path_difficulty = self._path_difficulty(trace_sites)
        actual_path = []
        for parent, actual, opposite, site, taken, _interesting in telemetry.branch_trace:
            actual_node = self.nodes.get(actual)
            if actual_node is None:
                actual_node = PrefixNode(
                    actual, parent, site, int(bool(taken)), "observed")
                self.nodes[actual] = actual_node
            actual_node.status = "observed"
            actual_node.visits += 1
            actual_node.last_update = now
            actual_node.difficulty = max(
                actual_node.difficulty * 0.8, telemetry.difficulty)
            self._update_node_pressure(actual_node, telemetry)
            self._update_target_path_metrics(
                actual_node, path_difficulty, reward)
            self._add_seed(actual_node, path)
            actual_path.append(actual_node)

            if opposite in open_ids:
                open_node = self.nodes.get(opposite)
                if open_node is None:
                    open_node = PrefixNode(
                        opposite, parent, site, int(not bool(taken)), "open")
                    self.nodes[opposite] = open_node
                if open_node.status not in {"resolved", "sat", "unsat"}:
                    open_node.status = "open"
                open_node.last_update = now
                open_node.difficulty = max(
                    open_node.difficulty * 0.8, telemetry.difficulty)
                self._update_node_pressure(open_node, telemetry)
                self._update_target_path_metrics(
                    open_node, path_difficulty, reward * 0.85)
                self._add_seed(open_node, path)

        # Propagate downstream utility to shared prefixes with depth decay.
        for depth, node in enumerate(reversed(actual_path)):
            propagated = reward * (0.92 ** depth)
            node.reward = 0.75 * node.reward + 0.25 * propagated

        self.constraints.observe(telemetry, now)
        reached_actions = {opposite for _parent, _actual, opposite,
                           _site, _taken, _interesting in telemetry.branch_trace}

        def record_action(target: PrefixNode, action_name: str,
                          last_status: str) -> None:
            action_key = (
                "sampling" if action_name == "sample"
                else (self._strategy_action(strategy)
                      if strategy is not None else "exact")
            )
            if action_key not in target.actions:
                action_key = "exact"
            action = target.actions[action_key]
            action.attempts += 1
            action.cost_sum += max(0.001, elapsed)
            action.reward_sum += _clamp01(reward)
            success = (
                not killed
                and bool(telemetry.generated or telemetry.solver_sat
                         or reward > 0.05)
            )
            if target.branch_id == telemetry.target_branch:
                success = success and telemetry.target_reached
            if success:
                action.successes += 1
                action.consecutive_failures = 0
            else:
                action.consecutive_failures += 1
            action.last_status = "killed" if killed else last_status

        if telemetry.target_branch:
            target = self.nodes.get(telemetry.target_branch)
            summary = self.constraints.entries.get(telemetry.target_branch)
            if target and summary:
                target.status = summary.status
                target.last_update = now
                if strategy is not None and not actions:
                    record_action(target, self._strategy_action(strategy),
                                  summary.status)
        if actions:
            status = (
                self.constraints.entries.get(telemetry.target_branch).status
                if telemetry.target_branch in self.constraints.entries
                else "observed"
            )
            for branch_id, action_name in actions:
                if action_name == "skip" or branch_id not in reached_actions:
                    continue
                target = self.nodes.get(branch_id)
                if target is None:
                    continue
                target.last_update = now
                record_action(target, action_name, status)
        self._update_dynamic_coloration(actual_path, telemetry, reward, killed)
        self._prune()

    @staticmethod
    def _strategy_action(strategy: int) -> str:
        if strategy == 0:
            return "exact"
        if strategy == 6:
            return "sampling"
        return "tailored"

    @staticmethod
    def _action_productivity(node: PrefixNode) -> float:
        attempted = [action for action in node.actions.values()
                     if action.attempts]
        if not attempted:
            return 0.0
        return max(
            action.reward_sum / action.attempts
            + 0.15 * action.successes / action.attempts
            for action in attempted
        )

    def _prune(self) -> None:
        overflow = len(self.nodes) - self.max_nodes
        if overflow <= 0:
            return
        # Preserve actionable nodes; evict old, low-value observed/terminal nodes first.
        victims = heapq.nsmallest(
            overflow,
            (node for node in self.nodes.values() if node.status not in self.ACTIVE),
            key=lambda node: (node.reward, node.last_update, node.visits),
        )
        for node in victims:
            self.nodes.pop(node.branch_id, None)
        overflow = len(self.nodes) - self.max_nodes
        if overflow <= 0:
            return
        # If a pathological target exposes more live prefixes than the budget, keep the
        # freshest and most valuable active nodes instead of letting memory grow without
        # bound.
        active_victims = heapq.nsmallest(
            overflow,
            (node for node in self.nodes.values() if node.status in self.ACTIVE),
            key=lambda node: (
                node.reward + 0.35 * node.data_reward
                + 0.20 * node.backsolver_reward
                + 0.20 * node.path_cover_reward
                + 0.25 * self._color_score(node)
                - 0.25 * max(node.timeout_penalty, node.mdp_cost),
                node.last_update,
                node.visits,
            ),
        )
        for node in active_victims:
            self.nodes.pop(node.branch_id, None)

    def select(
        self,
        limit: int,
        cooldown: float,
        now: float,
        exclude_targets: set[int] | None = None,
        *,
        commit: bool = True,
        proposal_limit: int | None = None,
    ) -> list[ReplayJob]:
        high_candidates = []
        low_candidates = []
        total_attempts = 1 + sum(node.attempts for node in self.nodes.values())
        blocked_targets = exclude_targets or set()
        sibling_visits: dict[tuple[int, int], tuple[int, int]] = {}
        for node in self.nodes.values():
            key = (node.parent_id, node.site_id)
            visits, outcomes = sibling_visits.get(key, (0, 0))
            sibling_visits[key] = (visits + node.visits, outcomes + 1)
        for node in self.nodes.values():
            if node.status not in self.ACTIVE:
                continue
            if node.branch_id in blocked_targets:
                continue
            if now - node.last_scheduled < cooldown:
                continue
            if not self.constraints.allows(node.branch_id, now):
                continue
            seed = next((path for path in reversed(node.seed_paths)
                         if os.path.isfile(path)), None)
            if seed is None:
                continue
            exploration = math.sqrt(math.log1p(total_attempts) / (1 + node.attempts))
            parent = self.nodes.get(node.parent_id)
            expected_reward = max(
                node.reward, (parent.reward * 0.92 if parent else 0.0))
            directed_bonus = self._directed_weight(node.site_id)
            concurrency_bonus = self._concurrency_weight(node.site_id)
            color_bonus = self._color_score(node)
            data_bonus = self._inherited_signal(node, "data_reward")
            backsolver_bonus = self._inherited_signal(
                node, "backsolver_reward")
            target_path_bonus = self._target_path_score(node)
            cost_pressure = self._inherited_signal(node, "solver_cost")
            timeout_pressure = self._inherited_signal(node, "timeout_penalty")
            visits, outcomes = sibling_visits[(node.parent_id, node.site_id)]
            mutation_probability = (node.visits + 1.0) / (visits + outcomes)
            mutation_rarity = 1.0 - mutation_probability
            age = min(1.0, (now - node.last_scheduled) / max(1.0, cooldown))
            cost_penalty = min(
                1.0,
                0.34 * cost_pressure + 0.42 * timeout_pressure
                + 0.24 * node.mdp_cost,
            )
            # UCT-style frontier scheduling: combine subtree utility with
            # exploration pressure, directed distance, data-comparison progress,
            # and a backoff for historically expensive solver contexts.
            score = (
                0.24 * expected_reward
                + 0.16 * exploration
                + 0.14 * node.difficulty
                + 0.12 * mutation_rarity
                + 0.06 * directed_bonus
                + 0.06 * concurrency_bonus
                + 0.15 * color_bonus
                + 0.10 * data_bonus
                + 0.05 * backsolver_bonus
                + 0.14 * node.path_cover_reward
                + 0.13 * target_path_bonus
                + 0.04 * age
                - 0.16 * cost_penalty
            )
            productive = self._action_productivity(node)
            is_high = (
                productive >= 0.12
                or expected_reward >= 0.18
                or data_bonus >= 0.30
                or color_bonus >= 0.50
                or concurrency_bonus >= 0.50
                or node.path_cover_reward >= 0.20
                or target_path_bonus >= 0.38
                or node.taco_generation_bonus >= 0.35
            )
            should_sample = (
                node.actions["exact"].attempts > 0
                and node.actions["sampling"].attempts < self.sampling_budget
                and node.difficulty >= 0.45
                and max(mutation_rarity, node.path_difficulty) >= 0.45
                and max(expected_reward, target_path_bonus) >= 0.10
                and cost_penalty < 0.55
            )
            action = "sample" if should_sample else "solve"
            (high_candidates if is_high else low_candidates).append(
                (score, node, seed, action))

        high_candidates.sort(key=lambda item: item[0], reverse=True)
        low_candidates.sort(key=lambda item: item[0], reverse=True)
        if high_candidates and low_candidates:
            if limit == 1:
                high_quota = 0 if self.queue_epoch % 4 == 3 else 1
            else:
                high_quota = max(
                    1, min(limit - 1, int(math.ceil(
                        limit * self.high_queue_fraction))))
        else:
            high_quota = limit if high_candidates else 0
        ordered = (high_candidates[:high_quota]
                   + low_candidates[:limit - high_quota]
                   + high_candidates[high_quota:]
                   + low_candidates[limit - high_quota:])

        high_by_seed: dict[str, list[tuple[float, PrefixNode, str, str]]] = {}
        low_by_seed: dict[str, list[tuple[float, PrefixNode, str, str]]] = {}
        for candidate in high_candidates:
            high_by_seed.setdefault(candidate[2], []).append(candidate)
        for candidate in low_candidates:
            low_by_seed.setdefault(candidate[2], []).append(candidate)

        output_limit = max(limit, proposal_limit or limit)
        selected = []
        selected_seeds = set()
        for candidate in ordered:
            seed = candidate[2]
            if seed in selected_seeds:
                continue
            selected.append(candidate)
            selected_seeds.add(seed)
            if len(selected) >= output_limit:
                break
        jobs = []
        for candidate in selected:
            _score, node, seed, selected_action = candidate
            action_sources = (
                high_by_seed.get(seed, ())
                if self.taco_extended_conditions
                and candidate in high_by_seed.get(seed, ())
                else (candidate,)
            )
            actions: list[tuple[int, str]] = []
            seen_actions: set[int] = set()
            for _candidate_score, action_node, _seed, action_name in action_sources:
                if action_node.branch_id in seen_actions:
                    continue
                if len(actions) >= self.action_cap:
                    break
                seen_actions.add(action_node.branch_id)
                actions.append((action_node.branch_id, action_name))
            for _candidate_score, low_node, _seed, _action_name in low_by_seed.get(seed, ()):
                if low_node.branch_id in seen_actions:
                    continue
                if len(actions) >= self.action_cap:
                    break
                seen_actions.add(low_node.branch_id)
                actions.append((low_node.branch_id, "skip"))
            if not actions:
                actions.append((node.branch_id, selected_action))
            primary = next(
                (branch for branch, action in actions if action != "skip"),
                node.branch_id,
            )
            jobs.append(ReplayJob(seed, primary, tuple(actions)))
        if commit:
            self.commit_jobs(jobs, now)
        return jobs

    def commit_jobs(self, jobs: list[ReplayJob], now: float) -> None:
        if jobs:
            self.queue_epoch += len(jobs)
        for job in jobs:
            for branch_id, action in job.actions:
                if action == "skip":
                    continue
                node = self.nodes.get(branch_id)
                if node is None:
                    continue
                node.attempts += 1
                node.last_scheduled = now

    def preferred_strategies(
        self,
        branch_id: int,
        strategy_count: int,
        dual_executor: bool = True,
    ) -> list[int]:
        node = self.nodes.get(branch_id)
        if node is None or strategy_count < 4:
            return list(range(strategy_count))
        exact_action = node.actions["exact"]
        sampling_action = node.actions["sampling"]
        if dual_executor and exact_action.attempts == 0:
            return [0]
        # Reserve multi-solution profiles for difficult, historically useful subtrees.
        parent = self.nodes.get(node.parent_id)
        expected_reward = max(
            node.reward, parent.reward * 0.92 if parent else 0.0)
        directed_bonus = max(self._directed_weight(node.site_id),
                             self._concurrency_weight(node.site_id),
                             self._color_score(node))
        target_path_bonus = self._target_path_score(node)
        data_bonus = self._inherited_signal(node, "data_reward")
        timeout_pressure = self._inherited_signal(node, "timeout_penalty")
        solver_cost = max(self._inherited_signal(node, "solver_cost"),
                          node.mdp_cost)
        siblings = [candidate for candidate in self.nodes.values()
                    if candidate.parent_id == node.parent_id
                    and candidate.site_id == node.site_id]
        mutation_probability = ((node.visits + 1.0)
                                / (sum(candidate.visits for candidate in siblings)
                                   + max(1, len(siblings))))
        optimistic_arm = [5] if strategy_count >= 6 else []
        sampling_arm = (
            [6] if strategy_count >= 7
            and sampling_action.attempts < self.sampling_budget
            else [])
        total_action_attempts = 1 + sum(
            action.attempts for action in node.actions.values())
        action_scores = {
            name: action.score(total_action_attempts)
            for name, action in node.actions.items()
        }
        if max(timeout_pressure, solver_cost) >= 0.55:
            # Retry expensive prefixes with the cheap exact/fast profiles and the
            # optimistic arm instead of immediately spending the sampling budget.
            arms = [0, 1] + optimistic_arm
        elif directed_bonus >= 0.50 or data_bonus >= 0.35 \
                or target_path_bonus >= 0.40:
            arms = [0, 3, 4] if strategy_count >= 5 else [0, 3]
            arms += sampling_arm + optimistic_arm
        elif (mutation_probability <= 0.20 and node.difficulty >= 0.45
              and max(expected_reward, target_path_bonus) >= 0.10):
            arms = [0, 3, 4] if strategy_count >= 5 else [0, 3]
            arms += sampling_arm + optimistic_arm
        elif node.difficulty >= 0.30 or node.path_difficulty >= 0.35:
            arms = [0, 1, 2, 4] if strategy_count >= 5 else [0, 1, 2]
            arms += sampling_arm + optimistic_arm
        else:
            arms = [0, 1]

        # The global portfolio selects within this list.  Ordering locally by
        # per-prefix action efficiency turns each prefix into an action-seed
        # subtree without multiplying DAG nodes.
        deduplicated = list(dict.fromkeys(
            arm for arm in arms if 0 <= arm < strategy_count))
        return sorted(
            deduplicated,
            key=lambda arm: action_scores[self._strategy_action(arm)],
            reverse=True,
        )

    def to_mapping(self) -> dict[str, Any]:
        nodes = []
        for node in self.nodes.values():
            item = vars(node).copy()
            item["seed_paths"] = list(node.seed_paths)
            item["actions"] = {
                name: vars(action).copy()
                for name, action in node.actions.items()
            }
            nodes.append(item)
        return {"max_nodes": self.max_nodes, "nodes": nodes,
                "directed_sites": sorted(self.directed_sites),
                "directed_distance_sites": len(self.directed_distances),
                "concurrency_guidance_sites": len(self.concurrency_distances),
                "s2f_sampling_budget": self.sampling_budget,
                "s2f_high_queue_fraction": self.high_queue_fraction,
                "s2f_actions_per_seed": self.action_cap,
                "s2f_queue_epoch": self.queue_epoch,
                "taco_enabled": self.taco_enabled,
                "taco_extended_conditions": self.taco_extended_conditions,
                "multigo_enabled": self.multigo_enabled,
                "multigo_poisson_scale": self.multigo_poisson_scale,
                "multigo_explore_fraction": self.multigo_explore_fraction,
                "multigo_site_frequency": sorted(
                    self.site_frequency.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:4096],
                "multigo_total_site_frequency": self.total_site_frequency,
                "dynamic_coloration": self.dynamic_coloration_enabled,
                "colorgo_gamma": self.mdp_gamma,
                "colorgo_min_feasibility": self.min_feasibility,
                "selective_mdp_iterations": self.selective_mdp_iterations,
                "selective_mdp_tolerance": self.selective_mdp_tolerance,
                "selective_mdp_last_iterations":
                    self.selective_mdp_last_iterations,
                "selective_mdp_last_residual":
                    self.selective_mdp_last_residual,
                "selective_mdp_transition_groups":
                    self.selective_mdp_transition_groups,
                "selective_mdp_refresh_interval":
                    self.selective_mdp_refresh_interval,
                "selective_mdp_small_graph_nodes":
                    self.selective_mdp_small_graph_nodes,
                "selective_mdp_refreshes": self.selective_mdp_refreshes,
                "selective_mdp_skipped_refreshes":
                    self.selective_mdp_skipped_refreshes,
                "constraints": self.constraints.to_mapping()}

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        nodes = raw.get("nodes")
        valid_statuses = self.ACTIVE | {"observed", "resolved", "sat", "unsat"}
        now = time.monotonic()
        if isinstance(nodes, list):
            for item in nodes[-self.max_nodes:]:
                if not isinstance(item, dict):
                    continue
                branch_id = _nonnegative_int(item.get("branch_id"))
                if not branch_id:
                    continue
                if item.get("status") == "stale":
                    item = dict(item)
                    item["status"] = "diverged"
                paths = item.get("seed_paths", ())
                if not isinstance(paths, (list, tuple)):
                    paths = ()
                action_items = item.get("actions", {})
                actions = _new_s2f_actions()
                if isinstance(action_items, dict):
                    for name, action_item in action_items.items():
                        if name not in actions or not isinstance(
                                action_item, dict):
                            continue
                        action = actions[name]
                        action.attempts = _nonnegative_int(
                            action_item.get("attempts"))
                        action.successes = min(
                            action.attempts,
                            _nonnegative_int(action_item.get("successes")))
                        action.reward_sum = _nonnegative_float(
                            action_item.get("reward_sum"))
                        action.cost_sum = _nonnegative_float(
                            action_item.get("cost_sum"))
                        action.consecutive_failures = _nonnegative_int(
                            action_item.get("consecutive_failures"))
                        action.last_status = str(
                            action_item.get("last_status", "untried"))
                try:
                    last_update = max(0.0, float(item.get("last_update", 0.0)))
                    last_scheduled = max(0.0, float(item.get("last_scheduled", 0.0)))
                    if last_update > now + 3600.0:
                        last_update = now
                    if last_scheduled > now + 3600.0:
                        last_scheduled = 0.0
                    status = str(item.get("status", "observed"))
                    if status not in valid_statuses:
                        status = "observed"
                    self.nodes[branch_id] = PrefixNode(
                        branch_id=branch_id,
                        parent_id=_nonnegative_int(item.get("parent_id")),
                        site_id=_nonnegative_int(item.get("site_id")),
                        outcome=int(bool(item.get("outcome"))),
                        status=status,
                        visits=_nonnegative_int(item.get("visits")),
                        attempts=_nonnegative_int(item.get("attempts")),
                        reward=_clamp01(float(item.get("reward", 0.0))),
                        difficulty=_clamp01(float(item.get("difficulty", 0.0))),
                        data_reward=_clamp01(
                            float(item.get("data_reward", 0.0))),
                        backsolver_reward=_clamp01(
                            float(item.get("backsolver_reward", 0.0))),
                        path_cover_reward=_clamp01(
                            float(item.get("path_cover_reward", 0.0))),
                        solver_cost=_clamp01(
                            float(item.get("solver_cost", 0.0))),
                        timeout_penalty=_clamp01(
                            float(item.get("timeout_penalty", 0.0))),
                        path_difficulty=_clamp01(
                            float(item.get("path_difficulty", 0.0))),
                        target_distance=_clamp01(
                            float(item.get("target_distance", 0.0))),
                        target_path_reward=_clamp01(
                            float(item.get("target_path_reward", 0.0))),
                        target_path_visits=_nonnegative_int(
                            item.get("target_path_visits")),
                        taco_generation_bonus=_clamp01(
                            float(item.get("taco_generation_bonus", 0.0))),
                        color_feasibility=_clamp01(
                            float(item.get("color_feasibility", 0.0))),
                        mdp_value=_clamp01(
                            float(item.get("mdp_value", 0.0))),
                        mdp_cost=_clamp01(
                            float(item.get("mdp_cost", 0.0))),
                        mdp_transition_probability=_clamp01(
                            float(item.get(
                                "mdp_transition_probability", 0.0))),
                        mdp_novelty_reward=_clamp01(
                            float(item.get("mdp_novelty_reward", 0.0))),
                        infeasible_streak=_nonnegative_int(
                            item.get("infeasible_streak")),
                        last_update=last_update,
                        last_scheduled=last_scheduled,
                        seed_paths=tuple(str(path) for path in paths[-4:]),
                        actions=actions,
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
        self.queue_epoch = _nonnegative_int(raw.get("s2f_queue_epoch"))
        raw_frequency = raw.get("multigo_site_frequency")
        if isinstance(raw_frequency, list):
            for item in raw_frequency[-self.site_frequency_cap:]:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                site = _nonnegative_int(item[0])
                count = _nonnegative_int(item[1])
                if site and count:
                    self.site_frequency[site] = count
        self._site_frequency_heap = [
            (count, site) for site, count in self.site_frequency.items()
        ]
        heapq.heapify(self._site_frequency_heap)
        restored_total = _nonnegative_int(raw.get("multigo_total_site_frequency"))
        self.total_site_frequency = max(
            restored_total, sum(self.site_frequency.values()))
        self.constraints.restore(raw.get("constraints"))
        self._refresh_mdp_values()


class DataCoverageTracker:
    """Code/data dominance corpus with conservative refinement tracking."""

    def __init__(
        self,
        max_comparison_entries: int = 16384,
        max_static_entries: int = 65536,
    ) -> None:
        self.max_comparison_entries = max(
            128,
            min(
                _MAX_ADAPTIVE_STATE_ENTRIES,
                _nonnegative_int(max_comparison_entries),
            ),
        )
        self.max_static_entries = max(
            128,
            min(
                _MAX_ADAPTIVE_STATE_ENTRIES,
                _nonnegative_int(max_static_entries),
            ),
        )
        self.best: dict[int, tuple[int, int, str]] = {}
        self.static_best: dict[
            tuple[int, int, int], tuple[int, str, int, int]
        ] = {}
        self._path_quality: dict[str, tuple[float, int]] = {}
        self.refinements = 0

    def _adjust_path_quality(
        self,
        path: str,
        matched: int,
        width: int,
        direction: int,
    ) -> None:
        quality, count = self._path_quality.get(path, (0.0, 0))
        quality += direction * matched / width
        count += direction
        if count <= 0:
            self._path_quality.pop(path, None)
        else:
            self._path_quality[path] = (max(0.0, quality), count)

    def _set_comparison(
        self,
        feature_id: int,
        value: tuple[int, int, str],
    ) -> None:
        previous = self.best.pop(feature_id, None)
        if previous is not None:
            self._adjust_path_quality(previous[2], previous[0], previous[1], -1)
        elif len(self.best) >= self.max_comparison_entries:
            evicted_id = next(iter(self.best))
            evicted = self.best.pop(evicted_id)
            self._adjust_path_quality(evicted[2], evicted[0], evicted[1], -1)
        self.best[feature_id] = value
        self._adjust_path_quality(value[2], value[0], value[1], 1)

    def _set_static(
        self,
        key: tuple[int, int, int],
        value: tuple[int, str, int, int],
    ) -> None:
        previous = self.static_best.pop(key, None)
        if previous is not None:
            self._adjust_path_quality(previous[1], previous[0], key[2], -1)
        elif len(self.static_best) >= self.max_static_entries:
            evicted_key = next(iter(self.static_best))
            evicted = self.static_best.pop(evicted_key)
            self._adjust_path_quality(
                evicted[1], evicted[0], evicted_key[2], -1
            )
        self.static_best[key] = value
        self._adjust_path_quality(value[1], value[0], key[2], 1)

    def observe(
        self,
        path: str,
        features: tuple[tuple[int, int, int], ...],
        *,
        static_features: tuple[tuple[int, int, int, int, int], ...] = (),
        code_summary: int = 0,
    ) -> int:
        delta = 0
        for raw_feature_id, raw_matched, raw_width in features:
            feature_id = _nonnegative_int(raw_feature_id)
            matched = _nonnegative_int(raw_matched)
            width = _nonnegative_int(raw_width)
            if not feature_id or not 0 < matched <= width <= 64:
                continue
            previous = self.best.get(feature_id)
            previous_bits = previous[0] if previous else 0
            if matched > previous_bits:
                delta += matched - previous_bits
                self._set_comparison(feature_id, (matched, width, path))
        for raw_object_id, raw_offset, raw_matched, raw_width, raw_kind in (
            static_features
        ):
            object_id = _nonnegative_int(raw_object_id)
            offset = _nonnegative_int(raw_offset)
            matched = _nonnegative_int(raw_matched)
            width = _nonnegative_int(raw_width)
            kind = min(2, _nonnegative_int(raw_kind))
            if not object_id or not 0 < matched <= width <= (1 << 32) - 1:
                continue
            key = (object_id, offset, width)
            previous = self.static_best.get(key)
            previous_bits = previous[0] if previous else 0
            if matched <= previous_bits:
                continue
            delta += matched - previous_bits
            if (previous is not None and previous[1] != path and
                    previous[2] != 0 and previous[2] == code_summary):
                self.refinements += 1
            self._set_static(key, (matched, path, code_summary, kind))
        return delta

    def path_bonus(self, path: str) -> float:
        quality_sum, winner_count = self._path_quality.get(path, (0.0, 0))
        if winner_count <= 0:
            return 0.0
        quality = quality_sum / winner_count
        return min(
            1.0,
            quality * (1.0 + math.log1p(winner_count) / 4.0),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 3,
            "comparison": [
                [feature_id, matched, width, path]
                for feature_id, (matched, width, path) in self.best.items()
            ],
            "static": [
                [object_id, offset, matched, width, path, summary, kind]
                for (object_id, offset, width),
                (matched, path, summary, kind)
                in self.static_best.items()
            ],
            "refinements": self.refinements,
        }

    def restore(self, raw: Any) -> None:
        comparison = raw
        static = ()
        if isinstance(raw, dict):
            comparison = raw.get("comparison", ())
            static = raw.get("static", ())
            self.refinements = _nonnegative_int(raw.get("refinements"))
        if not isinstance(comparison, list):
            return
        for item in comparison[-self.max_comparison_entries:]:
            if not isinstance(item, (list, tuple)) or len(item) != 4:
                continue
            feature_id, matched, width = (_nonnegative_int(value)
                                          for value in item[:3])
            if feature_id and 0 < width <= 64 and matched <= width:
                self._set_comparison(
                    feature_id, (matched, width, str(item[3]))
                )
        if not isinstance(static, list):
            return
        for item in static[-self.max_static_entries:]:
            if not isinstance(item, (list, tuple)) or len(item) != 7:
                continue
            object_id, offset, matched, width = (
                _nonnegative_int(value) for value in item[:4])
            summary = _nonnegative_int(item[5])
            kind = min(2, _nonnegative_int(item[6]))
            if object_id and 0 < matched <= width <= (1 << 32) - 1:
                self._set_static(
                    (object_id, offset, width),
                    (matched, str(item[4]), summary, kind),
                )


@dataclass
class ParetoCorpusEntry:
    path: str
    visits: int = 0
    edge_features: int = 0
    data_bits: int = 0
    string_queries: int = 0
    string_verified: int = 0
    path_hash: int = 0
    structural_sites: int = 0
    reward_ema: float = 0.0
    cost_ema: float = 0.0
    last_observation: int = 0


class ParetoCorpusArchive:
    """Bounded epsilon-Pareto metadata archive for seed scheduling."""

    DIMENSIONS = 6

    def __init__(self, max_entries: int = 4096, grid_bins: int = 8) -> None:
        self.max_entries = max(8, min(65536, int(max_entries)))
        self.grid_bins = max(2, min(32, int(grid_bins)))
        self.entries: dict[str, ParetoCorpusEntry] = {}
        self.observations = 0
        self.admissions = 0
        self.rejections = 0
        self.replacements = 0
        self.dominance_evictions = 0
        self.density_evictions = 0

    @staticmethod
    def _dominates(
        left: tuple[float, ...],
        right: tuple[float, ...],
    ) -> bool:
        epsilon = 1e-12
        return (
            all(a + epsilon >= b for a, b in zip(left, right)) and
            any(a > b + epsilon for a, b in zip(left, right))
        )

    def objectives(self, entry: ParetoCorpusEntry) -> tuple[float, ...]:
        string_yield = (
            entry.string_verified + 0.5
        ) / (entry.string_queries + 1.0)
        efficiency = entry.reward_ema / (
            1.0 + math.log1p(max(0.0, entry.cost_ema)))
        return (
            1.0 - math.exp(-entry.edge_features / 8.0),
            1.0 - math.exp(-entry.data_bits / 16.0),
            _clamp01(string_yield),
            1.0 - math.exp(-entry.structural_sites / 16.0),
            float(bool(entry.path_hash)),
            _clamp01(efficiency),
        )

    def _cell(self, objectives: tuple[float, ...]) -> tuple[int, ...]:
        return tuple(
            min(
                self.grid_bins - 1,
                int(_clamp01(value) * self.grid_bins),
            )
            for value in objectives
        )

    def _density_victim(self) -> str:
        objective_map = {
            path: self.objectives(entry)
            for path, entry in self.entries.items()
        }
        protected: set[str] = set()
        for dimension in range(self.DIMENSIONS):
            protected.add(max(
                self.entries,
                key=lambda path: (
                    objective_map[path][dimension], path),
            ))
        cells: dict[tuple[int, ...], list[str]] = {}
        for path, objectives in objective_map.items():
            if path in protected:
                continue
            cells.setdefault(self._cell(objectives), []).append(path)
        candidates = (
            max(
                cells.values(),
                key=lambda paths: (
                    len(paths),
                    tuple(sorted(paths)),
                ),
            )
            if cells else list(self.entries)
        )
        return min(
            candidates,
            key=lambda path: (
                sum(objective_map[path]),
                self.entries[path].last_observation,
                path,
            ),
        )

    def observe(
        self,
        path: str,
        *,
        coverage_delta: int,
        data_delta: int,
        reward: float,
        elapsed: float,
        telemetry: SolverTelemetry | None,
    ) -> bool:
        self.observations = min((1 << 63) - 1, self.observations + 1)
        existing = self.entries.get(path)
        is_new = existing is None
        entry = existing or ParetoCorpusEntry(path)
        entry.visits = min((1 << 31) - 1, entry.visits + 1)
        entry.edge_features = min(
            (1 << 31) - 1,
            entry.edge_features + max(0, int(coverage_delta)),
        )
        entry.data_bits = min(
            (1 << 31) - 1,
            entry.data_bits + max(0, int(data_delta)),
        )
        string_queries = (
            max(0, int(telemetry.string_solver_queries))
            if telemetry else 0)
        string_verified = (
            min(
                string_queries,
                max(0, int(telemetry.string_solver_verified)),
            )
            if telemetry else 0)
        entry.string_queries = min(
            (1 << 31) - 1, entry.string_queries + string_queries)
        entry.string_verified = min(
            entry.string_queries,
            entry.string_verified + string_verified,
        )
        if telemetry:
            entry.path_hash = int(telemetry.path_hash) or entry.path_hash
            entry.structural_sites = max(
                entry.structural_sites,
                len({
                    int(item[3])
                    for item in telemetry.branch_trace
                    if len(item) >= 4 and int(item[3]) > 0
                }),
            )
        entry.reward_ema = (
            _clamp01(reward) if entry.visits == 1 else
            0.8 * entry.reward_ema + 0.2 * _clamp01(reward)
        )
        entry.cost_ema = (
            max(0.0, elapsed) if entry.visits == 1 else
            0.8 * entry.cost_ema + 0.2 * max(0.0, elapsed)
        )
        entry.last_observation = self.observations
        if not is_new:
            self.entries[path] = entry
            return True

        candidate_objectives = self.objectives(entry)
        dominated = []
        for other_path, other in self.entries.items():
            other_objectives = self.objectives(other)
            if self._dominates(other_objectives, candidate_objectives):
                self.rejections += 1
                return False
            if self._dominates(candidate_objectives, other_objectives):
                dominated.append(other_path)
        for other_path in dominated:
            del self.entries[other_path]
            self.replacements += 1
            self.dominance_evictions += 1
        self.entries[path] = entry
        self.admissions += 1
        while len(self.entries) > self.max_entries:
            victim = self._density_victim()
            del self.entries[victim]
            self.replacements += 1
            self.density_evictions += 1
        return path in self.entries

    def priority(self, path: str) -> float:
        entry = self.entries.get(path)
        if entry is None:
            return 0.0
        objectives = self.objectives(entry)
        # Scheduling consumes archive membership and diversity; admission itself
        # remains governed solely by Pareto dominance and epsilon density.
        return _clamp01(
            0.5 * max(objectives) +
            0.5 * sum(objectives) / len(objectives)
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "max_entries": self.max_entries,
            "grid_bins": self.grid_bins,
            "observations": self.observations,
            "admissions": self.admissions,
            "rejections": self.rejections,
            "replacements": self.replacements,
            "dominance_evictions": self.dominance_evictions,
            "density_evictions": self.density_evictions,
            "entries": [
                {
                    "path": entry.path,
                    "visits": entry.visits,
                    "edge_features": entry.edge_features,
                    "data_bits": entry.data_bits,
                    "string_queries": entry.string_queries,
                    "string_verified": entry.string_verified,
                    "path_hash": entry.path_hash,
                    "structural_sites": entry.structural_sites,
                    "reward_ema": entry.reward_ema,
                    "cost_ema": entry.cost_ema,
                    "last_observation": entry.last_observation,
                }
                for entry in sorted(
                    self.entries.values(), key=lambda item: item.path)
            ],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return
        self.observations = min(
            (1 << 63) - 1, _nonnegative_int(raw.get("observations")))
        self.admissions = _nonnegative_int(raw.get("admissions"))
        self.rejections = _nonnegative_int(raw.get("rejections"))
        self.replacements = _nonnegative_int(raw.get("replacements"))
        self.dominance_evictions = _nonnegative_int(
            raw.get("dominance_evictions"))
        self.density_evictions = _nonnegative_int(
            raw.get("density_evictions"))
        entries = raw.get("entries", ())
        if not isinstance(entries, list):
            return
        restored: dict[str, ParetoCorpusEntry] = {}
        for item in entries[-self.max_entries:]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", ""))
            try:
                reward = float(item.get("reward_ema", 0.0))
                cost = float(item.get("cost_ema", 0.0))
            except (TypeError, ValueError, OverflowError):
                continue
            queries = _nonnegative_int(item.get("string_queries"))
            verified = _nonnegative_int(item.get("string_verified"))
            last_observation = _nonnegative_int(
                item.get("last_observation"))
            if (
                not path or len(path) > 4096 or
                not math.isfinite(reward) or not 0.0 <= reward <= 1.0 or
                not math.isfinite(cost) or cost < 0.0 or
                verified > queries or
                last_observation > self.observations
            ):
                continue
            restored[path] = ParetoCorpusEntry(
                path=path,
                visits=min(
                    (1 << 31) - 1,
                    _nonnegative_int(item.get("visits"))),
                edge_features=min(
                    (1 << 31) - 1,
                    _nonnegative_int(item.get("edge_features"))),
                data_bits=min(
                    (1 << 31) - 1,
                    _nonnegative_int(item.get("data_bits"))),
                string_queries=min((1 << 31) - 1, queries),
                string_verified=min((1 << 31) - 1, verified),
                path_hash=_nonnegative_int(item.get("path_hash")),
                structural_sites=min(
                    (1 << 31) - 1,
                    _nonnegative_int(item.get("structural_sites"))),
                reward_ema=reward,
                cost_ema=cost,
                last_observation=last_observation,
            )
        self.entries = restored


class LinUCBModel:
    """Small dependency-free contextual bandit using Sherman-Morrison updates."""

    def __init__(self, dimension: int, alpha: float = 0.65, ridge: float = 1.0):
        self.dimension = max(1, min(1024, _nonnegative_int(dimension)))
        self.alpha = min(10.0, max(0.0, _finite_float(alpha, 0.65)))
        ridge = _finite_float(ridge, 1.0)
        if ridge <= 1e-12:
            ridge = 1.0
        self.a_inv = [
            [1.0 / ridge if i == j else 0.0
             for j in range(self.dimension)]
            for i in range(self.dimension)
        ]
        self.b = [0.0] * self.dimension
        self.observations = 0

    def _matvec(self, vector: tuple[float, ...] | list[float]) -> list[float]:
        return [sum(row[j] * vector[j] for j in range(self.dimension))
                for row in self.a_inv]

    def score(self, vector: tuple[float, ...]) -> tuple[float, float]:
        if (
            len(vector) != self.dimension
            or any(not math.isfinite(value) for value in vector)
        ):
            return 0.0, 0.0
        ax = self._matvec(vector)
        theta = self._matvec(self.b)
        mean = sum(theta[i] * vector[i] for i in range(self.dimension))
        uncertainty = math.sqrt(max(0.0, sum(vector[i] * ax[i]
                                             for i in range(self.dimension))))
        return mean + self.alpha * uncertainty, uncertainty

    def update(self, vector: tuple[float, ...], reward: float) -> None:
        if (
            len(vector) != self.dimension
            or any(not math.isfinite(value) for value in vector)
        ):
            return
        reward = _finite_float(reward, -1.0)
        if reward < 0.0:
            return
        ax = self._matvec(vector)
        denominator = 1.0 + sum(vector[i] * ax[i] for i in range(self.dimension))
        if not math.isfinite(denominator) or denominator <= 1e-12:
            return
        scaled_reward = _clamp01(reward)
        updated_matrix = [
            [
                self.a_inv[i][j] - ax[i] * ax[j] / denominator
                for j in range(self.dimension)
            ]
            for i in range(self.dimension)
        ]
        updated_b = [
            self.b[i] + scaled_reward * vector[i]
            for i in range(self.dimension)
        ]
        if (
            any(not math.isfinite(value) for row in updated_matrix for value in row)
            or any(not math.isfinite(value) for value in updated_b)
        ):
            return
        self.a_inv = updated_matrix
        self.b = updated_b
        self.observations += 1

    def to_mapping(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "alpha": self.alpha,
            "a_inv": self.a_inv,
            "b": self.b,
            "observations": self.observations,
        }

    def restore(self, raw: dict[str, Any]) -> None:
        if raw.get("dimension") != self.dimension:
            return
        matrix = raw.get("a_inv")
        vector = raw.get("b")
        if not isinstance(matrix, list) or len(matrix) != self.dimension:
            return
        if not isinstance(vector, list) or len(vector) != self.dimension:
            return
        try:
            restored = [[float(value) for value in row] for row in matrix]
            if any(len(row) != self.dimension for row in restored):
                return
            restored_b = [float(value) for value in vector]
        except (TypeError, ValueError, OverflowError):
            return
        if (
            any(not math.isfinite(value) for row in restored for value in row)
            or any(not math.isfinite(value) for value in restored_b)
            or any(restored[index][index] <= 0.0
                   for index in range(self.dimension))
            or any(
                not math.isclose(
                    restored[i][j],
                    restored[j][i],
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
                for i in range(self.dimension)
                for j in range(i)
            )
        ):
            return
        self.a_inv = restored
        self.b = restored_b
        self.observations = _nonnegative_int(raw.get("observations"))


@dataclass
class SeedWorkerProfile:
    """Observed execution shape for one seed."""

    path_hash: int = 0
    sites: set[int] = field(default_factory=set)
    regions: set[int] = field(default_factory=set)
    observations: int = 0
    reward_ema: float = 0.0
    cost_ema: float = 0.0


@dataclass
class WorkerExplorationState:
    """Bounded worker-local exploration state used by pair scheduling."""

    executions: int = 0
    productive_runs: int = 0
    coverage_gain: int = 0
    cross_learning_gain: int = 0
    reward_ema: float = 0.0
    cost_ema: float = 0.0
    last_update: float = 0.0
    seen_sites: set[int] = field(default_factory=set)
    seen_paths: set[int] = field(default_factory=set)
    region_reward: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveSeedWorkerAssignment:
    path: str
    focus: str
    target_branch: int
    task_region: int
    vector: tuple[float, ...]


@dataclass(frozen=True)
class SeedWorkerFeedback:
    worker: int
    vector: tuple[float, ...]
    base_reward: float
    coverage_delta: int
    cross_learning_delta: int
    interesting_cases: int
    elapsed: float


class SeedWorkerBandit:
    """SimiFuzz-style seed-worker contextual assignment policy.

    The global seed model answers whether a seed is useful in general.  This
    model answers which worker should receive it, based on worker-local
    exploration state and the overlap with assignments already in flight.
    Feedback is committed in time slices so one lucky execution does not
    immediately dominate the pair policy.
    """

    SEED_DIM = 14
    WORKER_DIM = 6
    INTERACTION_DIM = 6
    DIMENSION = SEED_DIM + WORKER_DIM + INTERACTION_DIM

    def __init__(
        self,
        enabled: bool = True,
        max_profiles: int = 16384,
        max_workers: int = 65536,
    ) -> None:
        self.enabled = enabled
        try:
            alpha = float(os.environ.get("SYMCC_SIMIFUZZ_ALPHA", "0.55"))
        except ValueError:
            alpha = 0.55
        self.slice_seconds = min(
            3600.0,
            max(
                1.0,
                _finite_float(
                    os.environ.get("SYMCC_SIMIFUZZ_SLICE", "30"),
                    30.0,
                ),
            ),
        )
        self.max_profiles = max(
            128,
            min(_MAX_ADAPTIVE_STATE_ENTRIES, _nonnegative_int(max_profiles)),
        )
        self.max_workers = max(
            128,
            min(65536, _nonnegative_int(max_workers)),
        )
        self.max_pending = 65536
        self.model = LinUCBModel(
            self.DIMENSION,
            alpha=min(10.0, max(0.0, _finite_float(alpha, 0.55))),
        )
        self.workers: dict[int, WorkerExplorationState] = {}
        self.seeds: dict[str, SeedWorkerProfile] = {}
        self.active: dict[int, ActiveSeedWorkerAssignment] = {}
        self.pending: list[SeedWorkerFeedback] = []
        self.global_seen_sites: set[int] = set()
        self.slice_start = 0.0
        self.slices = 0
        self.assignments = 0
        self.feedback_count = 0
        self.global_coverage_gain = 0
        self.cross_learning_gain = 0

    @staticmethod
    def _squash(value: float, scale: float) -> float:
        return 1.0 - math.exp(-max(0.0, value) / max(1e-6, scale))

    def _worker(self, worker: int) -> WorkerExplorationState:
        worker = _nonnegative_int(worker)
        state = self.workers.pop(worker, None)
        if state is None:
            state = WorkerExplorationState()
        _bounded_mapping_set(self.workers, worker, state, self.max_workers)
        return state

    def _active_similarity(
        self,
        path: str,
        focus: str,
        target_branch: int,
        profile: SeedWorkerProfile | None,
    ) -> float:
        similarity = 0.0
        for assignment in self.active.values():
            if assignment.path == path:
                if (assignment.focus == focus
                        and assignment.target_branch == target_branch):
                    similarity = max(similarity, 1.0)
                elif assignment.focus and focus and assignment.focus != focus:
                    similarity = max(similarity, 0.25)
                else:
                    similarity = max(similarity, 0.65)
                continue
            other = self.seeds.get(assignment.path)
            if not profile or not other or not profile.sites or not other.sites:
                continue
            union = profile.sites | other.sites
            if union:
                similarity = max(
                    similarity, len(profile.sites & other.sites) / len(union))
        return _clamp01(similarity)

    def _pair_vector(
        self,
        worker: int,
        context: CandidateContext,
        *,
        focus: str = "",
        target_branch: int = 0,
        task_region: int = 0,
    ) -> tuple[float, ...]:
        seed_vector = tuple(context.vector[:self.SEED_DIM])
        if len(seed_vector) < self.SEED_DIM:
            seed_vector += (0.0,) * (self.SEED_DIM - len(seed_vector))
        state = self._worker(worker)
        profile = self.seeds.get(context.path)
        yield_rate = state.productive_runs / max(1, state.executions)
        efficiency = state.reward_ema / (
            1.0 + math.log1p(max(0.0, state.cost_ema)))
        global_sites = max(1, len(self.global_seen_sites))
        saturation = min(1.0, len(state.seen_sites) / global_sites)
        age = (
            1.0 if state.last_update <= 0.0
            else min(1.0, max(0.0, time.monotonic() - state.last_update)
                     / max(1.0, self.slice_seconds)))
        worker_vector = (
            self._squash(state.executions, 16.0),
            _clamp01(yield_rate),
            _clamp01(state.reward_ema),
            _clamp01(efficiency),
            saturation,
            age,
        )

        path_novelty = 1.0
        site_novelty = 1.0
        cross_learning = 0.0
        region_affinity = 0.5
        if profile is not None:
            if profile.path_hash:
                path_novelty = float(profile.path_hash not in state.seen_paths)
            if profile.sites:
                missing = profile.sites - state.seen_sites
                site_novelty = len(missing) / len(profile.sites)
                cross_learning = len(missing & self.global_seen_sites) / len(
                    profile.sites)
        if task_region:
            region_affinity = _clamp01(
                state.region_reward.get(task_region, 0.5))
        active_similarity = self._active_similarity(
            context.path, focus, target_branch, profile)
        interaction_vector = (
            _clamp01(path_novelty),
            _clamp01(site_novelty),
            _clamp01(cross_learning),
            region_affinity,
            float(bool(target_branch)),
            1.0 - active_similarity,
        )
        vector = seed_vector + worker_vector + interaction_vector
        assert len(vector) == self.DIMENSION
        return vector

    def score(
        self,
        worker: int,
        context: CandidateContext,
        *,
        focus: str = "",
        target_branch: int = 0,
        task_region: int = 0,
    ) -> float:
        if not self.enabled:
            return 0.0
        vector = self._pair_vector(
            worker, context, focus=focus, target_branch=target_branch,
            task_region=task_region)
        learned, _ = self.model.score(vector)
        interaction = vector[-self.INTERACTION_DIM:]
        # Stable cold-start prior. Learned pair utility dominates after enough
        # observations, while duplicate in-flight work remains disfavored.
        prior = (
            0.16 * interaction[0]
            + 0.20 * interaction[1]
            + 0.18 * interaction[2]
            + 0.12 * interaction[3]
            + 0.08 * interaction[4]
            + 0.26 * interaction[5]
        )
        return learned + prior

    def reserve(
        self,
        worker: int,
        context: CandidateContext,
        *,
        focus: str = "",
        target_branch: int = 0,
        task_region: int = 0,
    ) -> None:
        if not self.enabled:
            return
        worker = _nonnegative_int(worker)
        vector = self._pair_vector(
            worker, context, focus=focus, target_branch=target_branch,
            task_region=task_region)
        _bounded_mapping_set(
            self.active,
            worker,
            ActiveSeedWorkerAssignment(
                context.path,
                focus,
                _nonnegative_int(target_branch),
                _nonnegative_int(task_region),
                vector,
            ),
            self.max_workers,
        )
        self.assignments += 1

    def release(self, worker: int) -> bool:
        """Undo an assignment that was reserved but never dispatched."""
        if not self.enabled:
            return False
        worker = _nonnegative_int(worker)
        if self.active.pop(worker, None) is None:
            return False
        self.assignments = max(0, self.assignments - 1)
        return True

    def discard(self, worker: int) -> bool:
        """Retire an executed assignment without learning from its result."""
        if not self.enabled:
            return False
        return self.active.pop(_nonnegative_int(worker), None) is not None

    def observe(
        self,
        worker: int,
        context: CandidateContext,
        *,
        base_reward: float,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        telemetry: SolverTelemetry | None,
        task_region: int = 0,
        now: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        now = time.monotonic() if now is None else now
        if self.slice_start and now - self.slice_start >= self.slice_seconds:
            self.flush(now=now)
        if not self.slice_start:
            self.slice_start = now

        worker = _nonnegative_int(worker)
        state = self._worker(worker)
        assignment = self.active.pop(worker, None)
        vector = (
            assignment.vector if assignment is not None
            else self._pair_vector(
                worker, context, target_branch=(
                    telemetry.target_branch if telemetry else 0),
                task_region=task_region)
        )
        sites = {
            entry[3] for entry in (telemetry.branch_trace if telemetry else ())
            if len(entry) >= 4 and entry[3]
        }
        path_hash = telemetry.path_hash if telemetry else 0
        local_new = sites - state.seen_sites
        global_new = sites - self.global_seen_sites
        cross_learning = len(local_new - global_new)

        state.executions += 1
        state.productive_runs += int(
            coverage_delta > 0 or interesting_cases > 0)
        state.coverage_gain += max(0, coverage_delta)
        state.cross_learning_gain += cross_learning
        state.reward_ema = (
            base_reward if state.executions == 1
            else 0.82 * state.reward_ema + 0.18 * base_reward)
        state.cost_ema = (
            max(0.0, elapsed) if state.executions == 1
            else 0.82 * state.cost_ema + 0.18 * max(0.0, elapsed))
        state.last_update = now
        state.seen_sites.update(sites)
        if path_hash:
            state.seen_paths.add(path_hash)
        region = (
            assignment.task_region if assignment is not None
            else _nonnegative_int(task_region))
        if region:
            old = state.region_reward.get(region, 0.5)
            _bounded_mapping_set(
                state.region_reward,
                region,
                _clamp01(0.82 * old + 0.18 * base_reward),
                4096,
            )

        self.global_seen_sites.update(sites)
        _trim_set(self.global_seen_sites, 65536)
        _trim_set(state.seen_sites, 32768)
        _trim_set(state.seen_paths, 16384)
        profile = self.seeds.get(context.path)
        if profile is None:
            profile = SeedWorkerProfile()
            _bounded_mapping_set(
                self.seeds,
                context.path,
                profile,
                self.max_profiles,
            )
        else:
            self.seeds.pop(context.path)
            self.seeds[context.path] = profile
        profile.path_hash = path_hash or profile.path_hash
        profile.sites.update(sites)
        _trim_set(profile.sites, 512)
        if region:
            profile.regions.add(region)
            _trim_set(profile.regions, 64)
        profile.observations += 1
        profile.reward_ema = (
            base_reward if profile.observations == 1
            else 0.82 * profile.reward_ema + 0.18 * base_reward)
        profile.cost_ema = (
            max(0.0, elapsed) if profile.observations == 1
            else 0.82 * profile.cost_ema + 0.18 * max(0.0, elapsed))

        if len(self.pending) >= self.max_pending:
            self.flush(now=now, force=True)
        self.pending.append(SeedWorkerFeedback(
            worker=worker,
            vector=vector,
            base_reward=_clamp01(base_reward),
            coverage_delta=max(0, coverage_delta),
            cross_learning_delta=cross_learning,
            interesting_cases=max(0, interesting_cases),
            elapsed=max(0.001, elapsed),
        ))
        self.feedback_count += 1
        self.global_coverage_gain += max(0, coverage_delta)
        self.cross_learning_gain += cross_learning

    def flush(
        self,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> int:
        if not self.enabled or not self.pending:
            return 0
        now = time.monotonic() if now is None else now
        if (not force and self.slice_start
                and now - self.slice_start < self.slice_seconds):
            return 0

        by_worker: dict[int, list[SeedWorkerFeedback]] = {}
        for item in self.pending:
            by_worker.setdefault(item.worker, []).append(item)
        updates = 0
        for feedbacks in by_worker.values():
            slice_coverage = sum(item.coverage_delta for item in feedbacks)
            slice_cross = sum(
                item.cross_learning_delta for item in feedbacks)
            slice_interesting = sum(
                item.interesting_cases for item in feedbacks)
            slice_cost = sum(item.elapsed for item in feedbacks)
            mean_base = sum(
                item.base_reward for item in feedbacks) / len(feedbacks)
            slice_signal = _clamp01(
                0.40 * self._squash(slice_coverage, 4.0)
                + 0.25 * self._squash(slice_cross, 8.0)
                + 0.15 * self._squash(slice_interesting, 2.0)
                + 0.15 * mean_base
                + 0.05 / (1.0 + math.log1p(slice_cost))
            )
            for item in feedbacks:
                individual = _clamp01(
                    0.50 * item.base_reward
                    + 0.30 * self._squash(item.coverage_delta, 2.0)
                    + 0.20 * self._squash(
                        item.cross_learning_delta, 4.0))
                reward = _clamp01(0.58 * slice_signal + 0.42 * individual)
                self.model.update(item.vector, reward)
                updates += 1
        self.pending.clear()
        self.slice_start = now
        self.slices += 1
        return updates

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "enabled": self.enabled,
            "slice_seconds": self.slice_seconds,
            "slices": self.slices,
            "assignments": self.assignments,
            "feedback_count": self.feedback_count,
            "global_coverage_gain": self.global_coverage_gain,
            "cross_learning_gain": self.cross_learning_gain,
            "model": self.model.to_mapping(),
            "global_seen_sites": sorted(self.global_seen_sites)[-65536:],
            "workers": {
                str(worker): {
                    "executions": state.executions,
                    "productive_runs": state.productive_runs,
                    "coverage_gain": state.coverage_gain,
                    "cross_learning_gain": state.cross_learning_gain,
                    "reward_ema": state.reward_ema,
                    "cost_ema": state.cost_ema,
                    "seen_sites": sorted(state.seen_sites)[-32768:],
                    "seen_paths": sorted(state.seen_paths)[-16384:],
                    "region_reward": [
                        [region, reward]
                        for region, reward in
                        list(state.region_reward.items())[-4096:]
                    ],
                }
                for worker, state in self.workers.items()
            },
            "seeds": [
                {
                    "path": path,
                    "path_hash": profile.path_hash,
                    "sites": sorted(profile.sites)[-512:],
                    "regions": sorted(profile.regions)[-64:],
                    "observations": profile.observations,
                    "reward_ema": profile.reward_ema,
                    "cost_ema": profile.cost_ema,
                }
                for path, profile in list(self.seeds.items())[-self.max_profiles:]
            ],
        }

    def restore(self, raw: Any) -> None:
        if not self.enabled or not isinstance(raw, dict):
            return
        if isinstance(raw.get("model"), dict):
            self.model.restore(raw["model"])
        self.slices = _nonnegative_int(raw.get("slices"))
        self.assignments = _nonnegative_int(raw.get("assignments"))
        self.feedback_count = _nonnegative_int(raw.get("feedback_count"))
        self.global_coverage_gain = _nonnegative_int(
            raw.get("global_coverage_gain"))
        self.cross_learning_gain = _nonnegative_int(
            raw.get("cross_learning_gain"))
        self.global_seen_sites = {
            site for site in (
                _nonnegative_int(value)
                for value in _sequence_tail(
                    raw.get("global_seen_sites"), 65536
                ))
            if site
        }
        workers = raw.get("workers", {})
        if isinstance(workers, dict):
            worker_keys = list(islice(reversed(workers), self.max_workers))
            for key in reversed(worker_keys):
                item = workers[key]
                worker = _nonnegative_int(key)
                if not worker or not isinstance(item, dict):
                    continue
                region_reward = {}
                for pair in _sequence_tail(item.get("region_reward"), 4096):
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        continue
                    region = _nonnegative_int(pair[0])
                    if region:
                        region_reward[region] = _clamp01(
                            _nonnegative_float(pair[1]))
                self.workers[worker] = WorkerExplorationState(
                    executions=_nonnegative_int(item.get("executions")),
                    productive_runs=_nonnegative_int(
                        item.get("productive_runs")),
                    coverage_gain=_nonnegative_int(item.get("coverage_gain")),
                    cross_learning_gain=_nonnegative_int(
                        item.get("cross_learning_gain")),
                    reward_ema=_clamp01(
                        _nonnegative_float(item.get("reward_ema"))),
                    cost_ema=_nonnegative_float(item.get("cost_ema")),
                    seen_sites={
                        value for value in (
                            _nonnegative_int(site)
                            for site in _sequence_tail(
                                item.get("seen_sites"), 32768
                            ))
                        if value
                    },
                    seen_paths={
                        value for value in (
                            _nonnegative_int(path_hash)
                            for path_hash in
                            _sequence_tail(item.get("seen_paths"), 16384))
                        if value
                    },
                    region_reward=region_reward,
                )
        seeds = raw.get("seeds", ())
        if isinstance(seeds, list):
            for item in seeds[-self.max_profiles:]:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", ""))
                if not path:
                    continue
                _bounded_mapping_set(
                    self.seeds,
                    path,
                    SeedWorkerProfile(
                        path_hash=_nonnegative_int(item.get("path_hash")),
                        sites={
                            value for value in (
                                _nonnegative_int(site)
                                for site in _sequence_tail(
                                    item.get("sites"), 512
                                ))
                            if value
                        },
                        regions={
                            value for value in (
                                _nonnegative_int(region)
                                for region in _sequence_tail(
                                    item.get("regions"), 64
                                ))
                            if value
                        },
                        observations=_nonnegative_int(item.get("observations")),
                        reward_ema=_clamp01(
                            _nonnegative_float(item.get("reward_ema"))),
                        cost_ema=_nonnegative_float(item.get("cost_ema")),
                    ),
                    self.max_profiles,
                )


class StrategyPortfolio:
    """Cost-aware UCB portfolio for runtime solver profiles."""

    def __init__(self, count: int, exploration: float = 0.55):
        self.count = max(1, min(1024, _nonnegative_int(count)))
        self.exploration = max(0.0, _finite_float(exploration, 0.55))
        self.pulls = [0] * self.count
        self.pending = [0] * self.count
        self.reward_sum = [0.0] * self.count
        self.cost_sum = [0.0] * self.count

    def select(self, eligible: list[int] | None = None) -> int:
        arms = [arm for arm in (eligible or list(range(self.count)))
                if 0 <= arm < self.count]
        if not arms:
            arms = list(range(self.count))
        effective = [self.pulls[arm] + self.pending[arm]
                     for arm in range(self.count)]
        for arm in arms:
            if effective[arm] == 0:
                self.pending[arm] += 1
                return arm
        total = sum(effective)
        scores: dict[int, float] = {}
        for arm in arms:
            completed = self.pulls[arm]
            mean_reward = self.reward_sum[arm] / completed if completed else 0.0
            mean_cost = self.cost_sum[arm] / completed if completed else 1.0
            efficiency = mean_reward / math.sqrt(max(0.05, mean_cost))
            explore = self.exploration * math.sqrt(math.log1p(total) / effective[arm])
            scores[arm] = efficiency + explore
        selected = max(arms, key=scores.__getitem__)
        self.pending[selected] += 1
        return selected

    def update(self, arm: int, reward: float, elapsed: float) -> None:
        if not 0 <= arm < self.count:
            return
        self.pending[arm] = max(0, self.pending[arm] - 1)
        reward = _finite_float(reward, -1.0)
        elapsed = _finite_float(elapsed, -1.0)
        if reward < 0.0 or elapsed < 0.0:
            return
        self.pulls[arm] += 1
        self.reward_sum[arm] += _clamp01(reward)
        self.cost_sum[arm] += max(0.001, elapsed)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pulls": self.pulls,
            "reward_sum": self.reward_sum,
            "cost_sum": self.cost_sum,
        }

    def restore(self, raw: dict[str, Any]) -> None:
        try:
            raw_pulls = raw.get("pulls", [])
            if any(isinstance(value, bool) for value in raw_pulls):
                return
            pulls = [int(value) for value in raw_pulls]
            rewards = [float(value) for value in raw.get("reward_sum", [])]
            costs = [float(value) for value in raw.get("cost_sum", [])]
        except (TypeError, ValueError, OverflowError):
            return
        if not len(pulls) == len(rewards) == len(costs) == self.count:
            return
        if any(
            pulls[index] < 0
            or not math.isfinite(rewards[index])
            or rewards[index] < 0.0
            or rewards[index] > pulls[index]
            or not math.isfinite(costs[index])
            or costs[index] < 0.0
            or (pulls[index] == 0 and (rewards[index] or costs[index]))
            for index in range(self.count)
        ):
            return
        self.pulls = pulls
        self.reward_sum = rewards
        self.cost_sum = costs


class AdaptiveHybridScheduler:
    """Unified seed, path-replay, and solver-strategy policy."""

    FEATURE_COUNT = 14

    def __init__(self, strategy_count: int, state_path: str | None = None):
        self.model = LinUCBModel(self.FEATURE_COUNT)
        self.strategies = StrategyPortfolio(strategy_count)
        self.state_path = state_path
        self.max_state_entries = max(
            128,
            min(
                _MAX_ADAPTIVE_STATE_ENTRIES,
                _nonnegative_int(
                    os.environ.get("SYMCC_ADAPTIVE_STATE_ENTRIES", 262144)),
            ),
        )
        self.state_max_bytes = max(
            1024 * 1024,
            min(
                _MAX_ADAPTIVE_STATE_MAX_BYTES,
                _nonnegative_int(os.environ.get(
                    "SYMCC_ADAPTIVE_STATE_MAX_BYTES",
                    _DEFAULT_ADAPTIVE_STATE_MAX_BYTES,
                )),
            ),
        )
        self.contexts: dict[str, CandidateContext] = {}
        self.replay: dict[str, ReplayRecord] = {}
        self.path_visits: dict[int, int] = {}
        self.prefix_dag_enabled = os.environ.get("SYMCC_PREFIX_DAG", "1") != "0"
        self.selective_sampling_enabled = (
            os.environ.get("SYMCC_SELECTIVE_SAMPLING", "1") != "0")
        self.s2f_dual_executor_enabled = (
            os.environ.get("SYMCC_S2F_DUAL_EXECUTOR", "1") != "0")
        self.cstg_enabled = os.environ.get("SYMCC_CSTG", "1") != "0"
        self.cstg = ConcolicStateTransitionGraph(
            max_transitions=max(
                128, _nonnegative_int(
                    os.environ.get("SYMCC_CSTG_TRANSITIONS", 8192))),
            action_cap=max(
                1, _nonnegative_int(
                    os.environ.get("SYMCC_S2F_ACTIONS_PER_SEED", 16))),
        )
        self.edge_dependence_enabled = (
            os.environ.get("SYMCC_EDGE_DEPENDENCE", "1") != "0")
        self.prefix_dag = PrefixDAG(
            _nonnegative_int(os.environ.get("SYMCC_PREFIX_DAG_NODES", 4096)))
        edge_distance_path = (
            os.environ.get("SYMCC_EDGE_DEP_DISTANCE")
            or os.environ.get("SYMCC_DIRECTED_DISTANCE")
        )
        self.edge_dependence = EdgeDependenceCoverage(
            max_branches=_nonnegative_int(
                os.environ.get("SYMCC_EDGE_DEP_BRANCHES", 4096)),
            max_cells=_nonnegative_int(
                os.environ.get("SYMCC_EDGE_DEP_CELLS", 262144)),
            trace_cap=_nonnegative_int(
                os.environ.get("SYMCC_EDGE_DEP_TRACE", 128)),
            directed_distances=load_directed_distance_map(edge_distance_path),
            target_lease_seconds=_nonnegative_float(
                os.environ.get("SYMCC_EDGE_DEP_TARGET_LEASE", 120.0)),
        )
        self.structural_tasks = (
            DynamicStructuralTaskAllocator.from_environment()
            if os.environ.get("SYMCC_STRUCTURAL_TASKS", "1") != "0"
            else DynamicStructuralTaskAllocator(ProgramTaskGraph())
        )
        path_cover_graph = self.structural_tasks.graph
        if not path_cover_graph.enabled:
            path_cover_graph = ProgramTaskGraph.load(
                os.environ.get("SYMCC_TASK_GRAPH")
                or os.environ.get("SYMCC_DIRECTED_DISTANCE"))
        self.path_cover = MinimumPathCoverPlanner.from_environment(
            path_cover_graph)
        self.path_cover_enabled = (
            os.environ.get("SYMCC_PATH_COVER", "1") != "0"
            and self.path_cover.enabled)
        self.ect_enabled = os.environ.get("SYMCC_ECT", "1") != "0"
        self.ect = ExpressiveCoverageTree.from_environment(path_cover_graph)
        self.data_coverage = DataCoverageTracker(
            min(16384, self.max_state_entries),
            min(65536, self.max_state_entries * 4),
        )
        self.pareto_corpus_enabled = (
            os.environ.get("SYMCC_PARETO_CORPUS", "1") != "0")
        self.pareto_corpus = ParetoCorpusArchive(
            max_entries=max(
                8, _nonnegative_int(
                    os.environ.get(
                        "SYMCC_PARETO_CORPUS_ENTRIES", 4096))),
            grid_bins=max(
                2, _nonnegative_int(
                    os.environ.get(
                        "SYMCC_PARETO_CORPUS_BINS", 8))),
        )
        self.total_coverage_features = 0
        self.total_data_feature_bits = 0
        self.total_edge_dependence_features = 0
        self.total_concurrency_guidance_hits = 0
        self.concurrency_guidance = HierarchicalConcurrencyGuidance(
            os.environ.get("SYMCC_CONCURRENCY_GUIDANCE"),
            min(4096, self.max_state_entries),
        )
        self.simifuzz_enabled = (
            os.environ.get("SYMCC_SIMIFUZZ", "1") != "0")
        self.seed_worker = SeedWorkerBandit(
            self.simifuzz_enabled,
            min(16384, self.max_state_entries),
        )
        if state_path:
            self._load_state()

    @staticmethod
    def _squash(value: float, scale: float = 1.0) -> float:
        return 1.0 - math.exp(-max(0.0, value) / scale)

    def context(
        self,
        path: str,
        *,
        name: str | None = None,
        size: int | None = None,
        seed_type: str | None = None,
        generation: int = 0,
        base_score: float = 0.0,
        frontier: float = 0.0,
        type_yield: float = 0.5,
    ) -> CandidateContext:
        base_score = _finite_float(base_score)
        frontier = _finite_float(frontier)
        type_yield = _finite_float(type_yield, 0.5)
        name = name or os.path.basename(path)
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        size = _nonnegative_int(size)
        generation = _nonnegative_int(generation)
        if seed_type is None:
            seed_type = "cov" if "+cov" in name else (
                "symcc" if "symcc_" in name else "normal")
        previous = self.replay.get(path)
        prior_difficulty = previous.difficulty if previous else 0.0
        prior_reward = previous.reward if previous else 0.0
        prior_comparison = previous.comparison_locality if previous else 0.0
        data_bonus = self.data_coverage.path_bonus(path)
        edge_dependence_bonus = (
            self.edge_dependence.path_bonus(path)
            if self.edge_dependence_enabled else 0.0)
        concurrency_bonus = self.concurrency_guidance.path_bonus(path)
        vector = (
            1.0,
            math.tanh(base_score / 100.0),
            min(1.0, math.log1p(max(0, size)) / 12.0),
            1.0 if "+cov" in name else 0.0,
            1.0 if "+rare" in name else 0.0,
            1.0 if seed_type == "symcc" else 0.0,
            self._squash(generation, 4.0),
            self._squash(frontier, 10.0),
            max(0.0, min(1.0, type_yield)),
            max(prior_difficulty, prior_reward),
            data_bonus,
            edge_dependence_bonus,
            prior_comparison,
            concurrency_bonus,
        )
        context = CandidateContext(path, seed_type, generation, vector, base_score)
        _bounded_mapping_set(
            self.contexts,
            path,
            context,
            self.max_state_entries,
        )
        return context

    def score(self, context: CandidateContext) -> float:
        learned, _ = self.model.score(context.vector)
        dynamic_bonus = 0.0
        if self.edge_dependence_enabled:
            dynamic_bonus += 0.05 * self.edge_dependence.path_bonus(context.path)
        if self.structural_tasks.enabled:
            dynamic_bonus += 0.08 * self.structural_tasks.path_priority(
                context.path)
        dynamic_bonus += 0.05 * self.concurrency_guidance.path_bonus(
            context.path)
        if self.pareto_corpus_enabled:
            dynamic_bonus += 0.08 * self.pareto_corpus.priority(
                context.path)
        record = self.replay.get(context.path)
        if record is not None:
            dynamic_bonus += 0.04 * record.comparison_locality
        # Preserve useful cold-start priors while allowing observations to dominate.
        return 0.25 * math.tanh(context.prior_score / 100.0) + learned + dynamic_bonus

    def select_strategy(self, target_branch: int = 0) -> int:
        eligible = (self.prefix_dag.preferred_strategies(
            target_branch, self.strategies.count,
            self.s2f_dual_executor_enabled)
            if target_branch and self.selective_sampling_enabled else None)
        return self.strategies.select(eligible)

    @staticmethod
    def _reward(
        coverage_delta: int,
        data_delta: int,
        edge_dependence_delta: int,
        interesting_cases: int,
        elapsed: float,
        killed: bool,
        telemetry: SolverTelemetry | None,
    ) -> float:
        if killed:
            return 0.0
        coverage = 1.0 - math.exp(-max(0, coverage_delta) / 4.0)
        data = 1.0 - math.exp(-max(0, data_delta) / 8.0)
        if telemetry:
            data = max(data, 1.0 - math.exp(
                -telemetry.data_coverage_map_updates / 64.0))
        edge_dependence = 1.0 - math.exp(-max(0, edge_dependence_delta) / 32.0)
        corpus = 1.0 - math.exp(-max(0, interesting_cases) / 2.0)
        solver_yield = telemetry.solve_yield if telemetry else 0.0
        backsolver_yield = telemetry.backsolver_yield if telemetry else 0.0
        comparison = telemetry.comparison_taint_locality if telemetry else 0.0
        if telemetry:
            backsolver_yield = max(backsolver_yield,
                                   telemetry.backsolver_direct_yield)
        poly_yield = min(1.0, (
            (telemetry.poly_cache_hits + telemetry.poly_samples +
             telemetry.poly_dense_walks +
             telemetry.prefix_context_hits + telemetry.unsat_core_hits +
             telemetry.unsat_core_unification_hits +
             telemetry.linear_subsumption_prunes) / 7.0) if telemetry else 0.0)
        quality = (0.51 * coverage + 0.14 * data + 0.13 * edge_dependence
                   + 0.12 * corpus + 0.04 * solver_yield + 0.02 * poly_yield
                   + 0.01 * backsolver_yield + 0.03 * comparison)
        cost = 1.0 + 0.35 * math.log1p(max(0.0, elapsed))
        return max(0.0, min(1.0, quality / cost))

    def observe(
        self,
        path: str,
        *,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        killed: bool,
        strategy: int,
        telemetry: SolverTelemetry | None,
        worker: int = 0,
        s2f_actions: tuple[tuple[int, str], ...] = (),
        profile: dict[str, float] | None = None,
    ) -> float:
        def _mark_profile(name: str, started: float) -> None:
            if profile is not None:
                profile[name] = profile.get(name, 0.0) + (
                    time.monotonic() - started)

        step_started = time.monotonic() if profile is not None else 0.0
        context = self.contexts.get(path) or self.context(path)
        _mark_profile("context", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        data_delta = self.data_coverage.observe(
            path,
            telemetry.data_features if telemetry else (),
            static_features=(
                telemetry.static_data_features if telemetry else ()),
            code_summary=telemetry.path_hash if telemetry else 0,
        )
        _mark_profile("data_coverage", step_started)

        now = time.monotonic()
        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and telemetry.target_branch:
            self.edge_dependence.release_target(telemetry.target_branch)
        edge_dependence_delta = (
            self.edge_dependence.observe(
                path,
                telemetry,
                now,
                coverage_delta=coverage_delta,
                interesting_cases=interesting_cases,
                elapsed=elapsed,
                killed=killed,
            )
            if telemetry and self.edge_dependence_enabled else 0)
        _mark_profile("edge_dependence", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        reward = self._reward(
            coverage_delta, data_delta, edge_dependence_delta,
            interesting_cases, elapsed, killed, telemetry)
        _mark_profile("reward", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if self.pareto_corpus_enabled:
            self.pareto_corpus.observe(
                path,
                coverage_delta=coverage_delta,
                data_delta=data_delta,
                reward=reward,
                elapsed=elapsed,
                telemetry=telemetry,
            )
        _mark_profile("pareto_corpus", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        self.model.update(context.vector, reward)
        self.strategies.update(strategy, reward, elapsed)
        self.total_coverage_features += max(0, coverage_delta)
        self.total_data_feature_bits += data_delta
        self.total_edge_dependence_features += edge_dependence_delta
        _mark_profile("strategy_model", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        concurrency_hits = (
            self.concurrency_guidance.observe(path, telemetry, reward, now)
            if telemetry else 0)
        self.total_concurrency_guidance_hits += concurrency_hits
        _mark_profile("concurrency_guidance", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        difficulty = telemetry.difficulty if telemetry else 0.0
        comparison_locality = (
            telemetry.comparison_taint_locality if telemetry else 0.0)
        record = self.replay.get(path)
        if record is None:
            _bounded_mapping_set(
                self.replay,
                path,
                ReplayRecord(
                    path,
                    reward,
                    difficulty,
                    1,
                    now,
                    open_branches=(
                        telemetry.open_branches if telemetry else ()),
                    comparison_locality=comparison_locality,
                ),
                self.max_state_entries,
            )
        else:
            record.reward = 0.7 * record.reward + 0.3 * reward
            record.difficulty = max(0.7 * record.difficulty, difficulty)
            record.comparison_locality = max(
                0.75 * record.comparison_locality, comparison_locality)
            record.visits += 1
            record.last_seen = now
            if telemetry:
                resolved = (
                    telemetry.target_branch
                    if telemetry.target_status in {"sat", "unsat"} else 0
                )
                record.open_branches = tuple(
                    branch for branch in dict.fromkeys(
                        record.open_branches + telemetry.open_branches)
                    if branch != resolved
                )[:256]
                if record.open_branches:
                    record.target_cursor %= len(record.open_branches)
                else:
                    record.target_cursor = 0
            self.replay.pop(path)
            self.replay[path] = record
        if telemetry and telemetry.path_hash:
            visits = self.path_visits.pop(telemetry.path_hash, 0) + 1
            _bounded_mapping_set(
                self.path_visits,
                telemetry.path_hash,
                visits,
                self.max_state_entries,
            )
        _mark_profile("replay", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and self.path_cover_enabled:
            self.path_cover.observe(
                path, telemetry, reward=reward,
                coverage_delta=coverage_delta)
        _mark_profile("path_cover", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and self.ect_enabled:
            self.ect.observe(
                path,
                telemetry,
                reward=reward,
                coverage_delta=coverage_delta,
                interesting_cases=interesting_cases,
                elapsed=elapsed,
                killed=killed,
                now=now,
            )
        _mark_profile("ect", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and self.prefix_dag_enabled:
            self.prefix_dag.ingest(
                path, telemetry, reward, now,
                strategy=strategy, elapsed=elapsed, killed=killed,
                actions=s2f_actions)
            if self.path_cover_enabled:
                self.path_cover.apply_to_prefix_dag(self.prefix_dag)
        _mark_profile("prefix_dag", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and self.cstg_enabled:
            self.cstg.observe(
                path,
                telemetry,
                reward=reward,
                elapsed=elapsed,
                killed=killed,
                now=now,
            )
        _mark_profile("cstg", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if telemetry and self.structural_tasks.enabled:
            self.structural_tasks.observe(
                path,
                telemetry,
                reward=reward,
                coverage_delta=coverage_delta,
                interesting_cases=interesting_cases,
                elapsed=elapsed,
                now=now,
            )
        _mark_profile("structural_tasks", step_started)

        step_started = time.monotonic() if profile is not None else 0.0
        if worker > 0 and self.simifuzz_enabled:
            self.seed_worker.observe(
                worker,
                context,
                base_reward=reward,
                coverage_delta=coverage_delta,
                interesting_cases=interesting_cases,
                elapsed=elapsed,
                telemetry=telemetry,
                task_region=self.structural_task_region(
                    path, telemetry.target_branch if telemetry else 0),
                now=now,
            )
        _mark_profile("seed_worker", step_started)
        return reward

    def rebalance_structural_tasks(
        self,
        workers: list[int] | tuple[int, ...],
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        return self.structural_tasks.rebalance(
            workers, now=now, force=force)

    def structural_work_index(
        self,
        worker: int,
        items: list[tuple],
        start: int,
        *,
        active_worker_count: int,
    ) -> int | None:
        return self.structural_tasks.select_index(
            worker, items, start,
            active_worker_count=active_worker_count)

    def work_index(
        self,
        worker: int,
        items: list[tuple],
        start: int,
        *,
        active_worker_count: int,
    ) -> int | None:
        """Choose work for a specific worker using structural and pair context."""
        if start >= len(items):
            return None
        if not self.simifuzz_enabled:
            return self.structural_work_index(
                worker, items, start,
                active_worker_count=active_worker_count)
        try:
            candidate_cap = max(
                1, min(512, int(os.environ.get(
                    "SYMCC_SIMIFUZZ_CANDIDATES", "64"))))
        except ValueError:
            candidate_cap = 64
        stop = min(len(items), start + candidate_cap)
        candidates = list(range(start, stop))
        if self.structural_tasks.enabled and self.structural_tasks.worker_regions:
            owned = [
                index for index in candidates
                if self.structural_tasks.accepts(
                    worker, items[index][0], items[index][2])
            ]
            if owned:
                candidates = owned
            elif len(items) - start <= max(
                    4, 2 * max(1, active_worker_count)):
                return None
        best: tuple[float, int] | None = None
        for index in candidates:
            path = items[index][0]
            focus = items[index][1]
            target = items[index][2]
            context = self.contexts.get(path) or self.context(path)
            task_region = self.structural_task_region(path, target)
            pair_score = self.seed_worker.score(
                worker,
                context,
                focus=focus or "",
                target_branch=target,
                task_region=task_region,
            )
            score = pair_score + 0.12 * self.score(context)
            candidate = (score, -index)
            if best is None or candidate > (best[0], -best[1]):
                best = (score, index)
        return best[1] if best is not None else None

    def reserve_worker_assignment(
        self,
        worker: int,
        path: str,
        *,
        focus: str = "",
        target_branch: int = 0,
    ) -> None:
        if not self.simifuzz_enabled:
            return
        context = self.contexts.get(path) or self.context(path)
        self.seed_worker.reserve(
            worker,
            context,
            focus=focus,
            target_branch=target_branch,
            task_region=self.structural_task_region(path, target_branch),
        )

    def release_worker_assignment(self, worker: int) -> bool:
        return self.seed_worker.release(worker) if self.simifuzz_enabled else False

    def discard_worker_assignment(self, worker: int) -> bool:
        return self.seed_worker.discard(worker) if self.simifuzz_enabled else False

    def structural_task_region(self, path: str, target_branch: int = 0) -> int:
        return self.structural_tasks.task_region(path, target_branch)

    def _rank_ect_jobs(
        self,
        jobs: list[ReplayJob],
        limit: int | None = None,
    ) -> list[ReplayJob]:
        if self.ect_enabled:
            jobs.sort(
                key=lambda job: self.ect.priority(job.target_branch)
                if job.target_branch else 0.0,
                reverse=True,
            )
        return jobs if limit is None else jobs[:limit]

    def _reserve_target_jobs(
        self,
        jobs: list[ReplayJob],
        *,
        cooldown: float,
        now: float,
        on_accept: Callable[[list[ReplayJob], float], None] | None = None,
        external_admission: Callable[[ReplayJob], bool] | None = None,
        limit: int | None = None,
    ) -> list[ReplayJob]:
        reserved: list[ReplayJob] = []
        for job in jobs:
            if self._admit_target_job(
                    job,
                    cooldown=cooldown,
                    now=now,
                    external_admission=external_admission):
                reserved.append(job)
                if limit is not None and len(reserved) >= limit:
                    break
        if reserved and on_accept is not None:
            on_accept(reserved, now)
        return reserved

    def _admit_target_job(
        self,
        job: ReplayJob,
        *,
        cooldown: float,
        now: float,
        external_admission: Callable[[ReplayJob], bool] | None = None,
    ) -> bool:
        if not self.edge_dependence.reserve_job(
                job, cooldown=cooldown, now=now):
            return False
        if external_admission is None:
            return True
        try:
            accepted = bool(external_admission(job))
        except Exception:
            self.edge_dependence.release_job(job)
            raise
        if accepted:
            return True
        self.edge_dependence.release_job(job)
        return False

    def release_target_assignment(
        self,
        target_branch: int,
        actions: tuple[tuple[int, str], ...] = (),
    ) -> None:
        self.edge_dependence.release_job(
            ReplayJob("", target_branch, actions))

    def replay_candidates(
        self,
        limit: int,
        *,
        cooldown: float = 30.0,
        now: float | None = None,
        external_admission: Callable[[ReplayJob], bool] | None = None,
    ) -> list[ReplayJob]:
        """Return productive or hard paths when fresh cross-seeds are exhausted."""
        now = time.monotonic() if now is None else now
        if self.path_cover_enabled:
            self.path_cover.apply_to_prefix_dag(self.prefix_dag)
        blocked_targets = self.edge_dependence.leased_targets(now)
        jobs = (self.prefix_dag.select(
            limit,
            cooldown,
            now,
            blocked_targets,
            commit=False,
            proposal_limit=max(limit, len(self.prefix_dag.nodes)),
        )
                if self.prefix_dag_enabled else [])
        jobs = self._reserve_target_jobs(
            jobs,
            cooldown=cooldown,
            now=now,
            on_accept=self.prefix_dag.commit_jobs,
            external_admission=external_admission,
            limit=limit,
        )
        if len(jobs) >= limit:
            return self._rank_ect_jobs(jobs, limit)
        selected_paths = {job.path for job in jobs}
        remaining = limit - len(jobs)
        concurrency_jobs = self.concurrency_guidance.select(
            remaining,
            cooldown,
            now,
            selected_paths,
            self.edge_dependence.leased_targets(now),
            commit=False,
            proposal_limit=max(
                remaining, len(self.concurrency_guidance.records)),
        )
        concurrency_jobs = self._reserve_target_jobs(
            concurrency_jobs,
            cooldown=cooldown,
            now=now,
            on_accept=self.concurrency_guidance.commit_jobs,
            external_admission=external_admission,
            limit=remaining,
        )
        jobs.extend(concurrency_jobs)
        selected_paths.update(job.path for job in concurrency_jobs)
        if len(jobs) >= limit:
            return self._rank_ect_jobs(jobs, limit)
        if self.edge_dependence_enabled:
            edge_jobs = self.edge_dependence.select(
                limit - len(jobs), cooldown, now, selected_paths,
                admit=lambda job: self._admit_target_job(
                    job,
                    cooldown=cooldown,
                    now=now,
                    external_admission=external_admission,
                ),
            )
            jobs.extend(edge_jobs)
            selected_paths.update(job.path for job in edge_jobs)
        if len(jobs) >= limit:
            return self._rank_ect_jobs(jobs, limit)
        candidates: list[tuple[float, ReplayRecord]] = []
        for record in self.replay.values():
            if record.path in selected_paths:
                continue
            if not os.path.isfile(record.path):
                continue
            if now - record.last_replay < cooldown:
                continue
            novelty = 1.0 / math.sqrt(record.visits)
            open_bonus = 1.0 if record.open_branches else 0.0
            score = (0.45 * record.reward + 0.25 * record.difficulty
                     + 0.15 * novelty + 0.15 * open_bonus)
            if score > 0.05:
                candidates.append((score, record))
        selected = sorted(candidates, key=lambda item: item[0], reverse=True)
        for _, record in selected:
            target = 0
            target_index = -1
            if record.open_branches:
                blocked_targets = self.edge_dependence.leased_targets(now)
                for offset in range(len(record.open_branches)):
                    index = (record.target_cursor + offset) % len(
                        record.open_branches)
                    candidate_target = record.open_branches[index]
                    if candidate_target not in blocked_targets:
                        target = candidate_target
                        target_index = index
                        break
                if not target:
                    continue
            job = ReplayJob(record.path, target)
            if not self._admit_target_job(
                    job,
                    cooldown=cooldown,
                    now=now,
                    external_admission=external_admission):
                continue
            if target_index >= 0:
                record.target_cursor = (
                    target_index + 1) % len(record.open_branches)
            record.last_replay = now
            jobs.append(job)
            if len(jobs) >= limit:
                break
        return self._rank_ect_jobs(jobs, limit)

    def target_candidates(
        self,
        limit: int,
        *,
        cooldown: float = 30.0,
        now: float | None = None,
        external_admission: Callable[[ReplayJob], bool] | None = None,
    ) -> list[ReplayJob]:
        """Select only explicit open-prefix work for the reserved DAG budget."""
        timestamp = time.monotonic() if now is None else now
        blocked_targets = self.edge_dependence.leased_targets(timestamp)
        cstg_scan_limit = max(limit, len(self.cstg.transitions))
        cstg_jobs = (
            self.cstg.select(
                limit,
                cooldown=cooldown,
                now=timestamp,
                exclude_targets=blocked_targets,
                commit=False,
                proposal_limit=cstg_scan_limit,
            )
            if self.cstg_enabled else [])
        if not self.prefix_dag_enabled:
            cstg_jobs = self._reserve_target_jobs(
                cstg_jobs,
                cooldown=cooldown,
                now=timestamp,
                on_accept=self.cstg.commit_jobs,
                external_admission=external_admission,
                limit=limit,
            )
            return self._rank_ect_jobs(cstg_jobs, limit)
        if self.path_cover_enabled:
            self.path_cover.apply_to_prefix_dag(self.prefix_dag)
        prefix_jobs = self.prefix_dag.select(
            limit,
            cooldown,
            timestamp,
            blocked_targets,
            commit=False,
            proposal_limit=max(limit, len(self.prefix_dag.nodes)),
        )
        jobs: list[ReplayJob] = []
        seen: set[tuple[str, int]] = set()
        for index in range(max(len(prefix_jobs), len(cstg_jobs))):
            for source, on_accept in (
                (prefix_jobs, self.prefix_dag.commit_jobs),
                (cstg_jobs, self.cstg.commit_jobs),
            ):
                if index >= len(source):
                    continue
                job = source[index]
                key = (job.path, job.target_branch)
                if key in seen:
                    continue
                if not self._admit_target_job(
                        job,
                        cooldown=cooldown,
                        now=timestamp,
                        external_admission=external_admission):
                    continue
                seen.add(key)
                on_accept([job], timestamp)
                jobs.append(job)
                if len(jobs) >= limit:
                    break
            if len(jobs) >= limit:
                break
        return self._rank_ect_jobs(jobs, limit)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 14,
            "model": self.model.to_mapping(),
            "strategies": self.strategies.to_mapping(),
            "coverage_features": self.total_coverage_features,
            "data_feature_bits": self.total_data_feature_bits,
            "edge_dependence_features": self.total_edge_dependence_features,
            "concurrency_guidance_hits": self.total_concurrency_guidance_hits,
            "data_sites": (
                len(self.data_coverage.best) +
                len(self.data_coverage.static_best)),
            "data_static_sites": len(self.data_coverage.static_best),
            "data_corpus_refinements": self.data_coverage.refinements,
            "pareto_corpus_enabled": self.pareto_corpus_enabled,
            "pareto_corpus_entries": len(self.pareto_corpus.entries),
            "pareto_corpus_admissions": self.pareto_corpus.admissions,
            "pareto_corpus_rejections": self.pareto_corpus.rejections,
            "pareto_corpus_replacements": self.pareto_corpus.replacements,
            "unique_paths": len(self.path_visits),
            "replay_entries": len(self.replay),
            "prefix_nodes": len(self.prefix_dag.nodes),
            "open_prefixes": sum(
                node.status in self.prefix_dag.ACTIVE
                for node in self.prefix_dag.nodes.values()),
            "selective_mdp_refreshes": self.prefix_dag.selective_mdp_refreshes,
            "selective_mdp_skipped_refreshes":
                self.prefix_dag.selective_mdp_skipped_refreshes,
            "selective_mdp_refresh_interval":
                self.prefix_dag.selective_mdp_refresh_interval,
            "selective_mdp_small_graph_nodes":
                self.prefix_dag.selective_mdp_small_graph_nodes,
            "constraint_summaries": len(self.prefix_dag.constraints.entries),
            "s2f_dual_executor": self.s2f_dual_executor_enabled,
            "cstg_enabled": self.cstg_enabled,
            "cstg_transitions": len(self.cstg.transitions),
            "cstg_scheduled": self.cstg.scheduled,
            "cstg_divergence_updates": self.cstg.divergence_updates,
            "dynamic_coloration": self.prefix_dag.dynamic_coloration_enabled,
            "colorgo_feasible_prefixes": sum(
                node.color_feasibility >= self.prefix_dag.min_feasibility
                for node in self.prefix_dag.nodes.values()),
            "colorgo_mdp_frontier": max(
                (node.mdp_value for node in self.prefix_dag.nodes.values()),
                default=0.0,
            ),
            "taco_enabled": self.prefix_dag.taco_enabled,
            "taco_target_paths": sum(
                1 for node in self.prefix_dag.nodes.values()
                if node.target_path_visits),
            "multigo_enabled": self.prefix_dag.multigo_enabled,
            "multigo_site_frequency": len(self.prefix_dag.site_frequency),
            "multigo_max_path_difficulty": max(
                (node.path_difficulty for node in self.prefix_dag.nodes.values()),
                default=0.0,
            ),
            "concurrency_guidance_sites": len(
                self.prefix_dag.concurrency_distances),
            "hierarchical_concurrency_records": len(
                self.concurrency_guidance.records),
            "edge_dependence_enabled": self.edge_dependence_enabled,
            "edge_dependence_branches": len(self.edge_dependence.rows),
            "edge_dependence_cells": len(self.edge_dependence.cells),
            "edge_dependence_targets": sum(
                len(row.target_branches)
                for row in self.edge_dependence.rows.values()),
            "edge_dependence_directed_sites": len(
                self.edge_dependence.directed_distances),
            "edge_dependence_inflight_targets": len(
                self.edge_dependence.target_leases),
            "edge_dependence_lease_reservations": (
                self.edge_dependence.lease_reservations),
            "edge_dependence_lease_suppressions": (
                self.edge_dependence.lease_suppressions),
            "structural_tasks_enabled": self.structural_tasks.enabled,
            "structural_task_regions": len(
                self.structural_tasks.graph.regions),
            "structural_task_rebalances": self.structural_tasks.rebalances,
            "ect_enabled": self.ect_enabled,
            "ect_nodes": len(self.ect.nodes),
            "ect_filtered_duplicates": self.ect.filtered_duplicates,
            "ect_loop_compressions": self.ect.loop_compressions,
            "path_cover_enabled": self.path_cover_enabled,
            "path_cover_functions": len(self.path_cover.plans),
            "path_cover_count": sum(
                len(plan.covers) for plan in self.path_cover.plans.values()),
            "path_cover_infeasible": self.path_cover.infeasible_paths,
            "simifuzz_enabled": self.simifuzz_enabled,
            "simifuzz_workers": len(self.seed_worker.workers),
            "simifuzz_assignments": self.seed_worker.assignments,
            "simifuzz_slices": self.seed_worker.slices,
            "simifuzz_cross_learning": self.seed_worker.cross_learning_gain,
            "prefix_dag": self.prefix_dag.to_mapping(),
            "data_coverage": self.data_coverage.to_mapping(),
            "pareto_corpus": self.pareto_corpus.to_mapping(),
            "edge_dependence": self.edge_dependence.to_mapping(),
            "hierarchical_concurrency": self.concurrency_guidance.to_mapping(),
            "structural_tasks": self.structural_tasks.snapshot(),
            "expressive_coverage_tree": self.ect.to_mapping(),
            "path_cover": self.path_cover.snapshot(),
            "seed_worker": self.seed_worker.to_mapping(),
            "cstg": self.cstg.to_mapping(),
        }

    def save(self) -> None:
        if not self.state_path:
            return
        self.seed_worker.flush(force=True)
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(self.snapshot(), stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                if os.fstat(stream.fileno()).st_size > self.state_max_bytes:
                    raise OSError("adaptive state exceeds byte budget")
            os.replace(tmp, self.state_path)
            if self.ect_enabled:
                self.ect.export()
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _load_state(self) -> None:
        text = _read_bounded_regular_utf8(
            self.state_path, self.state_max_bytes
        )
        if text is None:
            return
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") not in {
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}:
            return
        if isinstance(raw.get("model"), dict):
            self.model.restore(raw["model"])
        if isinstance(raw.get("strategies"), dict):
            self.strategies.restore(raw["strategies"])
        self.total_coverage_features = _nonnegative_int(raw.get("coverage_features"))
        self.total_data_feature_bits = _nonnegative_int(raw.get("data_feature_bits"))
        self.total_edge_dependence_features = _nonnegative_int(
            raw.get("edge_dependence_features"))
        self.total_concurrency_guidance_hits = _nonnegative_int(
            raw.get("concurrency_guidance_hits"))
        self.prefix_dag.restore(raw.get("prefix_dag"))
        self.data_coverage.restore(raw.get("data_coverage"))
        self.pareto_corpus.restore(raw.get("pareto_corpus"))
        self.edge_dependence.restore(raw.get("edge_dependence"))
        self.concurrency_guidance.restore(raw.get("hierarchical_concurrency"))
        self.structural_tasks.restore(raw.get("structural_tasks"))
        self.ect.restore(raw.get("expressive_coverage_tree"))
        self.path_cover.restore(raw.get("path_cover"))
        self.seed_worker.restore(raw.get("seed_worker"))
        self.cstg.restore(raw.get("cstg"))
        if self.path_cover_enabled:
            self.path_cover.apply_to_prefix_dag(self.prefix_dag)
