#!/usr/bin/env python3
"""Bagged/boosted censored-cost optimization of SMT action schedules.

This module builds on the verified X-means policy artifact.  It fits bounded,
deterministic shallow-tree ensembles to logged single-action observations, then
searches action/time-slice schedules under one shared timeout budget.  The
result is an exploration prior, not a proof of counterfactual performance.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import random
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from offline_policy import TrajectoryEvent, load_trajectories
from smt_sequence_training import (
    DIMENSION,
    FEATURE_SCHEMA,
    adapt_feature_vector,
    _bool_value,
    _event_objective,
    _sha256,
    _valid_digest,
    _valid_number,
    build_sequence_policy,
    context_feature_vector,
    feature_contract,
    verify_sequence_policy,
)


ENSEMBLE_POLICY_SCHEMA = "symcc-smt-sequence-ensemble-policy-v1"
ENSEMBLE_SCHEMA = "bounded-bagged-boosted-cart-v1"
OPTIMIZER_SCHEMA = "budgeted-sequence-beam-search-v1"
TARGETS = ("completion", "par2_ratio", "reward")


@dataclass(frozen=True)
class _Row:
    vector: tuple[float, ...]
    value: float
    weight: float


def _mean(rows: Sequence[_Row]) -> float:
    weight = sum(row.weight for row in rows)
    return (
        sum(row.weight * row.value for row in rows) / weight
        if weight else 0.0
    )


def _sse(rows: Sequence[_Row]) -> float:
    center = _mean(rows)
    return sum(row.weight * (row.value - center) ** 2 for row in rows)


def _thresholds(
    rows: Sequence[_Row],
    feature: int,
    maximum: int,
) -> tuple[float, ...]:
    values = sorted({row.vector[feature] for row in rows})
    if len(values) < 2:
        return ()
    candidates = [
        0.5 * (values[index - 1] + values[index])
        for index in range(1, len(values))
    ]
    if len(candidates) <= maximum:
        return tuple(candidates)
    selected = {
        candidates[min(
            len(candidates) - 1,
            max(0, int((rank + 0.5) * len(candidates) / maximum)),
        )]
        for rank in range(maximum)
    }
    return tuple(sorted(selected))


def _fit_tree(
    rows: Sequence[_Row],
    *,
    depth: int,
    max_depth: int,
    min_leaf: int,
    max_thresholds: int,
) -> dict[str, Any]:
    value = _mean(rows)
    leaf = {
        "kind": "leaf",
        "value": float(value),
        "samples": len(rows),
        "weight": float(sum(row.weight for row in rows)),
    }
    if depth >= max_depth or len(rows) < 2 * min_leaf:
        return leaf
    parent_sse = _sse(rows)
    best: tuple[
        float, int, float, list[_Row], list[_Row]
    ] | None = None
    dimension = len(rows[0].vector)
    for feature in range(1, dimension):
        for threshold in _thresholds(rows, feature, max_thresholds):
            left = [row for row in rows if row.vector[feature] <= threshold]
            right = [row for row in rows if row.vector[feature] > threshold]
            if len(left) < min_leaf or len(right) < min_leaf:
                continue
            loss = _sse(left) + _sse(right)
            candidate = (loss, feature, threshold, left, right)
            if best is None or (
                candidate[0], candidate[1], candidate[2]
            ) < (best[0], best[1], best[2]):
                best = candidate
    if best is None or best[0] >= parent_sse - 1e-12:
        return leaf
    _, feature, threshold, left, right = best
    return {
        "kind": "split",
        "feature": feature,
        "threshold": float(threshold),
        "samples": len(rows),
        "weight": float(sum(row.weight for row in rows)),
        "left": _fit_tree(
            left,
            depth=depth + 1,
            max_depth=max_depth,
            min_leaf=min_leaf,
            max_thresholds=max_thresholds,
        ),
        "right": _fit_tree(
            right,
            depth=depth + 1,
            max_depth=max_depth,
            min_leaf=min_leaf,
            max_thresholds=max_thresholds,
        ),
    }


def _tree_predict(tree: Mapping[str, Any], vector: Sequence[float]) -> float:
    node = tree
    while node.get("kind") == "split":
        feature = int(node["feature"])
        node = (
            node["left"]
            if vector[feature] <= float(node["threshold"])
            else node["right"]
        )
    return float(node["value"])


def _stable_seed(seed: int, *parts: Any) -> int:
    payload = "\0".join([str(seed), *(str(part) for part in parts)])
    return int.from_bytes(
        hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")


def _train_ensemble(
    rows: Sequence[_Row],
    *,
    target: str,
    lower: float,
    upper: float,
    bag_estimators: int,
    boost_rounds: int,
    tree_depth: int,
    min_leaf: int,
    max_thresholds: int,
    learning_rate: float,
    seed: int,
    action: str,
) -> dict[str, Any]:
    baseline = _mean(rows)
    bag_trees = []
    for index in range(bag_estimators):
        rng = random.Random(_stable_seed(seed, action, target, "bag", index))
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        bag_trees.append(_fit_tree(
            sample,
            depth=0,
            max_depth=tree_depth,
            min_leaf=min_leaf,
            max_thresholds=max_thresholds,
        ))

    predictions = [baseline] * len(rows)
    boost_trees = []
    for index in range(boost_rounds):
        residuals = [
            _Row(row.vector, row.value - predictions[position], row.weight)
            for position, row in enumerate(rows)
        ]
        tree = _fit_tree(
            residuals,
            depth=0,
            max_depth=tree_depth,
            min_leaf=min_leaf,
            max_thresholds=max_thresholds,
        )
        boost_trees.append(tree)
        for position, row in enumerate(rows):
            predictions[position] += learning_rate * _tree_predict(
                tree, row.vector)
    weight_sum = sum(row.weight for row in rows)
    residual_rmse = math.sqrt(
        sum(
            row.weight * (row.value - predictions[position]) ** 2
            for position, row in enumerate(rows)
        ) / max(1e-12, weight_sum)
    )
    return {
        "schema": ENSEMBLE_SCHEMA,
        "target": target,
        "bounds": [float(lower), float(upper)],
        "training_samples": len(rows),
        "training_weight": float(weight_sum),
        "baseline": float(baseline),
        "learning_rate": float(learning_rate),
        "residual_rmse": float(residual_rmse),
        "bag_trees": bag_trees,
        "boost_trees": boost_trees,
    }


def _ensemble_predict(
    model: Mapping[str, Any],
    vector: Sequence[float],
) -> tuple[float, float]:
    bag_predictions = [
        _tree_predict(tree, vector) for tree in model["bag_trees"]
    ]
    bag_mean = sum(bag_predictions) / len(bag_predictions)
    boost = float(model["baseline"]) + float(
        model["learning_rate"]) * sum(
            _tree_predict(tree, vector)
            for tree in model["boost_trees"]
        )
    prediction = 0.5 * (bag_mean + boost)
    bag_variance = sum(
        (value - bag_mean) ** 2 for value in bag_predictions
    ) / len(bag_predictions)
    uncertainty = math.sqrt(
        bag_variance + float(model["residual_rmse"]) ** 2)
    lower, upper = (float(value) for value in model["bounds"])
    return (
        max(lower, min(upper, prediction)),
        max(0.0, min(upper - lower, uncertainty)),
    )


def _verify_tree(
    tree: Any,
    *,
    depth: int,
    max_depth: int,
    target_lower: float,
    target_upper: float,
    dimension: int = DIMENSION,
) -> tuple[bool, int]:
    if not isinstance(tree, dict):
        return False, 0
    kind = tree.get("kind")
    if kind == "leaf":
        if set(tree) != {"kind", "value", "samples", "weight"}:
            return False, 0
        valid = (
            _valid_number(
                tree.get("value"),
                lower=target_lower - 2.0,
                upper=target_upper + 2.0,
            )
            and isinstance(tree.get("samples"), int)
            and not isinstance(tree["samples"], bool)
            and tree["samples"] > 0
            and _valid_number(tree.get("weight"), lower=0.0)
        )
        return valid, 1
    if kind != "split" or set(tree) != {
        "kind", "feature", "threshold", "samples", "weight", "left", "right",
    }:
        return False, 0
    feature = tree.get("feature")
    if (
        depth >= max_depth
        or not isinstance(feature, int)
        or isinstance(feature, bool)
        or not 1 <= feature < dimension
        or not _valid_number(tree.get("threshold"), lower=0.0, upper=1.0)
        or not isinstance(tree.get("samples"), int)
        or tree["samples"] <= 0
        or not _valid_number(tree.get("weight"), lower=0.0)
    ):
        return False, 0
    left_valid, left_nodes = _verify_tree(
        tree["left"],
        depth=depth + 1,
        max_depth=max_depth,
        target_lower=target_lower,
        target_upper=target_upper,
        dimension=dimension,
    )
    right_valid, right_nodes = _verify_tree(
        tree["right"],
        depth=depth + 1,
        max_depth=max_depth,
        target_lower=target_lower,
        target_upper=target_upper,
        dimension=dimension,
    )
    return (
        left_valid
        and right_valid
        and tree["samples"]
        == tree["left"]["samples"] + tree["right"]["samples"],
        1 + left_nodes + right_nodes,
    )


def _schedule_key(schedule: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (str(stage["action"]), float(stage["budget_sec"]))
        for stage in schedule
    )


def _evaluate_schedule(
    schedule: Sequence[Mapping[str, Any]],
    contexts: Sequence[Mapping[str, Any]],
    models: Mapping[str, Any],
    *,
    timeout_sec: float,
    uncertainty_z: float,
    reward_weight: float,
) -> dict[str, Any]:
    total_weight = sum(float(context["weight"]) for context in contexts)
    objective_sum = 0.0
    par2_sum = 0.0
    reward_sum = 0.0
    unsolved_sum = 0.0
    total_budget = sum(float(stage["budget_sec"]) for stage in schedule)
    for context in contexts:
        vector = context["vector"]
        reach = 1.0
        expected_cost = 0.0
        expected_reward = 0.0
        for stage in schedule:
            action_models = models[str(stage["action"])]
            completion, completion_u = _ensemble_predict(
                action_models["completion"], vector)
            cost_ratio, cost_u = _ensemble_predict(
                action_models["par2_ratio"], vector)
            reward, reward_u = _ensemble_predict(
                action_models["reward"], vector)
            completion_lcb = max(
                0.0, min(1.0, completion - uncertainty_z * completion_u))
            cost_ucb = max(
                0.0, min(1.0, cost_ratio + uncertainty_z * cost_u))
            reward_lcb = max(
                -1.0, min(1.0, reward - uncertainty_z * reward_u))
            budget = float(stage["budget_sec"])
            predicted_cost = max(1e-9, cost_ucb * 2.0 * timeout_sec)
            within = completion_lcb * min(1.0, budget / predicted_cost)
            success_cost = min(budget, predicted_cost)
            expected_cost += reach * (
                within * success_cost + (1.0 - within) * budget)
            expected_reward += reach * within * reward_lcb
            reach *= 1.0 - within
        expected_par2 = expected_cost + reach * max(
            0.0, 2.0 * timeout_sec - total_budget)
        objective = (
            expected_par2 / (2.0 * timeout_sec)
            - reward_weight * expected_reward
        )
        weight = float(context["weight"])
        objective_sum += weight * objective
        par2_sum += weight * expected_par2
        reward_sum += weight * expected_reward
        unsolved_sum += weight * reach
    return {
        "schedule": [
            {
                "action": str(stage["action"]),
                "budget_sec": round(float(stage["budget_sec"]), 12),
            }
            for stage in schedule
        ],
        "objective": round(objective_sum / total_weight, 12),
        "mean_par2": round(par2_sum / total_weight, 12),
        "mean_reward_lcb": round(reward_sum / total_weight, 12),
        "unsolved_probability": round(unsolved_sum / total_weight, 12),
    }


def _optimize_schedule(
    contexts: Sequence[Mapping[str, Any]],
    models: Mapping[str, Any],
    *,
    timeout_sec: float,
    slice_fractions: Sequence[float],
    max_schedule_length: int,
    beam_width: int,
    uncertainty_z: float,
    reward_weight: float,
    min_schedule_improvement: float,
) -> dict[str, Any]:
    actions = tuple(sorted(models))
    beam: list[tuple[dict[str, Any], ...]] = [()]
    evaluated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for _ in range(max_schedule_length):
        expanded: list[tuple[dict[str, Any], ...]] = []
        for prefix in beam:
            used = {stage["action"] for stage in prefix}
            consumed = sum(stage["budget_sec"] for stage in prefix)
            for action in actions:
                if action in used:
                    continue
                for fraction in slice_fractions:
                    budget = float(max(1, int(round(timeout_sec * fraction))))
                    if consumed + budget > timeout_sec + 1e-10:
                        continue
                    schedule = prefix + ({
                        "action": action, "budget_sec": budget},)
                    key = _schedule_key(schedule)
                    if key not in evaluated:
                        evaluated[key] = _evaluate_schedule(
                            schedule,
                            contexts,
                            models,
                            timeout_sec=timeout_sec,
                            uncertainty_z=uncertainty_z,
                            reward_weight=reward_weight,
                        )
                    expanded.append(schedule)
        if not expanded:
            break
        expanded.sort(key=lambda schedule: (
            evaluated[_schedule_key(schedule)]["objective"],
            _schedule_key(schedule),
        ))
        beam = expanded[:beam_width]
    singles = [
        result for result in evaluated.values()
        if len(result["schedule"]) == 1
    ]
    if not singles:
        raise ValueError("optimizer could not construct a single-action schedule")
    best_single = min(
        singles, key=lambda result: (
            result["objective"], _schedule_key(result["schedule"])))
    best = min(
        evaluated.values(), key=lambda result: (
            result["objective"], _schedule_key(result["schedule"])))
    optimized = (
        len(best["schedule"]) > 1
        and best["objective"]
        <= best_single["objective"] - min_schedule_improvement
    )
    selected = best if optimized else best_single
    return {
        "schema": OPTIMIZER_SCHEMA,
        "candidate_count": len(evaluated),
        "optimized_multi_action": optimized,
        "best_single_objective": best_single["objective"],
        **selected,
    }


def _event_rows(
    events: Sequence[TrajectoryEvent],
    action: str,
    *,
    timeout_sec: float,
    min_propensity: float,
    max_weight: float,
    feature_schema: str = FEATURE_SCHEMA,
) -> dict[str, list[_Row]]:
    result = {target: [] for target in TARGETS}
    for event in events:
        if event.action != action:
            continue
        _, par2, timed_out = _event_objective(
            event, timeout_sec=timeout_sec, cost_weight=0.0)
        weight = min(
            max_weight, 1.0 / max(min_propensity, event.propensity))
        vector = context_feature_vector(
            event.context, schema=feature_schema)
        values = {
            "completion": (
                0.0
                if timed_out
                else float(_bool_value(event.context.get("solved", True)))
            ),
            "par2_ratio": par2 / (2.0 * timeout_sec),
            "reward": 0.0 if timed_out else event.adjusted_reward,
        }
        for target, value in values.items():
            result[target].append(_Row(vector, value, weight))
    return result


def build_ensemble_sequence_policy(
    events: Iterable[TrajectoryEvent],
    *,
    timeout_sec: float = 30.0,
    max_clusters: int = 8,
    min_cluster_size: int = 8,
    min_model_samples: int = 8,
    bag_estimators: int = 7,
    boost_rounds: int = 5,
    tree_depth: int = 3,
    min_leaf: int = 4,
    max_thresholds: int = 16,
    learning_rate: float = 0.2,
    beam_width: int = 64,
    max_schedule_length: int = 3,
    slice_fractions: Sequence[float] = (0.25, 0.5, 1.0),
    uncertainty_z: float = 1.0,
    reward_weight: float = 0.25,
    min_schedule_improvement: float = 0.01,
    min_propensity: float = 0.02,
    max_weight: float = 20.0,
    seed: int = 0,
    feature_schema: str = FEATURE_SCHEMA,
) -> dict[str, Any]:
    materialized = list(events)
    if not materialized:
        raise ValueError("at least one trajectory event is required")
    timeout_sec = max(1.0, min(86400.0, float(timeout_sec)))
    max_clusters = max(1, min(32, int(max_clusters)))
    min_cluster_size = max(2, min(10000, int(min_cluster_size)))
    min_model_samples = max(4, min(100000, int(min_model_samples)))
    bag_estimators = max(3, min(32, int(bag_estimators)))
    boost_rounds = max(1, min(32, int(boost_rounds)))
    tree_depth = max(1, min(5, int(tree_depth)))
    min_leaf = max(2, min(1024, int(min_leaf)))
    max_thresholds = max(2, min(32, int(max_thresholds)))
    learning_rate = max(0.01, min(1.0, float(learning_rate)))
    beam_width = max(4, min(1024, int(beam_width)))
    max_schedule_length = max(1, min(8, int(max_schedule_length)))
    normalized_fractions = tuple(sorted({
        round(max(0.01, min(1.0, float(value))), 12)
        for value in slice_fractions
    }))
    uncertainty_z = max(0.0, min(10.0, float(uncertainty_z)))
    reward_weight = max(0.0, min(2.0, float(reward_weight)))
    min_schedule_improvement = max(
        0.0, min(1.0, float(min_schedule_improvement)))
    min_propensity = max(1e-6, min(1.0, float(min_propensity)))
    max_weight = max(1.0, min(1e6, float(max_weight)))
    seed = int(seed)

    base_policy = build_sequence_policy(
        materialized,
        timeout_sec=timeout_sec,
        max_clusters=max_clusters,
        min_cluster_size=min_cluster_size,
        min_propensity=min_propensity,
        max_weight=max_weight,
        min_samples=max(2, min_model_samples // 2),
        min_effective_samples=max(2.0, min_model_samples / 2.0),
        feature_schema=feature_schema,
    )
    actions = [
        action for action in base_policy["source"]["actions"]
        if base_policy["global_estimates"][action]["samples"]
        >= min_model_samples
    ]
    if not actions:
        raise ValueError("no action has enough observations for an ensemble")

    target_bounds = {
        "completion": (0.0, 1.0),
        "par2_ratio": (0.0, 1.0),
        "reward": (-1.0, 1.0),
    }
    models: dict[str, Any] = {}
    for action in actions:
        rows_by_target = _event_rows(
            materialized,
            action,
            timeout_sec=timeout_sec,
            min_propensity=min_propensity,
            max_weight=max_weight,
            feature_schema=feature_schema,
        )
        models[action] = {
            target: _train_ensemble(
                rows_by_target[target],
                target=target,
                lower=target_bounds[target][0],
                upper=target_bounds[target][1],
                bag_estimators=bag_estimators,
                boost_rounds=boost_rounds,
                tree_depth=tree_depth,
                min_leaf=min_leaf,
                max_thresholds=max_thresholds,
                learning_rate=learning_rate,
                seed=seed,
                action=action,
            )
            for target in TARGETS
        }

    contexts = [
        {
            "id": int(cluster["id"]),
            "vector": [float(value) for value in cluster["centroid"]],
            "weight": int(cluster["samples"]),
        }
        for cluster in base_policy["clusters"]
    ]
    optimizer_arguments = {
        "timeout_sec": timeout_sec,
        "slice_fractions": normalized_fractions,
        "max_schedule_length": max_schedule_length,
        "beam_width": beam_width,
        "uncertainty_z": uncertainty_z,
        "reward_weight": reward_weight,
        "min_schedule_improvement": min_schedule_improvement,
    }
    global_schedule = _optimize_schedule(
        contexts, models, **optimizer_arguments)
    local_schedules = [
        {
            "cluster_id": context["id"],
            "cluster_weight": context["weight"],
            **_optimize_schedule([context], models, **optimizer_arguments),
        }
        for context in contexts
    ]
    configuration = {
        "timeout_sec": timeout_sec,
        "max_clusters": max_clusters,
        "min_cluster_size": min_cluster_size,
        "min_model_samples": min_model_samples,
        "bag_estimators": bag_estimators,
        "boost_rounds": boost_rounds,
        "tree_depth": tree_depth,
        "min_leaf": min_leaf,
        "max_thresholds": max_thresholds,
        "learning_rate": learning_rate,
        "beam_width": beam_width,
        "max_schedule_length": max_schedule_length,
        "slice_fractions": list(normalized_fractions),
        "uncertainty_z": uncertainty_z,
        "reward_weight": reward_weight,
        "min_schedule_improvement": min_schedule_improvement,
        "min_propensity": min_propensity,
        "max_weight": max_weight,
        "seed": seed,
    }
    artifact: dict[str, Any] = {
        "schema": ENSEMBLE_POLICY_SCHEMA,
        "ensemble_schema": ENSEMBLE_SCHEMA,
        "optimizer_schema": OPTIMIZER_SCHEMA,
        "feature_schema": dict(base_policy["feature_schema"]),
        "configuration": configuration,
        "source": {
            "events": len(materialized),
            "actions": list(base_policy["source"]["actions"]),
            "modeled_actions": sorted(models),
            "trajectory_sha256": _sha256(
                [asdict(event) for event in materialized]),
            "base_policy_sha256": base_policy["artifact_sha256"],
        },
        "base_policy": base_policy,
        "models": models,
        "optimization_contexts": contexts,
        "global_schedule": global_schedule,
        "cluster_schedules": local_schedules,
    }
    artifact["artifact_sha256"] = _sha256(artifact)
    if not verify_ensemble_sequence_policy(artifact):
        raise ValueError("internal ensemble-policy verification failed")
    return artifact


def _verify_model(
    model: Any,
    *,
    target: str,
    configuration: Mapping[str, Any],
    expected_samples: int,
    expected_weight: float,
    dimension: int,
) -> bool:
    if not isinstance(model, dict) or set(model) != {
        "schema", "target", "bounds", "training_samples", "training_weight",
        "baseline", "learning_rate", "residual_rmse", "bag_trees",
        "boost_trees",
    }:
        return False
    expected_bounds = {
        "completion": [0.0, 1.0],
        "par2_ratio": [0.0, 1.0],
        "reward": [-1.0, 1.0],
    }[target]
    if (
        model.get("schema") != ENSEMBLE_SCHEMA
        or model.get("target") != target
        or model.get("bounds") != expected_bounds
        or model.get("training_samples") != expected_samples
        or not _valid_number(model.get("training_weight"), lower=0.0)
        or abs(float(model["training_weight"]) - expected_weight)
        > 1e-8 * max(1.0, expected_weight)
        or not _valid_number(
            model.get("baseline"),
            lower=expected_bounds[0],
            upper=expected_bounds[1],
        )
        or model.get("learning_rate") != configuration["learning_rate"]
        or not _valid_number(model.get("residual_rmse"), lower=0.0, upper=2.0)
        or not isinstance(model.get("bag_trees"), list)
        or len(model["bag_trees"]) != configuration["bag_estimators"]
        or not isinstance(model.get("boost_trees"), list)
        or len(model["boost_trees"]) != configuration["boost_rounds"]
    ):
        return False
    for tree in [*model["bag_trees"], *model["boost_trees"]]:
        valid, nodes = _verify_tree(
            tree,
            depth=0,
            max_depth=configuration["tree_depth"],
            target_lower=expected_bounds[0],
            target_upper=expected_bounds[1],
            dimension=dimension,
        )
        if (
            not valid
            or nodes > 2 ** (configuration["tree_depth"] + 1) - 1
            or tree["samples"] != expected_samples
        ):
            return False
    if any(
        abs(float(tree["weight"]) - expected_weight)
        > 1e-8 * max(1.0, expected_weight)
        for tree in model["boost_trees"]
    ):
        return False
    return True


def verify_ensemble_sequence_policy(artifact: Mapping[str, Any]) -> bool:
    if not isinstance(artifact, dict) or set(artifact) != {
        "schema", "ensemble_schema", "optimizer_schema", "feature_schema",
        "configuration", "source", "base_policy", "models",
        "optimization_contexts", "global_schedule", "cluster_schedules",
        "artifact_sha256",
    }:
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
        artifact.get("schema") != ENSEMBLE_POLICY_SCHEMA
        or artifact.get("ensemble_schema") != ENSEMBLE_SCHEMA
        or artifact.get("optimizer_schema") != OPTIMIZER_SCHEMA
        or not verify_sequence_policy(artifact.get("base_policy", {}))
        or artifact.get("feature_schema")
        != artifact["base_policy"].get("feature_schema")
    ):
        return False
    configuration = artifact.get("configuration")
    source = artifact.get("source")
    models = artifact.get("models")
    contexts = artifact.get("optimization_contexts")
    if not all(isinstance(item, dict) for item in (
        configuration, source, models,
    )) or not isinstance(contexts, list):
        return False
    expected_configuration = {
        "timeout_sec", "max_clusters", "min_cluster_size",
        "min_model_samples", "bag_estimators", "boost_rounds", "tree_depth",
        "min_leaf", "max_thresholds", "learning_rate", "beam_width",
        "max_schedule_length", "slice_fractions", "uncertainty_z",
        "reward_weight", "min_schedule_improvement", "min_propensity",
        "max_weight", "seed",
    }
    if set(configuration) != expected_configuration:
        return False
    integer_bounds = {
        "max_clusters": (1, 32),
        "min_cluster_size": (2, 10000),
        "min_model_samples": (4, 100000),
        "bag_estimators": (3, 32),
        "boost_rounds": (1, 32),
        "tree_depth": (1, 5),
        "min_leaf": (2, 1024),
        "max_thresholds": (2, 32),
        "beam_width": (4, 1024),
        "max_schedule_length": (1, 8),
    }
    for name, bounds in integer_bounds.items():
        value = configuration.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not bounds[0] <= value <= bounds[1]
        ):
            return False
    numeric_bounds = {
        "timeout_sec": (1.0, 86400.0),
        "learning_rate": (0.01, 1.0),
        "uncertainty_z": (0.0, 10.0),
        "reward_weight": (0.0, 2.0),
        "min_schedule_improvement": (0.0, 1.0),
        "min_propensity": (1e-6, 1.0),
        "max_weight": (1.0, 1e6),
    }
    if any(
        not _valid_number(configuration.get(name), lower=low, upper=high)
        for name, (low, high) in numeric_bounds.items()
    ) or (
        not isinstance(configuration.get("seed"), int)
        or isinstance(configuration["seed"], bool)
    ):
        return False
    fractions = configuration.get("slice_fractions")
    if (
        not isinstance(fractions, list)
        or not fractions
        or fractions != sorted(set(fractions))
        or any(not _valid_number(value, lower=0.01, upper=1.0)
               for value in fractions)
    ):
        return False
    base = artifact["base_policy"]
    base_configuration = base["configuration"]
    if (
        set(source) != {
            "events", "actions", "modeled_actions", "trajectory_sha256",
            "base_policy_sha256",
        }
        or source.get("events") != base["source"]["events"]
        or source.get("actions") != base["source"]["actions"]
        or source.get("trajectory_sha256") != base["source"][
            "trajectory_sha256"]
        or source.get("base_policy_sha256") != base["artifact_sha256"]
        or not isinstance(source.get("modeled_actions"), list)
        or source["modeled_actions"] != sorted(models)
        or not set(models).issubset(source["actions"])
        or base_configuration["timeout_sec"] != configuration["timeout_sec"]
        or base_configuration["max_clusters"] != configuration["max_clusters"]
        or base_configuration["min_cluster_size"]
        != configuration["min_cluster_size"]
        or base_configuration["min_propensity"]
        != configuration["min_propensity"]
        or base_configuration["max_weight"] != configuration["max_weight"]
    ):
        return False
    for action, target_models in models.items():
        if (
            not isinstance(target_models, dict)
            or set(target_models) != set(TARGETS)
        ):
            return False
        samples = base["global_estimates"][action]["samples"]
        expected_weight = base["global_estimates"][action]["weight_sum"]
        if samples < configuration["min_model_samples"]:
            return False
        if any(
            not _verify_model(
                target_models[target],
                target=target,
                configuration=configuration,
                expected_samples=samples,
                expected_weight=expected_weight,
                dimension=dimension,
            )
            for target in TARGETS
        ):
            return False
    expected_contexts = [
        {
            "id": cluster["id"],
            "vector": cluster["centroid"],
            "weight": cluster["samples"],
        }
        for cluster in base["clusters"]
    ]
    if contexts != expected_contexts:
        return False
    arguments = {
        "timeout_sec": configuration["timeout_sec"],
        "slice_fractions": configuration["slice_fractions"],
        "max_schedule_length": configuration["max_schedule_length"],
        "beam_width": configuration["beam_width"],
        "uncertainty_z": configuration["uncertainty_z"],
        "reward_weight": configuration["reward_weight"],
        "min_schedule_improvement": configuration[
            "min_schedule_improvement"],
    }
    try:
        expected_global = _optimize_schedule(contexts, models, **arguments)
        expected_local = [
            {
                "cluster_id": context["id"],
                "cluster_weight": context["weight"],
                **_optimize_schedule([context], models, **arguments),
            }
            for context in contexts
        ]
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (
        artifact.get("global_schedule") == expected_global
        and artifact.get("cluster_schedules") == expected_local
    )


@dataclass(frozen=True)
class EnsembleSequencePrior:
    artifact_sha256: str
    actions: frozenset[str]
    clusters: tuple[
        tuple[
            int,
            tuple[float, ...],
            tuple[tuple[str, float], ...],
        ], ...
    ]
    global_schedule: tuple[tuple[str, float], ...]
    kind: str = "bagged-boosted-sequence"
    dimension: int = DIMENSION
    feature_schema: str = FEATURE_SCHEMA

    def budgeted_schedule(
        self,
        vector: Sequence[float],
    ) -> tuple[tuple[tuple[str, float], ...], int | None]:
        normalized = adapt_feature_vector(vector, self.dimension)
        if normalized is None:
            return (), None
        if (
            normalized[0] != 1.0
            or any(
                not math.isfinite(value) or not 0.0 <= value <= 1.0
                for value in normalized
            )
        ):
            return (), None
        identifier, _, schedule = min(
            self.clusters,
            key=lambda item: sum(
                (left - right) ** 2
                for left, right in zip(item[1], normalized)
            ),
        )
        permitted = tuple(
            stage for stage in schedule if stage[0] in self.actions)
        if not permitted:
            permitted = tuple(
                stage for stage in self.global_schedule
                if stage[0] in self.actions)
        return permitted, identifier

    def schedule(
        self,
        vector: Sequence[float],
    ) -> tuple[tuple[str, ...], int | None]:
        schedule, identifier = self.budgeted_schedule(vector)
        return tuple(stage[0] for stage in schedule), identifier

    def recommend(
        self,
        vector: Sequence[float],
    ) -> tuple[str, int | None]:
        schedule, identifier = self.schedule(vector)
        return (schedule[0] if schedule else ""), identifier


def load_ensemble_sequence_policy(
    path: str | None,
    *,
    allowed_actions: Iterable[str] | None = None,
) -> EnsembleSequencePrior | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            artifact = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    if not verify_ensemble_sequence_policy(artifact):
        return None
    contract = feature_contract(artifact["feature_schema"])
    if contract is None:
        return None
    feature_schema, dimension, _ = contract
    permitted = frozenset(
        str(action) for action in (
            allowed_actions
            if allowed_actions is not None
            else artifact["source"]["modeled_actions"]
        )
    )
    global_schedule = tuple(
        (str(stage["action"]), float(stage["budget_sec"]))
        for stage in artifact["global_schedule"]["schedule"]
    )
    local_by_id = {
        int(item["cluster_id"]): tuple(
            (str(stage["action"]), float(stage["budget_sec"]))
            for stage in item["schedule"])
        for item in artifact["cluster_schedules"]
    }
    clusters = tuple(
        (
            int(context["id"]),
            tuple(float(value) for value in context["vector"]),
            local_by_id[int(context["id"])],
        )
        for context in artifact["optimization_contexts"]
    )
    return EnsembleSequencePrior(
        artifact_sha256=str(artifact["artifact_sha256"]),
        actions=permitted,
        clusters=clusters,
        global_schedule=global_schedule,
        dimension=dimension,
        feature_schema=feature_schema,
    )


def write_ensemble_sequence_policy(
    path: str,
    artifact: Mapping[str, Any],
) -> None:
    if not verify_ensemble_sequence_policy(artifact):
        raise ValueError("refusing to write an invalid ensemble policy")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".smt-sequence-ensemble-", dir=directory, text=True)
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


def _fractions(raw: str) -> tuple[float, ...]:
    try:
        values = tuple(
            float(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if not values:
        raise argparse.ArgumentTypeError("at least one slice fraction required")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train or verify a bagged/boosted censored SMT schedule prior."))
    parser.add_argument("trajectory", nargs="?")
    parser.add_argument("--output", default="")
    parser.add_argument("--verify", default="", metavar="POLICY")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--max-events", type=int, default=20_000)
    parser.add_argument("--max-clusters", type=int, default=8)
    parser.add_argument("--min-cluster-size", type=int, default=8)
    parser.add_argument("--min-model-samples", type=int, default=8)
    parser.add_argument("--bag-estimators", type=int, default=7)
    parser.add_argument("--boost-rounds", type=int, default=5)
    parser.add_argument("--tree-depth", type=int, default=3)
    parser.add_argument("--min-leaf", type=int, default=4)
    parser.add_argument("--max-thresholds", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=0.2)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--max-schedule-length", type=int, default=3)
    parser.add_argument(
        "--slice-fractions", type=_fractions, default=(0.25, 0.5, 1.0))
    parser.add_argument("--uncertainty-z", type=float, default=1.0)
    parser.add_argument("--reward-weight", type=float, default=0.25)
    parser.add_argument("--min-schedule-improvement", type=float, default=0.01)
    parser.add_argument("--min-propensity", type=float, default=0.02)
    parser.add_argument("--max-weight", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.verify:
        try:
            with open(args.verify, encoding="utf-8") as stream:
                artifact = json.load(stream)
        except (OSError, ValueError, TypeError):
            artifact = {}
        verified = verify_ensemble_sequence_policy(artifact)
        print(json.dumps({
            "schema": ENSEMBLE_POLICY_SCHEMA,
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
        artifact = build_ensemble_sequence_policy(
            events,
            timeout_sec=args.timeout_sec,
            max_clusters=args.max_clusters,
            min_cluster_size=args.min_cluster_size,
            min_model_samples=args.min_model_samples,
            bag_estimators=args.bag_estimators,
            boost_rounds=args.boost_rounds,
            tree_depth=args.tree_depth,
            min_leaf=args.min_leaf,
            max_thresholds=args.max_thresholds,
            learning_rate=args.learning_rate,
            beam_width=args.beam_width,
            max_schedule_length=args.max_schedule_length,
            slice_fractions=args.slice_fractions,
            uncertainty_z=args.uncertainty_z,
            reward_weight=args.reward_weight,
            min_schedule_improvement=args.min_schedule_improvement,
            min_propensity=args.min_propensity,
            max_weight=args.max_weight,
            seed=args.seed,
        )
        write_ensemble_sequence_policy(args.output, artifact)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({
        "schema": ENSEMBLE_POLICY_SCHEMA,
        "verified": True,
        "events": artifact["source"]["events"],
        "actions": len(artifact["source"]["modeled_actions"]),
        "clusters": len(artifact["optimization_contexts"]),
        "artifact_sha256": artifact["artifact_sha256"],
        "output": args.output,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
