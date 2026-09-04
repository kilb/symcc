#!/usr/bin/env python3
"""Deterministic context clustering and conservative SMT-sequence training.

The online scheduler deliberately remains cheap.  This module consumes its
logged trajectories offline, grows bounded X-means clusters using a BIC split
test, estimates timeout-censored PAR-2 utility, and emits a self-checking policy
artifact that can be used only as an online exploration prior.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from offline_policy import TrajectoryEvent, load_trajectories


POLICY_SCHEMA = "symcc-smt-sequence-policy-v1"
TRAINING_SCHEMA = "symcc-smt-sequence-training-v1"
LEGACY_FEATURE_SCHEMA = "symcc-smt-context-features-v1"
FEATURE_SCHEMA = "symcc-smt-context-features-v2"
OBJECTIVE_SCHEMA = "censored-par2-ips-lcb-v1"
LEGACY_FEATURE_NAMES = (
    "bias",
    "log_input_bytes",
    "difficulty",
    "timeout_ratio",
    "solver_unknown_ratio",
    "dependency_ratio",
    "data_uncertainty",
    "branch_pressure",
    "targeted",
)
FEATURE_NAMES = LEGACY_FEATURE_NAMES + (
    "log_query_nodes",
    "log_query_input_bytes",
    "log_query_max_bits",
    "query_comparison_ratio",
    "query_nonlinear_ratio",
    "query_bitwise_ratio",
    "query_structural_ratio",
)
LEGACY_DIMENSION = len(LEGACY_FEATURE_NAMES)
DIMENSION = len(FEATURE_NAMES)
_HEX = frozenset("0123456789abcdef")


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _clamp01(value: Any) -> float:
    return max(0.0, min(1.0, _finite_float(value)))


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value) != 0.0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def feature_contract(
    schema: Any,
) -> tuple[str, int, tuple[str, ...]] | None:
    """Resolve the exact v1/v2 contract without accepting arbitrary shapes."""
    contracts = {
        LEGACY_FEATURE_SCHEMA: LEGACY_FEATURE_NAMES,
        FEATURE_SCHEMA: FEATURE_NAMES,
    }
    if isinstance(schema, str):
        name = schema
        mapping = None
    elif isinstance(schema, Mapping):
        name = schema.get("schema")
        mapping = schema
    else:
        return None
    features = contracts.get(name)
    if features is None:
        return None
    expected = {
        "schema": name,
        "dimension": len(features),
        "features": list(features),
    }
    if mapping is not None and dict(mapping) != expected:
        return None
    return name, len(features), features


def feature_schema_mapping(schema: str = FEATURE_SCHEMA) -> dict[str, Any]:
    contract = feature_contract(schema)
    if contract is None:
        raise ValueError("unsupported SMT context feature schema")
    name, dimension, features = contract
    return {
        "schema": name,
        "dimension": dimension,
        "features": list(features),
    }


def adapt_feature_vector(
    vector: Sequence[float],
    target_dimension: int,
) -> tuple[float, ...] | None:
    """Project v2 to v1 or zero-extend v1 for legacy state migration."""
    try:
        normalized = tuple(float(value) for value in vector)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        len(normalized) not in {LEGACY_DIMENSION, DIMENSION}
        or target_dimension not in {LEGACY_DIMENSION, DIMENSION}
        or any(not math.isfinite(value) for value in normalized)
    ):
        return None
    if len(normalized) > target_dimension:
        return normalized[:target_dimension]
    if len(normalized) < target_dimension:
        return normalized + (0.0,) * (target_dimension - len(normalized))
    return normalized


def context_feature_vector(
    context: Mapping[str, Any],
    *,
    schema: str = FEATURE_SCHEMA,
) -> tuple[float, ...]:
    """Project trajectory context into the online scheduler's bounded space."""
    contract = feature_contract(schema)
    if contract is None:
        raise ValueError("unsupported SMT context feature schema")
    size = max(0.0, _finite_float(context.get("input_bytes")))
    dependencies = max(
        0.0, _finite_float(context.get("dependency_bytes")))
    if size > 0.0:
        dependency_ratio = dependencies / size
    else:
        dependency_ratio = math.log1p(dependencies) / 16.0
    legacy = (
        1.0,
        min(1.0, math.log1p(size) / 16.0),
        _clamp01(context.get("difficulty")),
        _clamp01(context.get("timeout_ratio")),
        _clamp01(context.get("solver_unknown_ratio")),
        _clamp01(dependency_ratio),
        1.0 - _clamp01(context.get("data_quality")),
        _clamp01(context.get("branch_pressure")),
        float(_bool_value(context.get("targeted", False))),
    )
    if contract[1] == LEGACY_DIMENSION:
        return legacy
    queries = max(
        0.0, _finite_float(context.get("query_ir_queries")))
    divisor = max(1.0, queries)
    nodes = max(0.0, _finite_float(context.get("query_ir_nodes")))
    input_bytes = max(
        0.0, _finite_float(context.get("query_ir_input_bytes")))
    max_bits = max(
        0.0, _finite_float(context.get("query_ir_max_bits")))
    node_divisor = max(1.0, nodes)
    return legacy + (
        min(1.0, math.log1p(nodes / divisor) / 16.0),
        min(1.0, math.log1p(input_bytes / divisor) / 16.0),
        min(1.0, math.log2(max_bits + 1.0) / 16.0),
        _clamp01(
            _finite_float(context.get("query_ir_comparison_ops"))
            / node_divisor),
        _clamp01(
            _finite_float(context.get("query_ir_nonlinear_ops"))
            / node_divisor),
        _clamp01(
            _finite_float(context.get("query_ir_bitwise_ops"))
            / node_divisor),
        _clamp01(
            _finite_float(context.get("query_ir_structural_ops"))
            / node_divisor),
    )


@dataclass(frozen=True)
class _Cluster:
    members: tuple[int, ...]
    centroid: tuple[float, ...]
    sse: float


def _fit_cluster(
    points: Sequence[tuple[float, ...]],
    members: Iterable[int],
) -> _Cluster:
    selected = tuple(sorted(int(index) for index in members))
    if not selected:
        raise ValueError("an X-means cluster cannot be empty")
    dimension = len(points[0])
    if dimension <= 0 or any(len(point) != dimension for point in points):
        raise ValueError("X-means points must share a nonzero dimension")
    centroid = tuple(
        sum(points[index][axis] for index in selected) / len(selected)
        for axis in range(dimension)
    )
    sse = sum(
        sum(
            (points[index][axis] - centroid[axis]) ** 2
            for axis in range(dimension)
        )
        for index in selected
    )
    return _Cluster(selected, centroid, max(0.0, sse))


def _bic(
    sizes: Sequence[int],
    sses: Sequence[float],
    *,
    dimension: int = DIMENSION,
) -> float:
    """Spherical-Gaussian mixture BIC (larger is better)."""
    if (
        not sizes
        or len(sizes) != len(sses)
        or any(size <= 0 for size in sizes)
    ):
        return float("-inf")
    count = sum(sizes)
    components = len(sizes)
    if count <= components:
        return float("-inf")
    total_sse = sum(max(0.0, float(value)) for value in sses)
    variance = max(
        total_sse / max(1, count * dimension), 1e-12)
    log_likelihood = (
        -0.5 * count * dimension
        * (math.log(2.0 * math.pi) + 1.0 + math.log(variance))
    )
    for size in sizes:
        log_likelihood += size * math.log(size / count)
    parameters = components * dimension + (components - 1) + 1
    return log_likelihood - 0.5 * parameters * math.log(count)


def _member_digest(members: Sequence[int]) -> str:
    return _sha256(list(members))


def _split_cluster(
    points: Sequence[tuple[float, ...]],
    cluster: _Cluster,
    *,
    min_cluster_size: int,
    max_iterations: int,
) -> tuple[_Cluster, _Cluster, int] | None:
    if len(cluster.members) < 2 * min_cluster_size:
        return None
    dimension = len(cluster.centroid)
    variances = []
    for axis in range(dimension):
        variances.append(sum(
            (points[index][axis] - cluster.centroid[axis]) ** 2
            for index in cluster.members
        ) / len(cluster.members))
    axis = max(range(dimension), key=lambda item: (variances[item], -item))
    ordered = sorted(
        cluster.members,
        key=lambda index: (points[index][axis], points[index], index),
    )
    if points[ordered[0]][axis] == points[ordered[-1]][axis]:
        return None
    centroids = [points[ordered[0]], points[ordered[-1]]]
    assignments: tuple[tuple[int, ...], tuple[int, ...]] | None = None
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        groups: list[list[int]] = [[], []]
        for index in cluster.members:
            distances = [
                sum(
                    (points[index][item] - centroid[item]) ** 2
                    for item in range(dimension)
                )
                for centroid in centroids
            ]
            groups[0 if distances[0] <= distances[1] else 1].append(index)
        current = (tuple(sorted(groups[0])), tuple(sorted(groups[1])))
        if (
            len(current[0]) < min_cluster_size
            or len(current[1]) < min_cluster_size
        ):
            return None
        fitted = (
            _fit_cluster(points, current[0]),
            _fit_cluster(points, current[1]),
        )
        if assignments == current:
            return fitted[0], fitted[1], iterations
        assignments = current
        centroids = [fitted[0].centroid, fitted[1].centroid]
    if assignments is None:
        return None
    return (
        _fit_cluster(points, assignments[0]),
        _fit_cluster(points, assignments[1]),
        iterations,
    )


def _xmeans(
    points: Sequence[tuple[float, ...]],
    *,
    max_clusters: int,
    min_cluster_size: int,
    max_iterations: int,
    bic_margin: float,
) -> tuple[list[_Cluster], list[dict[str, Any]], float]:
    if not points:
        return [], [], 0.0
    dimension = len(points[0])
    clusters = [_fit_cluster(points, range(len(points)))]
    trials: list[dict[str, Any]] = []
    round_number = 0
    while len(clusters) < max_clusters:
        proposals: list[
            tuple[float, int, _Cluster, _Cluster, int, float, float]
        ] = []
        for index, parent in enumerate(clusters):
            split = _split_cluster(
                points,
                parent,
                min_cluster_size=min_cluster_size,
                max_iterations=max_iterations,
            )
            if split is None:
                continue
            left, right, iterations = split
            parent_bic = _bic(
                (len(parent.members),), (parent.sse,),
                dimension=dimension)
            child_bic = _bic(
                (len(left.members), len(right.members)),
                (left.sse, right.sse),
                dimension=dimension,
            )
            proposals.append((
                child_bic - parent_bic,
                index,
                left,
                right,
                iterations,
                parent_bic,
                child_bic,
            ))
        if not proposals:
            break
        viable = [
            proposal for proposal in proposals
            if proposal[0] > bic_margin
        ]
        selected: tuple[
            float, int, _Cluster, _Cluster, int, float, float
        ] | None = None
        if viable:
            selected = max(
                viable,
                key=lambda item: (
                    item[0],
                    -item[1],
                    tuple(-value for value in item[2].centroid),
                ),
            )
        for proposal in proposals:
            gain, index, left, right, iterations, parent_bic, child_bic = (
                proposal)
            trials.append({
                "round": round_number,
                "parent_member_sha256": _member_digest(
                    clusters[index].members),
                "parent_samples": len(clusters[index].members),
                "parent_sse": _rounded(clusters[index].sse),
                "child_samples": [len(left.members), len(right.members)],
                "child_sses": [_rounded(left.sse), _rounded(right.sse)],
                "parent_bic": _rounded(parent_bic),
                "child_bic": _rounded(child_bic),
                "bic_gain": _rounded(gain),
                "iterations": iterations,
                "accepted": proposal is selected,
            })
        if selected is None:
            break
        _, index, left, right, _, _, _ = selected
        clusters[index:index + 1] = [left, right]
        round_number += 1
    clusters.sort(key=lambda item: (item.centroid, _member_digest(item.members)))
    final_bic = _bic(
        [len(cluster.members) for cluster in clusters],
        [cluster.sse for cluster in clusters],
        dimension=dimension,
    )
    return clusters, trials, final_bic


def _event_objective(
    event: TrajectoryEvent,
    *,
    timeout_sec: float,
    cost_weight: float,
) -> tuple[float, float, bool]:
    timed_out = bool(
        event.killed or _bool_value(event.context.get("timed_out", False)))
    event_timeout = max(
        0.01,
        min(
            timeout_sec,
            _finite_float(
                event.context.get("budget_sec"), timeout_sec),
        ),
    )
    par2_cost = (
        2.0 * event_timeout
        if timed_out
        else min(2.0 * event_timeout, max(0.0, event.cost))
    )
    normalized_cost = (
        math.log1p(par2_cost) / math.log1p(2.0 * timeout_sec))
    reward = 0.0 if timed_out else event.adjusted_reward
    return reward - cost_weight * normalized_cost, par2_cost, timed_out


def _action_estimate(
    events: Sequence[TrajectoryEvent],
    action: str,
    *,
    timeout_sec: float,
    cost_weight: float,
    min_propensity: float,
    max_weight: float,
    prior_mean: float,
    prior_strength: float,
    confidence_z: float,
    min_samples: int,
    min_effective_samples: float,
    behavior_mean: float,
    min_improvement: float,
) -> dict[str, Any]:
    rows = []
    for event in events:
        if event.action != action:
            continue
        utility, par2_cost, timed_out = _event_objective(
            event, timeout_sec=timeout_sec, cost_weight=cost_weight)
        weight = min(
            max_weight, 1.0 / max(min_propensity, event.propensity))
        rows.append((utility, par2_cost, timed_out, weight))
    weight_sum = sum(row[3] for row in rows)
    weight_square_sum = sum(row[3] ** 2 for row in rows)
    effective = (
        weight_sum * weight_sum / weight_square_sum
        if weight_square_sum else 0.0
    )
    local_mean = (
        sum(row[0] * row[3] for row in rows) / weight_sum
        if weight_sum else prior_mean
    )
    par2_mean = (
        sum(row[1] * row[3] for row in rows) / weight_sum
        if weight_sum else 2.0 * timeout_sec
    )
    variance = (
        sum(row[3] * (row[0] - local_mean) ** 2 for row in rows)
        / weight_sum if weight_sum else 1.0
    )
    standard_error = (
        math.sqrt(max(0.0, variance) / max(1.0, effective))
        if effective > 1.0 else 1.0
    )
    denominator = effective + prior_strength
    shrunk = (
        (effective * local_mean + prior_strength * prior_mean) / denominator
        if denominator > 0.0 else prior_mean
    )
    lower = shrunk - confidence_z * standard_error
    supported = (
        len(rows) >= min_samples
        and effective >= min_effective_samples
        and lower >= behavior_mean + min_improvement - 1e-10
    )
    return {
        "samples": len(rows),
        "censored": sum(int(row[2]) for row in rows),
        # Preserve full binary64 repr for independent reconstruction. Rounding
        # second moments causes catastrophic cancellation on constant samples.
        "weight_sum": float(weight_sum),
        "weight_square_sum": float(weight_square_sum),
        "utility_sum": float(sum(row[0] for row in rows)),
        "weighted_utility_sum": float(sum(
            row[0] * row[3] for row in rows)),
        "weighted_utility_square_sum": float(sum(
            row[0] ** 2 * row[3] for row in rows)),
        "weighted_par2_sum": float(sum(
            row[1] * row[3] for row in rows)),
        "effective_samples": _rounded(effective),
        "mean_par2_cost": _rounded(par2_mean),
        "local_utility": _rounded(local_mean),
        "prior_utility": _rounded(prior_mean),
        "shrunk_utility": _rounded(shrunk),
        "standard_error": _rounded(standard_error),
        "lower_bound": _rounded(lower),
        "supported": supported,
    }


def _policy_estimates(
    events: Sequence[TrajectoryEvent],
    actions: Sequence[str],
    *,
    timeout_sec: float,
    cost_weight: float,
    min_propensity: float,
    max_weight: float,
    global_means: Mapping[str, float],
    prior_strength: float,
    confidence_z: float,
    min_samples: int,
    min_effective_samples: float,
    min_improvement: float,
) -> tuple[dict[str, dict[str, Any]], float, str]:
    objectives = [
        _event_objective(
            event, timeout_sec=timeout_sec, cost_weight=cost_weight)[0]
        for event in events
    ]
    behavior_mean = (
        sum(objectives) / len(objectives) if objectives else 0.0)
    estimates = {
        action: _action_estimate(
            events,
            action,
            timeout_sec=timeout_sec,
            cost_weight=cost_weight,
            min_propensity=min_propensity,
            max_weight=max_weight,
            prior_mean=global_means.get(action, 0.0),
            prior_strength=prior_strength,
            confidence_z=confidence_z,
            min_samples=min_samples,
            min_effective_samples=min_effective_samples,
            behavior_mean=behavior_mean,
            min_improvement=min_improvement,
        )
        for action in actions
    }
    supported = [
        (estimate["lower_bound"], estimate["effective_samples"], action)
        for action, estimate in estimates.items()
        if estimate["supported"]
    ]
    recommendation = max(supported)[2] if supported else ""
    return estimates, _rounded(behavior_mean), recommendation


def build_sequence_policy(
    events: Iterable[TrajectoryEvent],
    *,
    timeout_sec: float = 30.0,
    max_clusters: int = 8,
    min_cluster_size: int = 8,
    max_iterations: int = 64,
    bic_margin: float = 0.0,
    cost_weight: float = 0.35,
    min_propensity: float = 0.02,
    max_weight: float = 20.0,
    prior_strength: float = 4.0,
    confidence_z: float = 1.96,
    min_samples: int = 4,
    min_effective_samples: float = 4.0,
    min_improvement: float = 0.0,
    feature_schema: str = FEATURE_SCHEMA,
) -> dict[str, Any]:
    materialized = list(events)
    if not materialized:
        raise ValueError("at least one trajectory event is required")
    contract = feature_contract(feature_schema)
    if contract is None:
        raise ValueError("unsupported SMT context feature schema")
    timeout_sec = max(0.01, min(86400.0, float(timeout_sec)))
    max_clusters = max(1, min(32, int(max_clusters)))
    min_cluster_size = max(2, min(10000, int(min_cluster_size)))
    max_iterations = max(1, min(256, int(max_iterations)))
    bic_margin = max(0.0, min(1e9, float(bic_margin)))
    cost_weight = max(0.0, min(10.0, float(cost_weight)))
    min_propensity = max(1e-6, min(1.0, float(min_propensity)))
    max_weight = max(1.0, min(1e6, float(max_weight)))
    prior_strength = max(0.0, min(1e6, float(prior_strength)))
    confidence_z = max(0.0, min(10.0, float(confidence_z)))
    min_samples = max(2, min(100000, int(min_samples)))
    min_effective_samples = max(
        2.0, min(100000.0, float(min_effective_samples)))
    min_improvement = max(0.0, min(2.0, float(min_improvement)))

    actions = tuple(sorted({
        event.action for event in materialized if event.action
    }))
    if not actions:
        raise ValueError("trajectory contains no solver-sequence actions")
    points = [
        context_feature_vector(event.context, schema=feature_schema)
        for event in materialized
    ]
    clusters, trials, final_bic = _xmeans(
        points,
        max_clusters=max_clusters,
        min_cluster_size=min_cluster_size,
        max_iterations=max_iterations,
        bic_margin=bic_margin,
    )

    global_means: dict[str, float] = {}
    global_estimates: dict[str, dict[str, Any]] = {}
    global_objectives = [
        _event_objective(
            event, timeout_sec=timeout_sec, cost_weight=cost_weight)[0]
        for event in materialized
    ]
    global_behavior = sum(global_objectives) / len(global_objectives)
    for action in actions:
        estimate = _action_estimate(
            materialized,
            action,
            timeout_sec=timeout_sec,
            cost_weight=cost_weight,
            min_propensity=min_propensity,
            max_weight=max_weight,
            prior_mean=0.0,
            prior_strength=0.0,
            confidence_z=confidence_z,
            min_samples=min_samples,
            min_effective_samples=min_effective_samples,
            behavior_mean=global_behavior,
            min_improvement=min_improvement,
        )
        global_estimates[action] = estimate
        global_means[action] = estimate["local_utility"]

    cluster_artifacts = []
    for identifier, cluster in enumerate(clusters):
        cluster_events = [materialized[index] for index in cluster.members]
        estimates, behavior_mean, recommendation = _policy_estimates(
            cluster_events,
            actions,
            timeout_sec=timeout_sec,
            cost_weight=cost_weight,
            min_propensity=min_propensity,
            max_weight=max_weight,
            global_means=global_means,
            prior_strength=prior_strength,
            confidence_z=confidence_z,
            min_samples=min_samples,
            min_effective_samples=min_effective_samples,
            min_improvement=min_improvement,
        )
        cluster_artifacts.append({
            "id": identifier,
            "samples": len(cluster.members),
            "member_sha256": _member_digest(cluster.members),
            "centroid": [_rounded(value) for value in cluster.centroid],
            "sse": _rounded(cluster.sse),
            "behavior_utility": behavior_mean,
            "estimates": estimates,
            "recommended_sequence": recommendation,
        })

    event_payload = [asdict(event) for event in materialized]
    configuration = {
        "timeout_sec": _rounded(timeout_sec),
        "max_clusters": max_clusters,
        "min_cluster_size": min_cluster_size,
        "max_iterations": max_iterations,
        "bic_margin": _rounded(bic_margin),
        "cost_weight": _rounded(cost_weight),
        "min_propensity": _rounded(min_propensity),
        "max_weight": _rounded(max_weight),
        "prior_strength": _rounded(prior_strength),
        "confidence_z": _rounded(confidence_z),
        "min_samples": min_samples,
        "min_effective_samples": _rounded(min_effective_samples),
        "min_improvement": _rounded(min_improvement),
    }
    artifact: dict[str, Any] = {
        "schema": POLICY_SCHEMA,
        "training_schema": TRAINING_SCHEMA,
        "feature_schema": feature_schema_mapping(feature_schema),
        "objective_schema": OBJECTIVE_SCHEMA,
        "configuration": configuration,
        "source": {
            "events": len(materialized),
            "actions": list(actions),
            "trajectory_sha256": _sha256(event_payload),
            "explicit_timeout_events": sum(
                int("timed_out" in event.context)
                for event in materialized
            ),
            "censored_events": sum(
                int(
                    event.killed
                    or _bool_value(event.context.get("timed_out", False))
                )
                for event in materialized
            ),
        },
        "xmeans": {
            "algorithm": "bounded-local-bic-xmeans-v1",
            "clusters": len(clusters),
            "final_bic": _rounded(final_bic),
            "split_trials": trials,
        },
        "global_behavior_utility": _rounded(global_behavior),
        "global_estimates": global_estimates,
        "clusters": cluster_artifacts,
    }
    artifact["artifact_sha256"] = _sha256(artifact)
    if not verify_sequence_policy(artifact):
        raise ValueError("internal sequence-policy verification failed")
    return artifact


def _valid_number(
    value: Any,
    *,
    lower: float = -1e300,
    upper: float = 1e300,
) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and lower <= float(value) <= upper
    )


def _verify_estimate(
    estimate: Any,
    *,
    timeout_sec: float,
    prior_strength: float,
    confidence_z: float,
    expected_prior: float,
    min_samples: int,
    min_effective_samples: float,
    behavior_mean: float,
    min_improvement: float,
) -> bool:
    expected = {
        "samples", "censored", "weight_sum", "weight_square_sum",
        "utility_sum", "weighted_utility_sum",
        "weighted_utility_square_sum", "weighted_par2_sum",
        "effective_samples", "mean_par2_cost", "local_utility",
        "prior_utility", "shrunk_utility", "standard_error",
        "lower_bound", "supported",
    }
    if not isinstance(estimate, dict) or set(estimate) != expected:
        return False
    samples = estimate["samples"]
    censored = estimate["censored"]
    if (
        not isinstance(samples, int)
        or isinstance(samples, bool)
        or samples < 0
        or not isinstance(censored, int)
        or isinstance(censored, bool)
        or not 0 <= censored <= samples
    ):
        return False
    bounds = {
        "weight_sum": (0.0, 1e12),
        "weight_square_sum": (0.0, 1e18),
        "utility_sum": (-20.0 * samples, 2.0 * samples),
        "weighted_utility_sum": (-1e13, 1e13),
        "weighted_utility_square_sum": (0.0, 1e15),
        "weighted_par2_sum": (0.0, 2.0 * timeout_sec * 1e12),
        "effective_samples": (0.0, float(max(1, samples))),
        "mean_par2_cost": (0.0, 2.0 * timeout_sec),
        "local_utility": (-20.0, 2.0),
        "prior_utility": (-20.0, 2.0),
        "shrunk_utility": (-20.0, 2.0),
        "standard_error": (0.0, 100.0),
        "lower_bound": (-1000.0, 2.0),
    }
    if any(
        not _valid_number(estimate[name], lower=lower, upper=upper)
        for name, (lower, upper) in bounds.items()
    ):
        return False
    weight_sum = float(estimate["weight_sum"])
    weight_square_sum = float(estimate["weight_square_sum"])
    utility_sum = float(estimate["weighted_utility_sum"])
    utility_square_sum = float(estimate["weighted_utility_square_sum"])
    par2_sum = float(estimate["weighted_par2_sum"])
    effective = (
        weight_sum * weight_sum / weight_square_sum
        if weight_square_sum else 0.0
    )
    local_mean = (
        utility_sum / weight_sum if weight_sum else expected_prior)
    par2_mean = (
        par2_sum / weight_sum if weight_sum else 2.0 * timeout_sec)
    variance = (
        max(0.0, utility_square_sum / weight_sum - local_mean ** 2)
        if weight_sum else 1.0
    )
    standard_error = (
        math.sqrt(variance / max(1.0, effective))
        if effective > 1.0 else 1.0
    )
    denominator = effective + prior_strength
    shrunk = (
        (effective * local_mean + prior_strength * expected_prior)
        / denominator if denominator > 0.0 else expected_prior
    )
    lower = shrunk - confidence_z * standard_error

    def close(left: float, right: Any) -> bool:
        parsed = float(right)
        return abs(left - parsed) <= 1e-7 * max(1.0, abs(left), abs(parsed))

    if not (
        close(effective, estimate["effective_samples"])
        and close(local_mean, estimate["local_utility"])
        and close(par2_mean, estimate["mean_par2_cost"])
        and close(expected_prior, estimate["prior_utility"])
        and close(shrunk, estimate["shrunk_utility"])
        and close(standard_error, estimate["standard_error"])
        and close(lower, estimate["lower_bound"])
    ):
        return False
    expected_supported = (
        samples >= min_samples
        and float(estimate["effective_samples"]) >= min_effective_samples
        and float(estimate["lower_bound"])
        >= behavior_mean + min_improvement - 1e-10
    )
    return (
        isinstance(estimate["supported"], bool)
        and estimate["supported"] == expected_supported
    )


def verify_sequence_policy(artifact: Mapping[str, Any]) -> bool:
    """Fail closed on digest, schema, geometry, BIC, or policy inconsistency."""
    if not isinstance(artifact, dict):
        return False
    expected_top = {
        "schema", "training_schema", "feature_schema", "objective_schema",
        "configuration", "source", "xmeans", "global_behavior_utility",
        "global_estimates", "clusters", "artifact_sha256",
    }
    if set(artifact) != expected_top:
        return False
    supplied = artifact.get("artifact_sha256")
    if not _valid_digest(supplied):
        return False
    core = dict(artifact)
    core.pop("artifact_sha256", None)
    if _sha256(core) != supplied:
        return False
    contract = feature_contract(artifact.get("feature_schema"))
    if contract is None:
        return False
    _, dimension, _ = contract
    if (
        artifact.get("schema") != POLICY_SCHEMA
        or artifact.get("training_schema") != TRAINING_SCHEMA
        or artifact.get("objective_schema") != OBJECTIVE_SCHEMA
    ):
        return False
    configuration = artifact.get("configuration")
    source = artifact.get("source")
    xmeans = artifact.get("xmeans")
    clusters = artifact.get("clusters")
    global_estimates = artifact.get("global_estimates")
    if not all(isinstance(item, dict) for item in (
        configuration, source, xmeans, global_estimates,
    )) or not isinstance(clusters, list):
        return False
    expected_configuration = {
        "timeout_sec", "max_clusters", "min_cluster_size",
        "max_iterations", "bic_margin", "cost_weight", "min_propensity",
        "max_weight", "prior_strength", "confidence_z", "min_samples",
        "min_effective_samples", "min_improvement",
    }
    if set(configuration) != expected_configuration:
        return False
    integer_limits = {
        "max_clusters": (1, 32),
        "min_cluster_size": (2, 10000),
        "max_iterations": (1, 256),
        "min_samples": (2, 100000),
    }
    for name, (lower, upper) in integer_limits.items():
        value = configuration.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not lower <= value <= upper
        ):
            return False
    numeric_limits = {
        "timeout_sec": (0.01, 86400.0),
        "bic_margin": (0.0, 1e9),
        "cost_weight": (0.0, 10.0),
        "min_propensity": (1e-6, 1.0),
        "max_weight": (1.0, 1e6),
        "prior_strength": (0.0, 1e6),
        "confidence_z": (0.0, 10.0),
        "min_effective_samples": (2.0, 100000.0),
        "min_improvement": (0.0, 2.0),
    }
    if any(
        not _valid_number(
            configuration.get(name), lower=lower, upper=upper)
        for name, (lower, upper) in numeric_limits.items()
    ):
        return False
    expected_source = {
        "events", "actions", "trajectory_sha256",
        "explicit_timeout_events", "censored_events",
    }
    if set(source) != expected_source:
        return False
    events = source.get("events")
    actions = source.get("actions")
    if (
        not isinstance(events, int)
        or isinstance(events, bool)
        or events <= 0
        or not isinstance(actions, list)
        or not actions
        or actions != sorted(set(actions))
        or any(
            not isinstance(action, str) or not action or len(action) > 128
            for action in actions
        )
        or not _valid_digest(source.get("trajectory_sha256"))
    ):
        return False
    for name in ("explicit_timeout_events", "censored_events"):
        value = source.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= events
        ):
            return False
    if (
        set(xmeans) != {
            "algorithm", "clusters", "final_bic", "split_trials"}
        or xmeans.get("algorithm") != "bounded-local-bic-xmeans-v1"
        or xmeans.get("clusters") != len(clusters)
        or not 1 <= len(clusters) <= configuration["max_clusters"]
        or not _valid_number(xmeans.get("final_bic"))
        or not isinstance(xmeans.get("split_trials"), list)
    ):
        return False
    timeout_sec = float(configuration["timeout_sec"])
    behavior_global = artifact.get("global_behavior_utility")
    if not _valid_number(behavior_global, lower=-20.0, upper=2.0):
        return False
    if set(global_estimates) != set(actions):
        return False
    if any(
        not _verify_estimate(
            estimate,
            timeout_sec=timeout_sec,
            prior_strength=0.0,
            confidence_z=configuration["confidence_z"],
            expected_prior=0.0,
            min_samples=configuration["min_samples"],
            min_effective_samples=configuration["min_effective_samples"],
            behavior_mean=float(behavior_global),
            min_improvement=configuration["min_improvement"],
        )
        for estimate in global_estimates.values()
    ) or sum(
        estimate["samples"] for estimate in global_estimates.values()
    ) != events or sum(
        estimate["censored"] for estimate in global_estimates.values()
    ) != source["censored_events"]:
        return False
    reconstructed_global_behavior = sum(
        estimate["utility_sum"] for estimate in global_estimates.values()
    ) / events
    if abs(reconstructed_global_behavior - behavior_global) > 1e-8:
        return False

    total_samples = 0
    total_cluster_censored = 0
    total_cluster_utility = 0.0
    final_sizes = []
    final_sses = []
    member_digests: set[str] = set()
    for expected_id, cluster in enumerate(clusters):
        if not isinstance(cluster, dict) or set(cluster) != {
            "id", "samples", "member_sha256", "centroid", "sse",
            "behavior_utility", "estimates", "recommended_sequence",
        }:
            return False
        samples = cluster.get("samples")
        centroid = cluster.get("centroid")
        if (
            cluster.get("id") != expected_id
            or not isinstance(samples, int)
            or isinstance(samples, bool)
            or samples <= 0
            or not _valid_digest(cluster.get("member_sha256"))
            or cluster["member_sha256"] in member_digests
            or not isinstance(centroid, list)
            or len(centroid) != dimension
            or centroid[0] != 1.0
            or any(
                not _valid_number(value, lower=0.0, upper=1.0)
                for value in centroid
            )
            or not _valid_number(cluster.get("sse"), lower=0.0)
            or not _valid_number(
                cluster.get("behavior_utility"), lower=-20.0, upper=2.0)
            or not isinstance(cluster.get("estimates"), dict)
            or set(cluster["estimates"]) != set(actions)
        ):
            return False
        member_digests.add(cluster["member_sha256"])
        if any(
            not _verify_estimate(
                estimate,
                timeout_sec=timeout_sec,
                prior_strength=configuration["prior_strength"],
                confidence_z=configuration["confidence_z"],
                expected_prior=global_estimates[action][
                    "local_utility"],
                min_samples=configuration["min_samples"],
                min_effective_samples=configuration[
                    "min_effective_samples"],
                behavior_mean=float(cluster["behavior_utility"]),
                min_improvement=configuration["min_improvement"],
            )
            for action, estimate in cluster["estimates"].items()
        ) or sum(
            estimate["samples"]
            for estimate in cluster["estimates"].values()
        ) != samples:
            return False
        reconstructed_behavior = sum(
            estimate["utility_sum"]
            for estimate in cluster["estimates"].values()
        ) / samples
        if (
            abs(
                reconstructed_behavior
                - float(cluster["behavior_utility"])
            ) > 1e-8
        ):
            return False
        supported = [
            (
                estimate["lower_bound"],
                estimate["effective_samples"],
                action,
            )
            for action, estimate in cluster["estimates"].items()
            if estimate["supported"]
        ]
        expected_recommendation = max(supported)[2] if supported else ""
        if cluster.get("recommended_sequence") != expected_recommendation:
            return False
        total_samples += samples
        total_cluster_censored += sum(
            estimate["censored"]
            for estimate in cluster["estimates"].values()
        )
        total_cluster_utility += sum(
            estimate["utility_sum"]
            for estimate in cluster["estimates"].values()
        )
        final_sizes.append(samples)
        final_sses.append(float(cluster["sse"]))
    if (
        total_samples != events
        or total_cluster_censored != source["censored_events"]
        or abs(
            total_cluster_utility
            - sum(
                estimate["utility_sum"]
                for estimate in global_estimates.values()
            )
        ) > 1e-7 * max(1.0, abs(total_cluster_utility))
    ):
        return False
    if abs(
        _bic(final_sizes, final_sses, dimension=dimension)
        - xmeans["final_bic"]
    ) > 1e-7:
        return False

    accepted = 0
    accepted_rounds: list[int] = []
    for trial in xmeans["split_trials"]:
        if not isinstance(trial, dict) or set(trial) != {
            "round", "parent_member_sha256", "parent_samples",
            "parent_sse", "child_samples", "child_sses", "parent_bic",
            "child_bic", "bic_gain", "iterations", "accepted",
        }:
            return False
        child_samples = trial.get("child_samples")
        child_sses = trial.get("child_sses")
        if (
            not isinstance(trial.get("round"), int)
            or trial["round"] < 0
            or not _valid_digest(trial.get("parent_member_sha256"))
            or not isinstance(trial.get("parent_samples"), int)
            or trial["parent_samples"] <= 0
            or not _valid_number(trial.get("parent_sse"), lower=0.0)
            or not isinstance(child_samples, list)
            or len(child_samples) != 2
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < configuration["min_cluster_size"]
                for value in child_samples
            )
            or sum(child_samples) != trial["parent_samples"]
            or not isinstance(child_sses, list)
            or len(child_sses) != 2
            or any(not _valid_number(value, lower=0.0) for value in child_sses)
            or not isinstance(trial.get("iterations"), int)
            or not 1 <= trial["iterations"] <= configuration["max_iterations"]
            or not isinstance(trial.get("accepted"), bool)
        ):
            return False
        parent_bic = _bic(
            (trial["parent_samples"],), (trial["parent_sse"],),
            dimension=dimension)
        child_bic = _bic(
            child_samples, child_sses, dimension=dimension)
        gain = child_bic - parent_bic
        if (
            abs(parent_bic - trial.get("parent_bic", float("inf"))) > 1e-7
            or abs(child_bic - trial.get("child_bic", float("inf"))) > 1e-7
            or abs(gain - trial.get("bic_gain", float("inf"))) > 1e-7
            or (
                trial["accepted"]
                and gain <= configuration["bic_margin"]
            )
        ):
            return False
        accepted += int(trial["accepted"])
        if trial["accepted"]:
            accepted_rounds.append(trial["round"])
    return (
        accepted == len(clusters) - 1
        and accepted_rounds == list(range(accepted))
    )


@dataclass(frozen=True)
class ContextSequencePrior:
    artifact_sha256: str
    actions: frozenset[str]
    clusters: tuple[tuple[int, tuple[float, ...], str], ...]
    dimension: int = DIMENSION
    feature_schema: str = FEATURE_SCHEMA

    def recommend(
        self,
        vector: Sequence[float],
    ) -> tuple[str, int | None]:
        normalized = adapt_feature_vector(vector, self.dimension)
        if normalized is None:
            return "", None
        if normalized[0] != 1.0 or any(
            not 0.0 <= value <= 1.0 for value in normalized
        ):
            return "", None
        identifier, _, action = min(
            self.clusters,
            key=lambda item: sum(
                (left - right) ** 2
                for left, right in zip(item[1], normalized)
            ),
        )
        return (action if action in self.actions else ""), identifier


def load_sequence_policy(
    path: str | None,
    *,
    allowed_actions: Iterable[str] | None = None,
) -> ContextSequencePrior | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            artifact = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    if not verify_sequence_policy(artifact):
        return None
    contract = feature_contract(artifact["feature_schema"])
    if contract is None:
        return None
    feature_schema, dimension, _ = contract
    permitted = frozenset(
        str(action) for action in (
            allowed_actions
            if allowed_actions is not None
            else artifact["source"]["actions"]
        )
    )
    clusters = tuple(
        (
            int(cluster["id"]),
            tuple(float(value) for value in cluster["centroid"]),
            str(cluster["recommended_sequence"]),
        )
        for cluster in artifact["clusters"]
    )
    return ContextSequencePrior(
        artifact_sha256=str(artifact["artifact_sha256"]),
        actions=permitted,
        clusters=clusters,
        dimension=dimension,
        feature_schema=feature_schema,
    )


def write_sequence_policy(path: str, artifact: Mapping[str, Any]) -> None:
    if not verify_sequence_policy(artifact):
        raise ValueError("refusing to write an invalid sequence policy")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".smt-sequence-policy-", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                artifact,
                stream,
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train or verify a proof-carrying contextual SMT-sequence prior."))
    parser.add_argument(
        "trajectory", nargs="?", help="Offline trajectory JSONL path")
    parser.add_argument(
        "--output", default="", help="Required output path when training")
    parser.add_argument(
        "--verify", default="", metavar="POLICY",
        help="Verify an existing policy artifact instead of training")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--max-events", type=int, default=100_000)
    parser.add_argument("--max-clusters", type=int, default=8)
    parser.add_argument("--min-cluster-size", type=int, default=8)
    parser.add_argument("--max-iterations", type=int, default=64)
    parser.add_argument("--bic-margin", type=float, default=0.0)
    parser.add_argument("--cost-weight", type=float, default=0.35)
    parser.add_argument("--min-propensity", type=float, default=0.02)
    parser.add_argument("--max-weight", type=float, default=20.0)
    parser.add_argument("--prior-strength", type=float, default=4.0)
    parser.add_argument("--confidence-z", type=float, default=1.96)
    parser.add_argument("--min-samples", type=int, default=4)
    parser.add_argument("--min-effective-samples", type=float, default=4.0)
    parser.add_argument("--min-improvement", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.verify:
        try:
            with open(args.verify, encoding="utf-8") as stream:
                artifact = json.load(stream)
        except (OSError, ValueError, TypeError):
            artifact = {}
        verified = verify_sequence_policy(artifact)
        print(json.dumps({
            "schema": POLICY_SCHEMA,
            "verified": verified,
            "artifact_sha256": (
                artifact.get("artifact_sha256", "")
                if isinstance(artifact, dict) else ""),
        }, sort_keys=True))
        return 0 if verified else 1
    if not args.trajectory or not args.output:
        parser.error("trajectory and --output are required when training")
    events = load_trajectories(
        args.trajectory, max_events=max(1, args.max_events))
    try:
        artifact = build_sequence_policy(
            events,
            timeout_sec=args.timeout_sec,
            max_clusters=args.max_clusters,
            min_cluster_size=args.min_cluster_size,
            max_iterations=args.max_iterations,
            bic_margin=args.bic_margin,
            cost_weight=args.cost_weight,
            min_propensity=args.min_propensity,
            max_weight=args.max_weight,
            prior_strength=args.prior_strength,
            confidence_z=args.confidence_z,
            min_samples=args.min_samples,
            min_effective_samples=args.min_effective_samples,
            min_improvement=args.min_improvement,
        )
        write_sequence_policy(args.output, artifact)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({
        "schema": POLICY_SCHEMA,
        "verified": True,
        "events": artifact["source"]["events"],
        "clusters": artifact["xmeans"]["clusters"],
        "artifact_sha256": artifact["artifact_sha256"],
        "output": args.output,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
