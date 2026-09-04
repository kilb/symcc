#!/usr/bin/env python3
"""Versioned online delivery for empirical solver value profiles."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Mapping

from empirical_value_profile import (
    MAX_INPUT_BYTES,
    MAX_INPUT_FILES,
    ONLINE_PROFILE_SCHEMA,
    aggregate_value_profiles,
    apply_online_admission_policy,
    materialize_runtime_profile,
    normalize_value_profile_telemetry,
    verify_value_profile,
)


STATE_SCHEMA = "symcc-online-value-profile-state-v1"
MAX_RUNTIME_BYTES = 4 * 1024 * 1024
MAX_GENERATIONS = 8
UINT64_MAX = (1 << 64) - 1
CONSUMPTION_FIELDS = (
    "empirical_domain_profiles_loaded",
    "empirical_domain_context_skips",
    "empirical_domain_parse_failures",
    "empirical_domain_attempts",
    "empirical_domain_prefilter_rejects",
    "empirical_domain_solver_queries",
    "empirical_domain_solver_time_us",
    "empirical_domain_sat",
    "empirical_domain_validated",
    "empirical_domain_validation_failures",
    "empirical_domain_unsat_fallbacks",
    "empirical_domain_unknown_fallbacks",
)


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lower, min(upper, parsed))


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(byte in "0123456789abcdef" for byte in value)
    )


def _normalize_document(raw: Any) -> dict[str, Any] | None:
    """Bound an MPI telemetry document before retaining it in coordinator state."""
    normalized = normalize_value_profile_telemetry(raw)
    if (
        normalized is None
        or (
            not normalized["empirical_value_profiles"]
            and not normalized["empirical_domain_feedback"]
        )
    ):
        return None
    return normalized


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _runtime_semantics(content: bytes) -> bytes:
    """Ignore the evidence digest when deciding whether solver domains changed."""
    lines = content.splitlines(keepends=True)
    if len(lines) < 4 or not lines[1].startswith(b"artifact_sha256 "):
        return content
    return b"".join((lines[0], *lines[2:]))


@dataclass(frozen=True)
class PublishedValueProfile:
    version: str
    content: bytes
    artifact_sha256: str
    profile_count: int
    suppressed_count: int = 0


class OnlineValueProfileCoordinator:
    """Aggregate worker telemetry and publish immutable runtime generations."""

    def __init__(
        self,
        directory: str | Path,
        *,
        window: int = 256,
        min_observations: int = 8,
        max_distinct_values: int = 4,
        publish_interval_seconds: float = 1.0,
        feedback_min_solver_queries: int = 8,
        feedback_min_validated_ratio_ppm: int = 125_000,
        feedback_min_solver_time_us: int = 1_000,
    ) -> None:
        self.directory = Path(directory)
        self.generations = self.directory / "generations"
        self.state_path = self.directory / "state.json"
        self.runtime_path = self.directory / "current.runtime"
        self.window = _bounded_int(window, 256, 1, MAX_INPUT_FILES)
        self.min_observations = _bounded_int(
            min_observations, 8, 1, (1 << 64) - 1)
        self.max_distinct_values = _bounded_int(
            max_distinct_values, 4, 1, 64)
        self.feedback_min_solver_queries = _bounded_int(
            feedback_min_solver_queries, 8, 1, UINT64_MAX)
        self.feedback_min_validated_ratio_ppm = _bounded_int(
            feedback_min_validated_ratio_ppm, 125_000, 0, 1_000_000)
        self.feedback_min_solver_time_us = _bounded_int(
            feedback_min_solver_time_us, 1_000, 0, UINT64_MAX)
        try:
            interval = float(publish_interval_seconds)
        except (TypeError, ValueError, OverflowError):
            interval = 1.0
        self.publish_interval_seconds = max(0.0, min(3600.0, interval))
        self.records: deque[dict[str, Any]] = deque(maxlen=self.window)
        self.dirty = False
        self.last_publish = 0.0
        self.current: PublishedValueProfile | None = None
        self.records_observed = 0
        self.generations_published = 0
        self.semantic_noops = 0
        self.publication_failures = 0
        self.checkpoint_truncations = 0
        self.checkpoint_failures = 0
        self.recovery_replays = 0
        self.recovery_replay_mismatches = 0
        self.suppression_generations = 0
        self.profiles_suppressed = 0
        self.consumption = {name: 0 for name in CONSUMPTION_FIELDS}
        self._state_loaded = False
        self._state_artifact_sha256: str | None = None
        self._runtime_policy_mismatch = False
        self._load_state()
        self._load_runtime()
        if (
            self._state_artifact_sha256
            and self.current is None
        ):
            self.dirty = True
        if self.records and self.current is None:
            # A prior policy may have produced no sidecar. Re-evaluate the
            # recovered evidence so a changed policy cannot miss an admission.
            self.dirty = True
        if self._state_loaded and self.current is not None:
            self._reconcile_recovered_runtime()

    def _load_state(self) -> None:
        try:
            with self.state_path.open("rb") as stream:
                encoded = stream.read(MAX_INPUT_BYTES + 1)
            if len(encoded) > MAX_INPUT_BYTES:
                return
            state = json.loads(encoded.decode("ascii"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return
        if not isinstance(state, Mapping) or state.get("schema") != STATE_SCHEMA:
            return
        self._state_loaded = True
        persisted_artifact = state.get("current_artifact_sha256")
        if persisted_artifact == "" or _valid_sha256(persisted_artifact):
            self._state_artifact_sha256 = persisted_artifact
        else:
            # Legacy, malformed, or partially written policy state must be
            # reconciled with the separately published runtime generation.
            self.dirty = True
        self.records_observed = _bounded_int(
            state.get("records_observed"), 0, 0, UINT64_MAX)
        self.generations_published = _bounded_int(
            state.get("generations_published"), 0, 0, UINT64_MAX)
        self.semantic_noops = _bounded_int(
            state.get("semantic_noops"), 0, 0, UINT64_MAX)
        self.publication_failures = _bounded_int(
            state.get("publication_failures"), 0, 0, UINT64_MAX)
        self.checkpoint_truncations = _bounded_int(
            state.get("checkpoint_truncations"), 0, 0, UINT64_MAX)
        self.checkpoint_failures = _bounded_int(
            state.get("checkpoint_failures"), 0, 0, UINT64_MAX)
        self.recovery_replays = _bounded_int(
            state.get("recovery_replays"), 0, 0, UINT64_MAX)
        self.recovery_replay_mismatches = _bounded_int(
            state.get("recovery_replay_mismatches"), 0, 0, UINT64_MAX)
        records_complete = state.get("records_complete")
        if (
            records_complete is False
            or (
                records_complete is None
                and self.checkpoint_truncations > 0
            )
        ):
            # The current runtime was derived from a larger in-memory window.
            # Rebuild it from the records that actually survived recovery
            # before another work lease can reuse a stale admission decision.
            self.dirty = True
        self.suppression_generations = _bounded_int(
            state.get("suppression_generations"), 0, 0, UINT64_MAX)
        self.profiles_suppressed = _bounded_int(
            state.get("profiles_suppressed"), 0, 0, UINT64_MAX)
        consumption = state.get("consumption")
        if isinstance(consumption, Mapping):
            for name in CONSUMPTION_FIELDS:
                self.consumption[name] = _bounded_int(
                    consumption.get(name), 0, 0, UINT64_MAX)
        persisted_window = _bounded_int(
            state.get("window"), self.window, 1, MAX_INPUT_FILES)
        if persisted_window != self.window:
            # The retained sample set defines the aggregate. Re-materialize
            # before dispatching work when that policy changes across restart.
            self.dirty = True
        records = state.get("records")
        if not isinstance(records, list):
            self.dirty = True
            return
        for raw in records[-self.window:]:
            normalized = _normalize_document(raw)
            if normalized is None:
                self.dirty = True
                continue
            self.records.append(normalized)

    def _load_runtime(self) -> None:
        try:
            content = self.runtime_path.read_bytes()
        except OSError:
            return
        if not content or len(content) > MAX_RUNTIME_BYTES:
            return
        lines = content.splitlines()
        if len(lines) < 4 or not lines[1].startswith(b"artifact_sha256 "):
            return
        artifact_sha = lines[1].split(maxsplit=1)[1].decode(
            "ascii", errors="ignore")
        if not _valid_sha256(artifact_sha):
            return
        try:
            profile_count = int(lines[3].split(maxsplit=1)[1])
        except (IndexError, ValueError):
            return
        if profile_count < 0:
            return
        artifact_path = self.generations / f"{artifact_sha}.json"
        try:
            with artifact_path.open("rb") as stream:
                encoded = stream.read(MAX_INPUT_BYTES + 1)
            if len(encoded) > MAX_INPUT_BYTES:
                return
            artifact = json.loads(encoded.decode("ascii"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return
        if (
            not isinstance(artifact, Mapping)
            or not verify_value_profile(artifact)
            or artifact.get("profile_sha256") != artifact_sha
            or materialize_runtime_profile(artifact) != content
        ):
            return
        online_admission = artifact.get("online_admission")
        suppressed_count = (
            len(online_admission.get("suppressed", ()))
            if isinstance(online_admission, Mapping) else 0
        )
        self.current = PublishedValueProfile(
            version=hashlib.sha256(content).hexdigest(),
            content=content,
            artifact_sha256=artifact_sha,
            profile_count=profile_count,
            suppressed_count=suppressed_count,
        )
        if (
            not self._state_loaded
            or self._state_artifact_sha256 != artifact_sha
        ):
            self.dirty = True
        self._runtime_policy_mismatch = (
            artifact.get("schema") != ONLINE_PROFILE_SCHEMA
            or artifact.get("min_observations") != self.min_observations
            or artifact.get("max_distinct_values") != self.max_distinct_values
            or not isinstance(online_admission, Mapping)
            or online_admission.get("min_solver_queries")
            != self.feedback_min_solver_queries
            or online_admission.get("min_validated_ratio_ppm")
            != self.feedback_min_validated_ratio_ppm
            or online_admission.get("min_solver_time_us")
            != self.feedback_min_solver_time_us
        )
        if self._runtime_policy_mismatch:
            # A verified runtime can still be stale with respect to a new
            # admission policy. Keep it only as the predecessor so publish()
            # can emit a versioned replacement or an explicit tombstone.
            self.dirty = True

    def _build_candidate(self) -> tuple[dict[str, Any], bytes]:
        artifact = aggregate_value_profiles(
            self.records,
            min_observations=self.min_observations,
            max_distinct_values=self.max_distinct_values,
        )
        artifact = apply_online_admission_policy(
            artifact,
            self.records,
            min_solver_queries=self.feedback_min_solver_queries,
            min_validated_ratio_ppm=(
                self.feedback_min_validated_ratio_ppm),
            min_solver_time_us=self.feedback_min_solver_time_us,
        )
        return artifact, materialize_runtime_profile(artifact)

    def _reconcile_recovered_runtime(self) -> None:
        """Replay recovered records before trusting a separately stored sidecar."""
        self.recovery_replays = min(UINT64_MAX, self.recovery_replays + 1)
        try:
            _, runtime = self._build_candidate()
            matches = _runtime_semantics(runtime) == _runtime_semantics(
                self.current.content)
        except (KeyError, TypeError, ValueError, OverflowError):
            matches = False
        if not matches:
            self.recovery_replay_mismatches = min(
                UINT64_MAX, self.recovery_replay_mismatches + 1)
            self.dirty = True

    def observe(self, telemetry: Any) -> bool:
        normalized = _normalize_document(telemetry)
        if normalized is None:
            return False
        self.records.append(normalized)
        self.records_observed = min(UINT64_MAX, self.records_observed + 1)
        if isinstance(telemetry, Mapping):
            for name in CONSUMPTION_FIELDS:
                increment = _bounded_int(
                    telemetry.get(name), 0, 0, UINT64_MAX)
                self.consumption[name] = min(
                    UINT64_MAX, self.consumption[name] + increment)
        self.dirty = True
        return True

    def observe_many(self, telemetry: Iterable[Any]) -> int:
        return sum(1 for document in telemetry if self.observe(document))

    def _checkpoint(self) -> bool:
        persisted_records = list(self.records)
        state = {
            "schema": STATE_SCHEMA,
            "window": self.window,
            "records": persisted_records,
            "records_complete": True,
            "current_artifact_sha256": (
                self.current.artifact_sha256
                if self.current is not None else ""
            ),
            "records_observed": self.records_observed,
            "generations_published": self.generations_published,
            "semantic_noops": self.semantic_noops,
            "publication_failures": self.publication_failures,
            "checkpoint_truncations": self.checkpoint_truncations,
            "checkpoint_failures": self.checkpoint_failures,
            "recovery_replays": self.recovery_replays,
            "recovery_replay_mismatches": self.recovery_replay_mismatches,
            "suppression_generations": self.suppression_generations,
            "profiles_suppressed": self.profiles_suppressed,
            "consumption": self.consumption,
        }
        while True:
            encoded = json.dumps(
                state,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            if len(encoded) <= MAX_INPUT_BYTES:
                break
            if len(persisted_records) <= 1:
                return False
            drop = max(1, len(persisted_records) // 4)
            persisted_records = persisted_records[drop:]
            self.checkpoint_truncations = min(
                UINT64_MAX, self.checkpoint_truncations + drop)
            state["records"] = persisted_records
            state["records_complete"] = False
            state["checkpoint_truncations"] = self.checkpoint_truncations
        try:
            _atomic_write(self.state_path, encoded + b"\n")
        except OSError:
            return False
        return True

    def _checkpoint_or_retry(self) -> bool:
        if self._checkpoint():
            return True
        self.checkpoint_failures = min(
            UINT64_MAX, self.checkpoint_failures + 1)
        self.dirty = True
        return False

    def _prune_generations(self) -> None:
        try:
            artifacts = sorted(
                self.generations.glob("*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        current_name = (
            f"{self.current.artifact_sha256}.json"
            if self.current is not None else "")
        retained = 1 if current_name else 0
        for artifact in artifacts:
            if artifact.name == current_name:
                continue
            if retained < MAX_GENERATIONS:
                retained += 1
                continue
            try:
                artifact.unlink()
            except OSError:
                pass

    def publish(
        self, *, now: float | None = None, force: bool = False
    ) -> PublishedValueProfile | None:
        if not self.dirty:
            return None
        timestamp = time.monotonic() if now is None else float(now)
        if (
            not force
            and timestamp - self.last_publish < self.publish_interval_seconds
        ):
            return None
        artifact, runtime = self._build_candidate()
        self.dirty = False
        self.last_publish = timestamp
        if self.current is None and b"profile_count 0\n" in runtime:
            self._checkpoint_or_retry()
            return None
        if (
            self.current is not None
            and _runtime_semantics(runtime) == _runtime_semantics(
                self.current.content)
            and not self._runtime_policy_mismatch
        ):
            self.semantic_noops = min(UINT64_MAX, self.semantic_noops + 1)
            self._checkpoint_or_retry()
            return None

        artifact_sha = str(artifact["profile_sha256"])
        artifact_content = (
            json.dumps(artifact, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n"
        ).encode("ascii")
        artifact_path = self.generations / f"{artifact_sha}.json"
        try:
            _atomic_write(artifact_path, artifact_content)
            _atomic_write(self.runtime_path, runtime)
        except OSError:
            self.publication_failures = min(
                UINT64_MAX, self.publication_failures + 1)
            self.dirty = True
            self._checkpoint_or_retry()
            return None
        try:
            runtime_profile_count = int(
                runtime.splitlines()[3].split(maxsplit=1)[1])
        except (IndexError, ValueError):
            self.publication_failures = min(
                UINT64_MAX, self.publication_failures + 1)
            self.dirty = True
            self._checkpoint_or_retry()
            return None
        suppressed_count = len(
            artifact["online_admission"]["suppressed"])
        published = PublishedValueProfile(
            version=hashlib.sha256(runtime).hexdigest(),
            content=runtime,
            artifact_sha256=artifact_sha,
            profile_count=runtime_profile_count,
            suppressed_count=suppressed_count,
        )
        self.current = published
        self._runtime_policy_mismatch = False
        self.generations_published = min(
            UINT64_MAX, self.generations_published + 1)
        if suppressed_count:
            self.suppression_generations = min(
                UINT64_MAX, self.suppression_generations + 1)
            self.profiles_suppressed = min(
                UINT64_MAX, self.profiles_suppressed + suppressed_count)
        self._checkpoint_or_retry()
        self._prune_generations()
        return published

    def snapshot(self) -> dict[str, Any]:
        return {
            "dirty": self.dirty,
            "records_observed": self.records_observed,
            "window_records": len(self.records),
            "generations_published": self.generations_published,
            "semantic_noops": self.semantic_noops,
            "publication_failures": self.publication_failures,
            "checkpoint_truncations": self.checkpoint_truncations,
            "checkpoint_failures": self.checkpoint_failures,
            "recovery_replays": self.recovery_replays,
            "recovery_replay_mismatches": self.recovery_replay_mismatches,
            "suppression_generations": self.suppression_generations,
            "profiles_suppressed": self.profiles_suppressed,
            "current_version": (
                self.current.version if self.current is not None else ""),
            "current_profiles": (
                self.current.profile_count if self.current is not None else 0),
            "current_suppressed": (
                self.current.suppressed_count
                if self.current is not None else 0),
            **self.consumption,
        }


def value_profile_update_payload(
    profile: PublishedValueProfile | None,
    worker_version: str,
) -> dict[str, Any]:
    """Build a versioned MPI payload, eliding bytes already cached by a worker."""
    if profile is None:
        return {}
    payload: dict[str, Any] = {
        "empirical_profile_version": profile.version,
    }
    if worker_version != profile.version:
        payload["empirical_profile_content"] = profile.content
    return payload


def install_value_profile_update(
    message: Mapping[str, Any],
    destination: str | Path,
    current_version: str,
) -> tuple[str, bool]:
    """Install one master generation; never enable stale or unverified bytes."""
    version = message.get("empirical_profile_version")
    if not _valid_sha256(version):
        return "", False
    destination_path = Path(destination)
    if current_version == version:
        try:
            with destination_path.open("rb") as stream:
                cached_content = stream.read(MAX_RUNTIME_BYTES + 1)
        except OSError:
            cached_content = b""
        if (
            cached_content
            and len(cached_content) <= MAX_RUNTIME_BYTES
            and hashlib.sha256(cached_content).hexdigest() == version
        ):
            return current_version, True
    content = message.get("empirical_profile_content")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_RUNTIME_BYTES
        or hashlib.sha256(content).hexdigest() != version
    ):
        return "", False
    try:
        _atomic_write(destination_path, content)
    except OSError:
        return "", False
    return version, True
