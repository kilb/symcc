"""Auditable structured LLM proposal loop for concolic scheduling.

The model is deliberately outside the correctness root. It may propose a
bounded scheduling action or concrete candidate bytes; deterministic local
code validates the schema and budgets, and the existing worker/target/parser/
coverage gates decide whether an action has any effect.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import threading
import time
from typing import Any, Mapping, Sequence

from agentic_concolic_hooks import (
    AgenticBackend,
    CommandAgenticBackend,
    HttpAgenticBackend,
    _load_backend_specs,
    sanitize_hint,
)


TASK_SCHEMA = "symcc-agentic-task-v1"
RESPONSE_SCHEMA = "symcc-agentic-response-v1"
LEDGER_SCHEMA = "symcc-agentic-ledger-v1"
POLICY_SCHEMA = "symcc-agentic-policy-v1"
STRUCTURED_SYSTEM_PROMPT = (
    "Return only one JSON object with schema symcc-agentic-response-v1. "
    "Echo request_id and task_sha256 exactly; provide actions and usage. "
    "An action is either schedule (optional strategy, target_branch, "
    "focus_bytes, s2f_actions, route) or candidate (proposal_kind, "
    "lowercase data_hex, target_branch). Do not claim SAT, UNSAT, coverage, "
    "parser validity, or execution success; local execution decides them."
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_FOCUS = re.compile(r"[0-9]+-[0-9]+\Z")
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_TASK_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024


class AgenticProtocolError(ValueError):
    """Raised when an agentic protocol or persistence contract is invalid."""


def _reject_constant(value: str) -> None:
    raise AgenticProtocolError(f"non-finite JSON number {value!r}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgenticProtocolError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError) as error:
        raise AgenticProtocolError(f"non-canonical JSON value: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise AgenticProtocolError(
            f"{name} shape mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _bounded_int(
    value: Any, name: str, *, minimum: int = 0, maximum: int = (1 << 63) - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgenticProtocolError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise AgenticProtocolError(
            f"{name} is outside {minimum}..{maximum}")
    return value


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise AgenticProtocolError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise AgenticProtocolError(f"{name} is outside {minimum}..{maximum}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as error:
        raise AgenticProtocolError(f"{name} must be finite") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AgenticProtocolError(f"{name} is outside {minimum}..{maximum}")
    return value


def _bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise AgenticProtocolError(f"{name} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise AgenticProtocolError(f"{name} is not valid UTF-8") from error
    if size > limit:
        raise AgenticProtocolError(f"{name} exceeds {limit} bytes")
    return value


def _token_estimate(encoded_bytes: int) -> int:
    return max(1, (max(0, encoded_bytes) + 3) // 4)


def _regular_file_sha256(path: str, maximum: int = 256 * 1024 * 1024) -> str:
    try:
        resolved = Path(path).resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            return ""
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


@dataclass(frozen=True)
class BackendIdentity:
    name: str
    provider: str
    model: str
    prompt_sha256: str
    transport_sha256: str

    @classmethod
    def from_backend(cls, backend: AgenticBackend) -> "BackendIdentity":
        name = _bounded_text(str(getattr(backend, "name", "")), "backend name", 64)
        provider = _bounded_text(
            str(getattr(backend, "provider", "")), "backend provider", 64)
        model = _bounded_text(
            str(getattr(backend, "model", "")), "backend model", 128)
        if not name or not provider or not model:
            raise AgenticProtocolError(
                "structured backends require non-empty name/provider/model")
        if isinstance(backend, HttpAgenticBackend):
            prompt_sha256 = hashlib.sha256(
                backend.system_prompt.encode("utf-8")).hexdigest()
            transport = {
                "kind": "http",
                "endpoint_sha256": hashlib.sha256(
                    backend.url.encode("utf-8")).hexdigest(),
                "protocol": backend.protocol,
                "headers_sha256": _digest([
                    [
                        str(name).lower(),
                        hashlib.sha256(
                            backend._resolve_header(str(value)).encode("utf-8")
                        ).hexdigest(),
                    ]
                    for name, value in sorted(backend.headers.items())
                ]),
            }
        elif isinstance(backend, CommandAgenticBackend):
            prompt_sha256 = str(backend.prompt_sha256).lower()
            if not _HEX64.fullmatch(prompt_sha256):
                raise AgenticProtocolError(
                    "structured command backend requires prompt_sha256")
            try:
                argv = shlex.split(backend.command)
            except ValueError as error:
                raise AgenticProtocolError(
                    "structured command backend has invalid quoting") from error
            executable = (
                argv[0] if argv and os.path.sep in argv[0]
                else shutil.which(argv[0]) if argv else None
            )
            transport = {
                "kind": "command",
                "command_sha256": hashlib.sha256(
                    backend.command.encode("utf-8")).hexdigest(),
                "executable_sha256": (
                    _regular_file_sha256(executable) if executable else ""),
                "file_argument_sha256": [
                    [index, _regular_file_sha256(argument)]
                    for index, argument in enumerate(argv[1:], 1)
                    if os.path.isfile(argument)
                ],
            }
        else:
            prompt_sha256 = str(getattr(backend, "prompt_sha256", "")).lower()
            if not _HEX64.fullmatch(prompt_sha256):
                raise AgenticProtocolError(
                    "custom structured backend requires prompt_sha256")
            transport = {
                "kind": backend.__class__.__name__,
                "implementation": (
                    f"{backend.__class__.__module__}."
                    f"{backend.__class__.__qualname__}"
                ),
            }
        return cls(
            name=name,
            provider=provider,
            model=model,
            prompt_sha256=prompt_sha256,
            transport_sha256=_digest(transport),
        )

    def mapping(self) -> dict[str, str]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "prompt_sha256": self.prompt_sha256,
            "transport_sha256": self.transport_sha256,
        }


@dataclass(frozen=True)
class StructuredAgenticPolicy:
    mode: str
    strategy_count: int
    experiment_id: str
    program_identity: str
    trigger_mode: str = "reactive"
    plateau_threshold: int = 8
    max_requests: int = 128
    max_input_tokens: int = 1_048_576
    max_output_tokens: int = 262_144
    max_model_time_us: int = 600_000_000
    max_cost_microusd: int = 1_000_000
    max_input_tokens_per_request: int = 16_384
    max_output_tokens_per_request: int = 4_096
    max_actions_per_response: int = 8
    max_candidate_actions: int = 128
    max_candidate_bytes: int = 2 * 1024 * 1024
    max_candidate_bytes_per_action: int = 16 * 1024
    timeout_ms: int = 2_000
    input_microusd_per_million_tokens: int = 0
    output_microusd_per_million_tokens: int = 0

    def __post_init__(self) -> None:
        if self.mode not in {"online", "shadow", "fallback"}:
            raise AgenticProtocolError(
                "structured mode must be online, shadow, or fallback")
        if self.trigger_mode not in {"reactive", "always"}:
            raise AgenticProtocolError(
                "structured trigger mode must be reactive or always")
        _bounded_text(self.experiment_id, "experiment_id", 128)
        _bounded_text(self.program_identity, "program_identity", 16 * 1024)
        if not self.experiment_id or not self.program_identity:
            raise AgenticProtocolError(
                "experiment_id and program_identity must not be empty")
        bounds = {
            "strategy_count": (self.strategy_count, 1, 1024),
            "plateau_threshold": (self.plateau_threshold, 1, 1 << 31),
            "max_requests": (self.max_requests, 1, 1_000_000),
            "max_input_tokens": (self.max_input_tokens, 1, 1 << 50),
            "max_output_tokens": (self.max_output_tokens, 1, 1 << 50),
            "max_model_time_us": (self.max_model_time_us, 1, 1 << 60),
            "max_cost_microusd": (self.max_cost_microusd, 0, 1 << 60),
            "max_input_tokens_per_request": (
                self.max_input_tokens_per_request, 1, 1 << 30),
            "max_output_tokens_per_request": (
                self.max_output_tokens_per_request, 1, 1 << 30),
            "max_actions_per_response": (
                self.max_actions_per_response, 1, 256),
            "max_candidate_actions": (self.max_candidate_actions, 0, 1 << 30),
            "max_candidate_bytes": (self.max_candidate_bytes, 0, 1 << 40),
            "max_candidate_bytes_per_action": (
                self.max_candidate_bytes_per_action, 1, 1 << 30),
            "timeout_ms": (self.timeout_ms, 100, 600_000),
            "input price": (
                self.input_microusd_per_million_tokens, 0, 1 << 50),
            "output price": (
                self.output_microusd_per_million_tokens, 0, 1 << 50),
        }
        for name, (value, minimum, maximum) in bounds.items():
            _bounded_int(value, name, minimum=minimum, maximum=maximum)

    def mapping(self, backends: Sequence[BackendIdentity], *, include_mode: bool) -> dict[str, Any]:
        value = {
            "schema": POLICY_SCHEMA,
            "experiment_id": self.experiment_id,
            "program_identity": self.program_identity,
            "strategy_count": self.strategy_count,
            "backends": [backend.mapping() for backend in backends],
            "trigger": {
                "mode": self.trigger_mode,
                "plateau_threshold": self.plateau_threshold,
            },
            "budgets": {
                "requests": self.max_requests,
                "input_tokens": self.max_input_tokens,
                "output_tokens": self.max_output_tokens,
                "model_time_us": self.max_model_time_us,
                "cost_microusd": self.max_cost_microusd,
                "input_tokens_per_request": self.max_input_tokens_per_request,
                "output_tokens_per_request": self.max_output_tokens_per_request,
                "actions_per_response": self.max_actions_per_response,
                "candidate_actions": self.max_candidate_actions,
                "candidate_bytes": self.max_candidate_bytes,
                "candidate_bytes_per_action": self.max_candidate_bytes_per_action,
                "timeout_ms": self.timeout_ms,
            },
            "prices": {
                "input_microusd_per_million_tokens": (
                    self.input_microusd_per_million_tokens),
                "output_microusd_per_million_tokens": (
                    self.output_microusd_per_million_tokens),
            },
            "response_schema": RESPONSE_SCHEMA,
        }
        if include_mode:
            value["mode"] = self.mode
        return value


class DecisionLedger:
    """Single-writer canonical JSONL ledger with a SHA-256 hash chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise AgenticProtocolError("O_NOFOLLOW is required for agentic ledger")
        flags = os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0)
        self.descriptor = -1
        try:
            self.descriptor = os.open(self.path, flags, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            raise AgenticProtocolError(
                "agentic ledger cannot acquire exclusive ownership") from error
        metadata = os.fstat(self.descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_LEDGER_BYTES:
            os.close(self.descriptor)
            raise AgenticProtocolError(
                "agentic ledger must be a bounded regular file")
        self.records = self._read_descriptor(self.descriptor)
        self.sequence = len(self.records)
        self.previous = (
            self.records[-1]["record_sha256"] if self.records else "0" * 64)

    @staticmethod
    def _decode_line(line: bytes, line_number: int) -> dict[str, Any]:
        try:
            raw = json.loads(
                line.decode("ascii"),
                object_pairs_hook=_reject_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AgenticProtocolError(
                f"invalid ledger JSON at line {line_number}: {error}") from error
        if not isinstance(raw, dict):
            raise AgenticProtocolError(
                f"ledger line {line_number} is not an object")
        _exact_keys(
            raw,
            {"schema", "sequence", "previous_sha256", "event", "payload", "record_sha256"},
            f"ledger line {line_number}",
        )
        canonical = _canonical_json(raw)
        if canonical != line:
            raise AgenticProtocolError(
                f"ledger line {line_number} is not canonical JSON")
        return raw

    @classmethod
    def _validate_records(cls, records: list[dict[str, Any]]) -> None:
        previous = "0" * 64
        for index, record in enumerate(records, 1):
            if record["schema"] != LEDGER_SCHEMA:
                raise AgenticProtocolError(f"ledger line {index} has wrong schema")
            if record["sequence"] != index:
                raise AgenticProtocolError(f"ledger line {index} breaks sequence")
            if record["previous_sha256"] != previous:
                raise AgenticProtocolError(f"ledger line {index} breaks hash chain")
            if not isinstance(record["event"], str) or not isinstance(
                    record["payload"], dict):
                raise AgenticProtocolError(
                    f"ledger line {index} has invalid event/payload")
            body = dict(record)
            supplied = body.pop("record_sha256")
            if not _HEX64.fullmatch(str(supplied)) or _digest(body) != supplied:
                raise AgenticProtocolError(f"ledger line {index} has bad digest")
            previous = supplied

    @classmethod
    def _read_descriptor(cls, descriptor: int) -> list[dict[str, Any]]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = _MAX_LEDGER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _MAX_LEDGER_BYTES:
            raise AgenticProtocolError("agentic ledger exceeds byte budget")
        if encoded and not encoded.endswith(b"\n"):
            raise AgenticProtocolError("agentic ledger has a partial final record")
        records = [
            cls._decode_line(line, index)
            for index, line in enumerate(encoded.splitlines(), 1)
        ]
        cls._validate_records(records)
        return records

    @classmethod
    def read(cls, path: str | Path) -> list[dict[str, Any]]:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise AgenticProtocolError("O_NOFOLLOW is required for agentic ledger")
        descriptor = os.open(
            Path(path), os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AgenticProtocolError("agentic ledger is not regular")
            return cls._read_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def append(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _bounded_text(event, "ledger event", 128)
        body = {
            "schema": LEDGER_SCHEMA,
            "sequence": self.sequence + 1,
            "previous_sha256": self.previous,
            "event": event,
            "payload": dict(payload),
        }
        record = {**body, "record_sha256": _digest(body)}
        encoded = _canonical_json(record) + b"\n"
        current = os.fstat(self.descriptor).st_size
        if current + len(encoded) > _MAX_LEDGER_BYTES:
            raise AgenticProtocolError("agentic ledger would exceed byte budget")
        os.lseek(self.descriptor, 0, os.SEEK_END)
        view = memoryview(encoded)
        while view:
            written = os.write(self.descriptor, view)
            if written <= 0:
                raise OSError("short agentic ledger write")
            view = view[written:]
        os.fsync(self.descriptor)
        self.sequence += 1
        self.previous = record["record_sha256"]
        self.records.append(record)
        return record

    def close(self) -> None:
        if getattr(self, "descriptor", -1) < 0:
            return
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass(frozen=True)
class StructuredDecision:
    aliases: tuple[str, ...]
    request_id: str
    decision_id: str
    source: str
    hint: dict[str, Any]
    proposals: tuple[dict[str, Any], ...]
    fallback_reason: str
    model_actions: tuple[dict[str, Any], ...]


@dataclass
class _BackendState:
    backend: AgenticBackend
    identity: BackendIdentity
    failures: int = 0
    open_until: float = 0.0
    calls: int = 0
    successes: int = 0


@dataclass(frozen=True)
class _Reservation:
    input_tokens: int
    output_tokens: int
    model_time_us: int
    cost_microusd: int


class StructuredAgenticController:
    """Asynchronous schema/budget/replay/shadow control plane."""

    def __init__(
        self,
        backends: Sequence[AgenticBackend],
        policy: StructuredAgenticPolicy,
        ledger_path: str | Path,
        *,
        workers: int = 2,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.policy = policy
        self.backend_states = [
            _BackendState(backend, BackendIdentity.from_backend(backend))
            for backend in backends
        ]
        if policy.mode != "fallback" and not self.backend_states:
            raise AgenticProtocolError(
                "online/shadow structured mode requires a backend")
        self.backend_identities = tuple(
            state.identity for state in self.backend_states)
        self.base_policy = policy.mapping(
            self.backend_identities, include_mode=False)
        self.full_policy = policy.mapping(
            self.backend_identities, include_mode=True)
        self.base_policy_sha256 = _digest(self.base_policy)
        self.policy_sha256 = _digest(self.full_policy)
        self.ledger = DecisionLedger(ledger_path)
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.backend_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="symcc-structured-agent",
        )
        self.pending: dict[
            str,
            tuple[
                Future[dict[str, Any]], tuple[str, ...], dict[str, Any],
                dict[str, Any], _Reservation,
            ],
        ] = {}
        self.ready: list[StructuredDecision] = []
        self.reserved_input_tokens = 0
        self.reserved_output_tokens = 0
        self.reserved_model_time_us = 0
        self.reserved_cost_microusd = 0
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_time_us = 0
        self.cost_microusd = 0
        self.candidate_actions = 0
        self.candidate_bytes = 0
        self.proposed_candidate_actions = 0
        self.proposed_candidate_bytes = 0
        self.completed = 0
        self.valid_responses = 0
        self.fallbacks = 0
        self.shadow_decisions = 0
        self.trigger_evaluations = 0
        self.triggered_requests = 0
        self.suppressed_requests = 0
        self.plateau_count = 0
        self.episode_iterations: dict[str, int] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.known_decisions: set[str] = set()
        self.decision_sources: dict[str, str] = {}
        self.decision_episodes: dict[str, str] = {}
        self.candidate_decisions: dict[str, str] = {}
        self.selected_decisions: set[str] = set()
        self.closed = False
        self.executor_stopped = False
        try:
            self._restore()
        except BaseException:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.ledger.close()
            raise

    @classmethod
    def from_environment(
        cls,
        strategy_count: int,
        ledger_path: str | Path,
        program_identity: str,
    ) -> "StructuredAgenticController | None":
        enabled = os.environ.get("SYMCC_AGENTIC_STRUCTURED", "0").lower()
        if enabled in {"0", "false", "off", "no", ""}:
            return None
        mode = os.environ.get("SYMCC_AGENTIC_MODE", "online").strip().lower()
        specs = _load_backend_specs(os.environ.get("SYMCC_AGENTIC_BACKENDS"))
        backends: list[AgenticBackend] = []
        for index, spec in enumerate(specs):
            kind = str(spec.get("type", "command")).strip().lower()
            name = str(spec.get("name", f"{kind}-{index}"))[:64]
            if kind == "command" and spec.get("command"):
                backends.append(CommandAgenticBackend(
                    str(spec["command"]),
                    name=name,
                    provider=str(spec.get("provider", "command")),
                    model=str(spec.get("model", "")),
                    prompt_sha256=str(spec.get("prompt_sha256", "")),
                ))
            elif kind in {"http", "http-json", "chat-completions"} \
                    and spec.get("url"):
                protocol = str(spec.get(
                    "protocol",
                    "chat-completions" if kind == "chat-completions"
                    else "task-json",
                ))
                headers = spec.get("headers", {})
                backends.append(HttpAgenticBackend(
                    str(spec["url"]),
                    {
                        str(key): str(value)
                        for key, value in headers.items()
                    } if isinstance(headers, dict) else {},
                    protocol=protocol,
                    model=str(spec.get("model", "")),
                    system_prompt=str(spec.get(
                        "system_prompt", STRUCTURED_SYSTEM_PROMPT)),
                    name=name,
                    provider=str(spec.get("provider", "http")),
                ))
        experiment_id = os.environ.get(
            "SYMCC_AGENTIC_EXPERIMENT_ID",
            f"campaign-{hashlib.sha256(program_identity.encode('utf-8')).hexdigest()[:16]}",
        )
        policy = StructuredAgenticPolicy(
            mode=mode,
            strategy_count=strategy_count,
            experiment_id=experiment_id,
            program_identity=program_identity,
            trigger_mode=os.environ.get(
                "SYMCC_AGENTIC_TRIGGER", "reactive").strip().lower(),
            plateau_threshold=_env_int(
                "SYMCC_AGENTIC_PLATEAU_THRESHOLD", 8, 1, 1 << 31),
            max_requests=_env_int("SYMCC_AGENTIC_MAX_REQUESTS", 128, 1, 1_000_000),
            max_input_tokens=_env_int(
                "SYMCC_AGENTIC_MAX_INPUT_TOKENS", 1_048_576, 1, 1 << 50),
            max_output_tokens=_env_int(
                "SYMCC_AGENTIC_MAX_OUTPUT_TOKENS", 262_144, 1, 1 << 50),
            max_model_time_us=_env_int(
                "SYMCC_AGENTIC_MAX_MODEL_US", 600_000_000, 1, 1 << 60),
            max_cost_microusd=_env_int(
                "SYMCC_AGENTIC_MAX_COST_MICROUSD", 1_000_000, 0, 1 << 60),
            max_input_tokens_per_request=_env_int(
                "SYMCC_AGENTIC_INPUT_TOKENS_PER_REQUEST", 16_384, 1, 1 << 30),
            max_output_tokens_per_request=_env_int(
                "SYMCC_AGENTIC_OUTPUT_TOKENS_PER_REQUEST", 4_096, 1, 1 << 30),
            max_actions_per_response=_env_int(
                "SYMCC_AGENTIC_ACTIONS_PER_RESPONSE", 8, 1, 256),
            max_candidate_actions=_env_int(
                "SYMCC_AGENTIC_MAX_CANDIDATES", 128, 0, 1 << 30),
            max_candidate_bytes=_env_int(
                "SYMCC_AGENTIC_MAX_CANDIDATE_BYTES", 2 * 1024 * 1024,
                0, 1 << 40),
            max_candidate_bytes_per_action=_env_int(
                "SYMCC_AGENTIC_CANDIDATE_BYTES_PER_ACTION", 16 * 1024,
                1, 1 << 30),
            timeout_ms=_env_int(
                "SYMCC_AGENTIC_TIMEOUT_MS", 2_000, 100, 600_000),
            input_microusd_per_million_tokens=_env_int(
                "SYMCC_AGENTIC_INPUT_PRICE_MICROUSD", 0, 0, 1 << 50),
            output_microusd_per_million_tokens=_env_int(
                "SYMCC_AGENTIC_OUTPUT_PRICE_MICROUSD", 0, 0, 1 << 50),
        )
        return cls(
            backends,
            policy,
            ledger_path,
            workers=_env_int("SYMCC_AGENTIC_WORKERS", 2, 1, 64),
            failure_threshold=_env_int(
                "SYMCC_AGENTIC_FAILURE_THRESHOLD", 3, 1, 1024),
            cooldown_seconds=_env_float(
                "SYMCC_AGENTIC_COOLDOWN", 30.0, 1.0, 86_400.0),
        )

    def _event(self, name: str, payload: Mapping[str, Any]) -> None:
        self.ledger.append(name, {
            "policy_sha256": self.policy_sha256,
            **dict(payload),
        })

    def _restore(self) -> None:
        records = self.ledger.records
        if not records:
            self._event("policy", {
                "base_policy_sha256": self.base_policy_sha256,
                "policy": self.full_policy,
            })
            return
        first = records[0]
        if first["event"] != "policy":
            raise AgenticProtocolError("agentic ledger does not start with policy")
        payload = first["payload"]
        if (
            payload.get("policy_sha256") != self.policy_sha256 or
            payload.get("base_policy_sha256") != self.base_policy_sha256 or
            payload.get("policy") != self.full_policy
        ):
            raise AgenticProtocolError(
                "agentic ledger belongs to a different immutable policy")
        submitted: set[str] = set()
        completed: set[str] = set()
        for record in records[1:]:
            payload = record["payload"]
            if payload.get("policy_sha256") != self.policy_sha256:
                raise AgenticProtocolError("agentic ledger policy binding changed")
            event = record["event"]
            if event == "trigger_evaluated":
                self.trigger_evaluations += 1
                eligible = payload.get("eligible")
                if not isinstance(eligible, bool):
                    raise AgenticProtocolError(
                        "restored trigger eligibility is not boolean")
                self.plateau_count = _bounded_int(
                    payload.get("plateau_count"),
                    "restored plateau count", maximum=1 << 31)
                if eligible:
                    self.triggered_requests += 1
                else:
                    self.suppressed_requests += 1
            elif event == "request_submitted":
                request_id = str(payload.get("request_id", ""))
                if request_id in submitted:
                    raise AgenticProtocolError("duplicate request submission")
                submitted.add(request_id)
                self.requests += 1
                episode_id = str(payload.get("episode_id", ""))
                iteration = _bounded_int(
                    payload.get("iteration"), "restored iteration", maximum=1 << 31)
                self.episode_iterations[episode_id] = max(
                    self.episode_iterations.get(episode_id, 0), iteration + 1)
            elif event == "request_completed":
                request_id = str(payload.get("request_id", ""))
                if request_id not in submitted or request_id in completed:
                    raise AgenticProtocolError("orphan/duplicate request completion")
                completed.add(request_id)
                self.completed += 1
                self.input_tokens += _bounded_int(
                    payload.get("input_tokens"), "restored input tokens", maximum=1 << 50)
                self.output_tokens += _bounded_int(
                    payload.get("output_tokens"), "restored output tokens", maximum=1 << 50)
                self.model_time_us += _bounded_int(
                    payload.get("model_time_us"), "restored model time", maximum=1 << 60)
                self.cost_microusd += _bounded_int(
                    payload.get("cost_microusd"), "restored cost", maximum=1 << 60)
                self.candidate_actions += _bounded_int(
                    payload.get("candidate_actions"), "restored candidates", maximum=1 << 30)
                self.candidate_bytes += _bounded_int(
                    payload.get("candidate_bytes"), "restored candidate bytes", maximum=1 << 40)
                self.proposed_candidate_actions += _bounded_int(
                    payload.get("proposed_candidate_actions", 0),
                    "restored proposed candidates", maximum=1 << 30)
                self.proposed_candidate_bytes += _bounded_int(
                    payload.get("proposed_candidate_bytes", 0),
                    "restored proposed candidate bytes", maximum=1 << 40)
                if payload.get("valid_response"):
                    self.valid_responses += 1
                if payload.get("fallback_reason"):
                    self.fallbacks += 1
                if payload.get("mode") == "shadow":
                    self.shadow_decisions += 1
                decision_id = str(payload.get("decision_id", ""))
                if _HEX64.fullmatch(decision_id):
                    if decision_id in self.known_decisions:
                        raise AgenticProtocolError("duplicate restored decision")
                    self.known_decisions.add(decision_id)
                    source = str(payload.get("input_sha256", ""))
                    if source and not _HEX64.fullmatch(source):
                        raise AgenticProtocolError(
                            "restored decision source is invalid")
                    self.decision_sources[decision_id] = source
                    episode = str(payload.get("episode_id", ""))
                    if not _HEX64.fullmatch(episode):
                        raise AgenticProtocolError(
                            "restored decision episode is invalid")
                    self.decision_episodes[decision_id] = episode
            elif event == "request_recovered_cancelled":
                request_id = str(payload.get("request_id", ""))
                if request_id not in submitted or request_id in completed:
                    raise AgenticProtocolError(
                        "orphan/duplicate recovered cancellation")
                completed.add(request_id)
            elif event == "fallback_decision":
                episode_id = str(payload.get("episode_id", ""))
                iteration = _bounded_int(
                    payload.get("iteration"), "restored fallback iteration",
                    maximum=1 << 31)
                self.episode_iterations[episode_id] = max(
                    self.episode_iterations.get(episode_id, 0), iteration + 1)
                decision_id = str(payload.get("decision_id", ""))
                if not _HEX64.fullmatch(decision_id) or \
                        decision_id in self.known_decisions:
                    raise AgenticProtocolError(
                        "fallback decision identity is invalid or duplicated")
                self.known_decisions.add(decision_id)
                source = str(payload.get("input_sha256", ""))
                if source and not _HEX64.fullmatch(source):
                    raise AgenticProtocolError(
                        "restored fallback source is invalid")
                self.decision_sources[decision_id] = source
                if not _HEX64.fullmatch(episode_id):
                    raise AgenticProtocolError(
                        "restored fallback episode is invalid")
                self.decision_episodes[decision_id] = episode_id
                self.fallbacks += 1
            elif event == "candidate_admitted":
                candidate = str(payload.get("candidate_sha256", ""))
                decision = str(payload.get("decision_id", ""))
                if not _HEX64.fullmatch(candidate) or \
                        decision not in self.known_decisions:
                    raise AgenticProtocolError(
                        "candidate admission has invalid binding")
                existing = self.candidate_decisions.get(candidate)
                if existing and existing != decision:
                    raise AgenticProtocolError(
                        "candidate admission changes decision binding")
                self.candidate_decisions[candidate] = decision
            elif event == "candidate_rejected":
                decision = str(payload.get("decision_id", ""))
                proposal = str(payload.get("proposal_id", ""))
                reason = str(payload.get("reason", ""))
                if decision not in self.known_decisions or \
                        not _HEX64.fullmatch(proposal) or not reason:
                    raise AgenticProtocolError(
                        "candidate rejection has invalid binding")
            elif event == "hint_selected":
                decision = str(payload.get("decision_id", ""))
                if decision not in self.known_decisions or \
                        decision in self.selected_decisions:
                    raise AgenticProtocolError(
                        "hint selection references an unknown/used decision")
                self.selected_decisions.add(decision)
            elif event == "execution_outcome":
                episode = str(payload.get("episode_id", ""))
                decision = str(payload.get("decision_id", ""))
                summary = payload.get("summary")
                if decision not in self.known_decisions:
                    raise AgenticProtocolError(
                        "restored outcome references an unknown decision")
                if episode != self.decision_episodes.get(decision):
                    raise AgenticProtocolError(
                        "restored outcome changes decision episode")
                if episode and isinstance(summary, dict):
                    self.history.setdefault(episode, []).append(summary)
                    self.history[episode] = self.history[episode][-8:]
        if (
            self.requests > self.policy.max_requests or
            self.input_tokens > self.policy.max_input_tokens or
            self.output_tokens > self.policy.max_output_tokens or
            self.model_time_us > self.policy.max_model_time_us or
            self.cost_microusd > self.policy.max_cost_microusd or
            self.candidate_actions > self.policy.max_candidate_actions or
            self.candidate_bytes > self.policy.max_candidate_bytes
        ):
            raise AgenticProtocolError(
                "restored agentic usage exceeds the immutable policy")
        for request_id in sorted(submitted - completed):
            self._event("request_recovered_cancelled", {
                "request_id": request_id,
                "reason": "process_restart",
            })

    @staticmethod
    def _aliases(task: Mapping[str, Any]) -> tuple[str, ...]:
        values = (str(task.get("sha256", "") or ""),
                  str(task.get("input_path", "") or ""))
        return tuple(dict.fromkeys(value for value in values if value))

    def _normalize_task(self, task: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(task, Mapping):
            raise AgenticProtocolError("agentic task must be an object")

        def integer(name: str, maximum: int = (1 << 63) - 1) -> int:
            value = task.get(name, 0)
            if isinstance(value, bool):
                return 0
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return 0
            return parsed if 0 <= parsed <= maximum else 0

        digest = str(task.get("sha256", "") or "").lower()
        if not _HEX64.fullmatch(digest):
            digest = ""
        path = _bounded_text(str(task.get("input_path", "") or ""),
                             "input_path", 4096)
        focus = str(task.get("focus_bytes", "") or "")
        if focus and (len(focus) > 64 or not _FOCUS.fullmatch(focus)):
            focus = ""
        actions: list[list[Any]] = []
        seen: set[int] = set()
        raw_actions = task.get("s2f_actions", ())
        if isinstance(raw_actions, (list, tuple)):
            for item in raw_actions[:64]:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    branch = int(item[0])
                except (TypeError, ValueError, OverflowError):
                    continue
                action = str(item[1]).lower()
                if branch > 0 and branch not in seen and action in {
                        "solve", "sample", "skip"}:
                    actions.append([branch, action])
                    seen.add(branch)

        def branches(name: str, limit: int) -> list[int]:
            raw = task.get(name, ())
            if not isinstance(raw, (list, tuple)):
                return []
            result: list[int] = []
            seen_values: set[int] = set()
            for value in raw[:limit]:
                try:
                    parsed = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if parsed > 0 and parsed not in seen_values:
                    result.append(parsed)
                    seen_values.add(parsed)
            return result

        def features(name: str, limit: int, width: int) -> list[list[int]]:
            raw = task.get(name, ())
            if not isinstance(raw, (list, tuple)):
                return []
            result: list[list[int]] = []
            for row in raw[:limit]:
                if not isinstance(row, (list, tuple)) or len(row) > width:
                    continue
                converted: list[int] = []
                valid = True
                for value in row:
                    if isinstance(value, bool):
                        valid = False
                        break
                    try:
                        parsed = int(value)
                    except (TypeError, ValueError, OverflowError):
                        valid = False
                        break
                    if not 0 <= parsed <= (1 << 63) - 1:
                        valid = False
                        break
                    converted.append(parsed)
                if valid:
                    result.append(converted)
            return result

        normalized = {
            "input_sha256": digest,
            "input_name": os.path.basename(path)[:256],
            "strategy": min(self.policy.strategy_count - 1, integer("strategy")),
            "executor": _bounded_text(
                str(task.get("executor", "") or ""), "executor", 64),
            "target_branch": integer("target_branch"),
            "target_reached": bool(task.get("target_reached", False)),
            "focus_bytes": focus,
            "queue_remaining": integer("queue_remaining", 1 << 31),
            "state_task_id": _bounded_text(
                str(task.get("state_task_id", "") or ""), "state_task_id", 128),
            "state_shard": integer("state_shard", 1 << 31),
            "task_region": integer("task_region", 1 << 31),
            "s2f_actions": actions,
            "open_branches": branches("open_branches", 128),
            "symbolic_branches": integer("symbolic_branches", 1 << 31),
            "generated": integer("generated", 1 << 31),
            "coverage_delta": integer("coverage_delta", 1 << 31),
            "interesting_cases": integer("interesting_cases", 1 << 31),
            "solver_queries": integer("solver_queries", 1 << 31),
            "solver_unknown": integer("solver_unknown", 1 << 31),
            "z3_timeouts": integer("z3_timeouts", 1 << 31),
            "solver_time_us": integer("solver_time_us", 1 << 60),
            "backsolver_targets": integer("backsolver_targets", 1 << 31),
            "backsolver_attempts": integer("backsolver_attempts", 1 << 31),
            "backsolver_sat": integer("backsolver_sat", 1 << 31),
            "backsolver_constraints_kept": integer(
                "backsolver_constraints_kept", 1 << 31),
            "backsolver_constraints_dropped": integer(
                "backsolver_constraints_dropped", 1 << 31),
            "backsolver_direct_attempts": integer(
                "backsolver_direct_attempts", 1 << 31),
            "backsolver_direct_sat": integer("backsolver_direct_sat", 1 << 31),
            "backsolver_validations": integer("backsolver_validations", 1 << 31),
            "backsolver_validation_failures": integer(
                "backsolver_validation_failures", 1 << 31),
            "backsolver_z3_fallbacks": integer(
                "backsolver_z3_fallbacks", 1 << 31),
            "difficulty": integer("difficulty", 1 << 31),
            "data_features": features("data_features", 32, 8),
            "static_data_features": features("static_data_features", 64, 8),
            "comparison_taints": features("comparison_taints", 32, 8),
        }
        if len(_canonical_json(normalized)) > _MAX_TASK_BYTES:
            raise AgenticProtocolError("normalized agentic task exceeds byte budget")
        return normalized

    def _episode_id(self, normalized: Mapping[str, Any]) -> str:
        return _digest({
            "schema": "symcc-agentic-episode-v1",
            "program_identity": self.policy.program_identity,
            "input_sha256": normalized["input_sha256"],
            "input_name": normalized["input_name"],
            "target_branch": normalized["target_branch"],
        })

    def _evaluate_trigger(
        self, normalized: Mapping[str, Any], episode_id: str,
    ) -> bool:
        coverage_delta = int(normalized["coverage_delta"])
        target_reached = bool(normalized["target_reached"])
        if coverage_delta > 0 or target_reached:
            self.plateau_count = 0
        else:
            self.plateau_count = min(1 << 31, self.plateau_count + 1)

        reasons: list[str] = []
        if self.policy.trigger_mode == "always":
            reasons.append("always")
        elif not target_reached:
            if int(normalized["solver_unknown"]) > 0:
                reasons.append("solver_unknown")
            if int(normalized["z3_timeouts"]) > 0:
                reasons.append("z3_timeout")
            if int(normalized["backsolver_validation_failures"]) > 0:
                reasons.append("backsolver_validation_failure")
            if int(normalized["backsolver_z3_fallbacks"]) > 0:
                reasons.append("backsolver_z3_fallback")
            if int(normalized["symbolic_branches"]) > 0 and \
                    int(normalized["generated"]) == 0:
                reasons.append("symbolic_no_candidate")
            if self.plateau_count >= self.policy.plateau_threshold:
                reasons.append("coverage_plateau")

        eligible = bool(reasons)
        self.trigger_evaluations += 1
        if eligible:
            self.triggered_requests += 1
        else:
            self.suppressed_requests += 1
        self._event("trigger_evaluated", {
            "episode_id": episode_id,
            "task_sha256": _digest(normalized),
            "eligible": eligible,
            "reasons": reasons,
            "plateau_count": self.plateau_count,
        })
        return eligible

    def _cost(self, input_tokens: int, output_tokens: int) -> int:
        numerator = (
            input_tokens * self.policy.input_microusd_per_million_tokens +
            output_tokens * self.policy.output_microusd_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000

    def _reserve(self, request_bytes: int) -> _Reservation | None:
        estimated = _token_estimate(request_bytes)
        if estimated > self.policy.max_input_tokens_per_request:
            return None
        reservation = _Reservation(
            input_tokens=self.policy.max_input_tokens_per_request,
            output_tokens=self.policy.max_output_tokens_per_request,
            model_time_us=self.policy.timeout_ms * 1000,
            cost_microusd=self._cost(
                self.policy.max_input_tokens_per_request,
                self.policy.max_output_tokens_per_request,
            ),
        )
        if (
            self.requests >= self.policy.max_requests or
            self.input_tokens + self.reserved_input_tokens +
                reservation.input_tokens > self.policy.max_input_tokens or
            self.output_tokens + self.reserved_output_tokens +
                reservation.output_tokens > self.policy.max_output_tokens or
            self.model_time_us + self.reserved_model_time_us +
                reservation.model_time_us > self.policy.max_model_time_us or
            self.cost_microusd + self.reserved_cost_microusd +
                reservation.cost_microusd > self.policy.max_cost_microusd
        ):
            return None
        self.reserved_input_tokens += reservation.input_tokens
        self.reserved_output_tokens += reservation.output_tokens
        self.reserved_model_time_us += reservation.model_time_us
        self.reserved_cost_microusd += reservation.cost_microusd
        return reservation

    def _release_reservation(self, reservation: _Reservation) -> None:
        self.reserved_input_tokens -= reservation.input_tokens
        self.reserved_output_tokens -= reservation.output_tokens
        self.reserved_model_time_us -= reservation.model_time_us
        self.reserved_cost_microusd -= reservation.cost_microusd

    def _fallback_decision(
        self,
        aliases: tuple[str, ...],
        normalized: Mapping[str, Any],
        fallback_hint: Mapping[str, Any],
        reason: str,
        *,
        request_id: str = "",
        model_actions: Sequence[dict[str, Any]] = (),
    ) -> StructuredDecision:
        clean_fallback = sanitize_hint(
            fallback_hint, self.policy.strategy_count)
        identity = {
            "request_id": request_id,
            "task_sha256": _digest(normalized),
            "mode": self.policy.mode,
            "reason": reason,
            "fallback": clean_fallback,
            "model_actions": list(model_actions),
        }
        decision_id = _digest(identity)
        if decision_id in self.known_decisions:
            raise AgenticProtocolError("fallback decision identity is duplicated")
        self.known_decisions.add(decision_id)
        self.decision_sources[decision_id] = str(
            normalized.get("input_sha256", ""))
        self.decision_episodes[decision_id] = self._episode_id(normalized)
        self.fallbacks += 1
        return StructuredDecision(
            aliases=aliases,
            request_id=request_id,
            decision_id=decision_id,
            source="fallback",
            hint=clean_fallback,
            proposals=(),
            fallback_reason=reason,
            model_actions=tuple(model_actions),
        )

    def submit(
        self, task: Mapping[str, Any], fallback_hint: Mapping[str, Any] | None = None,
    ) -> bool:
        aliases = self._aliases(task)
        normalized = self._normalize_task(task)
        fallback = sanitize_hint(
            fallback_hint or {}, self.policy.strategy_count)
        episode_id = self._episode_id(normalized)
        if not self._evaluate_trigger(normalized, episode_id):
            return False
        iteration = self.episode_iterations.get(episode_id, 0)
        self.episode_iterations[episode_id] = iteration + 1
        task_sha256 = _digest(normalized)
        request_seed = {
            "policy_sha256": self.policy_sha256,
            "episode_id": episode_id,
            "iteration": iteration,
            "task_sha256": task_sha256,
        }
        request_id = _digest(request_seed)
        if self.policy.mode == "fallback":
            decision = self._fallback_decision(
                aliases, normalized, fallback, "ablation_fallback_mode",
                request_id=request_id)
            self.ready.append(decision)
            self._event("fallback_decision", {
                "request_id": request_id,
                "episode_id": episode_id,
                "iteration": iteration,
                "task_sha256": task_sha256,
                "input_sha256": normalized["input_sha256"],
                "decision_id": decision.decision_id,
                "reason": decision.fallback_reason,
                "hint": decision.hint,
            })
            return True
        if request_id in self.pending:
            return False
        envelope = {
            "schema": TASK_SCHEMA,
            "request_id": request_id,
            "task_sha256": task_sha256,
            "policy": {
                "base_policy_sha256": self.base_policy_sha256,
                "experiment_id": self.policy.experiment_id,
                "mode": self.policy.mode,
            },
            "episode": {"id": episode_id, "iteration": iteration},
            "task": normalized,
            "history": list(self.history.get(episode_id, ())[-4:]),
            "allowed_actions": ["schedule", "candidate"],
        }
        encoded = _canonical_json(envelope)
        reservation = self._reserve(len(encoded))
        if reservation is None:
            decision = self._fallback_decision(
                aliases, normalized, fallback, "budget_exhausted",
                request_id=request_id)
            self.ready.append(decision)
            self._event("fallback_decision", {
                "episode_id": episode_id,
                "iteration": iteration,
                "task_sha256": task_sha256,
                "decision_id": decision.decision_id,
                "request_id": request_id,
                "reason": decision.fallback_reason,
                "hint": decision.hint,
            })
            return True
        self.requests += 1
        self._event("request_submitted", {
            "request_id": request_id,
            "episode_id": episode_id,
            "iteration": iteration,
            "task_sha256": task_sha256,
            "task": normalized,
            "history": envelope["history"],
            "estimated_input_tokens": _token_estimate(len(encoded)),
        })
        future = self.executor.submit(self._invoke, envelope)
        self.pending[request_id] = (
            future, aliases, dict(task), fallback, reservation)
        return True

    def _validate_response(
        self,
        raw: Any,
        request: Mapping[str, Any],
        provider_usage: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        if not isinstance(raw, dict):
            raise AgenticProtocolError("response root must be an object")
        encoded = _canonical_json(raw)
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise AgenticProtocolError("response exceeds byte budget")
        _exact_keys(
            raw, {"schema", "request_id", "task_sha256", "actions", "usage"},
            "agentic response")
        if raw["schema"] != RESPONSE_SCHEMA:
            raise AgenticProtocolError("response schema mismatch")
        if raw["request_id"] != request["request_id"] or \
                raw["task_sha256"] != request["task_sha256"]:
            raise AgenticProtocolError("response request/task binding mismatch")
        usage = raw["usage"]
        if not isinstance(usage, dict):
            raise AgenticProtocolError("usage must be an object")
        _exact_keys(usage, {"input_tokens", "output_tokens"}, "usage")
        declared_input = _bounded_int(
            usage["input_tokens"], "input_tokens",
            maximum=self.policy.max_input_tokens_per_request)
        declared_output = _bounded_int(
            usage["output_tokens"], "output_tokens",
            maximum=self.policy.max_output_tokens_per_request)
        provider_input = _bounded_int(
            provider_usage.get("input_tokens", 0), "provider input tokens",
            maximum=self.policy.max_input_tokens_per_request)
        provider_output = _bounded_int(
            provider_usage.get("output_tokens", 0), "provider output tokens",
            maximum=self.policy.max_output_tokens_per_request)
        input_tokens = max(
            declared_input, provider_input,
            _token_estimate(len(_canonical_json(request))))
        output_tokens = max(
            declared_output, provider_output, _token_estimate(len(encoded)))
        if input_tokens > self.policy.max_input_tokens_per_request or \
                output_tokens > self.policy.max_output_tokens_per_request:
            raise AgenticProtocolError("provider usage exceeds per-request budget")
        actions = raw["actions"]
        if not isinstance(actions, list) or not 1 <= len(actions) <= \
                self.policy.max_actions_per_response:
            raise AgenticProtocolError("actions length is outside the budget")
        normalized: list[dict[str, Any]] = []
        schedule_seen = False
        candidate_bytes = 0
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise AgenticProtocolError(f"action {index} must be an object")
            kind = action.get("kind")
            if kind == "schedule":
                if schedule_seen:
                    raise AgenticProtocolError("response has multiple schedule actions")
                schedule_seen = True
                allowed = {
                    "kind", "strategy", "target_branch", "focus_bytes",
                    "s2f_actions", "route",
                }
                extra = sorted(set(action) - allowed)
                if extra:
                    raise AgenticProtocolError(
                        f"schedule action has unknown fields: {extra}")
                hint = sanitize_hint(action, self.policy.strategy_count)
                if not hint:
                    raise AgenticProtocolError("schedule action has no valid field")
                focus = hint.get("focus_bytes", "")
                if focus and (not _FOCUS.fullmatch(focus) or len(focus) > 64):
                    raise AgenticProtocolError("schedule focus_bytes is malformed")
                route = hint.get("route", "")
                if route and route not in {
                        "cottontail", "concollmic", "gordian", "hybrid"}:
                    raise AgenticProtocolError("schedule route is unsupported")
                normalized.append({"kind": "schedule", **hint})
            elif kind == "candidate":
                _exact_keys(
                    action,
                    {"kind", "proposal_kind", "data_hex", "target_branch"},
                    f"candidate action {index}",
                )
                proposal_kind = action["proposal_kind"]
                if proposal_kind not in {
                        "solve_complete", "history_acquisition",
                        "targeted_transform"}:
                    raise AgenticProtocolError(
                        "candidate proposal_kind is unsupported")
                data_hex = _bounded_text(
                    action["data_hex"], "candidate data_hex",
                    self.policy.max_candidate_bytes_per_action * 2)
                if len(data_hex) % 2 or any(
                        char not in "0123456789abcdef" for char in data_hex):
                    raise AgenticProtocolError(
                        "candidate data_hex must be canonical lowercase hex")
                size = len(data_hex) // 2
                candidate_bytes += size
                target = _bounded_int(
                    action["target_branch"], "candidate target_branch")
                normalized.append({
                    "kind": "candidate",
                    "proposal_kind": proposal_kind,
                    "data_hex": data_hex,
                    "target_branch": target,
                })
            else:
                raise AgenticProtocolError(f"unsupported action kind {kind!r}")
        return normalized, input_tokens, output_tokens, candidate_bytes

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        last_reason = "backend_unavailable"
        input_tokens_total = 0
        output_tokens_total = 0
        model_time_us_total = 0
        request_started = time.monotonic_ns()

        def attempt_usage(
            raw: Any, provider_usage: Mapping[str, Any], identity: BackendIdentity,
        ) -> tuple[int, int]:
            prompt_bytes = 0
            for state in self.backend_states:
                if state.identity == identity and isinstance(
                        state.backend, HttpAgenticBackend):
                    prompt_bytes = len(state.backend.system_prompt.encode("utf-8"))
                    break
            conservative_input = _token_estimate(
                len(_canonical_json(request)) + prompt_bytes)
            try:
                response_bytes = len(_canonical_json(raw))
            except AgenticProtocolError:
                response_bytes = _MAX_RESPONSE_BYTES
            conservative_output = _token_estimate(response_bytes)
            for name, current in (
                ("input_tokens", conservative_input),
                ("output_tokens", conservative_output),
            ):
                value = provider_usage.get(name, 0)
                if isinstance(value, int) and not isinstance(value, bool) \
                        and value >= 0:
                    if name == "input_tokens":
                        conservative_input = max(current, value)
                    else:
                        conservative_output = max(current, value)
            return conservative_input, conservative_output

        for state in self.backend_states:
            now = time.monotonic()
            with self.backend_lock:
                if state.open_until > now:
                    continue
                state.calls += 1
            elapsed_total_us = max(
                0, (time.monotonic_ns() - request_started) // 1000)
            remaining_us = self.policy.timeout_ms * 1000 - elapsed_total_us
            estimated_input, _ = attempt_usage({}, {}, state.identity)
            if remaining_us <= 0 or input_tokens_total + estimated_input > \
                    self.policy.max_input_tokens_per_request:
                last_reason = "request_budget_exhausted"
                break
            started = time.monotonic_ns()
            raw: Any = {}
            provider_usage: Mapping[str, Any] = {}
            try:
                raw, provider_usage = state.backend.query_with_metadata(
                    request, remaining_us / 1_000_000.0)
                elapsed_us = max(0, (time.monotonic_ns() - started) // 1000)
                actions, input_tokens, output_tokens, candidate_bytes = (
                    self._validate_response(raw, request, provider_usage))
                conservative_input, conservative_output = attempt_usage(
                    raw, provider_usage, state.identity)
                input_tokens = max(input_tokens, conservative_input)
                output_tokens = max(output_tokens, conservative_output)
            except Exception as error:
                elapsed_us = max(0, (time.monotonic_ns() - started) // 1000)
                input_tokens, output_tokens = attempt_usage(
                    raw, provider_usage, state.identity)
                input_tokens_total = min(
                    self.policy.max_input_tokens_per_request,
                    input_tokens_total + input_tokens,
                )
                output_tokens_total = min(
                    self.policy.max_output_tokens_per_request,
                    output_tokens_total + output_tokens,
                )
                model_time_us_total = min(
                    self.policy.timeout_ms * 1000,
                    model_time_us_total + elapsed_us,
                )
                last_reason = (
                    "invalid_response" if isinstance(error, AgenticProtocolError)
                    else "backend_failure")
                with self.backend_lock:
                    state.failures += 1
                    if state.failures >= self.failure_threshold:
                        state.open_until = now + self.cooldown_seconds
                        state.failures = 0
                continue
            input_tokens_total += input_tokens
            output_tokens_total += output_tokens
            model_time_us_total += elapsed_us
            if input_tokens_total > self.policy.max_input_tokens_per_request or \
                    output_tokens_total > self.policy.max_output_tokens_per_request or \
                    model_time_us_total > self.policy.timeout_ms * 1000:
                last_reason = "request_budget_exhausted"
                break
            with self.backend_lock:
                state.failures = 0
                state.successes += 1
            return {
                "status": "valid",
                "backend": state.identity.mapping(),
                "actions": actions,
                "input_tokens": input_tokens_total,
                "output_tokens": output_tokens_total,
                "candidate_bytes": candidate_bytes,
                "model_time_us": min(
                    model_time_us_total, self.policy.timeout_ms * 1000),
            }
        return {
            "status": "fallback",
            "reason": last_reason,
            "backend": {},
            "actions": [],
            "input_tokens": input_tokens_total,
            "output_tokens": output_tokens_total,
            "candidate_bytes": 0,
            "model_time_us": model_time_us_total,
        }

    def _complete(
        self,
        request_id: str,
        aliases: tuple[str, ...],
        original_task: Mapping[str, Any],
        fallback: Mapping[str, Any],
        reservation: _Reservation,
        result: Mapping[str, Any],
    ) -> StructuredDecision:
        self._release_reservation(reservation)
        normalized = self._normalize_task(original_task)
        episode_id = self._episode_id(normalized)
        schema_valid = result.get("status") == "valid"
        valid = schema_valid
        actions = list(result.get("actions", ())) if schema_valid else []
        input_tokens = _bounded_int(
            result.get("input_tokens", 0), "result input tokens",
            maximum=self.policy.max_input_tokens_per_request)
        output_tokens = _bounded_int(
            result.get("output_tokens", 0), "result output tokens",
            maximum=self.policy.max_output_tokens_per_request)
        model_time_us = _bounded_int(
            result.get("model_time_us", 0), "result model time",
            maximum=self.policy.timeout_ms * 1000)
        cost = self._cost(input_tokens, output_tokens)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.model_time_us += model_time_us
        self.cost_microusd += cost
        fallback_reason = "" if valid else str(
            result.get("reason", "backend_failure"))
        candidate_actions = [
            action for action in actions if action["kind"] == "candidate"]
        candidate_bytes = sum(len(action["data_hex"]) // 2
                              for action in candidate_actions)
        if schema_valid:
            self.proposed_candidate_actions += len(candidate_actions)
            self.proposed_candidate_bytes += candidate_bytes
        if (
            self.candidate_actions + len(candidate_actions) >
                self.policy.max_candidate_actions or
            self.candidate_bytes + candidate_bytes >
                self.policy.max_candidate_bytes
        ):
            fallback_reason = "candidate_budget_exhausted"
            valid = False
        if valid and candidate_actions and (
            not normalized["input_sha256"] or
            not str(original_task.get("input_path", "") or "")
        ):
            fallback_reason = "candidate_source_unbound"
            valid = False
        decision_body = {
            "request_id": request_id,
            "mode": self.policy.mode,
            "backend": result.get("backend", {}),
            "actions": actions,
            "fallback_reason": fallback_reason,
        }
        decision_id = _digest(decision_body)
        if decision_id in self.known_decisions:
            raise AgenticProtocolError("model decision identity is duplicated")
        self.known_decisions.add(decision_id)
        self.decision_sources[decision_id] = normalized["input_sha256"]
        self.decision_episodes[decision_id] = episode_id
        proposals: list[dict[str, Any]] = []
        schedule_hint: dict[str, Any] = {}
        source = "model"
        if valid:
            for index, action in enumerate(actions):
                if action["kind"] == "schedule":
                    schedule_hint = {
                        key: value for key, value in action.items()
                        if key != "kind"
                    }
                    continue
                input_sha = normalized["input_sha256"]
                source_path = str(original_task.get("input_path", "") or "")
                proposal = {
                    "id": _digest({
                        "decision_id": decision_id,
                        "action_index": index,
                        "candidate_sha256": hashlib.sha256(
                            bytes.fromhex(action["data_hex"])).hexdigest(),
                    }),
                    "kind": action["proposal_kind"],
                    "source_path": source_path,
                    "candidate": {"hex": action["data_hex"]},
                    "target_branch": action["target_branch"],
                    "generator": f"structured-agentic:{decision_id}",
                }
                if action["proposal_kind"] == "history_acquisition":
                    proposal["history_seed_id"] = input_sha
                proposals.append(proposal)
        if schema_valid:
            self.valid_responses += 1
        if valid:
            self.candidate_actions += len(proposals)
            accepted_bytes = sum(
                len(item["candidate"]["hex"]) // 2 for item in proposals)
            self.candidate_bytes += accepted_bytes
            candidate_bytes = accepted_bytes
        else:
            proposals = []
            candidate_bytes = 0
        if self.policy.mode == "shadow":
            self.shadow_decisions += 1
            source = "fallback"
            schedule_hint = sanitize_hint(fallback, self.policy.strategy_count)
            proposals = []
        elif not valid:
            source = "fallback"
            schedule_hint = sanitize_hint(fallback, self.policy.strategy_count)
            self.fallbacks += 1
        elif not schedule_hint:
            schedule_hint = sanitize_hint(fallback, self.policy.strategy_count)
            source = "model+fallback" if schedule_hint else "model"
        self.completed += 1
        self._event("request_completed", {
            "request_id": request_id,
            "episode_id": episode_id,
            "decision_id": decision_id,
            "input_sha256": normalized["input_sha256"],
            "mode": self.policy.mode,
            "source": source,
            "backend": result.get("backend", {}),
            "valid_response": bool(schema_valid),
            "action_admitted": bool(valid),
            "fallback_reason": fallback_reason,
            "actions": actions,
            "eligible_hint": schedule_hint,
            "proposed_candidate_actions": len(candidate_actions),
            "proposed_candidate_bytes": sum(
                len(action["data_hex"]) // 2 for action in candidate_actions),
            "candidate_actions": len(proposals),
            "candidate_bytes": candidate_bytes,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model_time_us": model_time_us,
            "cost_microusd": cost,
        })
        return StructuredDecision(
            aliases=aliases,
            request_id=request_id,
            decision_id=decision_id,
            source=source,
            hint=schedule_hint,
            proposals=tuple(proposals),
            fallback_reason=fallback_reason,
            model_actions=tuple(actions),
        )

    def drain(self) -> list[StructuredDecision]:
        completed = self.ready
        self.ready = []
        for request_id, pending in list(self.pending.items()):
            future, aliases, task, fallback, reservation = pending
            if not future.done():
                continue
            del self.pending[request_id]
            try:
                result = future.result()
            except Exception:
                result = {"status": "fallback", "reason": "controller_failure"}
            completed.append(self._complete(
                request_id, aliases, task, fallback, reservation, result))
        return completed

    def record_candidate_admission(
        self, decision_id: str, candidate_sha256: str, proposal_id: str,
    ) -> None:
        if decision_id not in self.known_decisions or \
                not _HEX64.fullmatch(candidate_sha256) or \
                not _HEX64.fullmatch(proposal_id):
            raise AgenticProtocolError("candidate admission is not bound")
        existing = self.candidate_decisions.get(candidate_sha256)
        if existing and existing != decision_id:
            raise AgenticProtocolError(
                "candidate digest is bound to another decision")
        self.candidate_decisions[candidate_sha256] = decision_id
        self._event("candidate_admitted", {
            "decision_id": decision_id,
            "candidate_sha256": candidate_sha256,
            "proposal_id": proposal_id,
        })

    def record_hint_selection(
        self, decision_id: str, task: Mapping[str, Any],
    ) -> None:
        if decision_id not in self.known_decisions or \
                decision_id in self.selected_decisions:
            raise AgenticProtocolError("hint selection is not uniquely bound")
        normalized = self._normalize_task(task)
        source = self.decision_sources.get(decision_id, "")
        if source and normalized["input_sha256"] != source:
            raise AgenticProtocolError("hint selection source does not match decision")
        self.selected_decisions.add(decision_id)
        self._event("hint_selected", {
            "decision_id": decision_id,
            "episode_id": self.decision_episodes[decision_id],
            "task_sha256": _digest(normalized),
        })

    def record_candidate_rejection(
        self, decision_id: str, proposal_id: str, reason: str,
    ) -> None:
        if decision_id not in self.known_decisions or \
                not _HEX64.fullmatch(proposal_id):
            raise AgenticProtocolError("candidate rejection is not bound")
        bounded_reason = _bounded_text(reason, "candidate rejection reason", 128)
        if not bounded_reason:
            raise AgenticProtocolError("candidate rejection reason is empty")
        self._event("candidate_rejected", {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "reason": bounded_reason,
        })

    def decision_for_candidate(self, candidate_sha256: str) -> str:
        return self.candidate_decisions.get(candidate_sha256, "")

    def observe(
        self,
        decision_id: str,
        task: Mapping[str, Any],
        telemetry: Mapping[str, Any] | None,
        result: Mapping[str, Any] | None,
    ) -> None:
        if decision_id not in self.known_decisions:
            raise AgenticProtocolError("execution outcome has unknown decision")
        normalized = self._normalize_task(task)
        input_sha256 = normalized["input_sha256"]
        source = self.decision_sources.get(decision_id, "")
        if source and input_sha256 != source and \
                self.candidate_decisions.get(input_sha256) != decision_id:
            raise AgenticProtocolError(
                "execution outcome source does not match decision")
        episode_id = self.decision_episodes[decision_id]
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        result = result if isinstance(result, Mapping) else {}

        def count(source: Mapping[str, Any], name: str, maximum: int = 1 << 40) -> int:
            value = source.get(name, 0)
            if isinstance(value, bool):
                return 0
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return 0
            return parsed if 0 <= parsed <= maximum else 0

        elapsed = result.get("elapsed", 0.0)
        try:
            elapsed_us = int(float(elapsed) * 1_000_000)
        except (TypeError, ValueError, OverflowError):
            elapsed_us = 0
        if elapsed_us < 0 or elapsed_us > 1 << 60:
            elapsed_us = 0
        summary = {
            "decision_id": decision_id,
            "target_branch": count(
                telemetry, "target_branch", (1 << 63) - 1),
            "target_reached": bool(telemetry.get("target_reached", False)),
            "generated": count(
                telemetry, "generated",
                count(result, "total_generated", 1 << 31)),
            "solver_time_us": count(telemetry, "solver_time_us", 1 << 60),
            "solver_unknown": count(telemetry, "solver_unknown", 1 << 31),
            "coverage_delta": count(result, "coverage_delta", 1 << 31),
            "executed_input_sha256": input_sha256,
            "retcode": count(result, "retcode", 1 << 31),
            "elapsed_us": elapsed_us,
        }
        self.history.setdefault(episode_id, []).append(summary)
        self.history[episode_id] = self.history[episode_id][-8:]
        self._event("execution_outcome", {
            "decision_id": decision_id,
            "episode_id": episode_id,
            "summary": summary,
        })

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.policy.mode,
            "policy_sha256": self.policy_sha256,
            "base_policy_sha256": self.base_policy_sha256,
            "requests": self.requests,
            "completed": self.completed,
            "pending": len(self.pending),
            "valid_responses": self.valid_responses,
            "fallbacks": self.fallbacks,
            "shadow_decisions": self.shadow_decisions,
            "trigger_evaluations": self.trigger_evaluations,
            "triggered_requests": self.triggered_requests,
            "suppressed_requests": self.suppressed_requests,
            "plateau_count": self.plateau_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_time_us": self.model_time_us,
            "cost_microusd": self.cost_microusd,
            "candidate_actions": self.candidate_actions,
            "candidate_bytes": self.candidate_bytes,
            "proposed_candidate_actions": self.proposed_candidate_actions,
            "proposed_candidate_bytes": self.proposed_candidate_bytes,
            "backends": {
                state.identity.name: {
                    "calls": state.calls,
                    "successes": state.successes,
                    "circuit_open": state.open_until > time.monotonic(),
                }
                for state in self.backend_states
            },
        }

    def finish_requests(self) -> list[StructuredDecision]:
        if not self.executor_stopped:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor_stopped = True
        return self.drain()

    def close(self) -> list[StructuredDecision]:
        if self.closed:
            return []
        completed = self.finish_requests()
        self._event("controller_closed", {"snapshot": self.snapshot()})
        self.ledger.close()
        self.closed = True
        return completed


def summarize_ledger(path: str | Path) -> dict[str, Any]:
    """Build a deterministic offline/shadow/ablation summary."""
    records = DecisionLedger.read(path)
    summary: dict[str, Any] = {
        "schema": "symcc-agentic-ablation-summary-v1",
        "policy_sha256": "",
        "base_policy_sha256": "",
        "mode": "",
        "requests": 0,
        "valid_responses": 0,
        "fallbacks": 0,
        "candidate_actions": 0,
        "candidate_bytes": 0,
        "proposed_candidate_actions": 0,
        "proposed_candidate_bytes": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "model_time_us": 0,
        "cost_microusd": 0,
        "outcomes": 0,
        "hint_selections": 0,
        "target_reached": 0,
        "generated": 0,
        "coverage_delta": 0,
        "trigger_evaluations": 0,
        "triggered_requests": 0,
        "suppressed_requests": 0,
        "ledger_tail_sha256": records[-1]["record_sha256"] if records else "",
    }
    for record in records:
        payload = record["payload"]
        if record["event"] == "policy":
            summary["policy_sha256"] = payload["policy_sha256"]
            summary["base_policy_sha256"] = payload["base_policy_sha256"]
            summary["mode"] = payload["policy"]["mode"]
        elif record["event"] == "trigger_evaluated":
            summary["trigger_evaluations"] += 1
            if payload["eligible"]:
                summary["triggered_requests"] += 1
            else:
                summary["suppressed_requests"] += 1
        elif record["event"] == "request_submitted":
            summary["requests"] += 1
        elif record["event"] == "request_completed":
            summary["valid_responses"] += int(bool(payload["valid_response"]))
            summary["fallbacks"] += int(bool(payload["fallback_reason"]))
            for name in (
                "candidate_actions", "candidate_bytes",
                "proposed_candidate_actions", "proposed_candidate_bytes",
                "input_tokens", "output_tokens", "model_time_us", "cost_microusd",
            ):
                summary[name] += int(payload[name])
        elif record["event"] == "fallback_decision":
            summary["fallbacks"] += 1
        elif record["event"] == "hint_selected":
            summary["hint_selections"] += 1
        elif record["event"] == "execution_outcome":
            outcome = payload["summary"]
            summary["outcomes"] += 1
            summary["target_reached"] += int(bool(outcome["target_reached"]))
            summary["generated"] += int(outcome["generated"])
            summary["coverage_delta"] += int(outcome["coverage_delta"])
    summary["summary_sha256"] = _digest(summary)
    return summary


def compare_ablation_ledgers(
    ledgers: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Compare online/shadow/fallback arms only when their task sets match."""
    if not isinstance(ledgers, Mapping) or len(ledgers) < 2:
        raise AgenticProtocolError("ablation comparison requires at least two arms")
    arms: dict[str, dict[str, Any]] = {}
    signatures: dict[str, list[tuple[str, int, str]]] = {}
    base_policy = ""
    for label, path in sorted(ledgers.items()):
        records = DecisionLedger.read(path)
        summary = summarize_ledger(path)
        mode = str(summary["mode"])
        if mode not in {"online", "shadow", "fallback"}:
            raise AgenticProtocolError(f"arm {label!r} has invalid mode")
        if mode in arms:
            raise AgenticProtocolError(f"duplicate ablation mode {mode!r}")
        if base_policy and summary["base_policy_sha256"] != base_policy:
            raise AgenticProtocolError(
                "ablation arms do not share one base policy identity")
        base_policy = str(summary["base_policy_sha256"])
        arm_signatures: list[tuple[str, int, str]] = []
        for record in records:
            if record["event"] not in {"request_submitted", "fallback_decision"}:
                continue
            payload = record["payload"]
            if not {
                "episode_id", "iteration", "task_sha256"
            }.issubset(payload):
                continue
            arm_signatures.append((
                str(payload["episode_id"]),
                _bounded_int(
                    payload["iteration"], "ablation iteration", maximum=1 << 31),
                str(payload["task_sha256"]),
            ))
        arm_signatures.sort()
        arms[mode] = summary
        signatures[mode] = arm_signatures
    reference_mode = sorted(signatures)[0]
    reference = signatures[reference_mode]
    if not reference:
        raise AgenticProtocolError("ablation task set is empty")
    for mode, signature in signatures.items():
        if signature != reference:
            raise AgenticProtocolError(
                f"ablation task set mismatch for {mode!r}")
    comparison: dict[str, Any] = {
        "schema": "symcc-agentic-ablation-comparison-v1",
        "base_policy_sha256": base_policy,
        "task_count": len(reference),
        "task_set_sha256": _digest(reference),
        "arms": arms,
        "deltas_vs_fallback": {},
    }
    fallback = arms.get("fallback")
    if fallback is not None:
        metrics = (
            "target_reached", "generated", "coverage_delta", "model_time_us",
            "input_tokens", "output_tokens", "cost_microusd",
        )
        comparison["deltas_vs_fallback"] = {
            mode: {
                metric: int(summary[metric]) - int(fallback[metric])
                for metric in metrics
            }
            for mode, summary in sorted(arms.items())
            if mode != "fallback"
        }
    comparison["comparison_sha256"] = _digest(comparison)
    return comparison
