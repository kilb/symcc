"""Run-level planning for repeated bounded symbolic-execution campaigns.

The planner treats one bounded execution as its unit of control.  Coverage is
accepted only from concrete replay, target proposals are reviewed before they
become executable plans, and exact run specifications are never dispatched
twice.  The execution engine remains responsible for state selection inside a
run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping, Sequence


STATE_SCHEMA = "symcc-agolic-planner-v1"
PLAN_SCHEMA = "symcc-agolic-run-plan-v1"
OUTCOME_SCHEMA = "symcc-agolic-run-outcome-v1"

_MODES = frozenset({"harness-entry", "witness-guided"})
_STATUSES = frozenset({"complete", "timeout", "error", "cancelled"})
_TARGET_CLASSES = frozenset({
    "new-reach",
    "increased-target-coverage",
    "reached-no-gain",
    "not-reached",
    "unverified",
})
_MAX_STATE_BYTES = 64 * 1024 * 1024
_MAX_COVERAGE_ELEMENTS = 1_000_000
_MAX_TARGETS = 100_000
_MAX_HISTORY = 16_384
_MAX_ISSUED = 4_096
_MAX_ARTIFACTS = 65_536
_MAX_TEXT = 4_096
_MAX_ENVIRONMENT = 256
_MAX_SYMBOLIC_INPUTS = 256
_MAX_TIME_SECONDS = 86_400.0
_MAX_MEMORY_MIB = 1_048_576
_MAX_COLLECTION_TEXT_BYTES = 32 * 1024 * 1024


class AgolicError(ValueError):
    """Base class for planning and evidence admission failures."""


class AgolicStateError(AgolicError):
    """Raised when persisted state is malformed or belongs to another run."""


class AgolicAdmissionError(AgolicError):
    """Raised when a proposed target cannot be executed as specified."""


def _reject_constant(value: str) -> None:
    raise AgolicStateError(f"non-finite JSON number {value!r} is not supported")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgolicStateError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicStateError(f"value is not canonical JSON: {error}") from error
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text(value: Any, name: str, *, required: bool = False,
          limit: int = _MAX_TEXT) -> str:
    if value is None:
        if required:
            raise AgolicAdmissionError(f"{name} must be a string")
        return ""
    if not isinstance(value, str):
        raise AgolicAdmissionError(f"{name} must be a string")
    if "\x00" in value or len(value.encode("utf-8")) > limit:
        raise AgolicAdmissionError(f"{name} is not a bounded UTF-8 string")
    normalized = value.strip()
    if required and not normalized:
        raise AgolicAdmissionError(f"{name} must not be empty")
    return normalized


def _positive_int(value: Any, name: str, *, maximum: int = (1 << 64) - 1,
                  required: bool = True) -> int:
    if isinstance(value, bool):
        raise AgolicAdmissionError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise AgolicAdmissionError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicAdmissionError(f"{name} must be an integer") from error
    if parsed <= 0:
        if required:
            raise AgolicAdmissionError(f"{name} must be positive")
        return 0
    if parsed > maximum:
        raise AgolicAdmissionError(f"{name} exceeds {maximum}")
    return parsed


def _nonnegative_int(value: Any, name: str, *, maximum: int = (1 << 63) - 1,
                     default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise AgolicAdmissionError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise AgolicAdmissionError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicAdmissionError(f"{name} must be an integer") from error
    if parsed < 0 or parsed > maximum:
        raise AgolicAdmissionError(f"{name} is outside 0..{maximum}")
    return parsed


def _finite(value: Any, name: str, *, minimum: float = 0.0,
            maximum: float = _MAX_TIME_SECONDS, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise AgolicAdmissionError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicAdmissionError(f"{name} must be finite") from error
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise AgolicAdmissionError(
            f"{name} is outside {minimum}..{maximum}")
    return parsed


def _sha256(value: Any, name: str, *, required: bool = False) -> str:
    text = _text(value, name, required=required, limit=64)
    if not text:
        return ""
    lowered = text.lower()
    if len(lowered) != 64 or any(char not in "0123456789abcdef" for char in lowered):
        raise AgolicAdmissionError(f"{name} must be a SHA-256 digest")
    return lowered


def _bounded_unique_texts(
    value: Any,
    name: str,
    limit: int,
    *,
    byte_limit: int = _MAX_COLLECTION_TEXT_BYTES,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise AgolicAdmissionError(f"{name} must contain at most {limit} items")
    result: list[str] = []
    seen: set[str] = set()
    consumed = 0
    for item in value:
        normalized = _text(item, name, required=True)
        if normalized not in seen:
            consumed += len(normalized.encode("utf-8"))
            if consumed > byte_limit:
                raise AgolicAdmissionError(
                    f"{name} exceeds its aggregate byte budget")
            result.append(normalized)
            seen.add(normalized)
    return tuple(result)


def _bounded_unique_branches(value: Any, name: str,
                             limit: int = _MAX_COVERAGE_ELEMENTS) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise AgolicAdmissionError(f"{name} must contain at most {limit} items")
    result: list[int] = []
    seen: set[int] = set()
    for item in value:
        branch = _positive_int(item, name)
        if branch not in seen:
            result.append(branch)
            seen.add(branch)
    return tuple(result)


def _bounded_mapping(value: Any, name: str, limit: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > limit:
        raise AgolicAdmissionError(f"{name} must contain at most {limit} entries")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _text(key, f"{name} key", required=True, limit=256)
        normalized_value = _text(item, f"{name}[{normalized_key}]", limit=4096)
        result[normalized_key] = normalized_value
    return result


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], name: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AgolicStateError(
            f"{name} has an invalid shape (missing={missing}, extra={extra})")


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: set[str], name: str,
) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise AgolicAdmissionError(f"{name} contains unknown fields: {extra}")


def _stable_json(path: Path) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AgolicStateError("planner state cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_STATE_BYTES:
            raise AgolicStateError("planner state must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    def identity(item: os.stat_result) -> tuple[int, ...]:
        return (
            item.st_dev, item.st_ino, item.st_mode, item.st_size,
            item.st_mtime_ns, item.st_ctime_ns,
        )
    try:
        public = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise AgolicStateError("planner state disappeared while reading") from error
    if len(encoded) > _MAX_STATE_BYTES or identity(before) != identity(after) \
            or identity(after) != identity(public):
        raise AgolicStateError("planner state identity changed while reading")
    try:
        return json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgolicStateError(f"invalid planner state JSON: {error}") from error


def _atomic_json(path: Path, value: Any) -> None:
    encoded = _canonical_json(value) + b"\n"
    if len(encoded) > _MAX_STATE_BYTES:
        raise AgolicStateError("planner state exceeds the byte budget")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short planner-state write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class CoverageSnapshot:
    """Coverage derived by replaying the current finite corpus."""

    elements: tuple[str, ...] = ()
    branches: tuple[int, ...] = ()
    functions: tuple[str, ...] = ()
    corpus_artifacts: tuple[str, ...] = ()
    replay_identity: str = ""

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        allow_empty_identity: bool = False,
    ) -> "CoverageSnapshot":
        if not isinstance(raw, Mapping):
            raise AgolicAdmissionError("coverage snapshot must be an object")
        artifacts = _bounded_unique_texts(
            raw.get("corpus_artifacts"), "corpus_artifacts", _MAX_ARTIFACTS)
        normalized_artifacts = tuple(
            _sha256(item, "corpus artifact", required=True) for item in artifacts)
        snapshot = cls(
            elements=tuple(sorted(_bounded_unique_texts(
                raw.get("elements"), "coverage elements",
                _MAX_COVERAGE_ELEMENTS))),
            branches=tuple(sorted(_bounded_unique_branches(
                raw.get("branches"), "branches"))),
            functions=tuple(sorted(_bounded_unique_texts(
                raw.get("functions"), "functions", _MAX_TARGETS))),
            corpus_artifacts=tuple(sorted(normalized_artifacts)),
            replay_identity=_sha256(
                raw.get("replay_identity"), "replay_identity",
                required=not allow_empty_identity),
        )
        if not snapshot.replay_identity and any((
                snapshot.elements, snapshot.branches, snapshot.functions,
                snapshot.corpus_artifacts)):
            raise AgolicAdmissionError(
                "non-empty coverage requires a replay identity")
        return snapshot

    def as_dict(self) -> dict[str, Any]:
        return {
            "elements": list(self.elements),
            "branches": list(self.branches),
            "functions": list(self.functions),
            "corpus_artifacts": list(self.corpus_artifacts),
            "replay_identity": self.replay_identity,
        }


@dataclass(frozen=True)
class Witness:
    sha256: str
    path: str
    release_function: str = ""
    release_branch: int = 0
    provenance: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Witness":
        if not isinstance(raw, Mapping):
            raise AgolicAdmissionError("witness must be an object")
        release_function = _text(raw.get("release_function"), "release_function")
        release_branch = _positive_int(
            raw.get("release_branch", 0), "release_branch", required=False)
        if not release_function and not release_branch:
            raise AgolicAdmissionError(
                "witness requires a release function or release branch")
        return cls(
            sha256=_sha256(raw.get("sha256"), "witness sha256", required=True),
            path=_text(raw.get("path"), "witness path", required=True),
            release_function=release_function,
            release_branch=release_branch,
            provenance=_text(raw.get("provenance"), "witness provenance"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "path": self.path,
            "release_function": self.release_function,
            "release_branch": self.release_branch,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class TargetCandidate:
    target_id: str
    target_branch: int
    source_file: str
    function: str
    line: int
    distance: float
    opportunity: str
    modes: tuple[str, ...]
    witnesses: tuple[Witness, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TargetCandidate":
        if not isinstance(raw, Mapping):
            raise AgolicAdmissionError("target must be an object")
        branch = _positive_int(raw.get("target_branch"), "target_branch")
        source_file = _text(raw.get("source_file"), "source_file", required=True)
        function = _text(raw.get("function"), "function", required=True, limit=512)
        line = _positive_int(raw.get("line"), "line", maximum=(1 << 31) - 1)
        modes = _bounded_unique_texts(raw.get("modes", ["harness-entry"]), "modes", 2)
        if not modes or any(mode not in _MODES for mode in modes):
            raise AgolicAdmissionError("target contains an unsupported BSE mode")
        witness_values = raw.get("witnesses", ())
        if not isinstance(witness_values, (list, tuple)) or len(witness_values) > 64:
            raise AgolicAdmissionError("target witnesses must contain at most 64 items")
        witnesses = tuple(Witness.from_mapping(item) for item in witness_values)
        if "witness-guided" in modes and not witnesses:
            modes = tuple(mode for mode in modes if mode != "witness-guided")
        if not modes:
            raise AgolicAdmissionError(
                "target has no executable mode after witness validation")
        target_id = _text(raw.get("target_id"), "target_id", limit=512)
        if not target_id:
            target_id = f"{source_file}:{function}:{line}:{branch}"
        return cls(
            target_id=target_id,
            target_branch=branch,
            source_file=source_file,
            function=function,
            line=line,
            distance=_finite(
                raw.get("distance", _MAX_TIME_SECONDS), "distance",
                maximum=1e12, default=_MAX_TIME_SECONDS),
            opportunity=_text(raw.get("opportunity"), "opportunity"),
            modes=modes,
            witnesses=witnesses,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_branch": self.target_branch,
            "source_file": self.source_file,
            "function": self.function,
            "line": self.line,
            "distance": self.distance,
            "opportunity": self.opportunity,
            "modes": list(self.modes),
            "witnesses": [witness.as_dict() for witness in self.witnesses],
        }


@dataclass(frozen=True)
class RunPlan:
    plan_id: str
    round: int
    target: TargetCandidate
    mode: str
    profile: str
    time_limit_seconds: float
    memory_limit_mib: int
    witness: Witness | None
    environment: dict[str, str]
    symbolic_inputs: dict[str, str]
    rationale: str
    issued_at: float

    def specification(self) -> dict[str, Any]:
        return {
            "target_id": self.target.target_id,
            "target_branch": self.target.target_branch,
            "source_file": self.target.source_file,
            "function": self.target.function,
            "line": self.target.line,
            "mode": self.mode,
            "profile": self.profile,
            "time_limit_seconds": self.time_limit_seconds,
            "memory_limit_mib": self.memory_limit_mib,
            "witness": self.witness.as_dict() if self.witness else None,
            "environment": dict(sorted(self.environment.items())),
            "symbolic_inputs": dict(sorted(self.symbolic_inputs.items())),
        }

    def fingerprint(self) -> str:
        return _digest(self.specification())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "round": self.round,
            "target": self.target.as_dict(),
            "specification": self.specification(),
            "fingerprint": self.fingerprint(),
            "rationale": self.rationale,
            "issued_at": self.issued_at,
        }


@dataclass(frozen=True)
class ReviewDiagnostic:
    index: int
    accepted: bool
    reason: str
    plan_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "accepted": self.accepted,
            "reason": self.reason,
            "plan_id": self.plan_id,
        }


class AgolicRunLevelPlanner:
    """Persistent planner and target-admission boundary between BSE runs.

    One coordinator owns an instance and its state file.  Parallel workers may
    execute admitted plans, but their results must be handed back to the owner
    serially so each replay delta has one unambiguous prior corpus.
    """

    def __init__(
        self,
        state_path: str | os.PathLike[str] | None,
        *,
        experiment_id: str,
        program_id: str,
        program_sha256: str,
        profiles: Mapping[str, Mapping[str, Any]],
        default_time_limit_seconds: float = 240.0,
        default_memory_limit_mib: int = 8192,
        max_history: int = _MAX_HISTORY,
    ) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.experiment_id = _text(
            experiment_id, "experiment_id", required=True, limit=256)
        self.program_id = _text(program_id, "program_id", required=True, limit=256)
        self.program_sha256 = _sha256(
            program_sha256, "program_sha256", required=True)
        if not isinstance(profiles, Mapping) or not 1 <= len(profiles) <= 64:
            raise AgolicAdmissionError("profiles must contain 1..64 entries")
        self.profiles: dict[str, dict[str, Any]] = {}
        for name, raw in profiles.items():
            profile_name = _text(name, "profile name", required=True, limit=128)
            if not isinstance(raw, Mapping):
                raise AgolicAdmissionError("profile must be an object")
            modes = _bounded_unique_texts(raw.get("modes", list(_MODES)), "profile modes", 2)
            if not modes or any(mode not in _MODES for mode in modes):
                raise AgolicAdmissionError("profile contains an unsupported mode")
            self.profiles[profile_name] = {
                "modes": list(modes),
                "environment": _bounded_mapping(
                    raw.get("environment"), "profile environment", _MAX_ENVIRONMENT),
                "symbolic_inputs": _bounded_mapping(
                    raw.get("symbolic_inputs"), "profile symbolic_inputs",
                    _MAX_SYMBOLIC_INPUTS),
                "time_limit_seconds": _finite(
                    raw.get("time_limit_seconds", default_time_limit_seconds),
                    "profile time_limit_seconds", minimum=0.1),
                "memory_limit_mib": _positive_int(
                    raw.get("memory_limit_mib", default_memory_limit_mib),
                    "profile memory_limit_mib", maximum=_MAX_MEMORY_MIB),
            }
        self.default_time_limit_seconds = _finite(
            default_time_limit_seconds, "default_time_limit_seconds", minimum=0.1)
        self.default_memory_limit_mib = _positive_int(
            default_memory_limit_mib, "default_memory_limit_mib",
            maximum=_MAX_MEMORY_MIB)
        self.max_history = _positive_int(
            max_history, "max_history", maximum=_MAX_HISTORY)
        self._state = self._initial_state()
        self._load()

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "experiment_id": self.experiment_id,
            "program_id": self.program_id,
            "program_sha256": self.program_sha256,
            "round": 0,
            "coverage": {
                "elements": [], "branches": [], "functions": [],
                "corpus_artifacts": [], "replay_identity": "",
            },
            "history": [],
            "issued": {},
            "planning_failures": 0,
            "last_planning_failure": "",
            "updated_at": 0.0,
        }

    def _normalize_loaded_state(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or raw.get("schema") != STATE_SCHEMA:
            raise AgolicStateError("planner state schema is unsupported")
        _require_exact_keys(raw, {
            "schema", "experiment_id", "program_id", "program_sha256", "round",
            "coverage", "history", "issued", "planning_failures",
            "last_planning_failure", "updated_at",
        }, "planner state")
        if raw.get("experiment_id") != self.experiment_id \
                or raw.get("program_id") != self.program_id \
                or raw.get("program_sha256") != self.program_sha256:
            raise AgolicStateError("planner state belongs to another experiment")
        snapshot = CoverageSnapshot.from_mapping(
            raw.get("coverage", {}), allow_empty_identity=True)
        history = raw.get("history", [])
        issued = raw.get("issued", {})
        if not isinstance(history, list) or len(history) > self.max_history:
            raise AgolicStateError("planner history exceeds its entry budget")
        if not isinstance(issued, Mapping) or len(issued) > _MAX_ISSUED:
            raise AgolicStateError("issued-plan state exceeds its entry budget")
        normalized_history: list[dict[str, Any]] = []
        completed: set[str] = set()
        completed_fingerprints: set[str] = set()
        highest_round = 0
        for record in history:
            normalized = self._normalize_history_record(record)
            plan_id = normalized["plan"]["plan_id"]
            fingerprint = normalized["plan"]["fingerprint"]
            if plan_id in completed or fingerprint in completed_fingerprints:
                raise AgolicStateError(
                    "history contains a duplicate run specification")
            completed.add(plan_id)
            completed_fingerprints.add(fingerprint)
            highest_round = max(highest_round, normalized["plan"]["round"])
            normalized_history.append(normalized)
        normalized_issued: dict[str, dict[str, Any]] = {}
        issued_fingerprints: set[str] = set()
        for plan_id, plan in issued.items():
            normalized_plan = self._normalize_plan_dict(plan)
            fingerprint = normalized_plan["fingerprint"]
            if plan_id != normalized_plan["plan_id"] or plan_id in completed \
                    or fingerprint in completed_fingerprints \
                    or fingerprint in issued_fingerprints:
                raise AgolicStateError("issued plan identity is inconsistent")
            issued_fingerprints.add(fingerprint)
            highest_round = max(highest_round, normalized_plan["round"])
            normalized_issued[plan_id] = normalized_plan
        round_number = _nonnegative_int(raw.get("round"), "round")
        if round_number < highest_round:
            raise AgolicStateError("planner round precedes a recorded plan")
        return {
            "schema": STATE_SCHEMA,
            "experiment_id": self.experiment_id,
            "program_id": self.program_id,
            "program_sha256": self.program_sha256,
            "round": round_number,
            "coverage": snapshot.as_dict(),
            "history": normalized_history,
            "issued": normalized_issued,
            "planning_failures": _nonnegative_int(
                raw.get("planning_failures"), "planning_failures"),
            "last_planning_failure": _text(
                raw.get("last_planning_failure"), "last_planning_failure",
                limit=2048),
            "updated_at": _finite(
                raw.get("updated_at"), "updated_at", maximum=1e12),
        }

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            os.lstat(self.state_path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise AgolicStateError("planner state cannot be inspected") from error
        try:
            self._state = self._normalize_loaded_state(_stable_json(self.state_path))
        except AgolicAdmissionError as error:
            raise AgolicStateError(f"invalid planner state: {error}") from error

    def _commit(self, candidate: dict[str, Any]) -> None:
        candidate["updated_at"] = time.time()
        if self.state_path is not None:
            _atomic_json(self.state_path, candidate)
        self._state = candidate

    def _normalize_plan_dict(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or raw.get("schema") != PLAN_SCHEMA:
            raise AgolicStateError("issued plan is malformed")
        _require_exact_keys(raw, {
            "schema", "plan_id", "round", "target", "specification",
            "fingerprint", "rationale", "issued_at",
        }, "issued plan")
        plan_id = _sha256(raw.get("plan_id"), "plan_id", required=True)
        fingerprint = _sha256(raw.get("fingerprint"), "fingerprint", required=True)
        specification = raw.get("specification")
        if not isinstance(specification, Mapping):
            raise AgolicStateError("issued plan specification is malformed")
        _require_exact_keys(specification, {
            "target_id", "target_branch", "source_file", "function", "line",
            "mode", "profile", "time_limit_seconds", "memory_limit_mib",
            "witness", "environment", "symbolic_inputs",
        }, "issued plan specification")
        if _digest(specification) != fingerprint:
            raise AgolicStateError("issued plan fingerprint is inconsistent")
        target_raw = raw.get("target", {})
        if not isinstance(target_raw, Mapping):
            raise AgolicStateError("issued plan target is malformed")
        _require_exact_keys(target_raw, {
            "target_id", "target_branch", "source_file", "function", "line",
            "distance", "opportunity", "modes", "witnesses",
        }, "issued plan target")
        target = TargetCandidate.from_mapping(target_raw)
        if target.as_dict() != target_raw:
            raise AgolicStateError("issued plan target is not canonical")
        mode = _text(specification.get("mode"), "mode", required=True, limit=32)
        profile_name = _text(
            specification.get("profile"), "profile", required=True, limit=128)
        if profile_name not in self.profiles:
            raise AgolicStateError("issued plan references an unknown profile")
        profile = self.profiles[profile_name]
        if mode not in target.modes or mode not in profile["modes"]:
            raise AgolicStateError("issued plan mode is not executable")
        witness_raw = specification.get("witness")
        witness = None
        if witness_raw is not None:
            if not isinstance(witness_raw, Mapping):
                raise AgolicStateError("issued plan witness is malformed")
            _require_exact_keys(witness_raw, {
                "sha256", "path", "release_function", "release_branch",
                "provenance",
            }, "issued plan witness")
            witness = Witness.from_mapping(witness_raw)
            if witness not in target.witnesses:
                raise AgolicStateError("issued plan witness was not reviewed")
        if (mode == "witness-guided") != (witness is not None):
            raise AgolicStateError("issued plan witness does not match its mode")
        canonical_specification = {
            "target_id": target.target_id,
            "target_branch": target.target_branch,
            "source_file": target.source_file,
            "function": target.function,
            "line": target.line,
            "mode": mode,
            "profile": profile_name,
            "time_limit_seconds": _finite(
                specification.get("time_limit_seconds"), "time_limit_seconds",
                minimum=0.1),
            "memory_limit_mib": _positive_int(
                specification.get("memory_limit_mib"), "memory_limit_mib",
                maximum=_MAX_MEMORY_MIB),
            "witness": witness.as_dict() if witness else None,
            "environment": dict(sorted(_bounded_mapping(
                specification.get("environment"), "environment",
                _MAX_ENVIRONMENT).items())),
            "symbolic_inputs": dict(sorted(_bounded_mapping(
                specification.get("symbolic_inputs"), "symbolic_inputs",
                _MAX_SYMBOLIC_INPUTS).items())),
        }
        if canonical_specification != specification:
            raise AgolicStateError("issued plan specification is not canonical")
        round_number = _positive_int(raw.get("round"), "round")
        expected_plan_id = _digest({
            "round": round_number,
            "fingerprint": fingerprint,
        })
        if plan_id != expected_plan_id:
            raise AgolicStateError("issued plan ID is inconsistent")
        return RunPlan(
            plan_id=plan_id,
            round=round_number,
            target=target,
            mode=mode,
            profile=profile_name,
            time_limit_seconds=canonical_specification["time_limit_seconds"],
            memory_limit_mib=canonical_specification["memory_limit_mib"],
            witness=witness,
            environment=canonical_specification["environment"],
            symbolic_inputs=canonical_specification["symbolic_inputs"],
            rationale=_text(raw.get("rationale"), "rationale", limit=2048),
            issued_at=_finite(raw.get("issued_at"), "issued_at", maximum=1e12),
        ).as_dict()

    def _normalize_history_record(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise AgolicStateError("history record must be an object")
        _require_exact_keys(raw, {"plan", "outcome"}, "history record")
        plan = self._normalize_plan_dict(raw.get("plan"))
        outcome = raw.get("outcome")
        if not isinstance(outcome, Mapping) or outcome.get("schema") != OUTCOME_SCHEMA:
            raise AgolicStateError("history outcome is malformed")
        _require_exact_keys(outcome, {
            "schema", "plan_id", "status", "replay_verified",
            "replay_identity", "target_class", "target_reached",
            "coverage_delta", "branch_delta", "function_delta",
            "target_coverage_elements", "artifacts", "generated",
            "elapsed_seconds", "cpu_seconds", "solver_time_seconds", "reason",
            "completed_at",
        }, "history outcome")
        if outcome.get("plan_id") != plan["plan_id"]:
            raise AgolicStateError("history outcome has the wrong plan ID")
        target_class = outcome.get("target_class")
        if target_class not in _TARGET_CLASSES:
            raise AgolicStateError("history outcome target class is invalid")
        status = outcome.get("status")
        if status not in _STATUSES:
            raise AgolicStateError("history outcome status is invalid")
        replay_verified = outcome.get("replay_verified")
        target_reached = outcome.get("target_reached")
        if not isinstance(replay_verified, bool) or not isinstance(target_reached, bool):
            raise AgolicStateError("history outcome flags must be boolean")
        replay_identity = _sha256(
            outcome.get("replay_identity"), "replay_identity",
            required=replay_verified)
        normalized = {
            "schema": OUTCOME_SCHEMA,
            "plan_id": plan["plan_id"],
            "status": status,
            "replay_verified": replay_verified,
            "replay_identity": replay_identity,
            "target_class": target_class,
            "target_reached": target_reached,
            "coverage_delta": list(_bounded_unique_texts(
                outcome.get("coverage_delta"), "coverage_delta",
                _MAX_COVERAGE_ELEMENTS)),
            "branch_delta": list(_bounded_unique_branches(
                outcome.get("branch_delta"), "branch_delta")),
            "function_delta": list(_bounded_unique_texts(
                outcome.get("function_delta"), "function_delta", _MAX_TARGETS)),
            "target_coverage_elements": list(_bounded_unique_texts(
                outcome.get("target_coverage_elements"),
                "target_coverage_elements", _MAX_COVERAGE_ELEMENTS)),
            "artifacts": [
                _sha256(item, "artifact", required=True)
                for item in _bounded_unique_texts(
                    outcome.get("artifacts"), "artifacts", _MAX_ARTIFACTS)
            ],
            "generated": _nonnegative_int(outcome.get("generated"), "generated"),
            "elapsed_seconds": _finite(
                outcome.get("elapsed_seconds"), "elapsed_seconds"),
            "cpu_seconds": _finite(outcome.get("cpu_seconds"), "cpu_seconds"),
            "solver_time_seconds": _finite(
                outcome.get("solver_time_seconds"), "solver_time_seconds"),
            "reason": _text(outcome.get("reason"), "reason", limit=2048),
            "completed_at": _finite(
                outcome.get("completed_at"), "completed_at", maximum=1e12),
        }
        if (not replay_verified and (
                target_class != "unverified" or target_reached
                or normalized["coverage_delta"] or normalized["branch_delta"]
                or normalized["function_delta"])) \
                or (replay_verified and target_class == "unverified"):
            raise AgolicStateError("history outcome evidence is inconsistent")
        classified_reach = target_class in {
            "new-reach", "increased-target-coverage", "reached-no-gain"}
        if target_reached != classified_reach:
            raise AgolicStateError(
                "history target reach disagrees with its evidence class")
        if normalized != outcome:
            raise AgolicStateError("history outcome is not canonical")
        return {"plan": plan, "outcome": normalized}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def pending_plans(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(plan) for _, plan in sorted(
            self._state["issued"].items(),
            key=lambda item: (item[1]["round"], item[0]))]

    def planning_context(self) -> dict[str, Any]:
        history = self._state["history"]
        return {
            "schema": "symcc-agolic-planning-context-v1",
            "experiment_id": self.experiment_id,
            "program_id": self.program_id,
            "program_sha256": self.program_sha256,
            "next_round": self._state["round"] + 1,
            "coverage": copy.deepcopy(self._state["coverage"]),
            "history": copy.deepcopy(history[-256:]),
            "pending": self.pending_plans(),
            "profiles": copy.deepcopy(self.profiles),
        }

    def update_replay_coverage(self, snapshot: CoverageSnapshot) -> None:
        candidate = copy.deepcopy(self._state)
        previous = CoverageSnapshot.from_mapping(
            candidate["coverage"], allow_empty_identity=True)
        if previous.replay_identity and snapshot.replay_identity == previous.replay_identity:
            if snapshot != previous:
                raise AgolicAdmissionError(
                    "replay identity was reused for different coverage")
            return
        if not set(previous.elements).issubset(snapshot.elements) \
                or not set(previous.branches).issubset(snapshot.branches) \
                or not set(previous.functions).issubset(snapshot.functions) \
                or not set(previous.corpus_artifacts).issubset(snapshot.corpus_artifacts):
            raise AgolicAdmissionError(
                "replayed campaign coverage must grow monotonically")
        candidate["coverage"] = snapshot.as_dict()
        self._commit(candidate)

    def record_planning_failure(self, reason: str) -> None:
        candidate = copy.deepcopy(self._state)
        failures = _nonnegative_int(
            candidate.get("planning_failures"), "planning_failures")
        candidate["planning_failures"] = min((1 << 63) - 1, failures + 1)
        candidate["last_planning_failure"] = _text(
            reason, "planning failure", limit=2048)
        self._commit(candidate)

    def _completed_fingerprints(self) -> set[str]:
        return {
            str(record["plan"]["fingerprint"])
            for record in self._state["history"]
        }

    def _issued_fingerprints(self) -> set[str]:
        return {
            str(plan["fingerprint"])
            for plan in self._state["issued"].values()
        }

    def _target_stats(self, target_id: str) -> tuple[int, int, int, set[str]]:
        attempts = gains = reaches = 0
        profiles: set[str] = set()
        for record in self._state["history"]:
            plan = record["plan"]
            if plan["target"]["target_id"] != target_id:
                continue
            attempts += 1
            profiles.add(str(plan["specification"]["profile"]))
            outcome = record["outcome"]
            if outcome["target_class"] in {
                    "new-reach", "increased-target-coverage"}:
                gains += 1
            if outcome["target_class"] in {
                    "new-reach", "increased-target-coverage",
                    "reached-no-gain"}:
                reaches += 1
        return attempts, gains, reaches, profiles

    def _target_score(self, target: TargetCandidate) -> tuple[float, str]:
        attempts, gains, reaches, _profiles = self._target_stats(target.target_id)
        distance_score = 1.0 / (1.0 + max(0.0, target.distance))
        opportunity = 0.25 if target.opportunity else 0.0
        witness_bonus = 0.15 if target.witnesses else 0.0
        diversity = 1.0 / (1.0 + attempts)
        evidence = 0.5 * gains + 0.1 * reaches
        score = distance_score + opportunity + witness_bonus + diversity + evidence
        reason = (
            f"uncovered target; distance={target.distance:g}; "
            f"prior_attempts={attempts}; prior_gains={gains}; prior_reaches={reaches}")
        return score, reason

    def _select_mode(self, target: TargetCandidate) -> tuple[str, Witness | None]:
        attempts, gains, reaches, _profiles = self._target_stats(target.target_id)
        if "harness-entry" not in target.modes:
            witness = target.witnesses[attempts % len(target.witnesses)]
            return "witness-guided", witness
        if "witness-guided" in target.modes and target.witnesses \
                and attempts > 0 and gains == 0 and reaches == 0:
            witness = target.witnesses[attempts % len(target.witnesses)]
            return "witness-guided", witness
        return "harness-entry", None

    def _profile_order(self, target: TargetCandidate, mode: str) -> list[str]:
        _attempts, _gains, _reaches, used = self._target_stats(target.target_id)
        compatible = [
            name for name, profile in self.profiles.items()
            if mode in profile["modes"]
        ]
        return sorted(compatible, key=lambda name: (name in used, name))

    def _build_plan(
        self,
        *,
        round_number: int,
        target: TargetCandidate,
        mode: str,
        profile_name: str,
        witness: Witness | None,
        rationale: str,
        overrides: Mapping[str, Any] | None = None,
    ) -> RunPlan:
        profile = self.profiles[profile_name]
        overrides = overrides if isinstance(overrides, Mapping) else {}
        if mode not in target.modes or mode not in profile["modes"]:
            raise AgolicAdmissionError("target/profile does not support the requested mode")
        if mode == "witness-guided" and witness is None:
            raise AgolicAdmissionError("witness-guided mode requires a reviewed witness")
        if mode == "harness-entry" and witness is not None:
            raise AgolicAdmissionError("harness-entry mode must not carry a witness")
        time_limit = _finite(
            overrides.get("time_limit_seconds", profile["time_limit_seconds"]),
            "time_limit_seconds", minimum=0.1)
        memory_limit = _positive_int(
            overrides.get("memory_limit_mib", profile["memory_limit_mib"]),
            "memory_limit_mib", maximum=_MAX_MEMORY_MIB)
        environment = dict(profile["environment"])
        environment.update(_bounded_mapping(
            overrides.get("environment"), "environment", _MAX_ENVIRONMENT))
        symbolic_inputs = dict(profile["symbolic_inputs"])
        symbolic_inputs.update(_bounded_mapping(
            overrides.get("symbolic_inputs"), "symbolic_inputs",
            _MAX_SYMBOLIC_INPUTS))
        shell = {
            "target_id": target.target_id,
            "target_branch": target.target_branch,
            "source_file": target.source_file,
            "function": target.function,
            "line": target.line,
            "mode": mode,
            "profile": profile_name,
            "time_limit_seconds": time_limit,
            "memory_limit_mib": memory_limit,
            "witness": witness.as_dict() if witness else None,
            "environment": dict(sorted(environment.items())),
            "symbolic_inputs": dict(sorted(symbolic_inputs.items())),
        }
        fingerprint = _digest(shell)
        plan_id = _digest({"round": round_number, "fingerprint": fingerprint})
        return RunPlan(
            plan_id=plan_id,
            round=round_number,
            target=target,
            mode=mode,
            profile=profile_name,
            time_limit_seconds=time_limit,
            memory_limit_mib=memory_limit,
            witness=witness,
            environment=environment,
            symbolic_inputs=symbolic_inputs,
            rationale=_text(rationale, "rationale", limit=2048),
            issued_at=time.time(),
        )

    def _proposal_plan(
        self,
        index: int,
        proposal: Mapping[str, Any],
        targets: Mapping[str, TargetCandidate],
        round_number: int,
    ) -> RunPlan:
        _reject_unknown_keys(proposal, {
            "target_id", "mode", "profile", "witness_sha256", "rationale",
            "time_limit_seconds", "memory_limit_mib", "environment",
            "symbolic_inputs",
        }, "proposal")
        target_id = _text(
            proposal.get("target_id"), "target_id", required=True, limit=512)
        target = targets.get(target_id)
        if target is None:
            raise AgolicAdmissionError("proposal target is not in the reviewed frontier")
        if target.target_branch in set(self._state["coverage"]["branches"]):
            raise AgolicAdmissionError("proposal target branch is already covered")
        mode = _text(proposal.get("mode", "harness-entry"), "mode", required=True)
        profile_name = _text(proposal.get("profile"), "profile", required=True, limit=128)
        if profile_name not in self.profiles:
            raise AgolicAdmissionError("proposal profile is unknown")
        witness: Witness | None = None
        if mode == "witness-guided":
            witness_sha = _sha256(
                proposal.get("witness_sha256"), "witness_sha256", required=True)
            witness = next(
                (item for item in target.witnesses if item.sha256 == witness_sha), None)
            if witness is None:
                raise AgolicAdmissionError("proposal witness was not reviewed for the target")
        rationale = _text(
            proposal.get("rationale", f"external proposal {index}"),
            "rationale", limit=2048)
        return self._build_plan(
            round_number=round_number,
            target=target,
            mode=mode,
            profile_name=profile_name,
            witness=witness,
            rationale=rationale,
            overrides=proposal,
        )

    def plan_round(
        self,
        targets: Sequence[Mapping[str, Any] | TargetCandidate],
        *,
        max_runs: int,
        proposals: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[ReviewDiagnostic]]:
        if self._state["issued"]:
            return self.pending_plans(), []
        limit = _positive_int(max_runs, "max_runs", maximum=256)
        capacity = self.max_history - len(self._state["history"])
        if capacity <= 0:
            raise AgolicAdmissionError(
                "planner history capacity is exhausted; archive the campaign state")
        limit = min(limit, capacity)
        if not isinstance(targets, Sequence) or len(targets) > _MAX_TARGETS:
            raise AgolicAdmissionError("target frontier exceeds its entry budget")
        normalized_targets: dict[str, TargetCandidate] = {}
        for value in targets:
            target = value if isinstance(value, TargetCandidate) \
                else TargetCandidate.from_mapping(value)
            if target.target_id in normalized_targets:
                raise AgolicAdmissionError("target frontier contains duplicate target IDs")
            normalized_targets[target.target_id] = target
        round_number = self._state["round"] + 1
        completed = self._completed_fingerprints()
        issued = self._issued_fingerprints()
        plans: list[RunPlan] = []
        diagnostics: list[ReviewDiagnostic] = []
        if proposals is not None:
            if not isinstance(proposals, Sequence) or len(proposals) > 1024:
                raise AgolicAdmissionError("proposal set exceeds its entry budget")
            for index, proposal in enumerate(proposals):
                try:
                    if not isinstance(proposal, Mapping):
                        raise AgolicAdmissionError("proposal must be an object")
                    plan = self._proposal_plan(
                        index, proposal, normalized_targets, round_number)
                    if plan.fingerprint() in completed | issued | {
                            item.fingerprint() for item in plans}:
                        raise AgolicAdmissionError(
                            "exact run specification was already issued")
                    plans.append(plan)
                    diagnostics.append(ReviewDiagnostic(
                        index, True, "admitted", plan.plan_id))
                    if len(plans) >= limit:
                        break
                except AgolicAdmissionError as error:
                    diagnostics.append(ReviewDiagnostic(index, False, str(error)))
        else:
            covered = set(self._state["coverage"]["branches"])
            ranked = sorted(
                (target for target in normalized_targets.values()
                 if target.target_branch not in covered),
                key=lambda target: (-self._target_score(target)[0], target.target_id),
            )
            for target in ranked:
                mode, witness = self._select_mode(target)
                score, rationale = self._target_score(target)
                for profile_name in self._profile_order(target, mode):
                    plan = self._build_plan(
                        round_number=round_number,
                        target=target,
                        mode=mode,
                        profile_name=profile_name,
                        witness=witness,
                        rationale=f"{rationale}; score={score:.9f}",
                    )
                    if plan.fingerprint() not in completed | issued | {
                            item.fingerprint() for item in plans}:
                        plans.append(plan)
                        break
                if len(plans) >= limit:
                    break
        if not plans:
            return [], diagnostics
        candidate = copy.deepcopy(self._state)
        candidate["round"] = round_number
        for plan in plans:
            candidate["issued"][plan.plan_id] = plan.as_dict()
        self._commit(candidate)
        return [plan.as_dict() for plan in plans], diagnostics

    def record_outcome(self, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        normalized_plan_id = _sha256(plan_id, "plan_id", required=True)
        plan = self._state["issued"].get(normalized_plan_id)
        if plan is None:
            raise AgolicAdmissionError("outcome does not match a pending plan")
        if not isinstance(raw, Mapping):
            raise AgolicAdmissionError("run outcome must be an object")
        _reject_unknown_keys(raw, {
            "status", "replay_verified", "replay_identity", "target_reached",
            "coverage_elements", "covered_branches", "covered_functions",
            "corpus_artifacts", "generated_artifacts",
            "target_coverage_elements", "generated", "elapsed_seconds",
            "cpu_seconds", "solver_time_seconds", "reason",
        }, "run outcome")
        status = _text(raw.get("status"), "status", required=True, limit=32)
        if status not in _STATUSES:
            raise AgolicAdmissionError("run outcome status is invalid")
        replay_verified = raw.get("replay_verified") is True
        snapshot = CoverageSnapshot.from_mapping({
            "elements": raw.get("coverage_elements", ()),
            "branches": raw.get("covered_branches", ()),
            "functions": raw.get("covered_functions", ()),
            "corpus_artifacts": raw.get("corpus_artifacts", ()),
            "replay_identity": raw.get("replay_identity"),
        })
        target_elements = _bounded_unique_texts(
            raw.get("target_coverage_elements"), "target_coverage_elements",
            _MAX_COVERAGE_ELEMENTS)
        if not set(target_elements).issubset(snapshot.elements):
            raise AgolicAdmissionError(
                "target coverage must be a subset of replayed coverage")
        generated_artifacts = tuple(
            _sha256(item, "generated artifact", required=True)
            for item in _bounded_unique_texts(
                raw.get("generated_artifacts"), "generated_artifacts",
                _MAX_ARTIFACTS)
        )
        if not set(generated_artifacts).issubset(snapshot.corpus_artifacts):
            raise AgolicAdmissionError(
                "generated artifacts must be present in the replay corpus")
        previous = CoverageSnapshot.from_mapping(
            self._state["coverage"], allow_empty_identity=True)
        previous_elements = set(previous.elements)
        previous_branches = set(previous.branches)
        previous_functions = set(previous.functions)
        previous_artifacts = set(previous.corpus_artifacts)
        if replay_verified:
            if previous.replay_identity == snapshot.replay_identity \
                    and snapshot != previous:
                raise AgolicAdmissionError(
                    "run replay identity was reused for different coverage")
            if not previous_elements.issubset(snapshot.elements) \
                    or not previous_branches.issubset(snapshot.branches) \
                    or not previous_functions.issubset(snapshot.functions) \
                    or not previous_artifacts.issubset(snapshot.corpus_artifacts):
                raise AgolicAdmissionError(
                    "run replay must include the corpus coverage recorded before it")
            delta_elements = sorted(set(snapshot.elements) - previous_elements)
            delta_branches = sorted(set(snapshot.branches) - previous_branches)
            delta_functions = sorted(set(snapshot.functions) - previous_functions)
            target = plan["target"]
            target_reached = target["function"] in snapshot.functions
            claimed_reach = raw.get("target_reached")
            if claimed_reach is not None and claimed_reach is not target_reached:
                raise AgolicAdmissionError(
                    "target reach disagrees with replayed function coverage")
            if target_reached and target["function"] not in previous_functions:
                target_class = "new-reach"
            elif target_reached and set(target_elements) - previous_elements:
                target_class = "increased-target-coverage"
            elif target_reached:
                target_class = "reached-no-gain"
            else:
                target_class = "not-reached"
        else:
            delta_elements = []
            delta_branches = []
            delta_functions = []
            target_class = "unverified"
            target_reached = False
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "plan_id": normalized_plan_id,
            "status": status,
            "replay_verified": replay_verified,
            "replay_identity": snapshot.replay_identity,
            "target_class": target_class,
            "target_reached": target_reached,
            "coverage_delta": delta_elements,
            "branch_delta": delta_branches,
            "function_delta": delta_functions,
            "target_coverage_elements": list(target_elements),
            "artifacts": list(generated_artifacts),
            "generated": _nonnegative_int(
                raw.get("generated", len(generated_artifacts)), "generated"),
            "elapsed_seconds": _finite(
                raw.get("elapsed_seconds"), "elapsed_seconds"),
            "cpu_seconds": _finite(raw.get("cpu_seconds"), "cpu_seconds"),
            "solver_time_seconds": _finite(
                raw.get("solver_time_seconds"), "solver_time_seconds"),
            "reason": _text(raw.get("reason"), "reason", limit=2048),
            "completed_at": time.time(),
        }
        candidate = copy.deepcopy(self._state)
        del candidate["issued"][normalized_plan_id]
        candidate["history"].append({"plan": plan, "outcome": outcome})
        if replay_verified:
            merged = CoverageSnapshot(
                elements=tuple(sorted(previous_elements | set(snapshot.elements))),
                branches=tuple(sorted(previous_branches | set(snapshot.branches))),
                functions=tuple(sorted(previous_functions | set(snapshot.functions))),
                corpus_artifacts=tuple(sorted(
                    previous_artifacts | set(snapshot.corpus_artifacts))),
                replay_identity=snapshot.replay_identity,
            )
            candidate["coverage"] = merged.as_dict()
        self._commit(candidate)
        return copy.deepcopy(outcome)


class AgolicRoundController:
    """Execute the paper's plan/admit/run/replay loop through typed callbacks.

    ``execute_run`` performs only the bounded worker execution and returns its
    artefacts.  ``replay_run`` is invoked serially in completion order; it must
    add only that run's artefacts to the current corpus, concretely replay the
    resulting corpus, and return cumulative coverage plus ``generated_artifacts``.
    The execution callback is also responsible for enforcing the plan's time
    and memory limits, because Python threads cannot preempt an external engine.
    """

    def __init__(
        self,
        planner: AgolicRunLevelPlanner,
        *,
        replay_coverage: Callable[[], Mapping[str, Any]],
        execute_run: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        replay_run: Callable[
            [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
            Mapping[str, Any]],
        preflight: Callable[[Mapping[str, Any]], tuple[bool, str]],
        targets: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
        propose: Callable[
            [Mapping[str, Any]], Sequence[Mapping[str, Any]] | None] | None = None,
        start_continuous: Callable[[], Any] | None = None,
        finalize_continuous: Callable[[Any], None] | None = None,
        max_workers: int = 1,
    ) -> None:
        self.planner = planner
        self.replay_coverage = replay_coverage
        self.execute_run = execute_run
        self.replay_run = replay_run
        self.preflight = preflight
        self.targets = targets
        self.propose = propose
        self.start_continuous = start_continuous
        self.finalize_continuous = finalize_continuous
        self.max_workers = _positive_int(
            max_workers, "max_workers", maximum=256)

    def run(
        self,
        *,
        wall_budget_seconds: float,
        max_rounds: int,
        minimum_round_seconds: float = 1.0,
    ) -> dict[str, Any]:
        budget = _finite(
            wall_budget_seconds, "wall_budget_seconds", minimum=0.1)
        rounds = _positive_int(max_rounds, "max_rounds", maximum=100_000)
        minimum = _finite(
            minimum_round_seconds, "minimum_round_seconds", minimum=0.0)
        started = time.monotonic()
        deadline = started + budget
        continuous = self.start_continuous() if self.start_continuous else None
        completed = rejected = planning_failures = 0
        try:
            for _ in range(rounds):
                if deadline - time.monotonic() < minimum:
                    break
                snapshot = CoverageSnapshot.from_mapping(self.replay_coverage())
                self.planner.update_replay_coverage(snapshot)
                try:
                    context = self.planner.planning_context()
                    frontier = self.targets(context)
                    proposals = self.propose(context) if self.propose else None
                except Exception as error:
                    planning_failures += 1
                    self.planner.record_planning_failure(type(error).__name__)
                    continue
                plans, diagnostics = self.planner.plan_round(
                    frontier, max_runs=self.max_workers, proposals=proposals)
                rejected += sum(not item.accepted for item in diagnostics)
                if not plans:
                    break
                admitted: list[Mapping[str, Any]] = []
                for plan in plans:
                    try:
                        accepted, reason = self.preflight(plan)
                    except Exception as error:
                        accepted, reason = False, type(error).__name__
                    if accepted:
                        admitted.append(plan)
                        continue
                    rejected += 1
                    self.planner.record_outcome(plan["plan_id"], {
                        "status": "error",
                        "replay_verified": False,
                        "replay_identity": snapshot.replay_identity,
                        "reason": f"preflight rejected: {reason}",
                    })
                if not admitted:
                    continue
                with ThreadPoolExecutor(max_workers=len(admitted)) as executor:
                    futures = {
                        executor.submit(self.execute_run, plan): plan
                        for plan in admitted
                    }
                    for future in as_completed(futures):
                        plan = futures[future]
                        try:
                            run_result = future.result()
                            if not isinstance(run_result, Mapping):
                                raise AgolicAdmissionError(
                                    "execute_run must return an object")
                            outcome = self.replay_run(
                                plan, run_result,
                                self.planner.snapshot()["coverage"],
                            )
                        except Exception as error:
                            outcome = {
                                "status": "error",
                                "replay_verified": False,
                                "replay_identity": snapshot.replay_identity,
                                "reason": type(error).__name__,
                            }
                        self.planner.record_outcome(plan["plan_id"], outcome)
                        completed += 1
        finally:
            if self.finalize_continuous:
                self.finalize_continuous(continuous)
            final_snapshot = CoverageSnapshot.from_mapping(self.replay_coverage())
            self.planner.update_replay_coverage(final_snapshot)
        return {
            "schema": "symcc-agolic-campaign-summary-v1",
            "experiment_id": self.planner.experiment_id,
            "program_id": self.planner.program_id,
            "completed_runs": completed,
            "rejected_targets": rejected,
            "planning_failures": planning_failures,
            "rounds": self.planner.snapshot()["round"],
            "elapsed_seconds": time.monotonic() - started,
            "coverage": self.planner.snapshot()["coverage"],
            "history_size": len(self.planner.snapshot()["history"]),
            "pending_runs": len(self.planner.snapshot()["issued"]),
        }
