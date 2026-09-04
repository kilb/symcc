#!/usr/bin/env python3
"""Budgeted schedule-level SMBO with expected-improvement acquisition."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from smt_sequence_optimizer import (
    ENSEMBLE_POLICY_SCHEMA,
    EnsembleSequencePrior,
    _Row,
    _ensemble_predict,
    _evaluate_schedule,
    _schedule_key,
    _stable_seed,
    _train_ensemble,
    verify_ensemble_sequence_policy,
)
from smt_sequence_training import (
    _sha256,
    _valid_digest,
    _valid_number,
    feature_contract,
)


SMBO_POLICY_SCHEMA = "symcc-smt-schedule-smbo-policy-v1"
SMBO_SCHEMA = "bounded-expected-improvement-smbo-v1"


def _candidate_pool(
    actions: Sequence[str],
    *,
    timeout_sec: float,
    slice_fractions: Sequence[float],
    max_schedule_length: int,
    max_candidates: int,
    seed: int,
) -> list[tuple[dict[str, Any], ...]]:
    budgets = tuple(sorted({
        float(max(1, int(round(timeout_sec * fraction))))
        for fraction in slice_fractions
    }))
    candidates: dict[
        tuple[Any, ...], tuple[dict[str, Any], ...]
    ] = {}

    def add(stages: Sequence[tuple[str, float]]) -> None:
        if not stages or sum(stage[1] for stage in stages) > timeout_sec:
            return
        schedule = tuple({
            "action": action,
            "budget_sec": budget,
        } for action, budget in stages)
        candidates.setdefault(_schedule_key(schedule), schedule)

    for action in sorted(actions):
        for budget in budgets:
            add(((action, budget),))
    if max_schedule_length >= 2:
        for left in sorted(actions):
            for right in sorted(actions):
                if left == right:
                    continue
                for left_budget in budgets:
                    for right_budget in budgets:
                        add((
                            (left, left_budget),
                            (right, right_budget),
                        ))
                        if len(candidates) >= max_candidates:
                            break
                    if len(candidates) >= max_candidates:
                        break
                if len(candidates) >= max_candidates:
                    break
            if len(candidates) >= max_candidates:
                break
    rng = random.Random(_stable_seed(seed, "schedule-candidate-pool"))
    attempts = 0
    maximum_attempts = max_candidates * 40
    while (
        len(candidates) < max_candidates
        and attempts < maximum_attempts
        and max_schedule_length >= 3
        and len(actions) >= 3
    ):
        attempts += 1
        length = rng.randint(
            3, min(max_schedule_length, len(actions)))
        selected = rng.sample(list(sorted(actions)), length)
        stages = tuple(
            (action, budgets[rng.randrange(len(budgets))])
            for action in selected
        )
        add(stages)
    return [
        candidates[key] for key in sorted(candidates)
    ][:max_candidates]


def _schedule_vector(
    schedule: Sequence[Mapping[str, Any]],
    actions: Sequence[str],
    *,
    timeout_sec: float,
    max_schedule_length: int,
) -> tuple[float, ...]:
    action_index = {
        action: index for index, action in enumerate(actions)
    }
    width = len(actions) + 1
    vector = [1.0] + [0.0] * (max_schedule_length * width)
    for position, stage in enumerate(schedule[:max_schedule_length]):
        base = 1 + position * width
        vector[base + action_index[str(stage["action"])]] = 1.0
        vector[base + len(actions)] = (
            float(stage["budget_sec"]) / timeout_sec)
    return tuple(vector)


def _expected_improvement(
    mean: float,
    uncertainty: float,
    incumbent: float,
    exploration: float,
) -> float:
    improvement = incumbent - mean - exploration
    if uncertainty <= 1e-12:
        return max(0.0, improvement)
    z_value = improvement / uncertainty
    cdf = 0.5 * (1.0 + math.erf(z_value / math.sqrt(2.0)))
    density = math.exp(-0.5 * z_value * z_value) / math.sqrt(
        2.0 * math.pi)
    return max(0.0, improvement * cdf + uncertainty * density)


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _smbo_optimize(
    contexts: Sequence[Mapping[str, Any]],
    ensemble: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    actions = tuple(ensemble["source"]["modeled_actions"])
    timeout_sec = float(ensemble["configuration"]["timeout_sec"])
    pool = _candidate_pool(
        actions,
        timeout_sec=timeout_sec,
        slice_fractions=configuration["slice_fractions"],
        max_schedule_length=configuration["max_schedule_length"],
        max_candidates=configuration["max_candidates"],
        seed=configuration["seed"],
    )
    if not pool:
        raise ValueError("empty SMBO candidate pool")
    identifiers = [_sha256(list(schedule)) for schedule in pool]
    vectors = [
        _schedule_vector(
            schedule,
            actions,
            timeout_sec=timeout_sec,
            max_schedule_length=configuration["max_schedule_length"],
        )
        for schedule in pool
    ]
    evaluations: dict[int, dict[str, Any]] = {}

    def observe(index: int) -> dict[str, Any]:
        result = evaluations.get(index)
        if result is None:
            result = _evaluate_schedule(
                pool[index],
                contexts,
                ensemble["models"],
                timeout_sec=timeout_sec,
                uncertainty_z=configuration["action_uncertainty_z"],
                reward_weight=configuration["reward_weight"],
            )
            evaluations[index] = result
        return result
    singles = {
        index for index, schedule in enumerate(pool)
        if len(schedule) == 1
    }
    if len(singles) > configuration["evaluation_budget"]:
        raise ValueError(
            "evaluation budget cannot cover the single-action design")
    initial_target = max(
        len(singles), configuration["initial_design"])
    initial_target = min(initial_target, configuration["evaluation_budget"])
    selected = set(singles)
    remaining_by_hash = sorted(
        (
            _sha256({
                "seed": configuration["seed"],
                "candidate": identifiers[index],
            }),
            index,
        )
        for index in range(len(pool))
        if index not in selected
    )
    for _, index in remaining_by_hash:
        if len(selected) >= initial_target:
            break
        selected.add(index)
    initial = tuple(sorted(selected))
    observed = {
        index: float(observe(index)["objective"])
        for index in initial
    }
    acquisition_trace: list[dict[str, Any]] = []
    while (
        len(observed) < configuration["evaluation_budget"]
        and len(observed) < len(pool)
    ):
        rows = [
            _Row(vectors[index], objective, 1.0)
            for index, objective in sorted(observed.items())
        ]
        model = _train_ensemble(
            rows,
            target="schedule_objective",
            lower=-3.0,
            upper=3.0,
            bag_estimators=configuration["bag_estimators"],
            boost_rounds=configuration["boost_rounds"],
            tree_depth=configuration["tree_depth"],
            min_leaf=min(
                configuration["min_leaf"],
                max(2, len(rows) // 2),
            ),
            max_thresholds=configuration["max_thresholds"],
            learning_rate=configuration["learning_rate"],
            seed=configuration["seed"],
            action=f"smbo-{len(observed)}",
        )
        incumbent = min(observed.values())
        acquisitions = []
        for index in range(len(pool)):
            if index in observed:
                continue
            mean, uncertainty = _ensemble_predict(model, vectors[index])
            expected = _expected_improvement(
                mean,
                max(
                    uncertainty, configuration["uncertainty_floor"]),
                incumbent,
                configuration["exploration"],
            )
            acquisitions.append((
                expected,
                -mean,
                identifiers[index],
                index,
                mean,
                uncertainty,
            ))
        if not acquisitions:
            break
        _, _, _, index, mean, uncertainty = max(acquisitions)
        observed_value = float(observe(index)["objective"])
        observed[index] = observed_value
        acquisition_trace.append({
            "iteration": len(acquisition_trace),
            "candidate_sha256": identifiers[index],
            "predicted_mean": _rounded(mean),
            "predicted_uncertainty": _rounded(uncertainty),
            "expected_improvement": _rounded(_expected_improvement(
                mean,
                max(uncertainty, configuration["uncertainty_floor"]),
                incumbent,
                configuration["exploration"],
            )),
            "incumbent_before": _rounded(incumbent),
            "observed_objective": _rounded(observed_value),
            "incumbent_after": _rounded(min(observed.values())),
        })
    best_index = min(
        observed,
        key=lambda index: (
            observed[index], _schedule_key(pool[index])))
    best_single_index = min(
        singles,
        key=lambda index: (
            observe(index)["objective"], _schedule_key(pool[index])))
    best = observe(best_index)
    best_single = observe(best_single_index)
    optimized = (
        len(best["schedule"]) > 1
        and best["objective"]
        <= best_single["objective"]
        - configuration["min_schedule_improvement"]
    )
    selected_result = best if optimized else best_single
    # The exhaustive oracle is computed only after the acquisition loop.  It is
    # diagnostic evidence and cannot influence candidate selection.
    oracle_evaluations = [
        _evaluate_schedule(
            schedule,
            contexts,
            ensemble["models"],
            timeout_sec=timeout_sec,
            uncertainty_z=configuration["action_uncertainty_z"],
            reward_weight=configuration["reward_weight"],
        )
        for schedule in pool
    ]
    oracle_index = min(
        range(len(pool)),
        key=lambda index: (
            oracle_evaluations[index]["objective"],
            _schedule_key(pool[index]),
        ))
    return {
        "schema": SMBO_SCHEMA,
        "candidate_pool_sha256": _sha256([
            list(schedule) for schedule in pool]),
        "candidate_count": len(pool),
        "evaluation_budget": configuration["evaluation_budget"],
        "evaluations": len(observed),
        "posthoc_oracle_evaluations": len(pool),
        "initial_design": [identifiers[index] for index in initial],
        "acquisition_trace": acquisition_trace,
        "optimized_multi_action": optimized,
        "best_single_objective": best_single["objective"],
        "oracle_objective": oracle_evaluations[oracle_index]["objective"],
        "simple_regret": _rounded(
            selected_result["objective"]
            - oracle_evaluations[oracle_index]["objective"]),
        **selected_result,
    }


def build_smbo_schedule_policy(
    ensemble: Mapping[str, Any],
    *,
    evaluation_budget: int = 64,
    initial_design: int = 12,
    max_candidates: int = 1024,
    max_schedule_length: int = 3,
    slice_fractions: Sequence[float] = (0.25, 0.5, 1.0),
    bag_estimators: int = 5,
    boost_rounds: int = 3,
    tree_depth: int = 3,
    min_leaf: int = 2,
    max_thresholds: int = 16,
    learning_rate: float = 0.2,
    exploration: float = 0.01,
    uncertainty_floor: float = 0.01,
    action_uncertainty_z: float = 1.0,
    reward_weight: float = 0.25,
    min_schedule_improvement: float = 0.01,
    seed: int = 0,
) -> dict[str, Any]:
    if not verify_ensemble_sequence_policy(ensemble):
        raise ValueError("SMBO requires a verified F240 ensemble artifact")
    evaluation_budget = max(4, min(512, int(evaluation_budget)))
    initial_design = max(4, min(128, int(initial_design)))
    max_candidates = max(8, min(4096, int(max_candidates)))
    max_schedule_length = max(1, min(8, int(max_schedule_length)))
    fractions = tuple(sorted({
        round(max(0.01, min(1.0, float(value))), 12)
        for value in slice_fractions
    }))
    timeout_sec = float(ensemble["configuration"]["timeout_sec"])
    single_design_size = len(ensemble["source"]["modeled_actions"]) * len({
        max(1, int(round(timeout_sec * fraction)))
        for fraction in fractions
    })
    if evaluation_budget < single_design_size:
        raise ValueError(
            "evaluation budget cannot cover every single-action baseline")
    if max_candidates < single_design_size:
        raise ValueError(
            "candidate cap cannot cover every single-action baseline")
    bag_estimators = max(3, min(16, int(bag_estimators)))
    boost_rounds = max(1, min(16, int(boost_rounds)))
    tree_depth = max(1, min(5, int(tree_depth)))
    min_leaf = max(2, min(64, int(min_leaf)))
    max_thresholds = max(2, min(32, int(max_thresholds)))
    learning_rate = max(0.01, min(1.0, float(learning_rate)))
    exploration = max(0.0, min(1.0, float(exploration)))
    uncertainty_floor = max(
        1e-6, min(1.0, float(uncertainty_floor)))
    action_uncertainty_z = max(
        0.0, min(10.0, float(action_uncertainty_z)))
    reward_weight = max(0.0, min(2.0, float(reward_weight)))
    min_schedule_improvement = max(
        0.0, min(1.0, float(min_schedule_improvement)))
    seed = int(seed)
    configuration = {
        "evaluation_budget": evaluation_budget,
        "initial_design": initial_design,
        "max_candidates": max_candidates,
        "max_schedule_length": max_schedule_length,
        "slice_fractions": list(fractions),
        "bag_estimators": bag_estimators,
        "boost_rounds": boost_rounds,
        "tree_depth": tree_depth,
        "min_leaf": min_leaf,
        "max_thresholds": max_thresholds,
        "learning_rate": learning_rate,
        "exploration": exploration,
        "uncertainty_floor": uncertainty_floor,
        "action_uncertainty_z": action_uncertainty_z,
        "reward_weight": reward_weight,
        "min_schedule_improvement": min_schedule_improvement,
        "seed": seed,
    }
    contexts = ensemble["optimization_contexts"]
    global_result = _smbo_optimize(contexts, ensemble, configuration)
    local_results = [
        {
            "cluster_id": context["id"],
            "cluster_weight": context["weight"],
            **_smbo_optimize([context], ensemble, configuration),
        }
        for context in contexts
    ]
    artifact: dict[str, Any] = {
        "schema": SMBO_POLICY_SCHEMA,
        "smbo_schema": SMBO_SCHEMA,
        "configuration": configuration,
        "source": {
            "ensemble_schema": ENSEMBLE_POLICY_SCHEMA,
            "ensemble_sha256": ensemble["artifact_sha256"],
            "trajectory_sha256": ensemble["source"]["trajectory_sha256"],
            "modeled_actions": ensemble["source"]["modeled_actions"],
        },
        "ensemble_policy": ensemble,
        "global_result": global_result,
        "cluster_results": local_results,
    }
    artifact["artifact_sha256"] = _sha256(artifact)
    if not verify_smbo_schedule_policy(artifact):
        raise ValueError("internal SMBO policy verification failed")
    return artifact


def verify_smbo_schedule_policy(artifact: Mapping[str, Any]) -> bool:
    if not isinstance(artifact, dict) or set(artifact) != {
        "schema", "smbo_schema", "configuration", "source",
        "ensemble_policy", "global_result", "cluster_results",
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
    ensemble = artifact.get("ensemble_policy")
    configuration = artifact.get("configuration")
    source = artifact.get("source")
    if (
        artifact.get("schema") != SMBO_POLICY_SCHEMA
        or artifact.get("smbo_schema") != SMBO_SCHEMA
        or not verify_ensemble_sequence_policy(ensemble)
        or not isinstance(configuration, dict)
        or not isinstance(source, dict)
    ):
        return False
    expected_configuration = {
        "evaluation_budget", "initial_design", "max_candidates",
        "max_schedule_length", "slice_fractions", "bag_estimators",
        "boost_rounds", "tree_depth", "min_leaf", "max_thresholds",
        "learning_rate", "exploration", "uncertainty_floor",
        "action_uncertainty_z", "reward_weight",
        "min_schedule_improvement", "seed",
    }
    if set(configuration) != expected_configuration:
        return False
    integer_bounds = {
        "evaluation_budget": (4, 512),
        "initial_design": (4, 128),
        "max_candidates": (8, 4096),
        "max_schedule_length": (1, 8),
        "bag_estimators": (3, 16),
        "boost_rounds": (1, 16),
        "tree_depth": (1, 5),
        "min_leaf": (2, 64),
        "max_thresholds": (2, 32),
    }
    for name, (lower, upper) in integer_bounds.items():
        value = configuration.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not lower <= value <= upper
        ):
            return False
    numeric_bounds = {
        "learning_rate": (0.01, 1.0),
        "exploration": (0.0, 1.0),
        "uncertainty_floor": (1e-6, 1.0),
        "action_uncertainty_z": (0.0, 10.0),
        "reward_weight": (0.0, 2.0),
        "min_schedule_improvement": (0.0, 1.0),
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
        or set(source) != {
            "ensemble_schema", "ensemble_sha256", "trajectory_sha256",
            "modeled_actions",
        }
        or source.get("ensemble_schema") != ENSEMBLE_POLICY_SCHEMA
        or source.get("ensemble_sha256") != ensemble["artifact_sha256"]
        or source.get("trajectory_sha256")
        != ensemble["source"]["trajectory_sha256"]
        or source.get("modeled_actions")
        != ensemble["source"]["modeled_actions"]
    ):
        return False
    try:
        expected_global = _smbo_optimize(
            ensemble["optimization_contexts"], ensemble, configuration)
        expected_local = [
            {
                "cluster_id": context["id"],
                "cluster_weight": context["weight"],
                **_smbo_optimize([context], ensemble, configuration),
            }
            for context in ensemble["optimization_contexts"]
        ]
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (
        artifact.get("global_result") == expected_global
        and artifact.get("cluster_results") == expected_local
    )


def load_smbo_schedule_policy(
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
    if not verify_smbo_schedule_policy(artifact):
        return None
    permitted = frozenset(
        str(action) for action in (
            allowed_actions
            if allowed_actions is not None
            else artifact["source"]["modeled_actions"]
        )
    )
    global_schedule = tuple(
        (str(stage["action"]), float(stage["budget_sec"]))
        for stage in artifact["global_result"]["schedule"]
    )
    local_by_id = {
        int(item["cluster_id"]): tuple(
            (str(stage["action"]), float(stage["budget_sec"]))
            for stage in item["schedule"]
        )
        for item in artifact["cluster_results"]
    }
    contexts = artifact["ensemble_policy"]["optimization_contexts"]
    contract = feature_contract(
        artifact["ensemble_policy"]["feature_schema"])
    if contract is None:
        return None
    feature_schema, dimension, _ = contract
    return EnsembleSequencePrior(
        artifact_sha256=str(artifact["artifact_sha256"]),
        actions=permitted,
        clusters=tuple(
            (
                int(context["id"]),
                tuple(float(value) for value in context["vector"]),
                local_by_id[int(context["id"])],
            )
            for context in contexts
        ),
        global_schedule=global_schedule,
        kind="smbo-expected-improvement-sequence",
        dimension=dimension,
        feature_schema=feature_schema,
    )


def write_smbo_schedule_policy(
    path: str,
    artifact: Mapping[str, Any],
) -> None:
    if not verify_smbo_schedule_policy(artifact):
        raise ValueError("refusing to write an invalid SMBO policy")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".smt-schedule-smbo-", dir=directory, text=True)
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
        raise argparse.ArgumentTypeError("at least one fraction required")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train or verify a schedule-level expected-improvement prior.")
    parser.add_argument("ensemble", nargs="?")
    parser.add_argument("--output", default="")
    parser.add_argument("--verify", default="", metavar="POLICY")
    parser.add_argument("--evaluation-budget", type=int, default=64)
    parser.add_argument("--initial-design", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=1024)
    parser.add_argument("--max-schedule-length", type=int, default=3)
    parser.add_argument(
        "--slice-fractions", type=_fractions, default=(0.25, 0.5, 1.0))
    parser.add_argument("--bag-estimators", type=int, default=5)
    parser.add_argument("--boost-rounds", type=int, default=3)
    parser.add_argument("--tree-depth", type=int, default=3)
    parser.add_argument("--min-leaf", type=int, default=2)
    parser.add_argument("--max-thresholds", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=0.2)
    parser.add_argument("--exploration", type=float, default=0.01)
    parser.add_argument("--uncertainty-floor", type=float, default=0.01)
    parser.add_argument("--action-uncertainty-z", type=float, default=1.0)
    parser.add_argument("--reward-weight", type=float, default=0.25)
    parser.add_argument("--min-schedule-improvement", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.verify:
        try:
            with open(args.verify, encoding="utf-8") as stream:
                artifact = json.load(stream)
        except (OSError, ValueError, TypeError):
            artifact = {}
        verified = verify_smbo_schedule_policy(artifact)
        print(json.dumps({
            "schema": SMBO_POLICY_SCHEMA,
            "verified": verified,
            "artifact_sha256": (
                artifact.get("artifact_sha256", "")
                if isinstance(artifact, dict) else ""),
        }, sort_keys=True))
        return 0 if verified else 1
    if not args.ensemble or not args.output:
        parser.error("ensemble and --output are required when training")
    try:
        with open(args.ensemble, encoding="utf-8") as stream:
            ensemble = json.load(stream)
        artifact = build_smbo_schedule_policy(
            ensemble,
            evaluation_budget=args.evaluation_budget,
            initial_design=args.initial_design,
            max_candidates=args.max_candidates,
            max_schedule_length=args.max_schedule_length,
            slice_fractions=args.slice_fractions,
            bag_estimators=args.bag_estimators,
            boost_rounds=args.boost_rounds,
            tree_depth=args.tree_depth,
            min_leaf=args.min_leaf,
            max_thresholds=args.max_thresholds,
            learning_rate=args.learning_rate,
            exploration=args.exploration,
            uncertainty_floor=args.uncertainty_floor,
            action_uncertainty_z=args.action_uncertainty_z,
            reward_weight=args.reward_weight,
            min_schedule_improvement=args.min_schedule_improvement,
            seed=args.seed,
        )
        write_smbo_schedule_policy(args.output, artifact)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps({
        "schema": SMBO_POLICY_SCHEMA,
        "verified": True,
        "evaluations": artifact["global_result"]["evaluations"],
        "candidates": artifact["global_result"]["candidate_count"],
        "simple_regret": artifact["global_result"]["simple_regret"],
        "artifact_sha256": artifact["artifact_sha256"],
        "output": args.output,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
