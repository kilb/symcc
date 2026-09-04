#!/usr/bin/env python3
"""Bounded ParaSuit-style branch-rarity parameter selection.

ParaSuit's selection stage scores standalone and combined parameter runs from
the branch sets they cover. This module keeps that coverage model program-bound
and layers it over the F397 value policy without making it a correctness oracle.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import tempfile
from typing import Any, Iterable

from parasuit_value_policy import (
    AdaptiveSelfConfiguringPolicy,
    _bounded_int,
    _bounded_json_state,
    _finite_float,
)
from self_config import ParameterAssignment, ParameterSpec, ValuePosterior


_SELECTION_STATE_SCHEMA = "symcc-parasuit-parameter-selection-v1"
_PROVIDER_SCHEMA = "symcc-parameter-provider-v1"
_PARAMETER_POLICIES = {"hierarchical", "parasuit", "hybrid"}
_MAX_SELECTION_HISTORY = 256
_MAX_COVERAGE_FEATURES = 128


def native_parameter_selection_provider_payload() -> dict[str, Any]:
    """Return the campaign controls for branch-rarity parameter selection."""
    active = {
        "SYMCC_SELF_CONFIG_PARAMETER_POLICY": ["parasuit", "hybrid"],
    }
    return {
        "schema": _PROVIDER_SCHEMA,
        "provider": "symcc-parasuit-parameter-selection",
        "scope": "coordinator-campaign",
        "parameters": {
            "SYMCC_SELF_CONFIG_PARAMETER_POLICY": {
                "values": ["hybrid", "parasuit", "hierarchical"],
            },
            "SYMCC_SELF_CONFIG_SELECTION_WINDOW": {
                "values": ["64", "128", "256"],
                "numeric": True,
                "min": 16,
                "max": _MAX_SELECTION_HISTORY,
                "active_when": active,
            },
            "SYMCC_SELF_CONFIG_MAX_COVERAGE_FEATURES": {
                "values": ["32", "64", "128"],
                "numeric": True,
                "min": 16,
                "max": _MAX_COVERAGE_FEATURES,
                "active_when": active,
            },
            "SYMCC_SELF_CONFIG_PARASUIT_WEIGHT": {
                "values": ["0.4", "0.6", "0.8"],
                "numeric": True,
                "min": 0.0,
                "max": 1.0,
                "active_when": {
                    "SYMCC_SELF_CONFIG_PARAMETER_POLICY": ["hybrid"],
                },
            },
        },
    }


def branch_outcome_features(
    trace: Iterable[Any], *, maximum: int = 128
) -> tuple[str, ...]:
    """Normalize runtime branch records into bounded stable outcome features."""
    limit = _bounded_int(maximum, 128, 16, _MAX_COVERAGE_FEATURES)
    features: set[str] = set()
    for raw in trace:
        if not isinstance(raw, (list, tuple)) or len(raw) != 6:
            continue
        site, taken = raw[3], raw[4]
        if (
            isinstance(site, bool)
            or not isinstance(site, int)
            or site <= 0
            or site > (1 << 64) - 1
            or isinstance(taken, bool)
            or not isinstance(taken, int)
        ):
            continue
        features.add(f"{site}:{int(taken != 0)}")
    return _bounded_feature_set(features, limit)


def _is_branch_outcome_feature(feature: Any) -> bool:
    if not isinstance(feature, str) or not feature or len(feature) > 64:
        return False
    site, separator, taken = feature.partition(":")
    return (
        separator == ":"
        and taken in {"0", "1"}
        and site.isascii()
        and site.isdigit()
        and not site.startswith("0")
        and int(site) <= (1 << 64) - 1
    )


def _normalize_coverage_features(
    features: Iterable[Any], maximum: int
) -> tuple[str, ...]:
    return _bounded_feature_set(
        {feature for feature in features if _is_branch_outcome_feature(feature)},
        maximum,
    )


def _bounded_feature_set(features: Iterable[str], maximum: int) -> tuple[str, ...]:
    unique = set(features)
    admitted = sorted(
        unique,
        key=lambda feature: (
            hashlib.blake2b(feature.encode("ascii"), digest_size=8).digest(),
            feature,
        ),
    )[:maximum]
    return tuple(sorted(admitted))


@dataclass(frozen=True)
class CoverageObservation:
    parameters: tuple[str, ...]
    features: tuple[str, ...]
    extraction: bool
    context: str
    sequence: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "parameters": list(self.parameters),
            "features": list(self.features),
            "extraction": self.extraction,
            "context": self.context,
            "sequence": self.sequence,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        allowed_parameters: set[str],
        maximum_features: int,
    ) -> "CoverageObservation | None":
        if not isinstance(raw, dict) or set(raw) != {
            "parameters",
            "features",
            "extraction",
            "context",
            "sequence",
        }:
            return None
        parameters = raw.get("parameters")
        features = raw.get("features")
        context = raw.get("context")
        sequence = raw.get("sequence")
        if (
            not isinstance(parameters, list)
            or not 1 <= len(parameters) <= 16
            or any(not isinstance(name, str) for name in parameters)
            or parameters != sorted(set(parameters))
            or any(name not in allowed_parameters for name in parameters)
            or not isinstance(features, list)
            or not 1 <= len(features) <= maximum_features
            or any(not _is_branch_outcome_feature(feature) for feature in features)
            or features != sorted(set(features))
            or not isinstance(raw.get("extraction"), bool)
            or (raw["extraction"] and len(parameters) != 1)
            or not isinstance(context, str)
            or len(context) > 256
            or "\x00" in context
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            return None
        return cls(
            tuple(parameters),
            tuple(features),
            raw["extraction"],
            context,
            sequence,
        )


@dataclass(frozen=True)
class ParameterSpaceAnalysis:
    baseline: dict[str, float]
    combined: dict[str, float]
    normalized: dict[str, float]
    feature_frequency: dict[str, int]
    baseline_samples: dict[str, int]
    combined_samples: dict[str, int]
    penalized_samples: dict[str, int]
    observations: int

    @property
    def ready(self) -> bool:
        return bool(self.feature_frequency) and any(self.baseline_samples.values())


def analyze_parameter_space(
    parameter_names: Iterable[str],
    observations: Iterable[CoverageObservation],
) -> ParameterSpaceAnalysis:
    """Recompute ParaSuit paper scores from one bounded observation window."""
    names = tuple(sorted(set(map(str, parameter_names))))
    history = tuple(observations)
    frequency: dict[str, int] = {}
    for observation in history:
        for feature in observation.features:
            frequency[feature] = frequency.get(feature, 0) + 1

    def rarity_score(observation: CoverageObservation) -> float:
        return sum(1.0 / frequency[item] for item in observation.features)

    baseline_values: dict[str, list[float]] = {name: [] for name in names}
    for observation in history:
        if observation.extraction and len(observation.parameters) == 1:
            name = observation.parameters[0]
            if name in baseline_values:
                baseline_values[name].append(rarity_score(observation))
    baseline = {
        name: (
            sum(baseline_values[name]) / len(baseline_values[name])
            if baseline_values[name]
            else 0.0
        )
        for name in names
    }

    combined_values: dict[str, list[float]] = {name: [] for name in names}
    penalized = {name: 0 for name in names}
    for observation in history:
        if observation.extraction:
            continue
        score = rarity_score(observation)
        for name in observation.parameters:
            if name not in combined_values:
                continue
            if score + 1e-12 < baseline[name]:
                combined_values[name].append(0.0)
                penalized[name] += 1
            else:
                combined_values[name].append(score)
    combined = {
        name: (
            sum(combined_values[name]) / len(combined_values[name])
            if combined_values[name]
            else baseline[name]
        )
        for name in names
    }
    maximum = max(combined.values(), default=0.0)
    normalized = {
        name: (combined[name] / maximum if maximum > 1e-12 else 0.0) for name in names
    }
    return ParameterSpaceAnalysis(
        baseline=baseline,
        combined=combined,
        normalized=normalized,
        feature_frequency=frequency,
        baseline_samples={name: len(baseline_values[name]) for name in names},
        combined_samples={name: len(combined_values[name]) for name in names},
        penalized_samples=penalized,
        observations=len(history),
    )


class ParaSuitSelfConfiguringPolicy(AdaptiveSelfConfiguringPolicy):
    """F397 value adaptation plus ParaSuit branch-rarity parameter selection."""

    def __init__(
        self,
        state_path: str | None,
        profiles: Iterable[dict[str, str]],
        *,
        parameter_policy: str | None = None,
        selection_window: int | str | None = None,
        max_coverage_features: int | str | None = None,
        parasuit_weight: float | str | None = None,
        selection_state_path: str | None = None,
        provider_payloads: Iterable[Any] = (),
        **kwargs: Any,
    ) -> None:
        requested_policy = str(
            parameter_policy
            if parameter_policy is not None
            else os.environ.get("SYMCC_SELF_CONFIG_PARAMETER_POLICY", "hybrid")
        ).lower()
        self.parameter_policy = (
            requested_policy if requested_policy in _PARAMETER_POLICIES else "hybrid"
        )
        self.selection_window = _bounded_int(
            selection_window
            if selection_window is not None
            else os.environ.get("SYMCC_SELF_CONFIG_SELECTION_WINDOW", "128"),
            128,
            16,
            _MAX_SELECTION_HISTORY,
        )
        self.max_coverage_features = _bounded_int(
            max_coverage_features
            if max_coverage_features is not None
            else os.environ.get("SYMCC_SELF_CONFIG_MAX_COVERAGE_FEATURES", "128"),
            128,
            16,
            _MAX_COVERAGE_FEATURES,
        )
        self.parasuit_weight = _finite_float(
            parasuit_weight
            if parasuit_weight is not None
            else os.environ.get("SYMCC_SELF_CONFIG_PARASUIT_WEIGHT", "0.6"),
            0.6,
            0.0,
            1.0,
        )
        base_state_path = state_path or ""
        self.selection_state_path = (
            selection_state_path
            if selection_state_path is not None
            else f"{base_state_path}.parameter-selection.json"
            if base_state_path
            else ""
        )
        super().__init__(
            state_path,
            profiles,
            provider_payloads=[
                native_parameter_selection_provider_payload(),
                *provider_payloads,
            ],
            **kwargs,
        )
        self.coverage_history: list[CoverageObservation] = []
        self.parameter_policy_counts = {
            "extraction": 0,
            "parasuit": 0,
            "hybrid": 0,
            "hierarchical": 0,
            "fallback": 0,
            "missing_coverage": 0,
            "state_rejections": 0,
        }
        self.last_parameter_analysis: dict[str, Any] = {}
        self._load_selection_state()

    def _policy_configuration(self) -> dict[str, Any]:
        result = super()._policy_configuration()
        result["parameter_selection"] = {
            "name": self.parameter_policy,
            "window": self.selection_window,
            "max_coverage_features": self.max_coverage_features,
            "parasuit_weight": self.parasuit_weight,
        }
        return result

    def _selection_configuration(self) -> dict[str, Any]:
        return self._policy_configuration()["parameter_selection"]

    def _activation_profile(self, name: str) -> dict[str, str] | None:
        result: dict[str, str] = {}
        visiting: set[str] = set()

        def activate(current: str) -> bool:
            if current in visiting:
                return False
            visiting.add(current)
            spec = self.parameters[current]
            for parent, allowed in sorted(spec.active_when.items()):
                if parent not in self.parameters or not allowed:
                    return False
                value = next(
                    (item for item in allowed if item != "__unset__"), allowed[0]
                )
                result[parent] = value
                if not activate(parent):
                    return False
            visiting.remove(current)
            return True

        return result if activate(name) else None

    def _next_extraction_candidate(
        self,
    ) -> tuple[ParameterSpec, str, dict[str, str]] | None:
        for name, spec in sorted(self.parameters.items()):
            activation = self._activation_profile(name)
            if activation is None:
                continue
            for value in self.declared_parameter_values.get(name, ()):
                if value == "__unset__":
                    continue
                posterior = spec.posteriors.get(value)
                if posterior is not None and posterior.pulls == 0:
                    return spec, value, activation
        return None

    def _make_assignment(
        self,
        profile: dict[str, str],
        context_key: str,
        choices: dict[str, str],
        phase: str,
    ) -> ParameterAssignment:
        profile = self._apply_guards(profile)
        self.sequence += 1
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        digest = hashlib.sha256(
            str(self.sequence).encode("ascii")
            + b":"
            + context_key.encode("utf-8")
            + b":"
            + canonical
        ).hexdigest()
        token = f"pcfg-{self.sequence}-{digest[:16]}"
        self.pending[token] = {
            "profile": profile,
            "context": context_key,
            "choices": choices,
            "selection_phase": phase,
        }
        return ParameterAssignment(token, profile, context_key)

    def select(
        self,
        base_profile: dict[str, str] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> ParameterAssignment:
        candidate = self._next_extraction_candidate()
        if candidate is not None:
            spec, value, activation = candidate
            if value == "__unset__":
                activation.pop(spec.name, None)
            else:
                activation[spec.name] = value
            self.parameter_policy_counts["extraction"] += 1
            return self._make_assignment(
                activation,
                self._context_key(context),
                {spec.name: value},
                "extraction",
            )
        assignment = super().select(base_profile, context=context)
        pending = self.pending.get(assignment.token)
        if isinstance(pending, dict):
            pending["selection_phase"] = "iterative"
        return assignment

    @staticmethod
    def _weighted_without_replacement(
        specs: list[ParameterSpec],
        weights: dict[str, float],
        count: int,
        rng: Any,
    ) -> list[ParameterSpec]:
        ranked = sorted(
            specs,
            key=lambda spec: (
                rng.random() ** (1.0 / max(1e-9, weights.get(spec.name, 0.0))),
                spec.name,
            ),
            reverse=True,
        )
        return ranked[:count]

    def _select_parameters(
        self,
        profile: dict[str, str],
        context_key: str,
    ) -> list[ParameterSpec]:
        hierarchical = super()._select_parameters(profile, context_key)
        if self.parameter_policy == "hierarchical":
            self.parameter_policy_counts["hierarchical"] += 1
            return hierarchical
        active = [spec for spec in self.parameters.values() if spec.is_active(profile)]
        analysis = analyze_parameter_space(
            (spec.name for spec in active), self.coverage_history
        )
        self.last_parameter_analysis = {
            "ready": analysis.ready,
            "observations": analysis.observations,
            "features": len(analysis.feature_frequency),
            "baseline": analysis.baseline,
            "combined": analysis.combined,
            "normalized": analysis.normalized,
            "penalized": analysis.penalized_samples,
        }
        if not analysis.ready or not any(analysis.normalized.values()):
            self.parameter_policy_counts["fallback"] += 1
            return hierarchical

        hierarchy_rank = {
            spec.name: (len(hierarchical) - index) / max(1, len(hierarchical))
            for index, spec in enumerate(hierarchical)
        }
        if self.parameter_policy == "parasuit":
            weights = dict(analysis.normalized)
            self.parameter_policy_counts["parasuit"] += 1
        else:
            weights = {
                spec.name: self.parasuit_weight
                * analysis.normalized.get(spec.name, 0.0)
                + (1.0 - self.parasuit_weight) * hierarchy_rank.get(spec.name, 0.0)
                for spec in active
            }
            self.parameter_policy_counts["hybrid"] += 1

        admitted = [
            spec
            for spec in sorted(active, key=lambda item: item.name)
            if self.rng.random() < max(0.0, min(1.0, weights.get(spec.name, 0.0)))
        ]
        if not admitted:
            admitted = self._weighted_without_replacement(active, weights, 1, self.rng)
        if len(admitted) > self.max_parameters:
            admitted = self._weighted_without_replacement(
                admitted, weights, self.max_parameters, self.rng
            )
        return sorted(
            admitted,
            key=lambda spec: (weights.get(spec.name, 0.0), spec.name),
            reverse=True,
        )

    def observe(
        self,
        token: str,
        *,
        reward: float,
        elapsed: float,
        killed: bool = False,
        coverage_features: Iterable[Any] = (),
    ) -> bool:
        pending = self.pending.get(str(token))
        choices: dict[str, str] = {}
        context = self._context_key(None)
        extraction = False
        if isinstance(pending, dict):
            raw_choices = pending.get("choices")
            if isinstance(raw_choices, dict):
                choices = {
                    str(name): str(value)
                    for name, value in raw_choices.items()
                    if name in self.parameters
                }
            context = str(pending.get("context", context))[:256]
            extraction = pending.get("selection_phase") == "extraction"
        normalized_features = _normalize_coverage_features(
            coverage_features, self.max_coverage_features
        )
        accepted = super().observe(
            token,
            reward=reward,
            elapsed=elapsed,
            killed=killed,
        )
        if not accepted:
            return False
        if choices and normalized_features:
            self.coverage_history.append(
                CoverageObservation(
                    tuple(sorted(choices)),
                    normalized_features,
                    extraction,
                    context,
                    self.observations,
                )
            )
            del self.coverage_history[: -self.selection_window]
        else:
            self.parameter_policy_counts["missing_coverage"] += 1
        return True

    def snapshot(self) -> dict[str, Any]:
        result = super().snapshot()
        result.update(
            {
                "parameter_policy": self.parameter_policy,
                "parameter_selection_window": self.selection_window,
                "parameter_coverage_observations": len(self.coverage_history),
                "parameter_coverage_features": len(
                    {
                        feature
                        for item in self.coverage_history
                        for feature in item.features
                    }
                ),
                "parameter_policy_counts": dict(self.parameter_policy_counts),
            }
        )
        return result

    def _clear_learned_state(self) -> None:
        self.sequence = 0
        self.observations = 0
        self.expanded_values = 0
        self.pending.clear()
        self.configurations.clear()
        self.context_posteriors.clear()
        self.interactions.clear()
        self.value_history.clear()
        self.last_value_decisions.clear()
        self.loaded_base_state_sha256 = ""
        self.loaded_value_state_sha256 = ""
        for name, spec in self.parameters.items():
            declared = list(self.declared_parameter_values.get(name, ("__unset__",)))
            spec.values = declared
            spec.posteriors = {value: ValuePosterior() for value in declared}

    def _reject_selection_state(self) -> None:
        for name in self.value_policy_counts:
            self.value_policy_counts[name] = 0
        self.value_policy_counts["state_rejections"] = 1
        for name in self.parameter_policy_counts:
            self.parameter_policy_counts[name] = 0
        self.parameter_policy_counts["state_rejections"] = 1
        self._clear_learned_state()
        self.coverage_history.clear()
        self.last_parameter_analysis = {}

    def save(self) -> None:
        super().save()
        if not self.selection_state_path:
            return
        base_snapshot = (
            _bounded_json_state(self.state_path) if self.state_path else None
        )
        value_snapshot = (
            _bounded_json_state(self.value_state_path)
            if self.value_state_path
            else None
        )
        base_state, base_sha256 = base_snapshot or ({}, "")
        value_state, value_sha256 = value_snapshot or ({}, "")
        if (
            not base_sha256
            or not value_sha256
            or base_state.get("sequence") != self.sequence
            or base_state.get("observations") != self.observations
            or value_state.get("base_state_sha256") != base_sha256
            or value_state.get("policy") != self._policy_configuration()
        ):
            return
        payload = {
            "schema": _SELECTION_STATE_SCHEMA,
            "base_state_sha256": base_sha256,
            "value_state_sha256": value_sha256,
            "base_sequence": self.sequence,
            "base_observations": self.observations,
            "parameter_schema_hash": self.parameter_schema_hash,
            "registry_provenance_hash": self.registry_provenance_hash,
            "program_identity_hash": self.program_identity_hash,
            "policy": self._selection_configuration(),
            "counts": self.parameter_policy_counts,
            "history": [item.to_mapping() for item in self.coverage_history],
        }
        directory = os.path.dirname(os.path.abspath(self.selection_state_path))
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".parasuit-selection-", dir=directory, text=True
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.selection_state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            return

    def _load_selection_state(self) -> None:
        if not self.selection_state_path:
            return
        if not os.path.lexists(self.selection_state_path):
            if self.observations or self.value_history:
                self._reject_selection_state()
            return
        selection_snapshot = _bounded_json_state(self.selection_state_path)
        if selection_snapshot is None:
            self._reject_selection_state()
            return
        raw, _selection_sha256 = selection_snapshot
        if (
            set(raw)
            != {
                "schema",
                "base_state_sha256",
                "value_state_sha256",
                "base_sequence",
                "base_observations",
                "parameter_schema_hash",
                "registry_provenance_hash",
                "program_identity_hash",
                "policy",
                "counts",
                "history",
            }
            or raw.get("schema") != _SELECTION_STATE_SCHEMA
            or raw.get("base_state_sha256") != self.loaded_base_state_sha256
            or raw.get("value_state_sha256") != self.loaded_value_state_sha256
            or not self.loaded_base_state_sha256
            or not self.loaded_value_state_sha256
            or raw.get("base_sequence") != self.sequence
            or raw.get("base_observations") != self.observations
            or raw.get("parameter_schema_hash") != self.parameter_schema_hash
            or raw.get("registry_provenance_hash") != self.registry_provenance_hash
            or raw.get("program_identity_hash") != self.program_identity_hash
            or raw.get("policy") != self._selection_configuration()
        ):
            self._reject_selection_state()
            return
        history = raw.get("history")
        counts = raw.get("counts")
        if (
            not isinstance(history, list)
            or len(history) > self.selection_window
            or not isinstance(counts, dict)
            or set(counts) != set(self.parameter_policy_counts)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 1 << 60
                for value in counts.values()
            )
        ):
            self._reject_selection_state()
            return
        loaded: list[CoverageObservation] = []
        allowed = set(self.parameters)
        previous_sequence = -1
        for record in history:
            item = CoverageObservation.from_mapping(
                record,
                allowed_parameters=allowed,
                maximum_features=self.max_coverage_features,
            )
            if (
                item is None
                or item.sequence > self.observations
                or item.sequence <= previous_sequence
            ):
                self._reject_selection_state()
                return
            loaded.append(item)
            previous_sequence = item.sequence
        self.coverage_history = loaded
        for name in self.parameter_policy_counts:
            self.parameter_policy_counts[name] = counts[name]


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the ParaSuit parameter-selection provider."
    )
    parser.add_argument("--print-parameters", action="store_true", required=True)
    args = parser.parse_args()
    if args.print_parameters:
        print(
            json.dumps(
                native_parameter_selection_provider_payload(),
                sort_keys=True,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
