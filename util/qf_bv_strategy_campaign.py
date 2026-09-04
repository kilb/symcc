#!/usr/bin/env python3
"""Run verified QF_BV sequence and cancellation strategies on one holdout."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import resource
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from offline_policy import TrajectoryEvent
from qf_bv_backend import (
    SmtLibQfbvSolver,
    lower_qfbv_query,
    normalize_qfbv_capabilities,
)
from qf_bv_campaign import (
    _patched_candidate,
    _selected_specs,
    _usage_us,
    verify_campaign as verify_baseline_campaign,
    verify_corpus,
)
from qf_bv_conformance import BackendSpec, verify_qfbv_conformance
from query_store import PortfolioSolver, QueryStore
from smt_schedule_smbo import (
    build_smbo_schedule_policy,
    verify_smbo_schedule_policy,
)
from smt_sequence_optimizer import (
    build_ensemble_sequence_policy,
    verify_ensemble_sequence_policy,
)
from smt_sequence_training import context_feature_vector, feature_contract


POLICY_BUNDLE_SCHEMA = "symcc-qfbv-policy-training-bundle-v1"
CAMPAIGN_SCHEMA = "symcc-qfbv-strategy-campaign-v1"
REPLAY_SCHEMA = "symcc-qfbv-strategy-campaign-replay-v1"
MIN_CONFIRMATORY_REPETITIONS = 20
MAX_TRAINING_EVENTS = 100_000
_SOLVED = frozenset({"sat", "unsat"})
_STATUSES = frozenset({"sat", "unsat", "unknown", "error"})
_COMPARISON_OPS = frozenset({
    "equal", "distinct", "ult", "ule", "ugt", "uge",
    "slt", "sle", "sgt", "sge",
})
_NONLINEAR_OPS = frozenset({"mul", "udiv", "sdiv", "urem", "srem"})
_BITWISE_OPS = frozenset({
    "not", "and", "or", "xor", "shl", "lshr", "ashr", "rol", "ror",
})
_STRUCTURAL_OPS = frozenset({
    "concat", "extract", "zext", "sext", "lor", "land", "lnot", "ite",
})


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def policy_bundle_digest(bundle: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in bundle.items()
        if key != "bundle_sha256"
    })


def campaign_strategy_digest(campaign: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in campaign.items()
        if key != "campaign_sha256"
    })


def _trajectory_events(
    raw_events: Sequence[Mapping[str, Any]],
) -> tuple[TrajectoryEvent, ...]:
    if (
        isinstance(raw_events, (str, bytes))
        or not 1 <= len(raw_events) <= MAX_TRAINING_EVENTS
    ):
        raise ValueError("training trajectory must contain 1--100000 events")
    events: list[TrajectoryEvent] = []
    for raw in raw_events:
        event = TrajectoryEvent.from_mapping(dict(raw))
        if event is None:
            raise ValueError("training trajectory contains an invalid event")
        events.append(event)
    return tuple(events)


def _rebuild_beam(
    events: Sequence[TrajectoryEvent],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    configuration = dict(artifact["configuration"])
    contract = feature_contract(artifact["feature_schema"])
    if contract is None:
        raise ValueError("beam artifact has an invalid feature schema")
    return build_ensemble_sequence_policy(
        events,
        feature_schema=contract[0],
        **configuration,
    )


def _rebuild_smbo(
    beam: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    return build_smbo_schedule_policy(
        beam,
        **dict(artifact["configuration"]),
    )


def seal_policy_bundle(
    corpus: Mapping[str, Any],
    raw_events: Sequence[Mapping[str, Any]],
    beam_policy: Mapping[str, Any],
    smbo_policy: Mapping[str, Any],
    *,
    source_kind: str = "sealed-train-trajectory",
) -> dict[str, Any]:
    if not verify_corpus(corpus):
        raise ValueError("QF_BV corpus artifact is invalid")
    events = _trajectory_events(raw_events)
    train_ids = {
        str(row["query_id"])
        for row in corpus["queries"]
        if row["split"] == "train"
    }
    if not train_ids:
        raise ValueError("policy training requires a non-empty train split")
    event_query_ids: list[str] = []
    for event in events:
        query_id = str(event.context.get("query_id", ""))
        if query_id not in train_ids:
            raise ValueError(
                "every policy event must name a query in the train split")
        event_query_ids.append(query_id)
    if not verify_ensemble_sequence_policy(beam_policy):
        raise ValueError("F240 beam policy is invalid")
    rebuilt_beam = _rebuild_beam(events, beam_policy)
    if rebuilt_beam != beam_policy:
        raise ValueError("F240 beam policy does not reproduce from trajectory")
    if not verify_smbo_schedule_policy(smbo_policy):
        raise ValueError("F241 SMBO policy is invalid")
    if smbo_policy.get("ensemble_policy") != beam_policy:
        raise ValueError("F241 SMBO policy is not derived from the F240 policy")
    rebuilt_smbo = _rebuild_smbo(beam_policy, smbo_policy)
    if rebuilt_smbo != smbo_policy:
        raise ValueError("F241 SMBO policy does not reproduce from F240")
    actions = sorted(str(value)
                     for value in beam_policy["source"]["modeled_actions"])
    if sorted(smbo_policy["source"]["modeled_actions"]) != actions:
        raise ValueError("F240/F241 modeled actions differ")
    source_kind = str(source_kind)[:64]
    if not source_kind:
        raise ValueError("policy source_kind must not be empty")
    normalized_events = [asdict(event) for event in events]
    bundle: dict[str, Any] = {
        "schema": POLICY_BUNDLE_SCHEMA,
        "source_kind": source_kind,
        "corpus_sha256": corpus["corpus_sha256"],
        "training_split": "train-only",
        "training_query_ids": sorted(set(event_query_ids)),
        "training_events": normalized_events,
        "modeled_backends": actions,
        "beam_policy": copy.deepcopy(dict(beam_policy)),
        "smbo_policy": copy.deepcopy(dict(smbo_policy)),
    }
    bundle["bundle_sha256"] = policy_bundle_digest(bundle)
    return bundle


def verify_policy_bundle(
    bundle: Mapping[str, Any],
    corpus: Mapping[str, Any],
) -> bool:
    try:
        if (
            not verify_corpus(corpus)
            or bundle.get("schema") != POLICY_BUNDLE_SCHEMA
            or bundle.get("bundle_sha256") != policy_bundle_digest(bundle)
            or bundle.get("corpus_sha256") != corpus.get("corpus_sha256")
            or bundle.get("training_split") != "train-only"
        ):
            return False
        source_kind = str(bundle["source_kind"])
        if not source_kind or len(source_kind.encode("utf-8")) > 64:
            return False
        raw_events = bundle.get("training_events")
        if (
            not isinstance(raw_events, Sequence)
            or isinstance(raw_events, (str, bytes))
        ):
            return False
        events = _trajectory_events(raw_events)
        train_ids = {
            str(row["query_id"])
            for row in corpus["queries"]
            if row["split"] == "train"
        }
        event_ids = [str(event.context.get("query_id", ""))
                     for event in events]
        if not event_ids or any(query_id not in train_ids
                                for query_id in event_ids):
            return False
        if bundle.get("training_query_ids") != sorted(set(event_ids)):
            return False
        beam = bundle.get("beam_policy")
        smbo = bundle.get("smbo_policy")
        if (
            not isinstance(beam, Mapping)
            or not isinstance(smbo, Mapping)
            or not verify_ensemble_sequence_policy(beam)
            or not verify_smbo_schedule_policy(smbo)
            or smbo.get("ensemble_policy") != beam
            or _rebuild_beam(events, beam) != beam
            or _rebuild_smbo(beam, smbo) != smbo
        ):
            return False
        actions = sorted(str(value)
                         for value in beam["source"]["modeled_actions"])
        return (
            actions
            and bundle.get("modeled_backends") == actions
            and sorted(smbo["source"]["modeled_actions"]) == actions
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def query_context(row: Mapping[str, Any]) -> dict[str, Any]:
    envelope = row["envelope"]
    nodes = envelope.get("nodes", ())
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise ValueError("Query IR nodes must be a sequence")
    counts: Counter[str] = Counter()
    input_offsets: set[int] = set()
    maximum_bits = 0
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ValueError("Query IR node must be an object")
        op = str(node.get("op", ""))
        counts[op] += 1
        maximum_bits = max(maximum_bits, int(node.get("bits", 0)))
        if op == "read":
            attrs = node.get("attrs")
            if isinstance(attrs, Mapping):
                input_offsets.add(int(attrs.get("index", -1)))
    try:
        input_size = len(bytes.fromhex(str(envelope.get("input_hex", ""))))
    except ValueError as error:
        raise ValueError("Query IR witness is not hexadecimal") from error
    return {
        "query_id": str(row["query_id"]),
        "input_bytes": input_size,
        "difficulty": 0.0,
        "timeout_ratio": 0.0,
        "solver_unknown_ratio": 0.0,
        "dependency_bytes": len(input_offsets),
        "data_quality": 0.5,
        "branch_pressure": 0.0,
        "targeted": False,
        "query_ir_queries": 1,
        "query_ir_nodes": len(nodes),
        "query_ir_input_bytes": len(input_offsets),
        "query_ir_max_bits": maximum_bits,
        "query_ir_comparison_ops": sum(
            counts[op] for op in _COMPARISON_OPS),
        "query_ir_nonlinear_ops": sum(
            counts[op] for op in _NONLINEAR_OPS),
        "query_ir_bitwise_ops": sum(
            counts[op] for op in _BITWISE_OPS),
        "query_ir_structural_ops": sum(
            counts[op] for op in _STRUCTURAL_OPS),
    }


def _policy_schedule(
    policy: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, list[float]]:
    if policy.get("schema") == "symcc-smt-sequence-ensemble-policy-v1":
        contexts = policy["optimization_contexts"]
        schedules = {
            int(item["cluster_id"]): item["schedule"]
            for item in policy["cluster_schedules"]
        }
        global_schedule = policy["global_schedule"]["schedule"]
        schema = policy["feature_schema"]
    elif policy.get("schema") == "symcc-smt-schedule-smbo-policy-v1":
        ensemble = policy["ensemble_policy"]
        contexts = ensemble["optimization_contexts"]
        schedules = {
            int(item["cluster_id"]): item["schedule"]
            for item in policy["cluster_results"]
        }
        global_schedule = policy["global_result"]["schedule"]
        schema = ensemble["feature_schema"]
    else:
        raise ValueError("unsupported sequence policy schema")
    contract = feature_contract(schema)
    if contract is None:
        raise ValueError("sequence policy feature schema is invalid")
    vector = list(context_feature_vector(context, schema=contract[0]))
    cluster = min(
        contexts,
        key=lambda item: sum(
            (float(left) - float(right)) ** 2
            for left, right in zip(item["vector"], vector)
        ),
    )
    selected = schedules.get(int(cluster["id"]), global_schedule)
    stages = [{
        "backend": str(stage["action"]),
        "budget_ms": max(1, int(round(float(stage["budget_sec"]) * 1000))),
    } for stage in selected]
    return stages, int(cluster["id"]), vector


def build_smoke_policy_bundle(
    corpus: Mapping[str, Any],
    backend_names: Sequence[str],
    *,
    timeout_ms: int = 2000,
) -> dict[str, Any]:
    train = [
        row for row in corpus["queries"]
        if row["split"] == "train"
    ]
    names = tuple(sorted(str(name) for name in backend_names))
    if len(train) < 2 or not names:
        raise ValueError("smoke policy requires train queries and backends")
    events: list[TrajectoryEvent] = []
    for backend_index, backend in enumerate(names):
        for repeat in range(2):
            for query_index, row in enumerate(train):
                killed = (
                    (query_index + backend_index + repeat) % len(names) == 0
                )
                context = query_context(row)
                context["instance_id"] = (
                    f"{row['query_id']}:{backend}:{repeat}")
                events.append(TrajectoryEvent(
                    schema=1,
                    timestamp=float(len(events)),
                    action=backend,
                    propensity=1.0 / len(names),
                    reward=0.0 if killed else 0.8,
                    adjusted_reward=0.0 if killed else 0.8,
                    cost=(2.0 * timeout_ms / 1000.0
                          if killed else 0.05),
                    coverage_delta=0 if killed else 1,
                    generated=1,
                    interesting=0 if killed else 1,
                    concurrent_workers=1,
                    interference=0.0,
                    killed=killed,
                    context=context,
                ))
    timeout_sec = timeout_ms / 1000.0
    beam = build_ensemble_sequence_policy(
        events,
        timeout_sec=timeout_sec,
        max_clusters=4,
        min_cluster_size=2,
        min_model_samples=4,
        bag_estimators=3,
        boost_rounds=2,
        tree_depth=2,
        min_leaf=2,
        max_thresholds=4,
        beam_width=64,
        max_schedule_length=2,
        slice_fractions=(0.5, 1.0),
        uncertainty_z=0.0,
        reward_weight=0.0,
        min_schedule_improvement=0.0,
        seed=246,
    )
    smbo = build_smbo_schedule_policy(
        beam,
        evaluation_budget=8,
        initial_design=6,
        max_candidates=64,
        max_schedule_length=2,
        slice_fractions=(0.5, 1.0),
        bag_estimators=3,
        boost_rounds=1,
        tree_depth=2,
        min_leaf=2,
        max_thresholds=4,
        exploration=0.0,
        action_uncertainty_z=0.0,
        reward_weight=0.0,
        min_schedule_improvement=0.0,
        seed=246,
    )
    return seal_policy_bundle(
        corpus,
        [asdict(event) for event in events],
        beam,
        smbo,
        source_kind="synthetic-smoke-policy",
    )


def _strategy_definitions(
    backend_names: Sequence[str],
    bundle: Mapping[str, Any],
    cancel_grace_ms: int,
) -> list[dict[str, Any]]:
    names = tuple(sorted(str(name) for name in backend_names))
    strategies: list[dict[str, Any]] = [{
        "name": f"individual:{name}",
        "kind": "individual",
        "backends": [name],
    } for name in names]
    strategies.extend([
        {
            "name": "f240-beam",
            "kind": "sequence",
            "policy_sha256": bundle["beam_policy"]["artifact_sha256"],
            "backends": list(bundle["modeled_backends"]),
        },
        {
            "name": "f241-smbo",
            "kind": "sequence",
            "policy_sha256": bundle["smbo_policy"]["artifact_sha256"],
            "backends": list(bundle["modeled_backends"]),
        },
        {
            "name": f"f242-parallel-grace-{cancel_grace_ms}ms",
            "kind": "parallel-cancellation",
            "backends": list(names),
            "parallelism": len(names),
            "cancel_grace_ms": cancel_grace_ms,
        },
    ])
    return strategies


def _task_order(
    query_ids: Sequence[str],
    strategy_names: Sequence[str],
    repetitions: int,
    order_seed: str,
) -> list[tuple[int, str, str]]:
    tasks = [
        (repetition, query_id, strategy)
        for repetition in range(repetitions)
        for query_id in query_ids
        for strategy in strategy_names
    ]
    return sorted(tasks, key=lambda task: hashlib.sha256(
        f"{order_seed}:{task[0]}:{task[1]}:{task[2]}".encode("ascii")
    ).digest())


def _normalize_attempt(
    backend: str,
    budget_ms: int,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    status = str(result.get("status", "error"))
    if status not in _STATUSES:
        status = "error"
    return {
        "backend": backend,
        "budget_ms": budget_ms,
        "status": status,
        "backend_status": str(result.get("backend_status", "")),
        "assignments": result.get("assignments", {}),
        "backend_model_verified": (
            result.get("backend_model_verified") is True),
        "backend_unsat_authorized": (
            result.get("backend_unsat_authorized") is True),
        "backend_unsat_confirmation": str(
            result.get("backend_unsat_confirmation", "")),
        "backend_capabilities": result.get("backend_capabilities", {}),
        "lowering_certificate": result.get("lowering_certificate", {}),
        "elapsed_us": max(0, int(result.get("elapsed_us", 0))),
        "cancelled": result.get("cancelled") is True,
        "cancel_reason": str(result.get("cancel_reason", ""))[:128],
        "reason": str(result.get("reason", ""))[:512],
    }


class _RecordingSolver:
    def __init__(
        self,
        backend: str,
        solver: SmtLibQfbvSolver,
        records: dict[str, dict[str, Any]],
        lock: threading.Lock,
    ):
        self.backend = backend
        self.solver = solver
        self.records = records
        self.lock = lock

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        result = dict(self.solver(lease))
        with self.lock:
            self.records[self.backend] = result
        return result

    def cancel(self, lease: Any) -> bool:
        return self.solver.cancel(lease)


def _run_strategy(
    row: Mapping[str, Any],
    strategy: Mapping[str, Any],
    specs: Mapping[str, BackendSpec],
    bundle: Mapping[str, Any],
    *,
    timeout_ms: int,
    repetition: int,
) -> dict[str, Any]:
    context = query_context(row)
    kind = str(strategy["kind"])
    cluster: int | None = None
    feature_vector: list[float] = []
    if strategy["name"] == "f240-beam":
        planned, cluster, feature_vector = _policy_schedule(
            bundle["beam_policy"], context)
    elif strategy["name"] == "f241-smbo":
        planned, cluster, feature_vector = _policy_schedule(
            bundle["smbo_policy"], context)
    elif kind == "individual":
        planned = [{
            "backend": str(strategy["backends"][0]),
            "budget_ms": timeout_ms,
        }]
    else:
        planned = [{
            "backend": str(backend),
            "budget_ms": timeout_ms,
        } for backend in strategy["backends"]]
    if not planned or any(stage["backend"] not in specs for stage in planned):
        raise ValueError("strategy selected an unavailable backend")
    if kind == "sequence" and sum(
            int(stage["budget_ms"]) for stage in planned) > timeout_ms:
        raise ValueError("sequence stage budgets exceed the shared wall budget")

    envelope = copy.deepcopy(dict(row["envelope"]))
    envelope["timeout_ms"] = timeout_ms
    owner = f"strategy-{strategy['name']}-{repetition}"
    with tempfile.TemporaryDirectory(prefix="symcc-qfbv-strategy-") as root:
        store = QueryStore(root)
        query_id, _ = store.ingest(envelope)
        if query_id != row["query_id"]:
            raise RuntimeError("strategy campaign query identity changed")
        lease = store.claim(owner)
        if lease is None:
            raise RuntimeError("strategy campaign query is not claimable")
        solvers = {
            name: SmtLibQfbvSolver(
                store,
                spec.command,
                name=spec.name,
                capabilities={"accept_unsat": True},
            )
            for name, spec in specs.items()
        }
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic_ns()
        attempts: list[dict[str, Any]] = []
        portfolio_meta = {
            "disagreement": False,
            "cancel_requested": False,
            "cancelled_attempts": 0,
            "consensus_complete": True,
        }
        raw_result: dict[str, Any]
        if kind == "parallel-cancellation":
            records: dict[str, dict[str, Any]] = {}
            lock = threading.Lock()
            wrappers = [(
                name,
                _RecordingSolver(name, solvers[name], records, lock),
            ) for name in strategy["backends"]]
            raw_result = dict(PortfolioSolver(
                wrappers,
                parallelism=int(strategy["parallelism"]),
                cancel_grace_ms=int(strategy["cancel_grace_ms"]),
            )(replace(lease, timeout_ms=timeout_ms)))
            portfolio = raw_result.get("portfolio", {})
            summaries = {
                str(item.get("name")): item
                for item in portfolio.get("attempts", ())
                if isinstance(item, Mapping)
            }
            for stage in planned:
                backend = str(stage["backend"])
                recorded = records.get(backend)
                if recorded is None:
                    summary = summaries.get(backend, {})
                    recorded = {
                        "status": str(summary.get("status", "unknown")),
                        "assignments": {},
                        "elapsed_us": int(summary.get("elapsed_us", 0)),
                        "cancelled": bool(summary.get("cancelled", False)),
                        "cancel_reason": str(
                            summary.get("cancel_reason", "")),
                        "reason": str(summary.get("reason", "")),
                    }
                attempts.append(_normalize_attempt(
                    backend, timeout_ms, recorded))
            portfolio_meta = {
                "disagreement": bool(portfolio.get("disagreement", False)),
                "cancel_requested": bool(
                    portfolio.get("cancel_requested", False)),
                "cancelled_attempts": int(
                    portfolio.get("cancelled_attempts", 0)),
                "consensus_complete": bool(
                    portfolio.get("consensus_complete", True)),
            }
        else:
            raw_result = {
                "status": "unknown",
                "assignments": {},
                "solver": "strategy-sequence",
                "reason": "no strategy stage returned SAT or UNSAT",
            }
            for stage in planned:
                backend = str(stage["backend"])
                budget_ms = int(stage["budget_ms"])
                stage_result = dict(solvers[backend](
                    replace(lease, timeout_ms=budget_ms)))
                attempts.append(_normalize_attempt(
                    backend, budget_ms, stage_result))
                raw_result = stage_result
                if stage_result.get("status") in _SOLVED:
                    break
            if raw_result.get("status") not in _SOLVED and kind == "sequence":
                raw_result = {
                    "status": "unknown",
                    "assignments": {},
                    "solver": "strategy-sequence",
                    "reason": "all sequence stages were inconclusive",
                }
        elapsed_us = max(0, (time.monotonic_ns() - started) // 1000)
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        completed = store.complete(lease, owner, raw_result)

    status = str(raw_result.get("status", "error"))
    if status not in _STATUSES:
        status = "error"
    par2_us = (
        min(elapsed_us, timeout_ms * 1000)
        if status in _SOLVED
        else 2 * timeout_ms * 1000
    )
    return {
        "query_id": str(row["query_id"]),
        "strategy": str(strategy["name"]),
        "kind": kind,
        "repetition": repetition,
        "status": status,
        "assignments": raw_result.get("assignments", {}),
        "backend_model_verified": (
            raw_result.get("backend_model_verified") is True),
        "backend_unsat_authorized": (
            raw_result.get("backend_unsat_authorized") is True),
        "winner_backend": str(
            raw_result.get("portfolio", {}).get(
                "winner", raw_result.get("solver", ""))
            if isinstance(raw_result.get("portfolio"), Mapping)
            else raw_result.get("solver", "")
        ),
        "planned_schedule": planned,
        "executed_attempts": attempts,
        "policy_cluster": cluster,
        "feature_vector": feature_vector,
        "portfolio": portfolio_meta,
        "elapsed_us": elapsed_us,
        "child_cpu_us": max(
            0, _usage_us(usage_after) - _usage_us(usage_before)),
        "par2_us": par2_us,
        "timed_out": any(
            "timeout" in str(attempt["reason"]).lower()
            for attempt in attempts
        ),
        "reason": str(raw_result.get("reason", ""))[:512],
        "store_completed": completed,
    }


def aggregate_strategy_results(
    results: Sequence[Mapping[str, Any]],
    strategy_names: Sequence[str],
) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, int]] = {}
    for strategy in strategy_names:
        selected = [row for row in results if row["strategy"] == strategy]
        statuses = Counter(str(row["status"]) for row in selected)
        total_par2 = sum(int(row["par2_us"]) for row in selected)
        by_strategy[strategy] = {
            "runs": len(selected),
            "sat": statuses["sat"],
            "unsat": statuses["unsat"],
            "unknown": statuses["unknown"],
            "error": statuses["error"],
            "solved": statuses["sat"] + statuses["unsat"],
            "model_verified": sum(
                row["backend_model_verified"] is True for row in selected),
            "authorized_unsat": sum(
                row["backend_unsat_authorized"] is True for row in selected),
            "timed_out": sum(row["timed_out"] is True for row in selected),
            "attempts": sum(
                len(row["executed_attempts"]) for row in selected),
            "cancel_requests": sum(
                row["portfolio"]["cancel_requested"] is True
                for row in selected),
            "cancelled_attempts": sum(
                int(row["portfolio"]["cancelled_attempts"])
                for row in selected),
            "incomplete_consensus": sum(
                row["portfolio"]["consensus_complete"] is False
                for row in selected),
            "elapsed_us": sum(int(row["elapsed_us"]) for row in selected),
            "child_cpu_us": sum(int(row["child_cpu_us"]) for row in selected),
            "par2_us": total_par2,
            "mean_par2_us": (
                total_par2 // len(selected) if selected else 0),
        }
    indexed = {
        (int(row["repetition"]), str(row["query_id"]),
         str(row["strategy"])): row
        for row in results
    }
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(strategy_names):
        for right in strategy_names[left_index + 1:]:
            keys = sorted({
                (repetition, query_id)
                for repetition, query_id, strategy in indexed
                if strategy == left
            })
            both = left_only = right_only = neither = disagreements = 0
            par2_delta = 0
            for repetition, query_id in keys:
                left_row = indexed[(repetition, query_id, left)]
                right_row = indexed[(repetition, query_id, right)]
                left_solved = left_row["status"] in _SOLVED
                right_solved = right_row["status"] in _SOLVED
                if left_solved and right_solved:
                    both += 1
                elif left_solved:
                    left_only += 1
                elif right_solved:
                    right_only += 1
                else:
                    neither += 1
                if {left_row["status"], right_row["status"]} == {
                        "sat", "unsat"}:
                    disagreements += 1
                par2_delta += (
                    int(left_row["par2_us"]) - int(right_row["par2_us"]))
            pairs.append({
                "left": left,
                "right": right,
                "paired_runs": len(keys),
                "both_solved": both,
                "left_only_solved": left_only,
                "right_only_solved": right_only,
                "neither_solved": neither,
                "sat_unsat_disagreements": disagreements,
                "left_minus_right_par2_us": par2_delta,
                "mean_left_minus_right_par2_us": (
                    par2_delta // len(keys) if keys else 0),
            })
    return {"strategies": by_strategy, "pairs": pairs}


def campaign_semantic_digest(campaign: Mapping[str, Any]) -> str:
    projected: list[dict[str, Any]] = []
    results = campaign.get("results", ())
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        for row in results:
            if not isinstance(row, Mapping):
                continue
            terminal_statuses = sorted({
                str(attempt.get("status"))
                for attempt in row.get("executed_attempts", ())
                if (
                    isinstance(attempt, Mapping)
                    and attempt.get("cancelled") is not True
                    and attempt.get("status") in _SOLVED
                )
            })
            projected.append({
                "query_id": row.get("query_id"),
                "strategy": row.get("strategy"),
                "repetition": row.get("repetition"),
                "status": row.get("status"),
                "backend_model_verified": row.get(
                    "backend_model_verified"),
                "backend_unsat_authorized": row.get(
                    "backend_unsat_authorized"),
                "planned_schedule": row.get("planned_schedule"),
                "policy_cluster": row.get("policy_cluster"),
                "terminal_statuses": terminal_statuses,
            })
    return _digest({
        "corpus_sha256": (
            campaign.get("corpus", {}).get("corpus_sha256")
            if isinstance(campaign.get("corpus"), Mapping) else ""),
        "conformance_semantic_sha256": (
            campaign.get("conformance", {}).get("semantic_sha256")
            if isinstance(campaign.get("conformance"), Mapping) else ""),
        "policy_bundle_sha256": (
            campaign.get("policy_bundle", {}).get("bundle_sha256")
            if isinstance(campaign.get("policy_bundle"), Mapping) else ""),
        "protocol": campaign.get("protocol"),
        "strategies": campaign.get("strategies"),
        "results": projected,
    })


def run_strategy_campaign(
    corpus: Mapping[str, Any],
    conformance: Mapping[str, Any],
    policy_bundle: Mapping[str, Any],
    *,
    backend_names: Sequence[str] | None = None,
    timeout_ms: int = 2000,
    repetitions: int = 1,
    order_seed: str = "symcc-qfbv-strategy-order-v1",
    cancel_grace_ms: int = 25,
    confirmatory: bool = False,
) -> dict[str, Any]:
    if not verify_corpus(corpus):
        raise ValueError("QF_BV corpus artifact is invalid")
    if not verify_policy_bundle(policy_bundle, corpus):
        raise ValueError("QF_BV policy training bundle is invalid")
    timeout_ms = int(timeout_ms)
    repetitions = int(repetitions)
    cancel_grace_ms = int(cancel_grace_ms)
    if not 1000 <= timeout_ms <= 3_600_000:
        raise ValueError("timeout_ms must be in 1000--3600000")
    if not 1 <= repetitions <= 100:
        raise ValueError("repetitions must be in 1--100")
    if not 0 <= cancel_grace_ms <= 60_000:
        raise ValueError("cancel_grace_ms must be in 0--60000")
    if confirmatory and repetitions < MIN_CONFIRMATORY_REPETITIONS:
        raise ValueError("confirmatory campaigns require at least 20 repetitions")
    if confirmatory and (
        corpus.get("source_kind") == "synthetic-smoke"
        or policy_bundle.get("source_kind") == "synthetic-smoke-policy"
    ):
        raise ValueError("synthetic corpus/policy cannot be confirmatory")
    order_seed = str(order_seed)
    if not order_seed or len(order_seed.encode("utf-8")) > 256:
        raise ValueError("order_seed must be non-empty and bounded")
    selected = _selected_specs(conformance, backend_names)
    specs = {spec.name: spec for spec, _ in selected}
    names = tuple(sorted(specs))
    if sorted(policy_bundle["modeled_backends"]) != list(names):
        raise ValueError(
            "policy modeled actions must exactly match selected backends")
    for policy_name in ("beam_policy", "smbo_policy"):
        policy = policy_bundle[policy_name]
        ensemble = (
            policy if policy_name == "beam_policy"
            else policy["ensemble_policy"]
        )
        policy_timeout_ms = int(round(
            float(ensemble["configuration"]["timeout_sec"]) * 1000))
        if policy_timeout_ms != timeout_ms:
            raise ValueError("policy and campaign timeout budgets differ")
    strategies = _strategy_definitions(
        names, policy_bundle, cancel_grace_ms)
    strategy_by_name = {
        str(strategy["name"]): strategy for strategy in strategies
    }
    rows = {
        str(row["query_id"]): row
        for row in corpus["queries"]
        if row["split"] == "holdout"
    }
    tasks = _task_order(
        sorted(rows),
        list(strategy_by_name),
        repetitions,
        order_seed,
    )
    results = [
        _run_strategy(
            rows[query_id],
            strategy_by_name[strategy],
            specs,
            policy_bundle,
            timeout_ms=timeout_ms,
            repetition=repetition,
        )
        for repetition, query_id, strategy in tasks
    ]
    identities = [{
        "name": spec.name,
        "command": list(spec.command),
        "expected_version": spec.expected_version,
        "actual_version": entry["actual_version"],
        "executable": entry["executable"],
        "executable_sha256": entry["executable_sha256"],
    } for spec, entry in selected]
    strategy_names = [str(strategy["name"]) for strategy in strategies]
    campaign: dict[str, Any] = {
        "schema": CAMPAIGN_SCHEMA,
        "generated_unix_ms": int(time.time() * 1000),
        "corpus": copy.deepcopy(dict(corpus)),
        "conformance": copy.deepcopy(dict(conformance)),
        "policy_bundle": copy.deepcopy(dict(policy_bundle)),
        "backend_names": list(names),
        "backend_identities": identities,
        "strategies": strategies,
        "protocol": {
            "timeout_ms": timeout_ms,
            "repetitions": repetitions,
            "order_seed": order_seed,
            "cancel_grace_ms": cancel_grace_ms,
            "confirmatory": bool(confirmatory),
            "minimum_confirmatory_repetitions": (
                MIN_CONFIRMATORY_REPETITIONS),
            "query_split": "holdout-only",
            "execution": "strategy-arms-sequential-randomized-order",
            "resource_contract": (
                "equal-logical-query-count-shared-wall-budget-"
                "measured-child-cpu-parallel-cpu-not-pre-equalized"),
            "performance_claims": bool(confirmatory),
        },
        "results": results,
        "aggregate": aggregate_strategy_results(results, strategy_names),
    }
    campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
    campaign["campaign_sha256"] = campaign_strategy_digest(campaign)
    return campaign


def _verify_attempt(
    attempt: Mapping[str, Any],
    store: QueryStore,
    query_id: str,
    certificate: Mapping[str, Any],
    capability: Mapping[str, Any],
    input_hex: str,
) -> bool:
    try:
        status = str(attempt["status"])
        if (
            status not in _STATUSES
            or int(attempt["budget_ms"]) < 1
            or int(attempt["elapsed_us"]) < 0
            or not isinstance(attempt.get("assignments"), Mapping)
            or str(attempt.get("backend_unsat_confirmation", ""))
            not in {"", "status-only-rerun-v1"}
        ):
            return False
        cancelled = attempt.get("cancelled") is True
        if cancelled:
            return status == "unknown" and not attempt["assignments"]
        if (
            attempt.get("backend_capabilities") != capability
            or attempt.get("lowering_certificate") != certificate
        ):
            return False
        if status == "sat":
            candidate = _patched_candidate(input_hex, attempt["assignments"])
            return (
                attempt.get("backend_model_verified") is True
                and candidate is not None
                and store.validate_candidate(query_id, candidate)
            )
        if attempt["assignments"]:
            return False
        return (
            status != "unsat"
            or attempt.get("backend_unsat_authorized") is True
        )
    except (KeyError, TypeError, ValueError):
        return False


def verify_strategy_campaign(campaign: Mapping[str, Any]) -> bool:
    try:
        if (
            campaign.get("schema") != CAMPAIGN_SCHEMA
            or campaign.get("campaign_sha256")
            != campaign_strategy_digest(campaign)
            or campaign.get("semantic_sha256")
            != campaign_semantic_digest(campaign)
        ):
            return False
        corpus = campaign.get("corpus")
        conformance = campaign.get("conformance")
        bundle = campaign.get("policy_bundle")
        if (
            not isinstance(corpus, Mapping)
            or not verify_corpus(corpus)
            or not isinstance(conformance, Mapping)
            or not verify_qfbv_conformance(conformance)
            or not isinstance(bundle, Mapping)
            or not verify_policy_bundle(bundle, corpus)
        ):
            return False
        names = campaign.get("backend_names")
        if (
            not isinstance(names, Sequence)
            or isinstance(names, (str, bytes))
            or not names
            or list(names) != sorted(set(str(name) for name in names))
            or sorted(bundle["modeled_backends"]) != list(names)
        ):
            return False
        selected = _selected_specs(
            conformance, names, check_current=False)
        expected_identities = [{
            "name": spec.name,
            "command": list(spec.command),
            "expected_version": spec.expected_version,
            "actual_version": entry["actual_version"],
            "executable": entry["executable"],
            "executable_sha256": entry["executable_sha256"],
        } for spec, entry in selected]
        if campaign.get("backend_identities") != expected_identities:
            return False
        protocol = campaign.get("protocol")
        if not isinstance(protocol, Mapping):
            return False
        timeout_ms = int(protocol["timeout_ms"])
        repetitions = int(protocol["repetitions"])
        order_seed = str(protocol["order_seed"])
        grace = int(protocol["cancel_grace_ms"])
        confirmatory = protocol.get("confirmatory") is True
        if (
            not 1000 <= timeout_ms <= 3_600_000
            or not 1 <= repetitions <= 100
            or not 0 <= grace <= 60_000
            or (confirmatory and repetitions < MIN_CONFIRMATORY_REPETITIONS)
            or (confirmatory and (
                corpus.get("source_kind") == "synthetic-smoke"
                or bundle.get("source_kind") == "synthetic-smoke-policy"))
            or protocol.get("minimum_confirmatory_repetitions") != 20
            or protocol.get("query_split") != "holdout-only"
            or protocol.get("execution")
            != "strategy-arms-sequential-randomized-order"
            or protocol.get("resource_contract")
            != (
                "equal-logical-query-count-shared-wall-budget-"
                "measured-child-cpu-parallel-cpu-not-pre-equalized")
            or protocol.get("performance_claims") is not confirmatory
        ):
            return False
        for policy_name in ("beam_policy", "smbo_policy"):
            policy = bundle[policy_name]
            ensemble = (
                policy if policy_name == "beam_policy"
                else policy["ensemble_policy"])
            if int(round(float(
                    ensemble["configuration"]["timeout_sec"]) * 1000)
                   ) != timeout_ms:
                return False
        expected_strategies = _strategy_definitions(names, bundle, grace)
        if campaign.get("strategies") != expected_strategies:
            return False
        strategy_by_name = {
            str(strategy["name"]): strategy
            for strategy in expected_strategies
        }
        holdout = {
            str(row["query_id"]): row
            for row in corpus["queries"]
            if row["split"] == "holdout"
        }
        expected_tasks = _task_order(
            sorted(holdout),
            list(strategy_by_name),
            repetitions,
            order_seed,
        )
        results = campaign.get("results")
        if (
            not isinstance(results, Sequence)
            or isinstance(results, (str, bytes))
            or len(results) != len(expected_tasks)
        ):
            return False
        actual_tasks = [
            (int(row["repetition"]), str(row["query_id"]),
             str(row["strategy"]))
            for row in results if isinstance(row, Mapping)
        ]
        if actual_tasks != expected_tasks:
            return False
        capability = normalize_qfbv_capabilities({"accept_unsat": True})
        with tempfile.TemporaryDirectory(
                prefix="symcc-qfbv-strategy-verify-") as root:
            stores: dict[str, QueryStore] = {}
            certificates: dict[str, Mapping[str, Any]] = {}
            for query_id, sealed_row in holdout.items():
                store = QueryStore(Path(root) / query_id)
                envelope = copy.deepcopy(dict(sealed_row["envelope"]))
                envelope["timeout_ms"] = timeout_ms
                ingested, _ = store.ingest(envelope)
                if ingested != query_id:
                    return False
                loaded = store.load_query_ir(query_id)
                if loaded is None:
                    return False
                _, certificate, _ = lower_qfbv_query(
                    query_id, loaded[0], loaded[1], capability)
                stores[query_id] = store
                certificates[query_id] = certificate
            for result in results:
                if not isinstance(result, Mapping):
                    return False
                query_id = str(result["query_id"])
                strategy = strategy_by_name[str(result["strategy"])]
                kind = str(strategy["kind"])
                context = query_context(holdout[query_id])
                expected_cluster: int | None = None
                expected_vector: list[float] = []
                if strategy["name"] == "f240-beam":
                    planned, expected_cluster, expected_vector = (
                        _policy_schedule(bundle["beam_policy"], context))
                elif strategy["name"] == "f241-smbo":
                    planned, expected_cluster, expected_vector = (
                        _policy_schedule(bundle["smbo_policy"], context))
                elif kind == "individual":
                    planned = [{
                        "backend": strategy["backends"][0],
                        "budget_ms": timeout_ms,
                    }]
                else:
                    planned = [{
                        "backend": backend,
                        "budget_ms": timeout_ms,
                    } for backend in strategy["backends"]]
                if (
                    result.get("kind") != kind
                    or result.get("planned_schedule") != planned
                    or result.get("policy_cluster") != expected_cluster
                    or result.get("feature_vector") != expected_vector
                    or (kind == "sequence" and sum(
                        int(stage["budget_ms"]) for stage in planned
                    ) > timeout_ms)
                ):
                    return False
                attempts = result.get("executed_attempts")
                if (
                    not isinstance(attempts, Sequence)
                    or isinstance(attempts, (str, bytes))
                    or not attempts
                ):
                    return False
                attempt_plan = [{
                    "backend": attempt.get("backend"),
                    "budget_ms": attempt.get("budget_ms"),
                } for attempt in attempts if isinstance(attempt, Mapping)]
                if kind == "parallel-cancellation":
                    if attempt_plan != planned:
                        return False
                elif attempt_plan != planned[:len(attempts)]:
                    return False
                for attempt in attempts:
                    if (
                        not isinstance(attempt, Mapping)
                        or not _verify_attempt(
                            attempt,
                            stores[query_id],
                            query_id,
                            certificates[query_id],
                            capability,
                            str(holdout[query_id]["envelope"].get(
                                "input_hex", "")),
                        )
                    ):
                        return False
                terminal = [
                    attempt for attempt in attempts
                    if attempt["status"] in _SOLVED
                ]
                status = str(result["status"])
                if status not in _STATUSES:
                    return False
                portfolio = result.get("portfolio")
                if not isinstance(portfolio, Mapping):
                    return False
                if kind == "parallel-cancellation":
                    statuses = {attempt["status"] for attempt in terminal}
                    disagreement = statuses == {"sat", "unsat"}
                    cancelled = sum(
                        attempt["cancelled"] is True for attempt in attempts)
                    if (
                        portfolio.get("disagreement") is not disagreement
                        or portfolio.get("cancelled_attempts") != cancelled
                        or portfolio.get("consensus_complete")
                        is not (cancelled == 0)
                        or (
                            portfolio.get("cancel_requested") is True
                            and "sat" not in statuses
                        )
                    ):
                        return False
                    expected_status = (
                        "unknown" if disagreement or not statuses
                        else ("sat" if "sat" in statuses else "unsat")
                    )
                    if status != expected_status:
                        return False
                else:
                    if portfolio != {
                        "disagreement": False,
                        "cancel_requested": False,
                        "cancelled_attempts": 0,
                        "consensus_complete": True,
                    }:
                        return False
                    if terminal:
                        if attempts.index(terminal[0]) != len(attempts) - 1:
                            return False
                        if status != terminal[0]["status"]:
                            return False
                    elif status not in {"unknown", "error"}:
                        return False
                assignments = result.get("assignments")
                if not isinstance(assignments, Mapping):
                    return False
                expected_winner = next(
                    (attempt for attempt in attempts
                     if attempt["status"] == "sat"),
                    None,
                )
                if expected_winner is None:
                    expected_winner = next(
                        (attempt for attempt in attempts
                         if attempt["status"] == "unsat"),
                        None,
                    )
                if status in _SOLVED:
                    if (
                        expected_winner is None
                        or result.get("winner_backend")
                        != expected_winner["backend"]
                        or assignments != expected_winner["assignments"]
                        or result.get("backend_model_verified")
                        is not expected_winner["backend_model_verified"]
                        or result.get("backend_unsat_authorized")
                        is not expected_winner["backend_unsat_authorized"]
                    ):
                        return False
                if status == "sat":
                    candidate = _patched_candidate(
                        str(holdout[query_id]["envelope"].get(
                            "input_hex", "")),
                        assignments,
                    )
                    if (
                        result.get("backend_model_verified") is not True
                        or candidate is None
                        or not stores[query_id].validate_candidate(
                            query_id, candidate)
                    ):
                        return False
                elif assignments:
                    return False
                if (
                    status == "unsat"
                    and result.get("backend_unsat_authorized") is not True
                ):
                    return False
                expected_timed_out = any(
                    "timeout" in str(attempt.get("reason", "")).lower()
                    for attempt in attempts
                )
                elapsed_us = int(result["elapsed_us"])
                child_cpu_us = int(result["child_cpu_us"])
                par2_us = int(result["par2_us"])
                expected_par2 = (
                    min(elapsed_us, timeout_ms * 1000)
                    if status in _SOLVED
                    else 2 * timeout_ms * 1000
                )
                if (
                    elapsed_us < 0
                    or child_cpu_us < 0
                    or par2_us != expected_par2
                    or result.get("timed_out") is not expected_timed_out
                    or result.get("store_completed") is not True
                ):
                    return False
        strategy_names = [
            str(strategy["name"]) for strategy in expected_strategies]
        return (
            campaign.get("aggregate")
            == aggregate_strategy_results(results, strategy_names)
            and int(campaign.get("generated_unix_ms", -1)) >= 0
        )
    except (
        KeyError, OSError, RuntimeError, TypeError, ValueError, OverflowError,
    ):
        return False


def replay_strategy_campaign(
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    if not verify_strategy_campaign(campaign):
        raise ValueError("source QF_BV strategy campaign is invalid")
    protocol = campaign["protocol"]
    replay = run_strategy_campaign(
        campaign["corpus"],
        campaign["conformance"],
        campaign["policy_bundle"],
        backend_names=campaign["backend_names"],
        timeout_ms=protocol["timeout_ms"],
        repetitions=protocol["repetitions"],
        order_seed=protocol["order_seed"],
        cancel_grace_ms=protocol["cancel_grace_ms"],
        confirmatory=protocol["confirmatory"],
    )
    result = {
        "schema": REPLAY_SCHEMA,
        "source_campaign_sha256": campaign["campaign_sha256"],
        "source_semantic_sha256": campaign["semantic_sha256"],
        "replay_semantic_sha256": replay["semantic_sha256"],
        "semantic_match": (
            replay["semantic_sha256"] == campaign["semantic_sha256"]),
        "replay": replay,
    }
    result["replay_sha256"] = _digest(result)
    return result


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_trajectory(path: str) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("trajectory JSONL rows must be objects")
    return rows


def _write_or_print(value: Mapping[str, Any], output: str | None) -> None:
    payload = _canonical_json(value) + b"\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    else:
        print(payload.decode("ascii"), end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify")
    action.add_argument("--replay")
    parser.add_argument("--base-campaign")
    parser.add_argument("--training-trajectory")
    parser.add_argument("--beam-policy")
    parser.add_argument("--smbo-policy")
    parser.add_argument("--smoke-policy", action="store_true")
    parser.add_argument("--backends", default="")
    parser.add_argument("--timeout-ms", type=int, default=2000)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--order-seed", default="symcc-qfbv-strategy-order-v1")
    parser.add_argument("--cancel-grace-ms", type=int, default=25)
    parser.add_argument("--confirmatory", action="store_true")
    parser.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        verified = verify_strategy_campaign(_load_json(args.verify))
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    if args.replay:
        replay = replay_strategy_campaign(_load_json(args.replay))
        _write_or_print(replay, args.output)
        return 0 if replay["semantic_match"] else 1
    if not args.base_campaign:
        raise ValueError("--base-campaign is required")
    baseline = _load_json(args.base_campaign)
    if not verify_baseline_campaign(baseline):
        raise ValueError("F245 base campaign is invalid")
    corpus = baseline["corpus"]
    conformance = baseline["conformance"]
    backend_names = tuple(
        value.strip() for value in args.backends.split(",") if value.strip())
    selected_names = (
        backend_names or tuple(sorted(baseline["backend_names"])))
    if args.smoke_policy:
        bundle = build_smoke_policy_bundle(
            corpus, selected_names, timeout_ms=args.timeout_ms)
    else:
        if not all((
            args.training_trajectory,
            args.beam_policy,
            args.smbo_policy,
        )):
            raise ValueError(
                "--training-trajectory, --beam-policy and --smbo-policy "
                "are required without --smoke-policy")
        bundle = seal_policy_bundle(
            corpus,
            _load_trajectory(args.training_trajectory),
            _load_json(args.beam_policy),
            _load_json(args.smbo_policy),
        )
    campaign = run_strategy_campaign(
        corpus,
        conformance,
        bundle,
        backend_names=selected_names,
        timeout_ms=args.timeout_ms,
        repetitions=args.repetitions,
        order_seed=args.order_seed,
        cancel_grace_ms=args.cancel_grace_ms,
        confirmatory=args.confirmatory,
    )
    _write_or_print(campaign, args.output)
    return 0 if verify_strategy_campaign(campaign) else 1


if __name__ == "__main__":
    raise SystemExit(main())
