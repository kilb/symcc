#!/usr/bin/env python3
"""Online component selection and parallelism control for hybrid execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
import random
import tempfile
import time
from typing import Any, Iterable


@dataclass
class ComponentArm:
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0
    reward_sum: float = 0.0
    cost_sum: float = 0.0

    def sample(self, rng: random.Random) -> float:
        quality = rng.betavariate(max(0.01, self.alpha),
                                  max(0.01, self.beta))
        mean_cost = self.cost_sum / self.pulls if self.pulls else 1.0
        return quality / math.sqrt(max(0.05, mean_cost))

    def update(self, reward: float, elapsed: float, killed: bool) -> None:
        bounded = 0.0 if killed else max(0.0, min(1.0, float(reward)))
        self.alpha += bounded
        self.beta += 1.0 - bounded
        self.pulls += 1
        self.reward_sum += bounded
        self.cost_sum += max(0.001, float(elapsed))

    def to_mapping(self) -> dict[str, float | int]:
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "pulls": self.pulls,
            "reward_sum": self.reward_sum,
            "cost_sum": self.cost_sum,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "ComponentArm":
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(
                alpha=max(0.01, float(raw.get("alpha", 1.0))),
                beta=max(0.01, float(raw.get("beta", 1.0))),
                pulls=max(0, int(raw.get("pulls", 0))),
                reward_sum=max(0.0, float(raw.get("reward_sum", 0.0))),
                cost_sum=max(0.0, float(raw.get("cost_sum", 0.0))),
            )
        except (TypeError, ValueError, OverflowError):
            return cls()


DEFAULT_COMPONENTS: dict[str, tuple[str, ...]] = {
    "seed": ("contextual", "frontier", "yield", "fifo"),
    "splitter": ("whole", "focus", "density"),
    "solver": ("learned", "exact", "diverse"),
    "replay": ("balanced", "prefix", "fresh"),
}


@dataclass
class ComponentPortfolio:
    """Factorized Thompson portfolio whose choices can change without restart."""

    state_path: str | None = None
    switch_interval: float = 15.0
    seed: int | None = None
    domains: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_COMPONENTS))

    def __post_init__(self) -> None:
        self.switch_interval = max(0.0, float(self.switch_interval))
        self.rng = random.Random(self.seed)
        self.arms: dict[str, dict[str, ComponentArm]] = {
            family: {name: ComponentArm() for name in names}
            for family, names in self.domains.items()
        }
        self.current: dict[str, str] = {}
        self.last_switch = 0.0
        self.switches = 0
        self.observations = 0
        self.load()

    def _choose(self, family: str, eligible: Iterable[str] | None) -> str:
        allowed = [name for name in (eligible or self.domains[family])
                   if name in self.arms[family]]
        if not allowed:
            allowed = list(self.domains[family])
        cold = [name for name in allowed
                if self.arms[family][name].pulls == 0]
        if cold:
            return self.rng.choice(cold)
        return max(allowed, key=lambda name: self.arms[family][name].sample(
            self.rng))

    def select(
        self,
        *,
        now: float | None = None,
        eligible: dict[str, Iterable[str]] | None = None,
        force: bool = False,
    ) -> dict[str, str]:
        now = time.monotonic() if now is None else float(now)
        if (self.current and not force
                and now - self.last_switch < self.switch_interval):
            return dict(self.current)
        choices = {
            family: self._choose(
                family, (eligible or {}).get(family))
            for family in self.domains
        }
        if choices != self.current:
            self.switches += 1
        self.current = choices
        self.last_switch = now
        return dict(choices)

    def observe(
        self,
        choices: dict[str, str] | None,
        *,
        reward: float,
        elapsed: float,
        killed: bool,
    ) -> None:
        if not isinstance(choices, dict):
            return
        updated = False
        for family, name in choices.items():
            arm = self.arms.get(str(family), {}).get(str(name))
            if arm is None:
                continue
            arm.update(reward, elapsed, killed)
            updated = True
        if updated:
            self.observations += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "current": dict(self.current),
            "switches": self.switches,
            "observations": self.observations,
            "families": {
                family: {
                    name: arm.to_mapping()
                    for name, arm in family_arms.items()
                }
                for family, family_arms in self.arms.items()
            },
        }

    def load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        families = raw.get("families", {})
        if isinstance(families, dict):
            for family, values in families.items():
                if family not in self.arms or not isinstance(values, dict):
                    continue
                for name, arm_raw in values.items():
                    if name in self.arms[family]:
                        self.arms[family][name] = ComponentArm.from_mapping(
                            arm_raw)
        current = raw.get("current", {})
        if isinstance(current, dict):
            self.current = {
                str(family): str(name)
                for family, name in current.items()
                if str(name) in self.arms.get(str(family), {})
            }
        try:
            self.switches = max(0, int(raw.get("switches", 0)))
            self.observations = max(0, int(raw.get("observations", 0)))
        except (TypeError, ValueError):
            pass

    def save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".component-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(self.snapshot(), stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.state_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass


@dataclass
class ParallelWindow:
    reward_sum: float = 0.0
    coverage_delta: int = 0
    generated: int = 0
    interesting: int = 0
    elapsed: float = 0.0
    killed: int = 0
    completed: int = 0

    def observe(
        self,
        *,
        reward: float,
        coverage_delta: int,
        generated: int,
        interesting: int,
        elapsed: float,
        killed: bool,
    ) -> None:
        self.reward_sum += max(0.0, float(reward))
        self.coverage_delta += max(0, int(coverage_delta))
        self.generated += max(0, int(generated))
        self.interesting += max(0, int(interesting))
        self.elapsed += max(0.0, float(elapsed))
        self.killed += int(bool(killed))
        self.completed += 1


@dataclass
class ParallelLevelEstimate:
    observations: int = 0
    objective: float = 0.0
    throughput: float = 0.0
    cpu_efficiency: float = 0.0
    useful_ratio: float = 0.0
    timeout_ratio: float = 0.0

    def update(
        self,
        *,
        objective: float,
        throughput: float,
        cpu_efficiency: float,
        useful_ratio: float,
        timeout_ratio: float,
    ) -> None:
        alpha = 1.0 if self.observations == 0 else 0.3
        for name, value in (
            ("objective", objective),
            ("throughput", throughput),
            ("cpu_efficiency", cpu_efficiency),
            ("useful_ratio", useful_ratio),
            ("timeout_ratio", timeout_ratio),
        ):
            previous = getattr(self, name)
            setattr(self, name, previous + alpha * (value - previous))
        self.observations += 1

    def snapshot(self) -> dict[str, float | int]:
        return {
            "observations": self.observations,
            "objective": self.objective,
            "throughput": self.throughput,
            "cpu_efficiency": self.cpu_efficiency,
            "useful_ratio": self.useful_ratio,
            "timeout_ratio": self.timeout_ratio,
        }


class AdaptiveParallelismController:
    """Switchback controller for causal active-worker comparisons.

    A scale experiment runs A (baseline), B (adjacent trial), then A again.
    The B objective is compared with the interpolation of the two A windows,
    reducing the bias from the campaign's naturally declining coverage yield.
    """

    def __init__(
        self,
        minimum: int,
        maximum: int,
        initial: int | None = None,
        *,
        interval: float = 20.0,
        step: int = 1,
        resource_exponent: float = 0.35,
        tolerance: float = 0.08,
        rejected_trial_cooldown: int = 2,
    ):
        self.minimum = max(1, int(minimum))
        self.maximum = max(self.minimum, int(maximum))
        self.current = min(
            self.maximum, max(self.minimum, int(
                self.maximum if initial is None else initial)))
        self.interval = max(1.0, float(interval))
        self.step = max(1, int(step))
        self.direction = 1
        self.last_adjust = 0.0
        self.last_window_time = 0.0
        self.previous_utility: float | None = None
        self.window = ParallelWindow()
        self.adjustments = 0
        self.resource_exponent = min(1.0, max(0.0, float(resource_exponent)))
        self.tolerance = min(0.5, max(0.0, float(tolerance)))
        self.levels: dict[int, ParallelLevelEstimate] = {}
        self.experiment: dict[str, float | int | str] | None = None
        self.completed_experiments = 0
        self.accepted_experiments = 0
        self.cohort = 0
        self.stale_observations = 0
        self.rejected_trial_cooldown = max(0, int(rejected_trial_cooldown))
        self.trial_cooldowns: dict[tuple[int, int], int] = {}

    def observe(self, *, cohort: int | None = None, **metrics: Any) -> None:
        if cohort is not None and int(cohort) != self.cohort:
            self.stale_observations += 1
            return
        self.window.observe(**metrics)

    def recommend(
        self,
        *,
        queue_depth: int,
        busy_workers: int,
        now: float | None = None,
    ) -> int:
        now = time.monotonic() if now is None else float(now)
        if now - self.last_adjust < self.interval:
            return self.current
        self.last_adjust = now
        window, self.window = self.window, ParallelWindow()
        if window.completed == 0:
            return self.current
        wall_elapsed = max(
            0.001,
            now - self.last_window_time
            if self.last_window_time > 0.0
            else self.interval,
        )
        self.last_window_time = now
        useful_ratio = window.interesting / max(1, window.generated)
        timeout_ratio = window.killed / max(1, window.completed)
        # AdaptiveHybridScheduler.reward is already a dimensionless, bounded
        # blend of code/data coverage, retained corpus yield, solver yield and
        # execution cost.  Adding raw edge and testcase counts here mixed
        # incompatible units and made the controller target-dependent.
        signal = window.reward_sum
        throughput = signal / wall_elapsed
        cpu_efficiency = signal / max(0.05, window.elapsed)
        utility = throughput / (float(self.current) ** self.resource_exponent)
        utility *= max(0.1, 1.0 - 0.9 * timeout_ratio)
        level = self.levels.setdefault(self.current, ParallelLevelEstimate())
        level.update(
            objective=utility,
            throughput=throughput,
            cpu_efficiency=cpu_efficiency,
            useful_ratio=useful_ratio,
            timeout_ratio=timeout_ratio,
        )
        self.previous_utility = utility

        saturated = busy_workers >= self.current and queue_depth > 0
        starved = queue_depth <= 0 and busy_workers < self.current
        old = self.current
        if starved:
            self.experiment = None
            self.direction = -1
            self.current = max(self.minimum, self.current - self.step)
        elif self.experiment is not None:
            phase = str(self.experiment["phase"])
            baseline = int(self.experiment["baseline"])
            trial = int(self.experiment["trial"])
            if phase == "trial" and self.current == trial:
                self.experiment["trial_utility"] = utility
                self.experiment["phase"] = "confirm"
                self.current = baseline
            elif phase == "confirm" and self.current == baseline:
                before = float(self.experiment["baseline_before"])
                trial_utility = float(self.experiment["trial_utility"])
                interpolated = 0.5 * (before + utility)
                margin = self.tolerance * max(0.02, abs(interpolated))
                accepted = trial_utility + margin >= interpolated
                self.completed_experiments += 1
                if accepted:
                    self.accepted_experiments += 1
                    self.current = trial
                    self.direction = 1 if trial > baseline else -1
                    self.trial_cooldowns.pop((baseline, trial), None)
                else:
                    self.current = baseline
                    self.direction = -1 if trial > baseline else 1
                    self.trial_cooldowns[(baseline, trial)] = (
                        self.rejected_trial_cooldown
                    )
                self.experiment = None
        elif saturated:
            if self.current >= self.maximum:
                trial = max(self.minimum, self.current - self.step)
            elif self.current <= self.minimum:
                trial = min(self.maximum, self.current + self.step)
            else:
                trial = min(
                    self.maximum,
                    max(self.minimum, self.current + self.direction * self.step),
                )
            pair = (self.current, trial)
            cooldown = self.trial_cooldowns.get(pair, 0)
            if cooldown > 0:
                self.trial_cooldowns[pair] = cooldown - 1
            elif trial != self.current:
                self.experiment = {
                    "phase": "trial",
                    "baseline": self.current,
                    "trial": trial,
                    "baseline_before": utility,
                }
                self.current = trial
        if self.current != old:
            self.adjustments += 1
            self.cohort += 1
        return self.current

    def snapshot(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "direction": self.direction,
            "adjustments": self.adjustments,
            "previous_utility": self.previous_utility,
            "objective_kind": "bounded-hybrid-reward-per-wall-second",
            "resource_exponent": self.resource_exponent,
            "tolerance": self.tolerance,
            "completed_experiments": self.completed_experiments,
            "accepted_experiments": self.accepted_experiments,
            "cohort": self.cohort,
            "stale_observations": self.stale_observations,
            "rejected_trial_cooldown": self.rejected_trial_cooldown,
            "trial_cooldowns": {
                f"{baseline}->{trial}": remaining
                for (baseline, trial), remaining in sorted(
                    self.trial_cooldowns.items()
                )
                if remaining > 0
            },
            "experiment": dict(self.experiment) if self.experiment else None,
            "levels": {
                str(workers): estimate.snapshot()
                for workers, estimate in sorted(self.levels.items())
            },
        }
