#!/usr/bin/env python3
"""Interference-aware trajectory logging and conservative policy evaluation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
import tempfile
import time
from typing import Any, Iterable

from hybrid_feedback import SolverTelemetry


def _clamp(value: float, lower: float, upper: float) -> float:
    if not math.isfinite(value):
        return lower
    return max(lower, min(upper, value))


def interference_adjusted_reward(
    reward: float,
    *,
    elapsed: float,
    generated: int,
    interesting: int,
    concurrent_workers: int,
) -> tuple[float, float]:
    """Return utility and estimated parallel-work interference.

    The penalty grows only when many generated cases are redundant and several
    workers are active, avoiding an automatic penalty for useful parallelism.
    """
    generated = max(0, int(generated))
    interesting = max(0, min(generated, int(interesting)))
    duplicate_ratio = (
        (generated - interesting) / generated if generated else 0.0)
    concurrency = 1.0 - math.exp(
        -max(0, int(concurrent_workers) - 1) / 8.0)
    interference = duplicate_ratio * concurrency
    efficiency = _clamp(
        float(reward), 0.0, 1.0) / (
            1.0 + math.log1p(max(0.0, float(elapsed))))
    return _clamp(efficiency - 0.30 * interference, -1.0, 1.0), interference


@dataclass(frozen=True)
class TrajectoryEvent:
    schema: int
    timestamp: float
    action: str
    propensity: float
    reward: float
    adjusted_reward: float
    cost: float
    coverage_delta: int
    generated: int
    interesting: int
    concurrent_workers: int
    interference: float
    killed: bool
    context: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Any) -> "TrajectoryEvent | None":
        if not isinstance(raw, dict):
            return None
        try:
            action = str(raw.get("action", ""))[:128]
            if not action:
                return None
            context = raw.get("context", {})
            if not isinstance(context, dict):
                context = {}
            return cls(
                schema=1,
                timestamp=float(raw.get("timestamp", 0.0)),
                action=action,
                propensity=_clamp(
                    float(raw.get("propensity", 0.0)), 1e-6, 1.0),
                reward=_clamp(float(raw.get("reward", 0.0)), 0.0, 1.0),
                adjusted_reward=_clamp(
                    float(raw.get("adjusted_reward", 0.0)), -1.0, 1.0),
                cost=max(0.0, float(raw.get("cost", 0.0))),
                coverage_delta=max(0, int(raw.get("coverage_delta", 0))),
                generated=max(0, int(raw.get("generated", 0))),
                interesting=max(0, int(raw.get("interesting", 0))),
                concurrent_workers=max(
                    0, int(raw.get("concurrent_workers", 0))),
                interference=_clamp(
                    float(raw.get("interference", 0.0)), 0.0, 1.0),
                killed=bool(raw.get("killed", False)),
                context=dict(list(context.items())[:64]),
            )
        except (TypeError, ValueError, OverflowError):
            return None


class TrajectoryRecorder:
    def __init__(self, path: str, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self.events = 0

    def append(self, event: TrajectoryEvent) -> None:
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            if os.path.getsize(self.path) >= self.max_bytes:
                rotated = self.path + ".1"
                try:
                    os.unlink(rotated)
                except OSError:
                    pass
                os.replace(self.path, rotated)
        except OSError:
            pass
        try:
            with open(self.path, "a", encoding="utf-8") as stream:
                json.dump(
                    asdict(event), stream,
                    sort_keys=True, separators=(",", ":"))
                stream.write("\n")
            self.events += 1
        except OSError:
            pass


def load_trajectories(
    path: str,
    *,
    max_events: int = 100_000,
) -> list[TrajectoryEvent]:
    events: list[TrajectoryEvent] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                event = TrajectoryEvent.from_mapping(raw)
                if event is not None:
                    events.append(event)
                    if len(events) > max_events:
                        del events[:len(events) - max_events]
    except OSError:
        pass
    return events


@dataclass(frozen=True)
class PolicyEstimate:
    action: str
    samples: int
    effective_samples: float
    snips: float
    doubly_robust: float
    standard_error: float
    lower_bound: float
    supported: bool


def evaluate_constant_policy(
    events: Iterable[TrajectoryEvent],
    action: str,
    *,
    min_propensity: float = 0.02,
    max_weight: float = 20.0,
    min_effective_samples: float = 20.0,
    confidence_z: float = 1.96,
) -> PolicyEstimate:
    materialized = list(events)
    action_events = [event for event in materialized if event.action == action]
    reward_model = (
        sum(event.adjusted_reward for event in action_events)
        / len(action_events) if action_events else 0.0)
    weights: list[float] = []
    weighted_rewards: list[float] = []
    dr_values: list[float] = []
    for event in materialized:
        if event.action == action:
            weight = min(
                max_weight, 1.0 / max(min_propensity, event.propensity))
            weights.append(weight)
            weighted_rewards.append(weight * event.adjusted_reward)
            dr_values.append(
                reward_model
                + weight * (event.adjusted_reward - reward_model))
        else:
            dr_values.append(reward_model)
    weight_sum = sum(weights)
    weight_square_sum = sum(weight * weight for weight in weights)
    effective = (
        weight_sum * weight_sum / weight_square_sum
        if weight_square_sum else 0.0)
    snips = (
        sum(weighted_rewards) / weight_sum if weight_sum else 0.0)
    doubly_robust = (
        sum(dr_values) / len(dr_values) if dr_values else 0.0)
    center = min(snips, doubly_robust)
    if weights and effective > 1.0:
        variance = sum(
            weight * (event.adjusted_reward - snips) ** 2
            for weight, event in zip(weights, action_events)
        ) / max(1e-9, weight_sum)
        standard_error = math.sqrt(variance / effective)
    else:
        standard_error = 1.0
    supported = (
        len(action_events) >= 2 and effective >= min_effective_samples)
    lower = center - confidence_z * standard_error
    return PolicyEstimate(
        action=action,
        samples=len(action_events),
        effective_samples=effective,
        snips=snips,
        doubly_robust=doubly_robust,
        standard_error=standard_error,
        lower_bound=lower,
        supported=supported,
    )


class ConservativePolicyGate:
    """Promote a solver sequence only with supported lower-bound improvement."""

    def __init__(
        self,
        actions: Iterable[str],
        *,
        min_effective_samples: float = 20.0,
        min_improvement: float = 0.01,
    ) -> None:
        self.actions = tuple(dict.fromkeys(str(action) for action in actions))
        self.min_effective_samples = max(2.0, float(min_effective_samples))
        self.min_improvement = max(0.0, float(min_improvement))

    def evaluate(
        self,
        events: Iterable[TrajectoryEvent],
    ) -> tuple[str, dict[str, PolicyEstimate], float]:
        materialized = list(events)
        if not materialized:
            return "", {}, 0.0
        rewards = [event.adjusted_reward for event in materialized]
        behavior_mean = sum(rewards) / len(rewards)
        estimates = {
            action: evaluate_constant_policy(
                materialized,
                action,
                min_effective_samples=self.min_effective_samples,
            )
            for action in self.actions
        }
        supported = [
            estimate for estimate in estimates.values()
            if estimate.supported
            and estimate.lower_bound
            >= behavior_mean + self.min_improvement
        ]
        if not supported:
            return "", estimates, behavior_mean
        selected = max(
            supported,
            key=lambda estimate: (
                estimate.lower_bound,
                estimate.effective_samples,
                estimate.action,
            ),
        )
        return selected.action, estimates, behavior_mean


class OfflinePolicyController:
    """Record trajectories and periodically gate a low-risk sequence prior."""

    def __init__(
        self,
        trajectory_path: str,
        state_path: str,
        actions: Iterable[str],
        *,
        evaluation_interval: int = 128,
        max_events: int = 50_000,
        min_effective_samples: float = 20.0,
    ) -> None:
        self.trajectory_path = trajectory_path
        self.state_path = state_path
        self.recorder = TrajectoryRecorder(trajectory_path)
        self.max_events = max(128, int(max_events))
        self.events = load_trajectories(
            trajectory_path, max_events=self.max_events)
        self.gate = ConservativePolicyGate(
            actions, min_effective_samples=min_effective_samples)
        self.evaluation_interval = max(8, int(evaluation_interval))
        self.approved_action = ""
        self.evaluations = 0
        self.last_report: dict[str, Any] = {}
        self._load()

    def recommend(self) -> str:
        return self.approved_action

    def observe(
        self,
        *,
        action: str,
        propensity: float,
        telemetry: SolverTelemetry | None,
        reward: float,
        elapsed: float,
        coverage_delta: int,
        generated: int,
        interesting: int,
        concurrent_workers: int,
        killed: bool,
        instance_id: str = "",
        budget_sec: float = 0.0,
    ) -> TrajectoryEvent | None:
        action = str(action)[:128]
        if not action:
            return None
        adjusted, interference = interference_adjusted_reward(
            reward,
            elapsed=elapsed,
            generated=generated,
            interesting=interesting,
            concurrent_workers=concurrent_workers,
        )
        try:
            bounded_budget = _clamp(
                float(budget_sec), 0.0, 86400.0)
        except (TypeError, ValueError, OverflowError):
            bounded_budget = 0.0
        timed_out = bool(
            killed
            or (
                telemetry is not None
                and (
                    telemetry.solver_unknown > 0
                    or telemetry.z3_timeouts > 0
                )
            )
        )
        context = {
            "engine": telemetry.engine if telemetry else "unknown",
            "input_bytes": telemetry.input_bytes if telemetry else 0,
            "difficulty": telemetry.difficulty if telemetry else 0.0,
            "timeout_ratio": telemetry.timeout_ratio if telemetry else 0.0,
            "solver_unknown_ratio": (
                telemetry.solver_unknown
                / max(1, telemetry.solver_queries)
                if telemetry else 0.0),
            "dependency_bytes": (
                telemetry.max_dependency_bytes if telemetry else 0),
            "query_ir_queries": (
                telemetry.query_exports if telemetry else 0),
            "query_ir_nodes": (
                telemetry.query_ir_nodes if telemetry else 0),
            "query_ir_input_bytes": (
                telemetry.query_ir_input_bytes if telemetry else 0),
            "query_ir_max_bits": (
                telemetry.query_ir_max_bits if telemetry else 0),
            "query_ir_comparison_ops": (
                telemetry.query_ir_comparison_ops if telemetry else 0),
            "query_ir_nonlinear_ops": (
                telemetry.query_ir_nonlinear_ops if telemetry else 0),
            "query_ir_bitwise_ops": (
                telemetry.query_ir_bitwise_ops if telemetry else 0),
            "query_ir_structural_ops": (
                telemetry.query_ir_structural_ops if telemetry else 0),
            "data_quality": telemetry.data_quality if telemetry else 0.0,
            "branch_pressure": (
                telemetry.interesting_branches
                / max(1, telemetry.symbolic_branches)
                if telemetry else 0.0),
            "targeted": bool(
                telemetry is not None and telemetry.target_branch),
            "timed_out": timed_out,
            "solved": bool(
                not timed_out
                and (
                    telemetry is None
                    or telemetry.solver_queries == 0
                    or telemetry.solver_sat + telemetry.solver_unsat > 0
                )
            ),
            "instance_id": str(instance_id)[:128],
            "budget_sec": bounded_budget,
        }
        event = TrajectoryEvent(
            schema=1,
            timestamp=time.time(),
            action=action,
            propensity=_clamp(float(propensity), 1e-6, 1.0),
            reward=_clamp(float(reward), 0.0, 1.0),
            adjusted_reward=adjusted,
            cost=max(0.0, float(elapsed)),
            coverage_delta=max(0, int(coverage_delta)),
            generated=max(0, int(generated)),
            interesting=max(0, int(interesting)),
            concurrent_workers=max(0, int(concurrent_workers)),
            interference=interference,
            killed=bool(killed),
            context=context,
        )
        self.recorder.append(event)
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[:len(self.events) - self.max_events]
        if len(self.events) % self.evaluation_interval == 0:
            self.evaluate()
        return event

    def evaluate(self) -> str:
        approved, estimates, behavior_mean = self.gate.evaluate(self.events)
        self.approved_action = approved
        self.evaluations += 1
        self.last_report = {
            "schema": 1,
            "events": len(self.events),
            "evaluations": self.evaluations,
            "behavior_mean": behavior_mean,
            "approved_action": approved,
            "estimates": {
                action: asdict(estimate)
                for action, estimate in estimates.items()
            },
        }
        self.save()
        return approved

    def save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".offline-policy-", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(
                        self.last_report or {
                            "schema": 1,
                            "events": len(self.events),
                            "evaluations": self.evaluations,
                            "approved_action": self.approved_action,
                        },
                        stream, sort_keys=True, indent=2)
                    stream.write("\n")
                os.replace(temporary, self.state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return
        approved = str(raw.get("approved_action", ""))
        if approved in self.gate.actions:
            self.approved_action = approved
        try:
            self.evaluations = max(0, int(raw.get("evaluations", 0)))
        except (TypeError, ValueError):
            pass
        self.last_report = raw


def evaluate_trajectory_file(
    trajectory_path: str,
    *,
    actions: Iterable[str] | None = None,
    max_events: int = 100_000,
    min_effective_samples: float = 20.0,
    min_improvement: float = 0.01,
) -> dict[str, Any]:
    events = load_trajectories(trajectory_path, max_events=max_events)
    selected_actions = tuple(dict.fromkeys(
        str(action) for action in (actions or ())
        if str(action)))
    if not selected_actions:
        selected_actions = tuple(sorted({
            event.action for event in events if event.action
        }))
    gate = ConservativePolicyGate(
        selected_actions,
        min_effective_samples=min_effective_samples,
        min_improvement=min_improvement,
    )
    approved, estimates, behavior_mean = gate.evaluate(events)
    return {
        "schema": 1,
        "trajectory": trajectory_path,
        "events": len(events),
        "actions": list(selected_actions),
        "behavior_mean": behavior_mean,
        "approved_action": approved,
        "estimates": {
            action: asdict(estimate)
            for action, estimate in estimates.items()
        },
    }


def _parse_actions(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        item.strip() for item in raw.replace(";", ",").split(",")
        if item.strip()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate SymCC offline scheduling trajectories with conservative "
            "SNIPS/DR estimates."))
    parser.add_argument("trajectory", help="Trajectory JSONL path")
    parser.add_argument(
        "--actions", default="",
        help="Comma-separated action names. Defaults to actions observed in the trajectory.")
    parser.add_argument(
        "--max-events", type=int, default=100_000,
        help="Maximum recent events to load")
    parser.add_argument(
        "--min-effective-samples", type=float, default=20.0,
        help="Minimum ESS required before a policy can be approved")
    parser.add_argument(
        "--min-improvement", type=float, default=0.01,
        help="Required lower-bound improvement over behavior mean")
    parser.add_argument(
        "--output", default="",
        help="Optional JSON output path. Defaults to stdout.")
    args = parser.parse_args(argv)

    report = evaluate_trajectory_file(
        args.trajectory,
        actions=_parse_actions(args.actions),
        max_events=max(1, args.max_events),
        min_effective_samples=args.min_effective_samples,
        min_improvement=args.min_improvement,
    )
    payload = json.dumps(report, sort_keys=True, indent=2)
    if args.output:
        directory = os.path.dirname(args.output) or "."
        os.makedirs(directory, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.write("\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
