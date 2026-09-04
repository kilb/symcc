#!/usr/bin/env python3
"""Runtime routing across independently configured concolic executors."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shlex
from typing import Any


_EXECUTOR_NAMES = {"exact", "tailored", "sampling"}
_ENGINE_NAMES = {"symcc", "symsan"}
_ENV_PREFIXES = ("SYMCC_", "SYMSAN_", "TAINT_")


@dataclass(frozen=True)
class ExecutionRoute:
    executor: str
    engine: str
    command: tuple[str, ...]
    timeout_sec: int
    use_stdin: bool
    environment: dict[str, str] = field(default_factory=dict)


def _parse_command(raw: Any, fallback: list[str]) -> tuple[str, ...]:
    if isinstance(raw, list) and raw and all(
            isinstance(item, str) and item for item in raw):
        return tuple(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = shlex.split(raw)
        except ValueError:
            parsed = []
        if parsed:
            return tuple(parsed)
    return tuple(fallback)


def _safe_environment(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in list(raw.items())[:128]
        if isinstance(name, str) and name.startswith(_ENV_PREFIXES)
        and "\x00" not in str(value) and len(str(value)) <= 512
    }


class ExecutorPortfolio:
    """Resolve exact, tailored and sampling work to real engine processes.

    A route can select another engine or target command.  Without an explicit
    route, it keeps the current target and engine, preserving compatibility.
    """

    def __init__(
        self,
        default_target: list[str],
        default_engine: str = "symcc",
        configuration: dict[str, Any] | None = None,
    ) -> None:
        self.default_target = list(default_target)
        self.default_engine = (
            default_engine if default_engine in _ENGINE_NAMES else "symcc")
        self.configuration = configuration or {}

    @classmethod
    def from_environment(cls, default_target: list[str]) -> "ExecutorPortfolio":
        raw = os.environ.get("SYMCC_EXECUTOR_PORTFOLIO", "")
        configuration: dict[str, Any] = {}
        if raw:
            try:
                if os.path.isfile(raw):
                    with open(raw, encoding="utf-8") as stream:
                        parsed = json.load(stream)
                else:
                    parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    configuration = parsed.get(
                        "executors", parsed)
            except (OSError, ValueError, TypeError):
                configuration = {}

        for executor in sorted(_EXECUTOR_NAMES):
            upper = executor.upper()
            engine = os.environ.get(f"SYMCC_{upper}_ENGINE")
            target = os.environ.get(f"SYMCC_{upper}_TARGET")
            if engine or target:
                route = configuration.setdefault(executor, {})
                if not isinstance(route, dict):
                    route = {}
                    configuration[executor] = route
                if engine:
                    route["engine"] = engine
                if target:
                    route["target"] = target
        return cls(
            default_target,
            default_engine=os.environ.get("SYMCC_ENGINE", "symcc").lower(),
            configuration=configuration,
        )

    def resolve(self, executor: str, timeout_sec: int) -> ExecutionRoute:
        executor = executor if executor in _EXECUTOR_NAMES else "exact"
        raw = self.configuration.get(executor, {})
        if not isinstance(raw, dict):
            raw = {}
        engine = str(raw.get("engine", self.default_engine)).lower()
        if engine not in _ENGINE_NAMES:
            engine = self.default_engine
        command = _parse_command(
            raw.get("target", raw.get("command")), self.default_target)
        try:
            scale = max(0.1, min(10.0, float(raw.get("timeout_scale", 1.0))))
        except (TypeError, ValueError, OverflowError):
            scale = 1.0
        routed_timeout = max(1, int(round(timeout_sec * scale)))
        use_stdin = bool(raw.get("use_stdin", "@@" not in command))
        return ExecutionRoute(
            executor=executor,
            engine=engine,
            command=command,
            timeout_sec=routed_timeout,
            use_stdin=use_stdin,
            environment=_safe_environment(raw.get("env")),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            executor: {
                "engine": self.resolve(executor, 1).engine,
                "command": list(self.resolve(executor, 1).command),
            }
            for executor in sorted(_EXECUTOR_NAMES)
        }
