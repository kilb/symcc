#!/usr/bin/env python3
"""Bounded ParaSuit-style value-space construction for self-configuration.

The upstream ParaSuit policy clusters (parameter value, coverage utility)
observations and exploits only when the clustering has a sufficiently strong
silhouette score.  This module keeps that policy separate from the F396 native
registry so historical provider contracts remain reproducible while the MPI
coordinator can use a program-bound, persistent value model.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import argparse
import hashlib
import json
import math
import os
import stat
import tempfile
from typing import Any, Iterable

from self_config import ParameterSpec, SelfConfiguringPolicy


_VALUE_STATE_SCHEMA = "symcc-parasuit-value-space-v1"
_PROVIDER_SCHEMA = "symcc-parameter-provider-v1"
_VALUE_POLICIES = {"hybrid", "silhouette", "thompson"}
_MAX_VALUE_STATE_BYTES = 4 * 1024 * 1024
_MAX_HISTORY_PER_PARAMETER = 512
_MAX_SHIFT_ITERATIONS = 32


def native_value_policy_provider_payload() -> dict[str, Any]:
    """Return the campaign-lifecycle contract for the value policy."""
    active = {
        "SYMCC_SELF_CONFIG_VALUE_POLICY": ["hybrid", "silhouette"],
    }
    return {
        "schema": _PROVIDER_SCHEMA,
        "provider": "symcc-parasuit-value-policy",
        "scope": "coordinator-campaign",
        "parameters": {
            "SYMCC_SELF_CONFIG_VALUE_POLICY": {
                "values": ["hybrid", "silhouette", "thompson"],
            },
            "SYMCC_SELF_CONFIG_SILHOUETTE_THRESHOLD": {
                "values": ["0.5", "0.7", "0.85"],
                "numeric": True,
                "min": -1.0,
                "max": 1.0,
                "active_when": active,
            },
            "SYMCC_SELF_CONFIG_CLUSTER_WINDOW": {
                "values": ["16", "32", "64", "128"],
                "numeric": True,
                "min": 8,
                "max": 256,
                "active_when": active,
            },
            "SYMCC_SELF_CONFIG_MIN_CLUSTER_SAMPLES": {
                "values": ["4", "6", "8"],
                "numeric": True,
                "min": 4,
                "max": 64,
                "active_when": active,
            },
            "SYMCC_SELF_CONFIG_EXPLORATION_RESERVE": {
                "values": ["0", "0.1", "0.2"],
                "numeric": True,
                "min": 0.0,
                "max": 0.5,
                "active_when": active,
            },
        },
    }


def _finite_float(raw: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(value):
        return default
    return max(minimum, min(maximum, value))


def _bounded_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value))


def _bounded_json_state(path: str) -> tuple[dict[str, Any], str] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAX_VALUE_STATE_BYTES
            ):
                return None
            chunks: list[bytes] = []
            remaining = _MAX_VALUE_STATE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    content = b"".join(chunks)
    if len(content) > _MAX_VALUE_STATE_BYTES:
        return None
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw, hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ValueObservation:
    value: str
    reward: float
    elapsed: float
    killed: bool
    context: str
    sequence: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "reward": self.reward,
            "elapsed": self.elapsed,
            "killed": self.killed,
            "context": self.context,
            "sequence": self.sequence,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "ValueObservation | None":
        if not isinstance(raw, dict):
            return None
        value = raw.get("value")
        context = raw.get("context")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or "\x00" in value
            or not isinstance(context, str)
            or len(context) > 256
            or "\x00" in context
            or not isinstance(raw.get("killed"), bool)
        ):
            return None
        raw_reward = raw.get("reward")
        raw_elapsed = raw.get("elapsed")
        if (
            isinstance(raw_reward, bool)
            or not isinstance(raw_reward, (int, float))
            or not math.isfinite(float(raw_reward))
            or not 0.0 <= float(raw_reward) <= 1.0
            or isinstance(raw_elapsed, bool)
            or not isinstance(raw_elapsed, (int, float))
            or not math.isfinite(float(raw_elapsed))
            or not 0.001 <= float(raw_elapsed) <= 86400.0
        ):
            return None
        reward = float(raw_reward)
        elapsed = float(raw_elapsed)
        raw_sequence = raw.get("sequence")
        if (
            isinstance(raw_sequence, bool)
            or not isinstance(raw_sequence, int)
            or raw_sequence < 0
        ):
            return None
        sequence = raw_sequence
        return cls(
            value=value,
            reward=reward,
            elapsed=elapsed,
            killed=raw["killed"],
            context=context,
            sequence=sequence,
        )


@dataclass(frozen=True)
class ValueSpaceAnalysis:
    mode: str
    reason: str
    silhouette: float
    cluster_count: int
    sample_count: int
    bandwidth: float
    labels: tuple[int, ...] = ()
    utilities: tuple[float, ...] = ()
    observations: tuple[ValueObservation, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "silhouette": round(self.silhouette, 9),
            "clusters": self.cluster_count,
            "samples": self.sample_count,
            "bandwidth": round(self.bandwidth, 9),
        }


def _distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * max(0.0, min(1.0, quantile))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _mean_shift_labels(
    points: tuple[tuple[float, float], ...], bandwidth: float
) -> tuple[int, ...]:
    shifted: list[tuple[float, float]] = []
    for seed in points:
        center = seed
        for _ in range(_MAX_SHIFT_ITERATIONS):
            neighbors = [
                point for point in points if _distance(point, center) <= bandwidth
            ]
            if not neighbors:
                break
            updated = (
                sum(point[0] for point in neighbors) / len(neighbors),
                sum(point[1] for point in neighbors) / len(neighbors),
            )
            if _distance(updated, center) <= 1e-7:
                center = updated
                break
            center = updated
        shifted.append(center)

    centers: list[tuple[float, float]] = []
    merge_radius = max(1e-9, bandwidth * 0.5)
    for center in sorted(shifted):
        nearest = min(
            range(len(centers)),
            key=lambda index: _distance(center, centers[index]),
            default=None,
        )
        if nearest is None or _distance(center, centers[nearest]) > merge_radius:
            centers.append(center)

    raw_labels = [
        min(
            range(len(centers)),
            key=lambda index: (_distance(point, centers[index]), index),
        )
        for point in points
    ]
    used = sorted(
        set(raw_labels),
        key=lambda label: (centers[label][0], centers[label][1], label),
    )
    canonical = {label: index for index, label in enumerate(used)}
    return tuple(canonical[label] for label in raw_labels)


def _silhouette_score(
    points: tuple[tuple[float, float], ...], labels: tuple[int, ...]
) -> float:
    clusters = sorted(set(labels))
    if len(clusters) < 2 or len(clusters) >= len(points):
        return 0.0
    scores: list[float] = []
    for index, point in enumerate(points):
        own = [
            _distance(point, other)
            for other_index, other in enumerate(points)
            if other_index != index and labels[other_index] == labels[index]
        ]
        if not own:
            scores.append(0.0)
            continue
        intra = sum(own) / len(own)
        inter = min(
            sum(
                _distance(point, other)
                for other_index, other in enumerate(points)
                if labels[other_index] == cluster
            )
            / sum(1 for label in labels if label == cluster)
            for cluster in clusters
            if cluster != labels[index]
        )
        denominator = max(intra, inter)
        scores.append(0.0 if denominator <= 1e-12 else (inter - intra) / denominator)
    return sum(scores) / len(scores)


def _observation_utilities(
    observations: tuple[ValueObservation, ...],
) -> tuple[float, ...]:
    elapsed_values = sorted(observation.elapsed for observation in observations)
    median_elapsed = _percentile(elapsed_values, 0.5)
    utilities: list[float] = []
    for observation in observations:
        if observation.killed:
            utilities.append(0.0)
            continue
        relative_cost = observation.elapsed / max(0.001, median_elapsed)
        cost_penalty = math.sqrt(max(0.25, min(4.0, relative_cost)))
        utilities.append(max(0.0, min(1.0, observation.reward / cost_penalty)))
    return tuple(utilities)


def analyze_value_space(
    spec: ParameterSpec,
    observations: Iterable[ValueObservation],
    *,
    threshold: float = 0.7,
    min_samples: int = 6,
    window: int = 64,
) -> ValueSpaceAnalysis:
    """Run a bounded deterministic MeanShift/silhouette admission check."""
    bounded_window = _bounded_int(window, 64, 4, 256)
    recent = tuple(deque(observations, maxlen=bounded_window))
    selected = tuple(
        normalized
        for normalized in (
            ValueObservation.from_mapping(observation.to_mapping())
            if isinstance(observation, ValueObservation)
            else None
            for observation in recent
        )
        if normalized is not None
    )
    if spec.numeric:
        usable: list[tuple[ValueObservation, float]] = []
        for observation in selected:
            try:
                numeric = float(observation.value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(numeric):
                usable.append((observation, numeric))
        selected = tuple(observation for observation, _ in usable)
        raw_values = [value for _, value in usable]
    else:
        known = list(spec.values)
        extras = sorted({item.value for item in selected if item.value not in known})
        encoded = {value: index for index, value in enumerate([*known, *extras])}
        raw_values = [float(encoded[observation.value]) for observation in selected]

    required = min(bounded_window, _bounded_int(min_samples, 6, 4, 64))
    if len(selected) < required:
        return ValueSpaceAnalysis(
            "explore", "insufficient-samples", 0.0, 0, len(selected), 0.0
        )
    lower = min(raw_values)
    upper = max(raw_values)
    if upper - lower <= 1e-12:
        return ValueSpaceAnalysis(
            "explore", "constant-value", 0.0, 0, len(selected), 0.0
        )
    normalized_values = [(value - lower) / (upper - lower) for value in raw_values]
    utilities = _observation_utilities(selected)
    points = tuple(zip(normalized_values, utilities, strict=True))
    distances = [
        _distance(points[left], points[right])
        for left in range(len(points))
        for right in range(left + 1, len(points))
        if _distance(points[left], points[right]) > 1e-12
    ]
    bandwidth = _percentile(distances, 0.2)
    if bandwidth <= 1e-12:
        return ValueSpaceAnalysis(
            "explore", "zero-bandwidth", 0.0, 0, len(selected), 0.0
        )
    labels = _mean_shift_labels(points, bandwidth)
    cluster_count = len(set(labels))
    if cluster_count < 2 or cluster_count >= len(points):
        return ValueSpaceAnalysis(
            "explore",
            "non-partitioning-clusters",
            0.0,
            cluster_count,
            len(selected),
            bandwidth,
            labels,
            utilities,
            selected,
        )
    score = _silhouette_score(points, labels)
    bounded_threshold = _finite_float(threshold, 0.7, -1.0, 1.0)
    return ValueSpaceAnalysis(
        "exploit" if score >= bounded_threshold else "explore",
        "silhouette-admitted"
        if score >= bounded_threshold
        else "silhouette-below-threshold",
        score,
        cluster_count,
        len(selected),
        bandwidth,
        labels,
        utilities,
        selected,
    )


class AdaptiveSelfConfiguringPolicy(SelfConfiguringPolicy):
    """F396 registry policy with program-bound ParaSuit value adaptation."""

    def __init__(
        self,
        state_path: str | None,
        profiles: Iterable[dict[str, str]],
        *,
        custom_space: str | None = None,
        schema_space: str | None = None,
        provider_commands: Iterable[Iterable[str]] | None = None,
        provider_payloads: Iterable[Any] = (),
        prior_path: str | None = None,
        max_parameters: int = 4,
        seed: int | None = None,
        value_policy: str | None = None,
        silhouette_threshold: float | str | None = None,
        cluster_window: int | str | None = None,
        min_cluster_samples: int | str | None = None,
        exploration_reserve: float | str | None = None,
        program_key: str | None = None,
        value_state_path: str | None = None,
    ) -> None:
        base_state_path = state_path or ""
        providers = [native_value_policy_provider_payload(), *provider_payloads]
        super().__init__(
            None,
            profiles,
            custom_space=custom_space,
            schema_space=schema_space,
            provider_commands=provider_commands,
            provider_payloads=providers,
            prior_path=None,
            max_parameters=max_parameters,
            seed=seed,
        )
        self.state_path = base_state_path
        requested_policy = str(
            value_policy
            if value_policy is not None
            else os.environ.get("SYMCC_SELF_CONFIG_VALUE_POLICY", "hybrid")
        ).lower()
        self.value_policy = (
            requested_policy if requested_policy in _VALUE_POLICIES else "hybrid"
        )
        self.silhouette_threshold = _finite_float(
            silhouette_threshold
            if silhouette_threshold is not None
            else os.environ.get("SYMCC_SELF_CONFIG_SILHOUETTE_THRESHOLD", "0.7"),
            0.7,
            -1.0,
            1.0,
        )
        self.cluster_window = _bounded_int(
            cluster_window
            if cluster_window is not None
            else os.environ.get("SYMCC_SELF_CONFIG_CLUSTER_WINDOW", "64"),
            64,
            8,
            256,
        )
        self.min_cluster_samples = min(
            self.cluster_window,
            _bounded_int(
                min_cluster_samples
                if min_cluster_samples is not None
                else os.environ.get("SYMCC_SELF_CONFIG_MIN_CLUSTER_SAMPLES", "6"),
                6,
                4,
                64,
            ),
        )
        self.exploration_reserve = _finite_float(
            exploration_reserve
            if exploration_reserve is not None
            else os.environ.get("SYMCC_SELF_CONFIG_EXPLORATION_RESERVE", "0.1"),
            0.1,
            0.0,
            0.5,
        )
        identity = str(program_key or "unspecified-program")
        self.program_identity_hash = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        self.value_state_path = (
            value_state_path
            if value_state_path is not None
            else f"{base_state_path}.value-space.json"
            if base_state_path
            else ""
        )
        self.value_history: dict[str, list[ValueObservation]] = {}
        self.value_policy_counts = {
            "exploit": 0,
            "explore": 0,
            "thompson": 0,
            "constructed": 0,
            "state_rejections": 0,
        }
        self.last_value_decisions: dict[str, dict[str, Any]] = {}
        self.loaded_base_state_sha256 = ""
        self.loaded_value_state_sha256 = ""
        self._load_value_state()
        self._load_prior(prior_path)

    def _history_for(self, name: str, context_key: str) -> list[ValueObservation]:
        history = self.value_history.get(name, [])
        contextual = [item for item in history if item.context == context_key]
        if len(contextual) >= self.min_cluster_samples:
            return contextual[-self.cluster_window :]
        return history[-self.cluster_window :]

    def _policy_configuration(self) -> dict[str, Any]:
        return {
            "name": self.value_policy,
            "silhouette_threshold": self.silhouette_threshold,
            "cluster_window": self.cluster_window,
            "min_cluster_samples": self.min_cluster_samples,
            "exploration_reserve": self.exploration_reserve,
        }

    @staticmethod
    def _render_numeric(spec: ParameterSpec, value: float) -> str:
        if spec.minimum is not None:
            value = max(spec.minimum, value)
        if spec.maximum is not None:
            value = min(spec.maximum, value)
        declared = [item for item in spec.values if item != "__unset__"]
        integral = bool(declared) and all(
            item.lstrip("+-").isdigit() for item in declared
        )
        if integral:
            rounded = int(round(value))
            if spec.minimum is not None:
                rounded = max(int(math.ceil(spec.minimum)), rounded)
            if spec.maximum is not None:
                rounded = min(int(math.floor(spec.maximum)), rounded)
            return str(rounded)
        return f"{value:.6g}"

    def _sample_exploit(self, spec: ParameterSpec, analysis: ValueSpaceAnalysis) -> str:
        clusters = sorted(set(analysis.labels))
        weights = [
            1e-6
            + sum(
                analysis.utilities[index]
                for index, label in enumerate(analysis.labels)
                if label == cluster
            )
            / sum(1 for label in analysis.labels if label == cluster)
            for cluster in clusters
        ]
        selected_cluster = self.rng.choices(clusters, weights=weights, k=1)[0]
        members = [
            index
            for index, label in enumerate(analysis.labels)
            if label == selected_cluster
        ]
        if spec.numeric:
            numeric = [float(analysis.observations[index].value) for index in members]
            utility = [analysis.utilities[index] for index in members]
            total = sum(utility)
            value = (
                sum(
                    item * weight for item, weight in zip(numeric, utility, strict=True)
                )
                / total
                if total > 1e-12
                else sum(numeric) / len(numeric)
            )
            rendered = self._render_numeric(spec, value)
        else:
            candidates = [analysis.observations[index].value for index in members]
            utility = [analysis.utilities[index] + 1e-6 for index in members]
            rendered = self.rng.choices(candidates, weights=utility, k=1)[0]
        if spec.add_value(rendered):
            self.value_policy_counts["constructed"] += 1
        return rendered

    def _sample_explore(
        self,
        spec: ParameterSpec,
        observations: list[ValueObservation],
    ) -> str:
        if not spec.numeric:
            return self.rng.choice(spec.values)
        numeric = []
        for observation in observations:
            try:
                value = float(observation.value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                numeric.append((value, observation.reward))
        if not numeric:
            return super()._select_value(spec, "", {})
        anchor = (
            self.rng.choice(numeric)[0]
            if self.rng.random() < 0.5
            else max(numeric, key=lambda item: (item[1], -item[0]))[0]
        )
        if abs(anchor - 1.0) <= 1e-12 and (spec.minimum is None or spec.minimum >= 0.0):
            lower, upper = 0.5, 2.0
        else:
            declared_integral = all(
                item == "__unset__" or item.lstrip("+-").isdigit()
                for item in spec.values
            )
            radius = max(1.0 if declared_integral else 0.5, abs(anchor) * 0.5)
            lower, upper = anchor - radius, anchor + radius
        if spec.minimum is not None:
            lower = max(spec.minimum, lower)
        if spec.maximum is not None:
            upper = min(spec.maximum, upper)
        if upper < lower:
            bounded_anchor = anchor
            if spec.minimum is not None:
                bounded_anchor = max(spec.minimum, bounded_anchor)
            if spec.maximum is not None:
                bounded_anchor = min(spec.maximum, bounded_anchor)
            lower = upper = bounded_anchor
        rendered = self._render_numeric(spec, self.rng.uniform(lower, upper))
        if spec.add_value(rendered):
            self.value_policy_counts["constructed"] += 1
        return rendered

    def _select_value(
        self,
        spec: ParameterSpec,
        context_key: str,
        chosen: dict[str, str],
    ) -> str:
        if self.value_policy == "thompson":
            self.value_policy_counts["thompson"] += 1
            return super()._select_value(spec, context_key, chosen)

        history = self._history_for(spec.name, context_key)
        analysis = analyze_value_space(
            spec,
            history,
            threshold=self.silhouette_threshold,
            min_samples=self.min_cluster_samples,
            window=self.cluster_window,
        )
        self.last_value_decisions[spec.name] = {
            **analysis.to_mapping(),
            "context": context_key,
        }
        if analysis.mode == "exploit" and self.rng.random() >= self.exploration_reserve:
            self.value_policy_counts["exploit"] += 1
            return self._sample_exploit(spec, analysis)

        self.value_policy_counts["explore"] += 1
        if self.value_policy == "silhouette" and history:
            return self._sample_explore(spec, history)
        return super()._select_value(spec, context_key, chosen)

    def observe(
        self,
        token: str,
        *,
        reward: float,
        elapsed: float,
        killed: bool = False,
    ) -> bool:
        pending = self.pending.get(str(token))
        choices: dict[str, str] = {}
        context = self._context_key(None)
        if isinstance(pending, dict):
            raw_choices = pending.get("choices", pending)
            if isinstance(raw_choices, dict):
                choices = {
                    str(name): str(value)
                    for name, value in raw_choices.items()
                    if name in self.parameters
                }
            context = str(pending.get("context", context))[:256]
        bounded_reward = _finite_float(reward, 0.0, 0.0, 1.0)
        bounded_elapsed = _finite_float(elapsed, 1.0, 0.001, 86400.0)
        accepted = super().observe(
            token,
            reward=bounded_reward,
            elapsed=bounded_elapsed,
            killed=killed,
        )
        if not accepted:
            return False
        for name, value in choices.items():
            history = self.value_history.setdefault(name, [])
            history.append(
                ValueObservation(
                    value=value,
                    reward=bounded_reward,
                    elapsed=bounded_elapsed,
                    killed=bool(killed),
                    context=context,
                    sequence=self.observations,
                )
            )
            del history[:-_MAX_HISTORY_PER_PARAMETER]
        return True

    def snapshot(self) -> dict[str, Any]:
        result = super().snapshot()
        result.update(
            {
                "value_policy": self.value_policy,
                "silhouette_threshold": self.silhouette_threshold,
                "cluster_window": self.cluster_window,
                "min_cluster_samples": self.min_cluster_samples,
                "exploration_reserve": self.exploration_reserve,
                "value_models": len(self.value_history),
                "value_samples": sum(map(len, self.value_history.values())),
                "value_policy_counts": dict(self.value_policy_counts),
            }
        )
        return result

    def save(self) -> None:
        super().save()
        if not self.value_state_path:
            return
        base_snapshot = (
            _bounded_json_state(self.state_path) if self.state_path else None
        )
        base_state, base_state_sha256 = base_snapshot or ({}, "")
        if (
            not base_state_sha256
            or base_state.get("schema") != self.SCHEMA
            or base_state.get("parameter_schema_hash") != self.parameter_schema_hash
            or base_state.get("sequence") != self.sequence
            or base_state.get("observations") != self.observations
        ):
            return
        payload = {
            "schema": _VALUE_STATE_SCHEMA,
            "base_state_sha256": base_state_sha256,
            "base_sequence": self.sequence,
            "base_observations": self.observations,
            "parameter_schema_hash": self.parameter_schema_hash,
            "registry_provenance_hash": self.registry_provenance_hash,
            "program_identity_hash": self.program_identity_hash,
            "policy": self._policy_configuration(),
            "counts": self.value_policy_counts,
            "history": {
                name: [item.to_mapping() for item in history]
                for name, history in sorted(self.value_history.items())
                if name in self.parameters
            },
        }
        directory = os.path.dirname(os.path.abspath(self.value_state_path))
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".parasuit-values-", dir=directory, text=True
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.value_state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            return

    def _reject_value_state(self) -> None:
        self.value_policy_counts["state_rejections"] += 1

    def _load_value_state(self) -> None:
        if not self.value_state_path:
            return
        if not os.path.lexists(self.value_state_path):
            return
        value_snapshot = _bounded_json_state(self.value_state_path)
        if value_snapshot is None:
            self._reject_value_state()
            return
        raw, value_state_sha256 = value_snapshot
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != _VALUE_STATE_SCHEMA
            or raw.get("parameter_schema_hash") != self.parameter_schema_hash
            or raw.get("registry_provenance_hash") != self.registry_provenance_hash
            or raw.get("program_identity_hash") != self.program_identity_hash
            or raw.get("policy") != self._policy_configuration()
        ):
            self._reject_value_state()
            return
        base_snapshot = (
            _bounded_json_state(self.state_path) if self.state_path else None
        )
        base_state, base_state_sha256 = base_snapshot or ({}, "")
        if (
            not self.state_path
            or base_state_sha256 != raw.get("base_state_sha256")
            or base_state.get("schema") != self.SCHEMA
            or base_state.get("parameter_schema_hash") != self.parameter_schema_hash
            or base_state.get("sequence") != raw.get("base_sequence")
            or base_state.get("observations") != raw.get("base_observations")
        ):
            self._reject_value_state()
            return
        history = raw.get("history")
        if not isinstance(history, dict) or len(history) > 128:
            self._reject_value_state()
            return
        loaded: dict[str, list[ValueObservation]] = {}
        for name, records in history.items():
            if name not in self.parameters or not isinstance(records, list):
                self._reject_value_state()
                return
            normalized: list[ValueObservation] = []
            for record in records[-_MAX_HISTORY_PER_PARAMETER:]:
                item = ValueObservation.from_mapping(record)
                if item is None:
                    self._reject_value_state()
                    return
                normalized.append(item)
            if normalized:
                loaded[name] = normalized
        self._load_mapping(base_state)
        self.value_history = loaded
        self.loaded_base_state_sha256 = base_state_sha256
        self.loaded_value_state_sha256 = value_state_sha256
        counts = raw.get("counts")
        if isinstance(counts, dict):
            for name in self.value_policy_counts:
                self.value_policy_counts[name] = _bounded_int(
                    counts.get(name), self.value_policy_counts[name], 0, 1 << 60
                )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the ParaSuit-style campaign parameter provider."
    )
    parser.add_argument("--print-parameters", action="store_true", required=True)
    args = parser.parse_args()
    if args.print_parameters:
        print(
            json.dumps(native_value_policy_provider_payload(), sort_keys=True, indent=2)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
