#!/usr/bin/env python3
"""Contextual Bayesian scheduling of solver-algorithm sequences.

SMT algorithms are represented by bounded SymCC runtime profiles.  A selected
sequence advances across replays of the same seed, which lets the distributed
executor explore a cheap algorithm before spending another worker-timeslice on
an exact fallback.  Cheap online moment clusters maintain posteriors while an
optional, independently verified X-means/BIC artifact supplies conservative
context-specific exploration priors.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import random
import tempfile
from typing import Any

from hybrid_feedback import SolverTelemetry
from self_config import sanitize_parameter_overrides
from smt_sequence_training import (
    ContextSequencePrior,
    DIMENSION,
    FEATURE_SCHEMA,
    adapt_feature_vector,
    context_feature_vector,
    load_sequence_policy,
)
from smt_sequence_optimizer import (
    EnsembleSequencePrior,
    load_ensemble_sequence_policy,
)
from smt_schedule_smbo import load_smbo_schedule_policy


@dataclass(frozen=True)
class AlgorithmStage:
    name: str
    overrides: dict[str, str]


@dataclass(frozen=True)
class AlgorithmSequence:
    name: str
    stages: tuple[AlgorithmStage, ...]


@dataclass(frozen=True)
class AlgorithmAssignment:
    token: str
    sequence: str
    stage: str
    stage_index: int
    overrides: dict[str, str]
    propensity: float


@dataclass
class ContextCluster:
    centroid: list[float]
    m2: list[float]
    samples: int = 1

    def distance(self, vector: tuple[float, ...]) -> float:
        return sum(
            (left - right) ** 2
            for left, right in zip(self.centroid, vector)
        )

    def update(self, vector: tuple[float, ...]) -> None:
        self.samples += 1
        for index, value in enumerate(vector):
            delta = value - self.centroid[index]
            self.centroid[index] += delta / self.samples
            self.m2[index] += delta * (value - self.centroid[index])

    @property
    def variance(self) -> list[float]:
        scale = max(1, self.samples - 1)
        return [value / scale for value in self.m2]


@dataclass
class BayesianArm:
    observations: int = 0
    mean: float = 0.0
    m2: float = 0.0
    cost_sum: float = 0.0
    timeouts: int = 0

    @property
    def variance(self) -> float:
        if self.observations < 2:
            return 1.0
        return max(0.01, self.m2 / (self.observations - 1))

    def sample(self, rng: random.Random) -> float:
        # Normal-normal posterior with a unit prior precision.
        posterior_variance = self.variance / (1.0 + self.observations)
        return rng.gauss(self.mean, math.sqrt(posterior_variance))

    def update(self, utility: float, cost: float, timed_out: bool) -> None:
        self.observations += 1
        delta = utility - self.mean
        self.mean += delta / self.observations
        self.m2 += delta * (utility - self.mean)
        self.cost_sum += max(0.0, cost)
        self.timeouts += int(timed_out)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "mean": self.mean,
            "m2": self.m2,
            "cost_sum": self.cost_sum,
            "timeouts": self.timeouts,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "BayesianArm":
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(
                observations=max(0, int(raw.get("observations", 0))),
                mean=float(raw.get("mean", 0.0)),
                m2=max(0.0, float(raw.get("m2", 0.0))),
                cost_sum=max(0.0, float(raw.get("cost_sum", 0.0))),
                timeouts=max(0, int(raw.get("timeouts", 0))),
            )
        except (TypeError, ValueError, OverflowError):
            return cls()


_DEFAULT_SEQUENCES = (
    AlgorithmSequence("exact", (
        AlgorithmStage("z3-exact", {
            "SYMCC_EXECUTOR_CLASS": "exact",
            "SYMCC_FAST_SOLVE": "0",
            "SYMCC_MULTI_SOLVE": "0",
            "SYMCC_OPTIMISTIC_FIRST": "0",
        }),
    )),
    AlgorithmSequence("fast-exact", (
        AlgorithmStage("fast-linear", {
            "SYMCC_EXECUTOR_CLASS": "tailored",
            "SYMCC_FAST_SOLVE": "1",
            "SYMCC_MULTI_SOLVE": "0",
        }),
        AlgorithmStage("z3-fallback", {
            "SYMCC_EXECUTOR_CLASS": "exact",
            "SYMCC_FAST_SOLVE": "0",
            "SYMCC_MULTI_SOLVE": "0",
        }),
    )),
    AlgorithmSequence("optimistic-layered", (
        AlgorithmStage("optimistic", {
            "SYMCC_EXECUTOR_CLASS": "tailored",
            "SYMCC_OPTIMISTIC_FIRST": "1",
            "SYMCC_MULTI_SOLVE": "1",
        }),
        AlgorithmStage("layered-z3", {
            "SYMCC_EXECUTOR_CLASS": "tailored",
            "SYMCC_OPTIMISTIC_FIRST": "0",
            "SYMCC_MULTI_SOLVE": "2",
            "SYMCC_UNSAT_CORE_CACHE": "1",
            "SYMCC_PREFIX_CONTEXT_CACHE": "1",
        }),
    )),
    AlgorithmSequence("polyhedral-exact", (
        AlgorithmStage("polyhedral", {
            "SYMCC_EXECUTOR_CLASS": "sampling",
            "SYMCC_POLY_CACHE": "1",
            "SYMCC_POLY_WALK": "john",
            "SYMCC_POLY_SAMPLES": "4",
            "SYMCC_UNSAT_CORE_CACHE": "1",
        }),
        AlgorithmStage("z3-fallback", {
            "SYMCC_EXECUTOR_CLASS": "exact",
            "SYMCC_FAST_SOLVE": "0",
            "SYMCC_MULTI_SOLVE": "0",
        }),
    )),
)


def _load_sequences(raw: str | None) -> tuple[AlgorithmSequence, ...]:
    if not raw:
        return _DEFAULT_SEQUENCES
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as stream:
                data = json.load(stream)
        else:
            data = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return _DEFAULT_SEQUENCES
    if isinstance(data, dict):
        data = data.get("sequences", ())
    if not isinstance(data, list):
        return _DEFAULT_SEQUENCES
    sequences: list[AlgorithmSequence] = []
    for sequence_raw in data[:32]:
        if not isinstance(sequence_raw, dict):
            continue
        name = str(sequence_raw.get("name", ""))[:64]
        stages_raw = sequence_raw.get("stages", ())
        if not name or not isinstance(stages_raw, list):
            continue
        stages: list[AlgorithmStage] = []
        for stage_raw in stages_raw[:8]:
            if not isinstance(stage_raw, dict):
                continue
            stage_name = str(stage_raw.get("name", ""))[:64]
            overrides = sanitize_parameter_overrides(
                stage_raw.get("overrides", {}))
            if stage_name and overrides:
                stages.append(AlgorithmStage(stage_name, overrides))
        if stages:
            sequences.append(AlgorithmSequence(name, tuple(stages)))
    return tuple(sequences) or _DEFAULT_SEQUENCES


class SMTAlgorithmScheduler:
    """Contextual sequence scheduling with online feedback and offline priors."""

    SCHEMA = 1
    DIMENSION = DIMENSION

    def __init__(
        self,
        state_path: str | None,
        *,
        sequence_space: str | None = None,
        prior_path: str | None = None,
        timeout_sec: float = 30.0,
        max_clusters: int = 8,
        seed: int | None = None,
    ) -> None:
        self.state_path = state_path or ""
        self.sequences = _load_sequences(sequence_space)
        self.sequence_by_name = {
            sequence.name: sequence for sequence in self.sequences
        }
        self.prior: (
            ContextSequencePrior | EnsembleSequencePrior | None
        ) = (
            load_sequence_policy(
                prior_path, allowed_actions=self.sequence_by_name)
            or load_ensemble_sequence_policy(
                prior_path, allowed_actions=self.sequence_by_name)
            or load_smbo_schedule_policy(
                prior_path, allowed_actions=self.sequence_by_name)
        )
        self.prior_recommendations = 0
        self.prior_matches = 0
        self.prior_budgeted_assignments = 0
        self.prior_schedule_completions = 0
        self.prior_progress: dict[
            str,
            tuple[
                str,
                int | None,
                tuple[str, ...],
                tuple[float, ...],
                int,
            ],
        ] = {}
        self.timeout_sec = max(0.01, float(timeout_sec))
        self.max_clusters = max(1, min(32, int(max_clusters)))
        self.rng = random.Random(seed)
        self.clusters: list[ContextCluster] = []
        self.arms: dict[str, BayesianArm] = {}
        self.seed_contexts: dict[str, tuple[float, ...]] = {}
        self.active_sequences: dict[str, tuple[str, int]] = {}
        self.pending: dict[str, tuple[int, str, str, tuple[float, ...]]] = {}
        self._pending_rollbacks: dict[str, dict[str, Any]] = {}
        self.sequence_number = 0
        self.observations = 0
        self._load()

    @staticmethod
    def context(
        telemetry: SolverTelemetry | None,
        *,
        input_bytes: int = 0,
        target_branch: int = 0,
    ) -> tuple[float, ...]:
        if telemetry is None:
            size = max(0, int(input_bytes))
            return context_feature_vector({
                "input_bytes": size,
                "data_quality": 0.5,
                "targeted": bool(target_branch),
            })
        return context_feature_vector({
            "input_bytes": telemetry.input_bytes,
            "difficulty": telemetry.difficulty,
            "timeout_ratio": telemetry.timeout_ratio,
            "solver_unknown_ratio": (
                telemetry.solver_unknown
                / max(1, telemetry.solver_queries)),
            "dependency_bytes": telemetry.max_dependency_bytes,
            "data_quality": telemetry.data_quality,
            "branch_pressure": (
                telemetry.interesting_branches
                / max(1, telemetry.symbolic_branches)),
            "targeted": bool(target_branch or telemetry.target_branch),
            "query_ir_queries": telemetry.query_exports,
            "query_ir_nodes": telemetry.query_ir_nodes,
            "query_ir_input_bytes": telemetry.query_ir_input_bytes,
            "query_ir_max_bits": telemetry.query_ir_max_bits,
            "query_ir_comparison_ops": telemetry.query_ir_comparison_ops,
            "query_ir_nonlinear_ops": telemetry.query_ir_nonlinear_ops,
            "query_ir_bitwise_ops": telemetry.query_ir_bitwise_ops,
            "query_ir_structural_ops": telemetry.query_ir_structural_ops,
        })

    def _cluster(self, vector: tuple[float, ...], *, update: bool) -> int:
        if not self.clusters:
            self.clusters.append(
                ContextCluster(list(vector), [0.0] * len(vector)))
            return 0
        index = min(
            range(len(self.clusters)),
            key=lambda candidate: self.clusters[candidate].distance(vector),
        )
        if update:
            self.clusters[index].update(vector)
            self._maybe_split(index)
        return index

    def _maybe_split(self, index: int) -> None:
        if len(self.clusters) >= self.max_clusters:
            return
        cluster = self.clusters[index]
        variances = cluster.variance
        axis = max(range(len(variances)), key=variances.__getitem__)
        if cluster.samples < 16 or variances[axis] < 0.035:
            return
        displacement = 0.5 * math.sqrt(variances[axis])
        left = list(cluster.centroid)
        right = list(cluster.centroid)
        left[axis] = max(0.0, left[axis] - displacement)
        right[axis] = min(1.0, right[axis] + displacement)
        half = max(1, cluster.samples // 2)
        self.clusters[index] = ContextCluster(
            left, [value * 0.5 for value in cluster.m2], half)
        self.clusters.append(ContextCluster(
            right, [value * 0.5 for value in cluster.m2],
            max(1, cluster.samples - half)))

    @staticmethod
    def _arm_key(cluster: int, sequence: str) -> str:
        return f"{cluster}:{sequence}"

    def _select_sequence(self, cluster: int) -> tuple[str, float]:
        cold = [
            sequence.name for sequence in self.sequences
            if self.arms.get(
                self._arm_key(cluster, sequence.name),
                BayesianArm(),
            ).observations == 0
        ]
        if cold:
            return cold[0], 1.0 / len(cold)
        sampled = []
        for sequence in self.sequences:
            arm = self.arms.setdefault(
                self._arm_key(cluster, sequence.name), BayesianArm())
            sampled.append((arm.sample(self.rng), sequence.name))
        sampled.sort(reverse=True)
        # The exact Thompson propensity is expensive; this bounded softmax
        # approximation is logged for conservative offline evaluation.
        peak = sampled[0][0]
        weights = [math.exp(min(20.0, score - peak)) for score, _ in sampled]
        propensity = weights[0] / max(1e-12, sum(weights))
        return sampled[0][1], propensity

    def select(
        self,
        path: str,
        *,
        input_bytes: int = 0,
        target_branch: int = 0,
        worker: int = 0,
        preferred_sequence: str = "",
    ) -> AlgorithmAssignment:
        del worker
        getstate = getattr(self.rng, "getstate", None)
        rollback = {
            "active": self.active_sequences.get(path),
            "active_present": path in self.active_sequences,
            "prior": self.prior_progress.get(path),
            "prior_present": path in self.prior_progress,
            "prior_recommendations": self.prior_recommendations,
            "prior_matches": self.prior_matches,
            "prior_budgeted_assignments": self.prior_budgeted_assignments,
            "prior_schedule_completions": self.prior_schedule_completions,
            "arms": set(self.arms),
            "cluster_count": len(self.clusters),
            "sequence_number": self.sequence_number,
            "rng_state": getstate() if callable(getstate) else None,
        }
        vector = self.seed_contexts.get(
            path,
            self.context(
                None, input_bytes=input_bytes, target_branch=target_branch))
        cluster = self._cluster(vector, update=False)
        active = self.active_sequences.get(path)
        prior_sequence = ""
        prior_cluster: int | None = None
        prior_schedule: tuple[str, ...] = ()
        prior_budgets: tuple[float, ...] = ()
        prior_schedule_index = 0
        explicit_preference = preferred_sequence in self.sequence_by_name
        if explicit_preference:
            self.prior_progress.pop(path, None)
        if (
            active is None
            and not explicit_preference
            and self.prior is not None
        ):
            progress = self.prior_progress.get(path)
            if (
                progress is not None
                and progress[0] == self.prior.artifact_sha256
            ):
                (
                    _,
                    prior_cluster,
                    prior_schedule,
                    prior_budgets,
                    prior_schedule_index,
                ) = progress
            else:
                budgeted_method = getattr(
                    self.prior, "budgeted_schedule", None)
                schedule_method = getattr(self.prior, "schedule", None)
                if callable(budgeted_method):
                    budgeted_schedule, prior_cluster = budgeted_method(vector)
                    prior_schedule = tuple(
                        stage[0] for stage in budgeted_schedule)
                    prior_budgets = tuple(
                        float(stage[1]) for stage in budgeted_schedule)
                elif callable(schedule_method):
                    prior_schedule, prior_cluster = schedule_method(vector)
                else:
                    recommendation, prior_cluster = self.prior.recommend(
                        vector)
                    prior_schedule = (
                        (recommendation,) if recommendation else ())
                prior_schedule_index = 0
            if prior_schedule_index < len(prior_schedule):
                prior_sequence = prior_schedule[prior_schedule_index]
                self.prior_progress[path] = (
                    self.prior.artifact_sha256,
                    prior_cluster,
                    prior_schedule,
                    prior_budgets,
                    prior_schedule_index,
                )
            if prior_sequence in self.sequence_by_name:
                preferred_sequence = prior_sequence
                self.prior_recommendations += 1
        if active is None or active[0] not in self.sequence_by_name:
            has_preference = preferred_sequence in self.sequence_by_name
            if has_preference and self.rng.random() < 0.90:
                sequence_name = preferred_sequence
                propensity = 0.90 + 0.10 / len(self.sequences)
            else:
                sequence_name, raw_propensity = self._select_sequence(cluster)
                propensity = raw_propensity
                if has_preference:
                    propensity = 0.10 * raw_propensity
                    if sequence_name == preferred_sequence:
                        propensity += 0.90
            stage_index = 0
        else:
            sequence_name, stage_index = active
            propensity = 1.0
        sequence = self.sequence_by_name[sequence_name]
        stage_index = min(stage_index, len(sequence.stages) - 1)
        stage = sequence.stages[stage_index]
        next_index = stage_index + 1
        if next_index < len(sequence.stages):
            self.active_sequences[path] = (sequence_name, next_index)
        else:
            self.active_sequences.pop(path, None)

        self.sequence_number += 1
        digest = hashlib.blake2b(
            f"{path}\0{self.sequence_number}\0{sequence_name}\0{stage_index}"
            .encode("utf-8", errors="surrogateescape"),
            digest_size=10,
        ).hexdigest()
        token = f"alg-{digest}"
        self.pending[token] = (
            cluster, sequence_name, path, vector)
        self._pending_rollbacks[token] = rollback
        overrides = dict(stage.overrides)
        overrides["SYMCC_SOLVER_ALGORITHM"] = (
            f"{sequence_name}:{stage.name}")
        overrides["SYMCC_ALGORITHM_TOKEN"] = token
        overrides["SYMCC_ALGORITHM_SEQUENCE"] = sequence_name
        overrides["SYMCC_ALGORITHM_PROPENSITY"] = f"{propensity:.9g}"
        if prior_sequence:
            self.prior_matches += int(sequence_name == prior_sequence)
            if sequence_name == prior_sequence:
                next_prior_index = prior_schedule_index + 1
                if next_prior_index < len(prior_schedule):
                    self.prior_progress[path] = (
                        self.prior.artifact_sha256,
                        prior_cluster,
                        prior_schedule,
                        prior_budgets,
                        next_prior_index,
                    )
                else:
                    self.prior_progress.pop(path, None)
                    if len(prior_schedule) > 1:
                        self.prior_schedule_completions += 1
            overrides["SYMCC_ALGORITHM_PRIOR_SHA256"] = (
                self.prior.artifact_sha256 if self.prior is not None else "")
            overrides["SYMCC_ALGORITHM_PRIOR_CLUSTER"] = str(prior_cluster)
            overrides["SYMCC_ALGORITHM_PRIOR_SEQUENCE"] = prior_sequence
            overrides["SYMCC_ALGORITHM_PRIOR_SCHEDULE_INDEX"] = str(
                prior_schedule_index)
            overrides["SYMCC_ALGORITHM_PRIOR_SCHEDULE_LENGTH"] = str(
                len(prior_schedule))
            if prior_schedule_index < len(prior_budgets):
                self.prior_budgeted_assignments += 1
                overrides["SYMCC_ALGORITHM_BUDGET_SEC"] = (
                    f"{prior_budgets[prior_schedule_index]:.12g}")
        return AlgorithmAssignment(
            token, sequence_name, stage.name, stage_index,
            overrides, propensity)

    def observe(
        self,
        token: str,
        *,
        path: str,
        telemetry: SolverTelemetry | None,
        reward: float,
        elapsed: float,
        killed: bool,
    ) -> bool:
        pending = self.pending.pop(str(token), None)
        if pending is None:
            return False
        self._pending_rollbacks.pop(str(token), None)
        cluster, sequence_name, pending_path, _old_vector = pending
        path = path or pending_path
        vector = self.context(
            telemetry,
            input_bytes=(telemetry.input_bytes if telemetry else 0),
            target_branch=(telemetry.target_branch if telemetry else 0),
        )
        assigned_cluster = self._cluster(vector, update=True)
        self.seed_contexts[path] = vector
        # Keep attribution at dispatch-time cluster; the updated cluster is for
        # future selections and may have split after this observation.
        arm = self.arms.setdefault(
            self._arm_key(cluster, sequence_name), BayesianArm())
        timed_out = bool(
            killed
            or (telemetry is not None and (
                telemetry.solver_unknown > 0 or telemetry.z3_timeouts > 0)))
        par2_cost = (
            2.0 * self.timeout_sec
            if timed_out else min(2.0 * self.timeout_sec, max(0.0, elapsed)))
        normalized_cost = (
            math.log1p(par2_cost)
            / max(1e-9, math.log1p(2.0 * self.timeout_sec)))
        bounded_reward = 0.0 if killed else max(0.0, min(1.0, reward))
        utility = bounded_reward - 0.35 * normalized_cost
        arm.update(utility, par2_cost, timed_out)
        if assigned_cluster != cluster:
            mirror = self.arms.setdefault(
                self._arm_key(assigned_cluster, sequence_name),
                BayesianArm())
            mirror.update(utility, par2_cost, timed_out)
        self.observations += 1
        if self.observations % 8 == 0:
            self.save()
        return True

    def abandon(self, token: str) -> bool:
        """Undo a selection whose algorithm stage was never dispatched."""
        token = str(token)
        pending = self.pending.pop(token, None)
        rollback = self._pending_rollbacks.pop(token, None)
        if pending is None or rollback is None:
            return False
        if self.sequence_number != int(rollback["sequence_number"]) + 1:
            # Exact rewind is only safe before another global selection. MPI
            # pre-dispatch rollback has this property; late result rejection
            # uses discard() instead.
            return True
        path = pending[2]
        if rollback["active_present"]:
            self.active_sequences[path] = rollback["active"]
        else:
            self.active_sequences.pop(path, None)
        if rollback["prior_present"]:
            self.prior_progress[path] = rollback["prior"]
        else:
            self.prior_progress.pop(path, None)
        self.prior_recommendations = rollback["prior_recommendations"]
        self.prior_matches = rollback["prior_matches"]
        self.prior_budgeted_assignments = rollback[
            "prior_budgeted_assignments"]
        self.prior_schedule_completions = rollback[
            "prior_schedule_completions"]
        old_arms = rollback["arms"]
        for key in set(self.arms) - old_arms:
            self.arms.pop(key, None)
        cluster_count = int(rollback["cluster_count"])
        if len(self.clusters) > cluster_count:
            del self.clusters[cluster_count:]
        setstate = getattr(self.rng, "setstate", None)
        if rollback["rng_state"] is not None and callable(setstate):
            setstate(rollback["rng_state"])
        self.sequence_number = int(rollback["sequence_number"])
        return True

    def discard(self, token: str) -> bool:
        """Drop feedback for an executed stage whose result cannot be trusted."""
        token = str(token)
        pending = self.pending.pop(token, None)
        self._pending_rollbacks.pop(token, None)
        return pending is not None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "feature_schema": FEATURE_SCHEMA,
            "feature_dimension": self.DIMENSION,
            "observations": self.observations,
            "sequence_number": self.sequence_number,
            "prior_artifact_sha256": (
                self.prior.artifact_sha256 if self.prior is not None else ""),
            "prior_kind": (
                self.prior.kind
                if isinstance(self.prior, EnsembleSequencePrior)
                else "xmeans-lcb"
                if isinstance(self.prior, ContextSequencePrior)
                else ""),
            "prior_recommendations": self.prior_recommendations,
            "prior_matches": self.prior_matches,
            "prior_budgeted_assignments": self.prior_budgeted_assignments,
            "prior_schedule_completions": self.prior_schedule_completions,
            "clusters": [{
                "centroid": cluster.centroid,
                "m2": cluster.m2,
                "samples": cluster.samples,
            } for cluster in self.clusters],
            "arms": {
                key: arm.to_mapping() for key, arm in self.arms.items()
            },
        }

    def save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".smt-algorithm-", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(
                        self.to_mapping(), stream,
                        sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                os.replace(tmp_path, self.state_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != self.SCHEMA:
            return
        try:
            self.observations = max(0, int(raw.get("observations", 0)))
            self.sequence_number = max(0, int(raw.get("sequence_number", 0)))
            current_prior = (
                self.prior.artifact_sha256 if self.prior is not None else "")
            if str(raw.get("prior_artifact_sha256", "")) == current_prior:
                self.prior_recommendations = max(
                    0, int(raw.get("prior_recommendations", 0)))
                self.prior_matches = max(
                    0, int(raw.get("prior_matches", 0)))
                self.prior_budgeted_assignments = max(
                    0, int(raw.get("prior_budgeted_assignments", 0)))
                self.prior_schedule_completions = max(
                    0, int(raw.get("prior_schedule_completions", 0)))
        except (TypeError, ValueError):
            self.observations = self.sequence_number = 0
            self.prior_recommendations = self.prior_matches = 0
            self.prior_budgeted_assignments = 0
            self.prior_schedule_completions = 0
        clusters = raw.get("clusters", ())
        if isinstance(clusters, list):
            for item in clusters[:self.max_clusters]:
                if not isinstance(item, dict):
                    continue
                centroid = item.get("centroid")
                m2 = item.get("m2")
                if not isinstance(centroid, list) or not isinstance(m2, list):
                    continue
                normalized_centroid = adapt_feature_vector(
                    centroid, self.DIMENSION)
                normalized_m2 = adapt_feature_vector(m2, self.DIMENSION)
                if (
                    normalized_centroid is None
                    or normalized_m2 is None
                    or normalized_centroid[0] != 1.0
                    or any(
                        not 0.0 <= value <= 1.0
                        for value in normalized_centroid
                    )
                    or any(value < 0.0 for value in normalized_m2)
                ):
                    continue
                try:
                    self.clusters.append(ContextCluster(
                        list(normalized_centroid),
                        list(normalized_m2),
                        max(1, int(item.get("samples", 1))),
                    ))
                except (TypeError, ValueError, OverflowError):
                    continue
        arms = raw.get("arms", {})
        if isinstance(arms, dict):
            for key, value in arms.items():
                if isinstance(key, str) and len(key) <= 128:
                    self.arms[key] = BayesianArm.from_mapping(value)
