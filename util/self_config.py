#!/usr/bin/env python3
"""Online self-configuration for SymCC concolic workers.

The policy uses the existing strategy profiles as priors, discovers their
runtime parameters, and learns parameter/value utility independently from the
coarser strategy portfolio.  Assignments carry an opaque token so rewards can
be attributed after asynchronous MPI execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import argparse
import hashlib
import json
import math
import os
import random
import re
import selectors
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable


_VALID_PARAMETER = re.compile(r"^SYMCC_[A-Z0-9_]{1,80}$")
_VALID_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_UNSET = "__unset__"
_PROVIDER_SCHEMA = "symcc-parameter-provider-v1"
_MAX_PROVIDER_BYTES = 1024 * 1024
_MAX_PROVIDERS = 8
_PARAMETER_SCOPES = {"task", "query-service", "coordinator-campaign"}


@dataclass
class ValuePosterior:
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0
    reward_sum: float = 0.0
    cost_sum: float = 0.0

    @property
    def mean(self) -> float:
        return self.alpha / max(1e-9, self.alpha + self.beta)

    @property
    def mean_cost(self) -> float:
        return self.cost_sum / self.pulls if self.pulls else 1.0

    def sample(self, rng: random.Random) -> float:
        utility = rng.betavariate(max(0.01, self.alpha), max(0.01, self.beta))
        return utility / math.sqrt(max(0.05, self.mean_cost))

    def update(self, reward: float, elapsed: float, killed: bool) -> None:
        bounded = 0.0 if killed else max(0.0, min(1.0, reward))
        self.alpha += bounded
        self.beta += 1.0 - bounded
        self.pulls += 1
        self.reward_sum += bounded
        self.cost_sum += max(0.001, float(elapsed))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "pulls": self.pulls,
            "reward_sum": self.reward_sum,
            "cost_sum": self.cost_sum,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "ValuePosterior":
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


@dataclass
class ParameterSpec:
    name: str
    values: list[str]
    scope: str = "task"
    numeric: bool = False
    minimum: float | None = None
    maximum: float | None = None
    active_when: dict[str, tuple[str, ...]] = field(default_factory=dict)
    posteriors: dict[str, ValuePosterior] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: list[str] = []
        for value in self.values:
            item = str(value)
            if item not in normalized:
                normalized.append(item)
        if _UNSET not in normalized:
            normalized.insert(0, _UNSET)
        self.values = normalized
        for value in self.values:
            self.posteriors.setdefault(value, ValuePosterior())

    @property
    def pulls(self) -> int:
        return sum(item.pulls for item in self.posteriors.values())

    @property
    def impact(self) -> float:
        means = [item.mean for item in self.posteriors.values() if item.pulls > 0]
        if len(means) < 2:
            return 0.0
        return max(means) - min(means)

    def add_value(self, value: str) -> bool:
        value = str(value)
        if value in self.posteriors:
            return False
        self.values.append(value)
        self.posteriors[value] = ValuePosterior()
        return True

    def is_active(self, profile: dict[str, str]) -> bool:
        return all(
            profile.get(parent, _UNSET) in allowed
            for parent, allowed in self.active_when.items()
        )

    def expand_around(self, value: str) -> int:
        if not self.numeric or value == _UNSET:
            return 0
        posterior = self.posteriors.get(value)
        if posterior is None or posterior.pulls < 4 or posterior.mean < 0.55:
            return 0
        try:
            center = float(value)
        except ValueError:
            return 0
        created = 0
        for candidate in (center * 0.5, center * 2.0):
            if self.minimum is not None:
                candidate = max(self.minimum, candidate)
            if self.maximum is not None:
                candidate = min(self.maximum, candidate)
            rendered = (
                str(int(candidate)) if candidate.is_integer() else f"{candidate:.6g}"
            )
            created += int(self.add_value(rendered))
        return created


@dataclass(frozen=True)
class ParameterProvider:
    name: str
    source: str
    digest: str
    parameter_count: int


@dataclass
class ParameterRegistry:
    specs: dict[str, ParameterSpec]
    providers: list[ParameterProvider] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def provenance(self) -> dict[str, Any]:
        return {
            "schema": "symcc-self-config-registry-provenance-v1",
            "providers": [
                {
                    "name": provider.name,
                    "source": provider.source,
                    "digest": provider.digest,
                    "parameter_count": provider.parameter_count,
                }
                for provider in self.providers
            ],
            "errors": self.errors,
            "conflicts": self.conflicts,
        }


@dataclass(frozen=True)
class ParameterAssignment:
    token: str
    overrides: dict[str, str]
    context_key: str = ""


_DEFAULT_DOMAINS: dict[str, tuple[Iterable[str], bool, float | None, float | None]] = {
    "SYMCC_EXECUTOR_CLASS": (("exact", "tailored", "sampling"), False, None, None),
    "SYMCC_FAST_SOLVE": (("0", "1"), False, None, None),
    "SYMCC_MULTI_SOLVE": (("0", "1", "2"), False, None, None),
    "SYMCC_OPTIMISTIC_FIRST": (("0", "1"), False, None, None),
    "SYMCC_BACKSOLVER": (("0", "1"), False, None, None),
    "SYMCC_TACE": (("0", "1"), False, None, None),
    "SYMCC_TACE_MIN_BYTES": (("1024", "4096", "16384"), True, 256, 1048576),
    "SYMCC_UNSAT_CORE_CACHE": (("0", "1"), False, None, None),
    "SYMCC_QUERY_TRAVERSAL": (
        ("structural", "dfs", "bfs", "priority"),
        False,
        None,
        None,
    ),
    "SYMCC_QUERY_SHAPE_SELECTION": (("0", "1"), False, None, None),
    "SYMCC_SELECTIVE_QUERY": (("0", "1"), False, None, None),
    "SYMCC_SELECTIVE_QUERY_MIN_FIXED": (("1", "2", "4", "8"), True, 1, 4096),
    "SYMCC_SELECTIVE_QUERY_MAX_FIXED": (("16", "32", "64", "128"), True, 1, 4096),
    "SYMCC_SELECTIVE_QUERY_MAX_SYMBOLIC": (("4", "8", "16", "32"), True, 1, 4096),
    "SYMCC_SELECTIVE_QUERY_TIMEOUT": (("10", "25", "50", "100"), True, 1, 1000),
    "SYMCC_SELECTIVE_QUERY_COMPLETIONS": (("1", "2", "3", "4"), True, 0, 8),
    "SYMCC_SELECTIVE_QUERY_GRAPH_PARTITION": (("0", "1"), False, None, None),
    "SYMCC_SELECTIVE_QUERY_GRAPH_MIN_COSTLY": (("1", "2", "4", "8"), True, 1, 64),
    "SYMCC_SELECTIVE_QUERY_LEARNING": (("0", "1"), False, None, None),
    "SYMCC_SELECTIVE_QUERY_POLICY_EXPLORE": (("2", "4", "8", "16"), True, 0, 1024),
    "SYMCC_SELECTIVE_QUERY_POLICY_MIN_SAVINGS_US": (
        ("0", "100", "1000", "10000"),
        True,
        0,
        3600000000,
    ),
    "SYMCC_SELECTIVE_QUERY_POLICY_MIN_TIMEOUT": (("2", "5", "10", "25"), True, 1, 1000),
    "SYMCC_PREFIX_CONTEXT_CACHE": (("0", "1"), False, None, None),
    "SYMCC_POLY_CACHE": (("0", "1"), False, None, None),
    "SYMCC_POLY_CROSS_PREFIX": (("0", "1"), False, None, None),
    "SYMCC_POLY_PROJECTED_REUSE": (("0", "1"), False, None, None),
    "SYMCC_POLY_EXACT_PROJECTION": (("0", "1"), False, None, None),
    "SYMCC_POLY_EXACT_PROJECTION_VARS": (("1", "2", "4", "6"), True, 1, 6),
    "SYMCC_POLY_EXACT_PROJECTION_ROWS": (("16", "32", "64", "128"), True, 1, 128),
    "SYMCC_POLY_EXACT_PROJECTION_TIMEOUT": (("5", "10", "25", "50"), True, 1, 1000),
    "SYMCC_POLY_EXACT_PROJECTION_PROBES": (("2", "4", "8", "16"), True, 1, 64),
    "SYMCC_POLY_FIELD_RENAMING": (("0", "1"), False, None, None),
    "SYMCC_POLY_RENAME_VARS": (("4", "6", "8"), True, 1, 8),
    "SYMCC_POLY_RENAME_ATTEMPTS": (("32", "64", "128", "256"), True, 1, 4096),
    "SYMCC_POLY_RENAME_EXACT_PROBES": (("2", "4", "8", "16"), True, 1, 32),
    "SYMCC_POLY_CROSS_PREFIX_PROBES": (("8", "16", "32", "64"), True, 1, 256),
    "SYMCC_POLY_RANGE_BYTES": (("4", "8", "12", "16"), True, 2, 16),
    "SYMCC_POLY_LINEAR_BYTES": (("4", "6", "8", "12"), True, 2, 16),
    "SYMCC_POLY_TEMPLATE_BYTES": (("4", "8", "12", "16"), True, 2, 16),
    "SYMCC_POLY_TEMPLATE_PAIRS": (("8", "16", "24", "48"), True, 4, 128),
    "SYMCC_POLY_SAMPLES": (("2", "4", "8", "16"), True, 1, 32),
    "SYMCC_POLY_DENSE_DIM": (("8", "16", "32", "64"), True, 4, 64),
    "SYMCC_POLY_JOHN_STEPS": (("2", "4", "8", "16"), True, 1, 32),
    "SYMCC_POLY_WALK": (("john", "dikin", "coordinate"), False, None, None),
}

_SAMPLING_PARAMETERS = {
    name for name in _DEFAULT_DOMAINS if name.startswith("SYMCC_POLY_")
}

_DEFAULT_ACTIVE_WHEN: dict[str, dict[str, tuple[str, ...]]] = {
    "SYMCC_FAST_SOLVE": {
        "SYMCC_EXECUTOR_CLASS": ("tailored", "sampling"),
    },
    "SYMCC_MULTI_SOLVE": {
        "SYMCC_EXECUTOR_CLASS": ("tailored", "sampling"),
    },
    "SYMCC_OPTIMISTIC_FIRST": {
        "SYMCC_EXECUTOR_CLASS": ("tailored", "sampling"),
    },
    "SYMCC_TACE_MIN_BYTES": {"SYMCC_TACE": ("1",)},
    "SYMCC_SELECTIVE_QUERY_MIN_FIXED": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_MAX_FIXED": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_MAX_SYMBOLIC": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_TIMEOUT": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_COMPLETIONS": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_GRAPH_PARTITION": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_GRAPH_MIN_COSTLY": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
        "SYMCC_SELECTIVE_QUERY_GRAPH_PARTITION": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_LEARNING": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_POLICY_EXPLORE": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
        "SYMCC_SELECTIVE_QUERY_LEARNING": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_POLICY_MIN_SAVINGS_US": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
        "SYMCC_SELECTIVE_QUERY_LEARNING": ("1",),
    },
    "SYMCC_SELECTIVE_QUERY_POLICY_MIN_TIMEOUT": {
        "SYMCC_SELECTIVE_QUERY": ("1",),
        "SYMCC_SELECTIVE_QUERY_LEARNING": ("1",),
    },
    "SYMCC_POLY_CACHE": {"SYMCC_EXECUTOR_CLASS": ("sampling",)},
    "SYMCC_POLY_CROSS_PREFIX": {
        "SYMCC_EXECUTOR_CLASS": ("sampling",),
        "SYMCC_POLY_CACHE": ("1",),
    },
    "SYMCC_POLY_PROJECTED_REUSE": {
        "SYMCC_POLY_CROSS_PREFIX": ("1",),
    },
    "SYMCC_POLY_EXACT_PROJECTION": {
        "SYMCC_POLY_PROJECTED_REUSE": ("1",),
    },
    "SYMCC_POLY_EXACT_PROJECTION_VARS": {
        "SYMCC_POLY_EXACT_PROJECTION": ("1",),
    },
    "SYMCC_POLY_EXACT_PROJECTION_ROWS": {
        "SYMCC_POLY_EXACT_PROJECTION": ("1",),
    },
    "SYMCC_POLY_EXACT_PROJECTION_TIMEOUT": {
        "SYMCC_POLY_EXACT_PROJECTION": ("1",),
    },
    "SYMCC_POLY_EXACT_PROJECTION_PROBES": {
        "SYMCC_POLY_EXACT_PROJECTION": ("1",),
    },
    "SYMCC_POLY_FIELD_RENAMING": {
        "SYMCC_POLY_CROSS_PREFIX": ("1",),
    },
    "SYMCC_POLY_RENAME_VARS": {
        "SYMCC_POLY_FIELD_RENAMING": ("1",),
    },
    "SYMCC_POLY_RENAME_ATTEMPTS": {
        "SYMCC_POLY_FIELD_RENAMING": ("1",),
    },
    "SYMCC_POLY_RENAME_EXACT_PROBES": {
        "SYMCC_POLY_FIELD_RENAMING": ("1",),
        "SYMCC_POLY_EXACT_PROJECTION": ("1",),
    },
    "SYMCC_POLY_CROSS_PREFIX_PROBES": {
        "SYMCC_POLY_CROSS_PREFIX": ("1",),
    },
    "SYMCC_POLY_RANGE_BYTES": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_LINEAR_BYTES": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_TEMPLATE_BYTES": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_TEMPLATE_PAIRS": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_SAMPLES": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_DENSE_DIM": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_JOHN_STEPS": {"SYMCC_POLY_CACHE": ("1",)},
    "SYMCC_POLY_WALK": {"SYMCC_POLY_CACHE": ("1",)},
}


def _parameter_contract(spec: ParameterSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "values": [value for value in spec.values if value != _UNSET],
    }
    if spec.numeric:
        payload["numeric"] = True
    if spec.scope != "task":
        payload["scope"] = spec.scope
    if spec.minimum is not None:
        payload["min"] = spec.minimum
    if spec.maximum is not None:
        payload["max"] = spec.maximum
    if spec.active_when:
        payload["active_when"] = {
            parent: list(values) for parent, values in sorted(spec.active_when.items())
        }
    return payload


def native_parameter_provider_payload() -> dict[str, Any]:
    """Return the coordinator's executable parameter-provider contract."""
    parameters: dict[str, Any] = {}
    for name, (values, numeric, minimum, maximum) in _DEFAULT_DOMAINS.items():
        spec = ParameterSpec(
            name,
            list(values),
            scope=(
                "coordinator-campaign"
                if name in {"SYMCC_TACE", "SYMCC_TACE_MIN_BYTES"}
                else "query-service"
                if name.startswith("SYMCC_SELECTIVE_QUERY")
                or name in {"SYMCC_QUERY_TRAVERSAL", "SYMCC_QUERY_SHAPE_SELECTION"}
                else "task"
            ),
            numeric=numeric,
            minimum=minimum,
            maximum=maximum,
            active_when=_DEFAULT_ACTIVE_WHEN.get(name, {}),
        )
        parameters[name] = _parameter_contract(spec)
    return {
        "schema": _PROVIDER_SCHEMA,
        "provider": "symcc-coordinator",
        "parameters": parameters,
    }


def _load_custom_space(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data: Any
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as stream:
                data = json.load(stream)
        else:
            data = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("parameters"), dict):
        data = data["parameters"]
    return data if isinstance(data, dict) else {}


def _normalize_active_when(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for name, values in list(raw.items())[:32]:
        if not isinstance(name, str) or not _VALID_PARAMETER.fullmatch(name):
            continue
        if not isinstance(values, (list, tuple)) or not values:
            continue
        normalized = tuple(dict.fromkeys(str(value) for value in values))
        if normalized:
            result[name] = normalized[:32]
    return result


def _parameter_description(
    name: str,
    description: Any,
) -> ParameterSpec | None:
    numeric = False
    minimum = None
    maximum = None
    active_when: dict[str, tuple[str, ...]] = {}
    scope = "task"
    values: Any = description
    if isinstance(description, dict):
        values = description.get("values", ())
        numeric = bool(description.get("numeric", False))
        requested_scope = description.get("scope", "task")
        if isinstance(requested_scope, str) and requested_scope in _PARAMETER_SCOPES:
            scope = requested_scope
        active_when = _normalize_active_when(description.get("active_when"))
        try:
            minimum = float(description["min"]) if "min" in description else None
            maximum = float(description["max"]) if "max" in description else None
        except (TypeError, ValueError, OverflowError):
            minimum = maximum = None
    if not isinstance(values, (list, tuple)) or not values:
        return None
    return ParameterSpec(
        name,
        [str(value) for value in values[:128]],
        scope=scope,
        numeric=numeric,
        minimum=minimum,
        maximum=maximum,
        active_when=active_when,
    )


def _strict_provider_description(
    name: str,
    description: Any,
    default_scope: str,
) -> ParameterSpec:
    if not isinstance(description, dict):
        raise ValueError(f"{name}: declaration is not an object")
    values = description.get("values")
    if not isinstance(values, list) or not 1 <= len(values) <= 128:
        raise ValueError(f"{name}: values must contain 1..128 entries")
    rendered: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name}: value is not a scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name}: value is not finite")
        item = str(value)
        if not item or len(item) > 256 or "\x00" in item or item == _UNSET:
            raise ValueError(f"{name}: value is outside the protocol bounds")
        rendered.append(item)

    numeric = description.get("numeric", False)
    if not isinstance(numeric, bool):
        raise ValueError(f"{name}: numeric is not boolean")
    scope = description.get("scope", default_scope)
    if not isinstance(scope, str) or scope not in _PARAMETER_SCOPES:
        raise ValueError(f"{name}: scope is invalid")
    minimum: float | None = None
    maximum: float | None = None
    for key in ("min", "max"):
        if key not in description:
            continue
        value = description[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name}: {key} is not numeric")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError(f"{name}: {key} is not finite")
        if key == "min":
            minimum = converted
        else:
            maximum = converted
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{name}: min exceeds max")
    if numeric:
        for value in rendered:
            try:
                converted = float(value)
            except ValueError as error:
                raise ValueError(f"{name}: numeric value is invalid") from error
            if not math.isfinite(converted):
                raise ValueError(f"{name}: numeric value is not finite")
            if minimum is not None and converted < minimum:
                raise ValueError(f"{name}: numeric value is below min")
            if maximum is not None and converted > maximum:
                raise ValueError(f"{name}: numeric value is above max")

    raw_conditions = description.get("active_when", {})
    if not isinstance(raw_conditions, dict) or len(raw_conditions) > 32:
        raise ValueError(f"{name}: active_when is invalid")
    active_when: dict[str, tuple[str, ...]] = {}
    for parent, allowed in raw_conditions.items():
        if not isinstance(parent, str) or not _VALID_PARAMETER.fullmatch(parent):
            raise ValueError(f"{name}: active_when parent is invalid")
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= 32:
            raise ValueError(f"{name}: active_when values are invalid")
        normalized: list[str] = []
        for value in allowed:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"{name}: active_when value is invalid")
            item = str(value)
            if not item or len(item) > 256 or "\x00" in item:
                raise ValueError(f"{name}: active_when value is invalid")
            if item not in normalized:
                normalized.append(item)
        active_when[parent] = tuple(normalized)
    return ParameterSpec(
        name,
        rendered,
        scope=scope,
        numeric=numeric,
        minimum=minimum,
        maximum=maximum,
        active_when=active_when,
    )


def _normalize_provider_payload(
    raw: Any,
    source: str,
) -> tuple[ParameterProvider, dict[str, ParameterSpec]]:
    if not isinstance(raw, dict) or raw.get("schema") != _PROVIDER_SCHEMA:
        raise ValueError("unsupported provider schema")
    provider_name = raw.get("provider")
    if not isinstance(provider_name, str) or not _VALID_PROVIDER.fullmatch(
        provider_name
    ):
        raise ValueError("invalid provider name")
    default_scope = raw.get("scope", "task")
    if not isinstance(default_scope, str) or default_scope not in _PARAMETER_SCOPES:
        raise ValueError("invalid provider scope")
    parameters = raw.get("parameters")
    if not isinstance(parameters, dict) or not 1 <= len(parameters) <= 256:
        raise ValueError("provider parameters must contain 1..256 entries")
    specs: dict[str, ParameterSpec] = {}
    for name, description in parameters.items():
        if not isinstance(name, str) or not _VALID_PARAMETER.fullmatch(name):
            raise ValueError("invalid parameter name")
        specs[name] = _strict_provider_description(name, description, default_scope)
    normalized = {
        name: _parameter_contract(spec) for name, spec in sorted(specs.items())
    }
    digest = hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ParameterProvider(
        provider_name,
        source,
        digest,
        len(specs),
    ), specs


def _configured_provider_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    raw_commands = os.environ.get("SYMCC_SELF_CONFIG_PROVIDER_COMMANDS")
    if raw_commands:
        try:
            decoded = json.loads(raw_commands)
        except (TypeError, ValueError):
            decoded = []
        if isinstance(decoded, list):
            for item in decoded[:_MAX_PROVIDERS]:
                if isinstance(item, str):
                    command = [item]
                elif isinstance(item, list) and all(
                    isinstance(part, str) for part in item
                ):
                    command = list(item)
                else:
                    continue
                if command and all(
                    part and len(part) <= 4096 and "\x00" not in part
                    for part in command[:16]
                ):
                    commands.append(command[:16])

    configured_solver = os.environ.get("SYMCC_QUERY_SOLVER")
    if configured_solver:
        try:
            solver_command = shlex.split(configured_solver)
        except ValueError:
            solver_command = []
        if solver_command:
            commands.append(solver_command[:16])
    else:
        solver = shutil.which("symcc-query-solver")
        if not solver:
            repository = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for build in ("build", "build-llvm17"):
                candidate = os.path.join(
                    repository,
                    build,
                    "SymCCRuntime-prefix",
                    "src",
                    "SymCCRuntime-build",
                    "src",
                    "backends",
                    "qsym",
                    "symcc-query-solver",
                )
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    solver = candidate
                    break
        if solver:
            commands.append([solver])

    deduplicated: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        if "--print-parameters" not in command:
            command = [*command, "--print-parameters"]
        key = tuple(command)
        if key not in seen:
            seen.add(key)
            deduplicated.append(command)
    return deduplicated[:_MAX_PROVIDERS]


def _read_provider_command(
    command: list[str],
    timeout_seconds: float,
) -> tuple[ParameterProvider, dict[str, ParameterSpec]]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            events = selector.select(remaining)
            if not events:
                if process.poll() is None:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > _MAX_PROVIDER_BYTES:
                    raise ValueError("provider output exceeds 1 MiB")
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    if returncode != 0:
        raise ValueError(f"provider exited with status {returncode}")
    try:
        decoded = output.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("provider output is not valid UTF-8 JSON") from error
    source = "command:" + os.path.basename(command[0])
    return _normalize_provider_payload(raw, source)


def _drop_invalid_condition_nodes(
    specs: dict[str, ParameterSpec],
) -> dict[str, ParameterSpec]:
    invalid = {
        name
        for name, spec in specs.items()
        if any(parent not in specs for parent in spec.active_when)
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in visited or name in invalid:
            return
        if name in visiting:
            try:
                start = path.index(name)
            except ValueError:
                start = 0
            invalid.update(path[start:])
            return
        visiting.add(name)
        path.append(name)
        for parent in specs[name].active_when:
            visit(parent, path)
        path.pop()
        visiting.discard(name)
        visited.add(name)

    for name in specs:
        visit(name, [])
    return {name: spec for name, spec in specs.items() if name not in invalid}


def discover_parameter_specs(
    profiles: Iterable[dict[str, str]],
    custom_space: str | None = None,
    schema_space: str | None = None,
    provider_commands: Iterable[Iterable[str]] | None = None,
    provider_payloads: Iterable[Any] = (),
) -> dict[str, ParameterSpec]:
    """Discover tunables from native providers, profiles, and overrides."""
    return discover_parameter_registry(
        profiles,
        custom_space,
        schema_space,
        provider_commands,
        provider_payloads,
    ).specs


def discover_parameter_registry(
    profiles: Iterable[dict[str, str]],
    custom_space: str | None = None,
    schema_space: str | None = None,
    provider_commands: Iterable[Iterable[str]] | None = None,
    provider_payloads: Iterable[Any] = (),
) -> ParameterRegistry:
    """Build one fail-closed registry from executable provider contracts."""
    profile_list = list(profiles)
    observed: dict[str, list[str]] = {}
    for profile in profile_list:
        for name, value in profile.items():
            if _VALID_PARAMETER.fullmatch(str(name)):
                observed.setdefault(str(name), []).append(str(value))

    candidates: list[tuple[ParameterProvider, dict[str, ParameterSpec]]] = []
    errors: list[str] = []
    conflicts: list[str] = []
    try:
        candidates.append(
            _normalize_provider_payload(
                native_parameter_provider_payload(), "builtin:self_config.py"
            )
        )
    except ValueError as error:
        errors.append(f"symcc-coordinator:{error}")

    for index, raw in enumerate(list(provider_payloads)[:_MAX_PROVIDERS]):
        try:
            candidates.append(_normalize_provider_payload(raw, f"payload:{index}"))
        except ValueError as error:
            errors.append(f"payload:{index}:{error}")

    commands = (
        _configured_provider_commands()
        if provider_commands is None
        else [list(command) for command in provider_commands][:_MAX_PROVIDERS]
    )
    try:
        timeout_seconds = float(
            os.environ.get("SYMCC_SELF_CONFIG_PROVIDER_TIMEOUT", "2")
        )
    except (TypeError, ValueError, OverflowError):
        timeout_seconds = 2.0
    timeout_seconds = max(0.05, min(30.0, timeout_seconds))
    for command in commands:
        label = os.path.basename(command[0]) if command else "empty"
        if (
            not command
            or len(command) > 16
            or any(
                not isinstance(part, str) or not part or "\x00" in part
                for part in command
            )
        ):
            errors.append(f"command:{label}:invalid command")
            continue
        normalized_command = list(command)
        if "--print-parameters" not in normalized_command:
            normalized_command.append("--print-parameters")
        try:
            candidates.append(
                _read_provider_command(normalized_command, timeout_seconds)
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            errors.append(f"command:{label}:{error}")

    specs: dict[str, ParameterSpec] = {}
    providers: list[ParameterProvider] = []
    provider_digests: dict[str, str] = {}
    for provider, declarations in candidates:
        previous_digest = provider_digests.get(provider.name)
        if previous_digest is not None:
            if previous_digest != provider.digest:
                conflicts.append(f"{provider.name}:provider identity collision")
            continue
        conflicting = [
            name
            for name, spec in declarations.items()
            if name in specs
            and _parameter_contract(specs[name]) != _parameter_contract(spec)
        ]
        if conflicting:
            conflicts.append(
                f"{provider.name}:contract conflict:"
                + ",".join(sorted(conflicting)[:16])
            )
            continue
        provider_digests[provider.name] = provider.digest
        providers.append(provider)
        for name, spec in declarations.items():
            specs.setdefault(name, spec)

    for name, values in observed.items():
        if name in specs:
            for value in values:
                specs[name].add_value(value)

    schema = _load_custom_space(schema_space)
    for name, description in schema.items():
        if not isinstance(name, str) or not _VALID_PARAMETER.fullmatch(name):
            continue
        discovered = _parameter_description(name, description)
        if discovered is None:
            continue
        for value in observed.get(name, ()):
            discovered.add_value(value)
        specs[name] = discovered

    for name, values in observed.items():
        if name not in specs:
            specs[name] = ParameterSpec(name, values)

    for name, description in _load_custom_space(custom_space).items():
        if not isinstance(name, str) or not _VALID_PARAMETER.fullmatch(name):
            continue
        discovered = _parameter_description(name, description)
        if discovered is None:
            continue
        specs[name] = discovered
    return ParameterRegistry(
        _drop_invalid_condition_nodes(specs),
        providers,
        errors[: _MAX_PROVIDERS * 2],
        conflicts[: _MAX_PROVIDERS * 2],
    )


def sanitize_parameter_overrides(raw: Any) -> dict[str, str]:
    """Normalize an MPI/config payload without accepting arbitrary env keys."""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for name, value in list(raw.items())[:128]:
        if not isinstance(name, str) or not _VALID_PARAMETER.fullmatch(name):
            continue
        rendered = str(value)
        if len(rendered) > 256 or "\x00" in rendered:
            continue
        result[name] = rendered
    return result


class SelfConfiguringPolicy:
    """ParaSuit-style online parameter discovery and posterior sampling."""

    SCHEMA = 3
    MAX_CONTEXTS = 128
    MAX_INTERACTIONS = 4096

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
    ) -> None:
        self.state_path = state_path or ""
        registry = discover_parameter_registry(
            profiles,
            custom_space,
            schema_space,
            provider_commands,
            provider_payloads,
        )
        self.registry_parameter_count = len(registry.specs)
        self.parameters = {
            name: spec for name, spec in registry.specs.items() if spec.scope == "task"
        }
        self.declared_parameter_values = {
            name: tuple(spec.values) for name, spec in self.parameters.items()
        }
        self.registry_provenance = registry.provenance()
        self.registry_provenance_hash = hashlib.sha256(
            json.dumps(
                self.registry_provenance,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        schema_payload = {
            name: {
                "values": spec.values,
                "scope": spec.scope,
                "numeric": spec.numeric,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "active_when": spec.active_when,
            }
            for name, spec in sorted(self.parameters.items())
        }
        self.parameter_schema_hash = hashlib.sha256(
            json.dumps(
                schema_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.max_parameters = max(1, min(16, int(max_parameters)))
        self.rng = random.Random(seed)
        self.sequence = 0
        self.observations = 0
        self.pending: dict[str, dict[str, Any]] = {}
        self.configurations: dict[str, ValuePosterior] = {}
        self.context_posteriors: dict[str, dict[str, dict[str, ValuePosterior]]] = {}
        self.interactions: dict[str, ValuePosterior] = {}
        self.prior_parameters: dict[str, dict[str, ValuePosterior]] = {}
        self.expanded_values = 0
        self._load()
        self._load_prior(prior_path)

    @property
    def parameter_names(self) -> set[str]:
        return set(self.parameters)

    @staticmethod
    def _input_size_bucket(value: Any) -> str:
        try:
            size = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return "unknown"
        if size <= 64:
            return "tiny"
        if size <= 1024:
            return "small"
        if size <= 16384:
            return "medium"
        return "large"

    def _context_key(self, context: dict[str, Any] | None) -> str:
        raw = context if isinstance(context, dict) else {}
        phase = str(raw.get("phase", "")).lower()
        if phase not in {"cold", "warm", "mature"}:
            phase = (
                "cold"
                if self.observations < 32
                else "warm"
                if self.observations < 256
                else "mature"
            )
        try:
            targeted = int(raw.get("target_branch", 0) or 0) != 0
        except (TypeError, ValueError, OverflowError):
            targeted = False
        try:
            structured = int(raw.get("task_region", 0) or 0) != 0
        except (TypeError, ValueError, OverflowError):
            structured = False
        size = self._input_size_bucket(raw.get("input_bytes"))
        return (
            f"phase={phase}|targeted={int(targeted)}|"
            f"structured={int(structured)}|input={size}"
        )

    def _cold_candidate(
        self,
        profile: dict[str, str],
    ) -> tuple[ParameterSpec, str] | None:
        candidates: list[tuple[int, str, str]] = []
        for name, spec in self.parameters.items():
            if not spec.is_active(profile):
                continue
            for value, posterior in spec.posteriors.items():
                candidates.append((posterior.pulls, name, value))
        if not candidates:
            return None
        pulls, name, value = min(candidates)
        if pulls > 0:
            return None
        return self.parameters[name], value

    def _context_posterior(
        self,
        context_key: str,
        name: str,
        value: str,
        *,
        create: bool = False,
    ) -> ValuePosterior | None:
        context = self.context_posteriors.get(context_key)
        if context is None:
            if not create:
                return None
            if len(self.context_posteriors) >= self.MAX_CONTEXTS:
                return None
            context = {}
            self.context_posteriors[context_key] = context
        parameter = context.get(name)
        if parameter is None:
            if not create:
                return None
            parameter = {}
            context[name] = parameter
        posterior = parameter.get(value)
        if posterior is None and create:
            posterior = ValuePosterior()
            parameter[value] = posterior
        return posterior

    @staticmethod
    def _interaction_key(
        context_key: str,
        left: tuple[str, str],
        right: tuple[str, str],
    ) -> str:
        first, second = sorted((left, right))
        return json.dumps(
            [context_key, first[0], first[1], second[0], second[1]],
            separators=(",", ":"),
        )

    def _select_parameters(
        self,
        profile: dict[str, str],
        context_key: str,
    ) -> list[ParameterSpec]:
        total = max(1, self.observations)
        ranked: list[tuple[float, str, ParameterSpec]] = []
        for name, spec in self.parameters.items():
            if not spec.is_active(profile):
                continue
            exploration = math.sqrt(math.log1p(total) / max(1, spec.pulls))
            posterior_uncertainty = 1.0 / math.sqrt(1.0 + spec.pulls)
            context_pulls = sum(
                posterior.pulls
                for posterior in (
                    self.context_posteriors.get(context_key, {}).get(name, {}).values()
                )
            )
            context_uncertainty = 1.0 / math.sqrt(1.0 + context_pulls)
            score = (
                spec.impact
                + 0.25 * exploration
                + 0.15 * posterior_uncertainty
                + 0.20 * context_uncertainty
            )
            ranked.append((score, name, spec))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [spec for _, _, spec in ranked[: self.max_parameters]]

    def _select_value(
        self,
        spec: ParameterSpec,
        context_key: str,
        chosen: dict[str, str],
    ) -> str:
        untried = [value for value in spec.values if spec.posteriors[value].pulls == 0]
        if untried:
            return untried[0]

        def score(value: str) -> float:
            total = 0.55 * spec.posteriors[value].sample(self.rng)
            contextual = self._context_posterior(context_key, spec.name, value)
            total += 0.25 * (
                contextual.sample(self.rng) if contextual is not None else 0.5
            )
            prior = self.prior_parameters.get(spec.name, {}).get(value)
            local_pulls = spec.posteriors[value].pulls
            if prior is not None:
                prior_weight = 0.12 / math.sqrt(1.0 + local_pulls)
                total += prior_weight * prior.sample(self.rng)
            interaction_scores: list[float] = []
            for other_name, other_value in chosen.items():
                key = self._interaction_key(
                    context_key,
                    (spec.name, value),
                    (other_name, other_value),
                )
                posterior = self.interactions.get(key)
                if posterior is not None:
                    interaction_scores.append(posterior.sample(self.rng))
            if interaction_scores:
                total += 0.20 * (sum(interaction_scores) / len(interaction_scores))
            return total

        return max(spec.values, key=score)

    def _apply_guards(self, profile: dict[str, str]) -> dict[str, str]:
        result = dict(profile)
        executor = result.get("SYMCC_EXECUTOR_CLASS", "exact")
        if executor != "sampling":
            for name in _SAMPLING_PARAMETERS:
                result.pop(name, None)
        else:
            result["SYMCC_POLY_CACHE"] = "1"
            result.setdefault("SYMCC_POLY_CROSS_PREFIX", "1")
            result.setdefault("SYMCC_POLY_PROJECTED_REUSE", "1")
            result.setdefault("SYMCC_POLY_EXACT_PROJECTION", "1")
            result.setdefault("SYMCC_POLY_FIELD_RENAMING", "1")
            result.setdefault("SYMCC_UNSAT_CORE_CACHE", "1")
        changed = True
        while changed:
            changed = False
            for name in tuple(result):
                spec = self.parameters.get(name)
                if spec is not None and not spec.is_active(result):
                    result.pop(name, None)
                    changed = True
        if executor == "exact":
            result["SYMCC_FAST_SOLVE"] = "0"
            result["SYMCC_MULTI_SOLVE"] = "0"
            result["SYMCC_OPTIMISTIC_FIRST"] = "0"
        return result

    def select(
        self,
        base_profile: dict[str, str] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> ParameterAssignment:
        context_key = self._context_key(context)
        profile = {
            str(name): str(value)
            for name, value in (base_profile or {}).items()
            if name in self.parameters
        }
        chosen: list[tuple[ParameterSpec, str]] = []
        cold = self._cold_candidate(profile)
        if cold is not None:
            chosen.append(cold)
        else:
            chosen_values: dict[str, str] = {}
            for spec in self._select_parameters(profile, context_key):
                value = self._select_value(spec, context_key, chosen_values)
                chosen.append((spec, value))
                chosen_values[spec.name] = value

        for spec, value in chosen:
            if value == _UNSET:
                profile.pop(spec.name, None)
            else:
                profile[spec.name] = value
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
            "choices": {spec.name: value for spec, value in chosen},
        }
        return ParameterAssignment(token, profile, context_key)

    def observe(
        self,
        token: str,
        *,
        reward: float,
        elapsed: float,
        killed: bool = False,
    ) -> bool:
        pending = self.pending.pop(str(token), None)
        if pending is None:
            return False
        if isinstance(pending.get("profile"), dict):
            profile = {
                str(name): str(value)
                for name, value in pending["profile"].items()
                if name in self.parameters
            }
            choices = (
                {
                    str(name): str(value)
                    for name, value in pending.get("choices", {}).items()
                    if name in self.parameters
                }
                if isinstance(pending.get("choices"), dict)
                else {}
            )
            context_key = str(pending.get("context", self._context_key(None)))
        else:
            profile = {
                str(name): str(value)
                for name, value in pending.items()
                if name in self.parameters
            }
            choices = dict(profile)
            context_key = self._context_key(None)
        self.observations += 1
        key = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        self.configurations.setdefault(key, ValuePosterior()).update(
            reward, elapsed, killed
        )

        for name, value in choices.items():
            spec = self.parameters[name]
            posterior = spec.posteriors.setdefault(value, ValuePosterior())
            if value not in spec.values:
                spec.values.append(value)
            posterior.update(reward, elapsed, killed)
            contextual = self._context_posterior(context_key, name, value, create=True)
            if contextual is not None:
                contextual.update(reward, elapsed, killed)
            self.expanded_values += spec.expand_around(value)
        selected = sorted(choices.items())[: self.max_parameters]
        for index, left in enumerate(selected):
            for right in selected[index + 1 :]:
                key = self._interaction_key(context_key, left, right)
                posterior = self.interactions.get(key)
                if posterior is None:
                    if len(self.interactions) >= self.MAX_INTERACTIONS:
                        continue
                    posterior = ValuePosterior()
                    self.interactions[key] = posterior
                posterior.update(reward, elapsed, killed)
        return True

    def abandon(self, token: str) -> bool:
        """Drop a parameter assignment that never reached an executor."""
        return self.pending.pop(str(token), None) is not None

    def snapshot(self) -> dict[str, Any]:
        ranked = sorted(
            self.parameters.values(),
            key=lambda spec: (spec.impact, spec.pulls),
            reverse=True,
        )
        return {
            "parameters": len(self.parameters),
            "registry_parameters": self.registry_parameter_count,
            "parameter_schema_hash": self.parameter_schema_hash,
            "registry_provenance_hash": self.registry_provenance_hash,
            "registry_providers": len(self.registry_provenance["providers"]),
            "registry_errors": len(self.registry_provenance["errors"]),
            "registry_conflicts": len(self.registry_provenance["conflicts"]),
            "observations": self.observations,
            "pending": len(self.pending),
            "configurations": len(self.configurations),
            "contexts": len(self.context_posteriors),
            "interactions": len(self.interactions),
            "prior_parameters": len(self.prior_parameters),
            "expanded_values": self.expanded_values,
            "top_impacts": [
                [spec.name, round(spec.impact, 6), spec.pulls] for spec in ranked[:8]
            ],
        }

    def save(self) -> None:
        if not self.state_path:
            return
        data = {
            "schema": self.SCHEMA,
            "parameter_schema_hash": self.parameter_schema_hash,
            "registry_provenance": self.registry_provenance,
            "registry_provenance_hash": self.registry_provenance_hash,
            "sequence": self.sequence,
            "observations": self.observations,
            "expanded_values": self.expanded_values,
            "pending": self.pending,
            "parameters": {
                name: {
                    "values": spec.values,
                    "scope": spec.scope,
                    "numeric": spec.numeric,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "posteriors": {
                        value: posterior.to_mapping()
                        for value, posterior in spec.posteriors.items()
                    },
                }
                for name, spec in self.parameters.items()
            },
            "configurations": {
                key: posterior.to_mapping()
                for key, posterior in self.configurations.items()
            },
            "context_posteriors": {
                context_key: {
                    name: {
                        value: posterior.to_mapping()
                        for value, posterior in values.items()
                    }
                    for name, values in parameters.items()
                }
                for context_key, parameters in self.context_posteriors.items()
            },
            "interactions": {
                key: posterior.to_mapping()
                for key, posterior in self.interactions.items()
            },
        }
        directory = os.path.dirname(os.path.abspath(self.state_path))
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".self-config-", dir=directory, text=True
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, sort_keys=True)
                    stream.write("\n")
                os.replace(temporary, self.state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            return

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return

        self._load_mapping(raw)

    def _load_mapping(self, raw: Any) -> None:
        """Import one already-parsed state snapshot.

        Subclasses that bind state to an independently verified artifact can
        pass the exact mapping they authenticated instead of reopening a path.
        """
        if not isinstance(raw, dict) or raw.get("schema") not in {1, 2, self.SCHEMA}:
            return
        if (
            raw.get("schema") == self.SCHEMA
            and raw.get("parameter_schema_hash") != self.parameter_schema_hash
        ):
            return
        try:
            self.sequence = max(0, int(raw.get("sequence", 0)))
            self.observations = max(0, int(raw.get("observations", 0)))
            self.expanded_values = max(0, int(raw.get("expanded_values", 0)))
        except (TypeError, ValueError, OverflowError):
            return

        parameters = raw.get("parameters", {})
        if isinstance(parameters, dict):
            for name, state in parameters.items():
                spec = self.parameters.get(name)
                if spec is None or not isinstance(state, dict):
                    continue
                values = state.get("values", ())
                if isinstance(values, list):
                    for value in values[:128]:
                        spec.add_value(str(value))
                posteriors = state.get("posteriors", {})
                if isinstance(posteriors, dict):
                    for value, posterior in posteriors.items():
                        rendered = str(value)
                        spec.add_value(rendered)
                        spec.posteriors[rendered] = ValuePosterior.from_mapping(
                            posterior
                        )

        pending = raw.get("pending", {})
        if isinstance(pending, dict):
            for token, record in list(pending.items())[-4096:]:
                if not isinstance(record, dict):
                    continue
                if isinstance(record.get("profile"), dict):
                    clean_profile = {
                        str(name): str(value)
                        for name, value in record["profile"].items()
                        if name in self.parameters
                    }
                    clean_choices = (
                        {
                            str(name): str(value)
                            for name, value in record.get("choices", {}).items()
                            if name in self.parameters
                        }
                        if isinstance(record.get("choices"), dict)
                        else {}
                    )
                    self.pending[str(token)] = {
                        "profile": clean_profile,
                        "context": str(record.get("context", ""))[:256],
                        "choices": clean_choices,
                    }
                    phase = record.get("selection_phase")
                    if phase in {"extraction", "iterative"}:
                        self.pending[str(token)]["selection_phase"] = phase
                else:
                    clean = {
                        str(name): str(value)
                        for name, value in record.items()
                        if name in self.parameters
                    }
                    self.pending[str(token)] = clean

        configurations = raw.get("configurations", {})
        if isinstance(configurations, dict):
            for key, posterior in list(configurations.items())[-4096:]:
                self.configurations[str(key)] = ValuePosterior.from_mapping(posterior)

        context_posteriors = raw.get("context_posteriors", {})
        if isinstance(context_posteriors, dict):
            for context_key, parameters in list(context_posteriors.items())[
                -self.MAX_CONTEXTS :
            ]:
                if not isinstance(parameters, dict):
                    continue
                normalized: dict[str, dict[str, ValuePosterior]] = {}
                for name, values in list(parameters.items())[:128]:
                    if name not in self.parameters or not isinstance(values, dict):
                        continue
                    normalized[name] = {
                        str(value): ValuePosterior.from_mapping(posterior)
                        for value, posterior in list(values.items())[:128]
                    }
                if normalized:
                    self.context_posteriors[str(context_key)[:256]] = normalized

        interactions = raw.get("interactions", {})
        if isinstance(interactions, dict):
            for key, posterior in list(interactions.items())[-self.MAX_INTERACTIONS :]:
                self.interactions[str(key)[:1024]] = ValuePosterior.from_mapping(
                    posterior
                )

    def _load_prior(self, prior_path: str | None) -> None:
        if not prior_path or os.path.abspath(prior_path) == os.path.abspath(
            self.state_path or "."
        ):
            return
        try:
            with open(prior_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") not in {1, 2, self.SCHEMA}:
            return
        parameters = raw.get("parameters", {})
        if not isinstance(parameters, dict):
            return
        for name, state in list(parameters.items())[:256]:
            if name not in self.parameters or not isinstance(state, dict):
                continue
            posteriors = state.get("posteriors", {})
            if not isinstance(posteriors, dict):
                continue
            imported = {
                str(value): ValuePosterior.from_mapping(posterior)
                for value, posterior in list(posteriors.items())[:128]
                if str(value) in self.parameters[name].posteriors
            }
            if imported:
                self.prior_parameters[name] = imported


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect SymCC's native self-configuration registry."
    )
    parser.add_argument(
        "--schema", help="Optional JSON schema path or inline JSON override."
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the normalized discovered schema as JSON.",
    )
    output.add_argument(
        "--print-parameters",
        action="store_true",
        help="Print this coordinator's native provider contract as JSON.",
    )
    parser.add_argument(
        "--provider-command",
        action="append",
        default=[],
        metavar="COMMAND",
        help="Executable provider command; may be repeated.",
    )
    parser.add_argument(
        "--no-provider-discovery",
        action="store_true",
        help="Do not auto-discover executable providers.",
    )
    args = parser.parse_args()
    if args.print_parameters:
        print(json.dumps(native_parameter_provider_payload(), sort_keys=True, indent=2))
        return 0
    commands: list[list[str]] | None = None
    if args.no_provider_discovery:
        commands = []
    elif args.provider_command:
        commands = []
        for raw in args.provider_command[:_MAX_PROVIDERS]:
            try:
                command = shlex.split(raw)
            except ValueError as error:
                parser.error(f"invalid --provider-command: {error}")
            if not command:
                parser.error("--provider-command may not be empty")
            commands.append(command)
    registry = discover_parameter_registry(
        (), schema_space=args.schema, provider_commands=commands
    )
    payload = {
        "schema": "symcc-self-config-parameter-schema-v1",
        "parameters": {
            name: {
                "values": spec.values,
                "scope": spec.scope,
                "numeric": spec.numeric,
                "min": spec.minimum,
                "max": spec.maximum,
                "active_when": spec.active_when,
            }
            for name, spec in sorted(registry.specs.items())
        },
        "provenance": registry.provenance(),
    }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
