"""Crash-recoverable, generation-fenced frontier for live symbolic states."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import os
import re
import stat
import threading
import time
from typing import Any, Mapping, Sequence

from distributed_state import (
    bounded_advisory_lock,
    durable_makedirs,
    durable_replace,
    fsync_directory,
)
from live_state_search import LiveStateSearchPolicy


FRONTIER_SCHEMA = "symcc-persistent-live-state-frontier-v1"
_ENVELOPE_SCHEMA = "symcc-persistent-live-state-frontier-envelope-v1"
_LEASE_ENVELOPE_SCHEMA = "symcc-live-state-frontier-lease-envelope-v1"
_TRANSITION_ENVELOPE_SCHEMA = (
    "symcc-persistent-live-state-frontier-transition-envelope-v1"
)
_DIGEST_LENGTH = 64
_MAX_GENERATION = (1 << 63) - 1
_MAX_OWNER_BYTES = 256
_MAX_WORKER = 1_000_000
_DEFAULT_MAX_STATES = 100_000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_COMPACTION_RECORDS = 64
_MAX_COMPACTION_RECORDS = 1024
_MAX_TRANSITION_BYTES = 4 * 1024 * 1024
_MAX_RECOVERY_BATCH = 64


def _digest(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} is not a SHA-256 identity")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} is outside {minimum}..{maximum}")
    return result


def _timestamp(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite and non-negative") from error
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _owner(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("frontier owner must be a string")
    result = value.strip()
    if (
        not result
        or "\x00" in result
        or len(result.encode("utf-8")) > _MAX_OWNER_BYTES
    ):
        raise ValueError("frontier owner is invalid")
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


@dataclass(frozen=True)
class LiveStateFrontierLease:
    checkpoint_id: str
    token: str
    owner: str
    worker: int
    expires: float
    claim_generation: int


@dataclass(frozen=True)
class LiveStateFrontierSnapshot:
    root_checkpoint: str
    program_root: str
    generation: int
    ready: tuple[str, ...]
    leases: tuple[LiveStateFrontierLease, ...]
    done: tuple[str, ...]
    search: dict[str, Any]

    def lease_for(self, checkpoint_id: str) -> LiveStateFrontierLease | None:
        return next(
            (lease for lease in self.leases
             if lease.checkpoint_id == checkpoint_id),
            None,
        )

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": FRONTIER_SCHEMA,
            "root_checkpoint": self.root_checkpoint,
            "program_root": self.program_root,
            "generation": self.generation,
            "ready": len(self.ready),
            "leased": len(self.leases),
            "done": len(self.done),
        }


@dataclass(frozen=True)
class LiveStateFrontierUpdate:
    status: str
    snapshot: LiveStateFrontierSnapshot


class PersistentLiveStateFrontier:
    """Atomically publish ready/leased/done state with stale-result fencing."""

    def __init__(
        self,
        root: str,
        *,
        max_states: int = _DEFAULT_MAX_STATES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        lease_ttl: float = 300.0,
        lock_timeout: float = 60.0,
        compaction_records: int = _DEFAULT_COMPACTION_RECORDS,
        recovery_batch: int = _MAX_RECOVERY_BATCH,
    ) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise TypeError("frontier root must be a filesystem path")
        self.root = os.path.abspath(os.fspath(root))
        self.max_states = _integer(
            max_states, "frontier state limit", 1, _DEFAULT_MAX_STATES,
        )
        self.max_bytes = _integer(
            max_bytes, "frontier byte limit", 4096, _DEFAULT_MAX_BYTES,
        )
        self.lease_ttl = _timestamp(lease_ttl, "frontier lease TTL")
        if self.lease_ttl < 0.1 or self.lease_ttl > 86_400.0:
            raise ValueError("frontier lease TTL is outside 0.1..86400 seconds")
        self.lock_timeout = _timestamp(
            lock_timeout, "frontier lock timeout",
        )
        if self.lock_timeout <= 0.0 or self.lock_timeout > 3600.0:
            raise ValueError("frontier lock timeout is outside 0..3600 seconds")
        self.compaction_records = _integer(
            compaction_records,
            "frontier compaction record count",
            4,
            _MAX_COMPACTION_RECORDS,
        )
        self.recovery_batch = _integer(
            recovery_batch,
            "frontier recovery batch",
            1,
            _MAX_RECOVERY_BATCH,
        )
        durable_makedirs(self.root)
        self.path = os.path.join(self.root, "frontier.json")
        self.lock_path = os.path.join(self.root, "frontier.lock")
        self.transition_root = os.path.join(self.root, "frontier-transitions")
        self.lease_root = os.path.join(self.root, "lease-heartbeats")
        self.lease_lock_root = os.path.join(self.root, "lease-locks")
        durable_makedirs(self.lease_root)
        durable_makedirs(self.lease_lock_root)
        durable_makedirs(self.transition_root)
        self._cached_snapshot: LiveStateFrontierSnapshot | None = None
        self._cached_base_identity: tuple[int, ...] | None = None
        self._cached_transition_identities: dict[int, tuple[int, ...]] = {}
        self._cached_snapshot_bytes: int | None = None
        self._maintenance_failures = 0
        self._last_maintenance_error = ""
        self._expiry_index_lock = threading.Lock()
        self._lease_expiry_hints: dict[str, tuple[str, float]] = {}
        self._lease_expiry_heap: list[tuple[float, str, str]] = []

    def _lock(self):
        return bounded_advisory_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            description="persistent live-state frontier",
        )

    def _lease_paths(self, checkpoint_id: str) -> tuple[str, str]:
        checkpoint_id = _digest(checkpoint_id, "frontier lease checkpoint")
        state_dir = os.path.join(self.lease_root, checkpoint_id[:2])
        lock_dir = os.path.join(self.lease_lock_root, checkpoint_id[:2])
        durable_makedirs(state_dir)
        durable_makedirs(lock_dir)
        return (
            os.path.join(state_dir, f"{checkpoint_id}.json"),
            os.path.join(lock_dir, f"{checkpoint_id}.lock"),
        )

    def _lease_lock(self, checkpoint_id: str):
        _state_path, lock_path = self._lease_paths(checkpoint_id)
        return bounded_advisory_lock(
            lock_path,
            timeout=self.lock_timeout,
            description="persistent live-state frontier lease",
        )

    @staticmethod
    def _lease_payload(lease: LiveStateFrontierLease) -> dict[str, Any]:
        return {
            "checkpoint_id": lease.checkpoint_id,
            "token": lease.token,
            "owner": lease.owner,
            "worker": lease.worker,
            "expires": lease.expires,
            "claim_generation": lease.claim_generation,
        }

    @staticmethod
    def _same_lease_identity(
        left: LiveStateFrontierLease,
        right: LiveStateFrontierLease,
    ) -> bool:
        return (
            left.checkpoint_id == right.checkpoint_id
            and left.token == right.token
            and left.owner == right.owner
            and left.worker == right.worker
            and left.claim_generation == right.claim_generation
        )

    @classmethod
    def _lease_is_current(
        cls,
        authoritative: LiveStateFrontierLease,
        presented: LiveStateFrontierLease,
        timestamp: float,
    ) -> bool:
        return (
            cls._same_lease_identity(authoritative, presented)
            and authoritative.expires >= presented.expires
            and authoritative.expires > timestamp
        )

    def _write_lease_state(self, lease: LiveStateFrontierLease) -> None:
        payload = self._lease_payload(lease)
        envelope = {
            "schema": _LEASE_ENVELOPE_SCHEMA,
            "payload": payload,
            "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        }
        content = _canonical(envelope) + b"\n"
        if len(content) > 4096:
            raise ValueError("frontier lease heartbeat exceeds its byte budget")
        path, _lock_path = self._lease_paths(lease.checkpoint_id)
        temporary = (
            f"{path}.{os.getpid()}.{time.time_ns()}.{os.urandom(8).hex()}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        published = False
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short frontier lease write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            durable_replace(temporary, path)
            published = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _read_lease_state(
        self,
        baseline: LiveStateFrontierLease,
        *,
        strict_fence: bool = True,
    ) -> LiveStateFrontierLease:
        path, _lock_path = self._lease_paths(baseline.checkpoint_id)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("O_NOFOLLOW is required for frontier lease recovery")
        try:
            descriptor = os.open(path, flags | nofollow)
        except FileNotFoundError:
            return baseline
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4096:
                raise ValueError("persistent frontier lease size is invalid")
            content = os.read(descriptor, metadata.st_size + 1)
            final = os.fstat(descriptor)
            if (
                len(content) != metadata.st_size
                or (metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns, metadata.st_ctime_ns)
                != (final.st_dev, final.st_ino, final.st_size,
                    final.st_mtime_ns, final.st_ctime_ns)
            ):
                raise ValueError("persistent frontier lease changed during read")
        finally:
            os.close(descriptor)
        try:
            envelope = json.loads(content.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("persistent frontier lease JSON is invalid") from error
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {"schema", "payload", "sha256"}
            or envelope.get("schema") != _LEASE_ENVELOPE_SCHEMA
        ):
            raise ValueError("persistent frontier lease envelope is invalid")
        payload = envelope.get("payload")
        if envelope.get("sha256") != hashlib.sha256(_canonical(payload)).hexdigest():
            raise ValueError("persistent frontier lease digest mismatch")
        if not isinstance(payload, Mapping) or set(payload) != set(
            self._lease_payload(baseline)
        ):
            raise ValueError("persistent frontier lease payload is invalid")
        observed = LiveStateFrontierLease(
            checkpoint_id=_digest(payload.get("checkpoint_id"), "leased checkpoint"),
            token=_digest(payload.get("token"), "frontier lease token"),
            owner=_owner(payload.get("owner")),
            worker=_integer(payload.get("worker"), "frontier worker", 0, _MAX_WORKER),
            expires=_timestamp(payload.get("expires"), "frontier lease expiry"),
            claim_generation=_integer(
                payload.get("claim_generation"),
                "frontier claim generation",
                1,
                _MAX_GENERATION,
            ),
        )
        if observed.checkpoint_id != baseline.checkpoint_id or (
            strict_fence and (
                observed.token != baseline.token
                or observed.owner != baseline.owner
                or observed.worker != baseline.worker
                or observed.claim_generation != baseline.claim_generation
                or observed.expires < baseline.expires
            )
        ) or self._lease_payload(observed) != payload:
            raise ValueError("persistent frontier lease fencing is invalid")
        return observed

    def _remove_lease_state(self, checkpoint_id: str) -> None:
        path, _lock_path = self._lease_paths(checkpoint_id)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        fsync_directory(os.path.dirname(path))

    def _remember_lease_expiry(self, lease: LiveStateFrontierLease) -> None:
        hint = (lease.token, lease.expires)
        with self._expiry_index_lock:
            if self._lease_expiry_hints.get(lease.checkpoint_id) == hint:
                return
            self._lease_expiry_hints[lease.checkpoint_id] = hint
            heapq.heappush(
                self._lease_expiry_heap,
                (lease.expires, lease.checkpoint_id, lease.token),
            )
            self._compact_expiry_heap_locked()

    def _compact_expiry_heap_locked(self) -> None:
        maximum = max(64, len(self._lease_expiry_hints) * 4)
        if len(self._lease_expiry_heap) <= maximum:
            return
        self._lease_expiry_heap = [
            (expires, checkpoint_id, token)
            for checkpoint_id, (token, expires)
            in self._lease_expiry_hints.items()
        ]
        heapq.heapify(self._lease_expiry_heap)

    def _forget_lease_expiry(self, checkpoint_id: str) -> None:
        with self._expiry_index_lock:
            self._lease_expiry_hints.pop(checkpoint_id, None)

    def _due_lease_hints(
        self,
        leases: Sequence[LiveStateFrontierLease],
        timestamp: float,
    ) -> list[LiveStateFrontierLease]:
        """Return a bounded due batch without reading every heartbeat file."""
        current = {lease.checkpoint_id: lease for lease in leases}
        with self._expiry_index_lock:
            for checkpoint_id in tuple(self._lease_expiry_hints):
                if checkpoint_id not in current:
                    del self._lease_expiry_hints[checkpoint_id]
            for lease in leases:
                known = self._lease_expiry_hints.get(lease.checkpoint_id)
                if (
                    known is None
                    or known[0] != lease.token
                    or known[1] < lease.expires
                ):
                    hint = (lease.token, lease.expires)
                    self._lease_expiry_hints[lease.checkpoint_id] = hint
                    heapq.heappush(
                        self._lease_expiry_heap,
                        (lease.expires, lease.checkpoint_id, lease.token),
                    )
            self._compact_expiry_heap_locked()

            due: list[LiveStateFrontierLease] = []
            scanned = 0
            while (
                self._lease_expiry_heap
                and self._lease_expiry_heap[0][0] <= timestamp
                and scanned < self.recovery_batch
            ):
                scanned += 1
                expires, checkpoint_id, token = heapq.heappop(
                    self._lease_expiry_heap
                )
                if self._lease_expiry_hints.get(checkpoint_id) != (
                    token, expires,
                ):
                    continue
                baseline = current.get(checkpoint_id)
                if baseline is None or baseline.token != token:
                    self._lease_expiry_hints.pop(checkpoint_id, None)
                    continue
                del self._lease_expiry_hints[checkpoint_id]
                due.append(baseline)
            return due

    def _overlay_lease_states(
        self, snapshot: LiveStateFrontierSnapshot
    ) -> LiveStateFrontierSnapshot:
        leases = tuple(self._read_lease_state(lease) for lease in snapshot.leases)
        for lease in leases:
            self._remember_lease_expiry(lease)
        if leases == snapshot.leases:
            return snapshot
        return LiveStateFrontierSnapshot(
            root_checkpoint=snapshot.root_checkpoint,
            program_root=snapshot.program_root,
            generation=snapshot.generation,
            ready=snapshot.ready,
            leases=leases,
            done=snapshot.done,
            search=snapshot.search,
        )

    @staticmethod
    def _normalized_search(raw: Any) -> dict[str, Any]:
        return LiveStateSearchPolicy.from_snapshot(raw).snapshot()

    def _validate_payload(self, raw: Any) -> LiveStateFrontierSnapshot:
        expected = {
            "schema", "root_checkpoint", "program_root", "generation",
            "ready", "leases", "done", "search",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected \
                or raw.get("schema") != FRONTIER_SCHEMA:
            raise ValueError("persistent live-state frontier is invalid")
        root_checkpoint = _digest(
            raw.get("root_checkpoint"), "frontier root checkpoint",
        )
        program_root = _digest(raw.get("program_root"), "frontier program root")
        generation = _integer(
            raw.get("generation"), "frontier generation", 0, _MAX_GENERATION,
        )

        raw_ready = raw.get("ready")
        raw_done = raw.get("done")
        raw_leases = raw.get("leases")
        if not isinstance(raw_ready, list) or not isinstance(raw_done, list) \
                or not isinstance(raw_leases, list):
            raise ValueError("persistent live-state frontier sets are invalid")
        ready = tuple(
            _digest(value, "ready checkpoint") for value in raw_ready
        )
        done = tuple(_digest(value, "done checkpoint") for value in raw_done)
        if len(set(ready)) != len(ready) or list(done) != sorted(set(done)):
            raise ValueError("persistent live-state frontier sets are invalid")

        leases: list[LiveStateFrontierLease] = []
        previous_checkpoint = ""
        for entry in raw_leases:
            if not isinstance(entry, Mapping) or set(entry) != {
                "checkpoint_id", "token", "owner", "worker", "expires",
                "claim_generation",
            }:
                raise ValueError("persistent live-state frontier lease is invalid")
            checkpoint = _digest(
                entry.get("checkpoint_id"), "leased checkpoint",
            )
            token = _digest(entry.get("token"), "frontier lease token")
            if checkpoint <= previous_checkpoint:
                raise ValueError("persistent live-state frontier leases are invalid")
            previous_checkpoint = checkpoint
            leases.append(LiveStateFrontierLease(
                checkpoint_id=checkpoint,
                token=token,
                owner=_owner(entry.get("owner")),
                worker=_integer(
                    entry.get("worker"), "frontier worker", 0, _MAX_WORKER,
                ),
                expires=_timestamp(entry.get("expires"), "frontier lease expiry"),
                claim_generation=_integer(
                    entry.get("claim_generation"),
                    "frontier claim generation", 1, _MAX_GENERATION,
                ),
            ))
        identities = [*ready, *done, *(lease.checkpoint_id for lease in leases)]
        if len(identities) > self.max_states or len(set(identities)) != len(identities):
            raise ValueError("persistent live-state frontier ownership is invalid")
        lease_tokens = [lease.token for lease in leases]
        claim_generations = [lease.claim_generation for lease in leases]
        if (
            len(set(lease_tokens)) != len(lease_tokens)
            or len(set(claim_generations)) != len(claim_generations)
            or any(value > generation for value in claim_generations)
        ):
            raise ValueError("persistent live-state frontier fencing is invalid")
        if root_checkpoint not in set(identities):
            raise ValueError("persistent live-state frontier lost its root")
        search = self._normalized_search(raw.get("search"))
        return LiveStateFrontierSnapshot(
            root_checkpoint=root_checkpoint,
            program_root=program_root,
            generation=generation,
            ready=ready,
            leases=tuple(leases),
            done=done,
            search=search,
        )

    @staticmethod
    def _payload(snapshot: LiveStateFrontierSnapshot) -> dict[str, Any]:
        return {
            "schema": FRONTIER_SCHEMA,
            "root_checkpoint": snapshot.root_checkpoint,
            "program_root": snapshot.program_root,
            "generation": snapshot.generation,
            "ready": list(snapshot.ready),
            "leases": [
                {
                    "checkpoint_id": lease.checkpoint_id,
                    "token": lease.token,
                    "owner": lease.owner,
                    "worker": lease.worker,
                    "expires": lease.expires,
                    "claim_generation": lease.claim_generation,
                }
                for lease in sorted(
                    snapshot.leases, key=lambda value: value.checkpoint_id,
                )
            ],
            "done": sorted(snapshot.done),
            "search": snapshot.search,
        }

    @classmethod
    def _encoded_snapshot_size(
        cls, snapshot: LiveStateFrontierSnapshot,
    ) -> int:
        payload = cls._payload(snapshot)
        envelope = {
            "schema": _ENVELOPE_SCHEMA,
            "payload": payload,
            "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        }
        return len(_canonical(envelope)) + 1

    @staticmethod
    def _list_separator_bytes(count: int) -> int:
        return max(0, count - 1)

    def _next_encoded_snapshot_size(
        self,
        before: LiveStateFrontierSnapshot,
        after: LiveStateFrontierSnapshot,
    ) -> int:
        """Compute the exact next envelope size in O(changed state)."""
        before_size = self._cached_snapshot_bytes
        if self._cached_snapshot != before or before_size is None:
            before_size = self._encoded_snapshot_size(before)

        digest_bytes = len(_canonical(before.root_checkpoint))
        ready_delta = digest_bytes * (len(after.ready) - len(before.ready))
        ready_delta += self._list_separator_bytes(len(after.ready))
        ready_delta -= self._list_separator_bytes(len(before.ready))
        done_delta = digest_bytes * (len(after.done) - len(before.done))
        done_delta += self._list_separator_bytes(len(after.done))
        done_delta -= self._list_separator_bytes(len(before.done))

        before_leases = {
            lease.checkpoint_id: lease for lease in before.leases
        }
        after_leases = {
            lease.checkpoint_id: lease for lease in after.leases
        }
        added_leases = set(after_leases) - set(before_leases)
        removed_leases = set(before_leases) - set(after_leases)
        lease_delta = sum(
            len(_canonical(self._lease_payload(after_leases[checkpoint])))
            for checkpoint in added_leases
        )
        lease_delta -= sum(
            len(_canonical(self._lease_payload(before_leases[checkpoint])))
            for checkpoint in removed_leases
        )
        lease_delta += self._list_separator_bytes(len(after.leases))
        lease_delta -= self._list_separator_bytes(len(before.leases))

        return (
            before_size
            + len(_canonical(after.generation))
            - len(_canonical(before.generation))
            + ready_delta
            + done_delta
            + lease_delta
            + len(_canonical(after.search))
            - len(_canonical(before.search))
        )

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _base_identity(self) -> tuple[int, ...]:
        metadata = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("persistent live-state frontier is not regular")
        return self._identity(metadata)

    def _read_base(self) -> LiveStateFrontierSnapshot:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("O_NOFOLLOW is required for frontier recovery")
        descriptor = os.open(self.path, flags | nofollow)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) \
                    or metadata.st_size <= 0 \
                    or metadata.st_size > self.max_bytes:
                raise ValueError("persistent live-state frontier size is invalid")
            content = bytearray()
            while len(content) < metadata.st_size:
                chunk = os.read(descriptor, min(1 << 20, metadata.st_size - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            final = os.fstat(descriptor)
            if len(content) != metadata.st_size \
                    or self._identity(final) != self._identity(metadata):
                raise ValueError("persistent live-state frontier changed during read")
        finally:
            os.close(descriptor)
        try:
            envelope = json.loads(bytes(content).decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("persistent live-state frontier JSON is invalid") from error
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "schema", "payload", "sha256",
        } or envelope.get("schema") != _ENVELOPE_SCHEMA:
            raise ValueError("persistent live-state frontier envelope is invalid")
        payload = envelope.get("payload")
        expected_digest = hashlib.sha256(_canonical(payload)).hexdigest()
        if envelope.get("sha256") != expected_digest:
            raise ValueError("persistent live-state frontier digest mismatch")
        snapshot = self._validate_payload(payload)
        if self._payload(snapshot) != payload:
            raise ValueError("persistent live-state frontier is not canonical")
        return snapshot

    def _transition_path(self, generation: int) -> str:
        generation = _integer(
            generation, "frontier transition generation", 1, _MAX_GENERATION,
        )
        return os.path.join(self.transition_root, f"{generation:020d}.json")

    def _transition_paths(self) -> list[tuple[int, str, tuple[int, ...]]]:
        result: list[tuple[int, str, tuple[int, ...]]] = []
        with os.scandir(self.transition_root) as entries:
            scanned = 0
            for entry in entries:
                scanned += 1
                if scanned > max(4096, self.compaction_records * 4):
                    raise ValueError("persistent frontier journal budget exceeded")
                match = re.fullmatch(r"([0-9]{20})\.json", entry.name)
                if match is None:
                    if entry.name.endswith(".tmp"):
                        continue
                    raise ValueError(
                        "persistent frontier journal contains an invalid entry"
                    )
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        "persistent frontier transition is not a regular file"
                    )
                generation = int(match.group(1))
                if not 1 <= generation <= _MAX_GENERATION:
                    raise ValueError(
                        "persistent frontier transition generation is invalid"
                    )
                result.append((generation, entry.path, self._identity(metadata)))
        return sorted(result)

    @staticmethod
    def _transition_payload(
        before: LiveStateFrontierSnapshot,
        after: LiveStateFrontierSnapshot,
        operation: str,
    ) -> dict[str, Any]:
        before_ready = set(before.ready)
        after_ready = set(after.ready)
        before_leases = {
            lease.checkpoint_id: lease for lease in before.leases
        }
        after_leases = {
            lease.checkpoint_id: lease for lease in after.leases
        }
        unchanged_leases = set(before_leases) & set(after_leases)
        if any(
            before_leases[checkpoint] != after_leases[checkpoint]
            for checkpoint in unchanged_leases
        ):
            raise ValueError("frontier transitions cannot rewrite live leases")
        return {
            "operation": operation,
            "before_generation": before.generation,
            "after_generation": after.generation,
            "ready_add": [
                checkpoint for checkpoint in after.ready
                if checkpoint not in before_ready
            ],
            "ready_remove": sorted(before_ready - after_ready),
            "leases_add": [
                PersistentLiveStateFrontier._lease_payload(lease)
                for lease in after.leases
                if lease.checkpoint_id not in before_leases
            ],
            "leases_remove": sorted(set(before_leases) - set(after_leases)),
            "done_add": sorted(set(after.done) - set(before.done)),
            "search": after.search,
        }

    def _read_transition(self, path: str) -> Mapping[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("O_NOFOLLOW is required for frontier recovery")
        descriptor = os.open(path, flags | nofollow)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not 0 < metadata.st_size <= _MAX_TRANSITION_BYTES
            ):
                raise ValueError("persistent frontier transition size is invalid")
            content = os.read(descriptor, metadata.st_size + 1)
            final = os.fstat(descriptor)
            if (
                len(content) != metadata.st_size
                or self._identity(metadata) != self._identity(final)
            ):
                raise ValueError(
                    "persistent frontier transition changed during read"
                )
        finally:
            os.close(descriptor)
        try:
            envelope = json.loads(content.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("persistent frontier transition JSON is invalid") from error
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {"schema", "payload", "sha256"}
            or envelope.get("schema") != _TRANSITION_ENVELOPE_SCHEMA
        ):
            raise ValueError("persistent frontier transition envelope is invalid")
        payload = envelope.get("payload")
        if (
            not isinstance(payload, Mapping)
            or envelope.get("sha256")
            != hashlib.sha256(_canonical(payload)).hexdigest()
        ):
            raise ValueError("persistent frontier transition digest mismatch")
        return payload

    def _apply_transition(
        self,
        current: LiveStateFrontierSnapshot,
        raw: Mapping[str, Any],
    ) -> LiveStateFrontierSnapshot:
        expected = {
            "operation", "before_generation", "after_generation",
            "ready_add", "ready_remove", "leases_add", "leases_remove",
            "done_add", "search",
        }
        if set(raw) != expected:
            raise ValueError("persistent frontier transition payload is invalid")
        operation = raw.get("operation")
        if operation not in {"claim", "complete", "abandon", "recover"}:
            raise ValueError("persistent frontier transition operation is invalid")
        before_generation = _integer(
            raw.get("before_generation"),
            "frontier transition previous generation",
            0,
            _MAX_GENERATION,
        )
        after_generation = _integer(
            raw.get("after_generation"),
            "frontier transition next generation",
            1,
            _MAX_GENERATION,
        )
        if (
            before_generation != current.generation
            or after_generation != before_generation + 1
        ):
            raise ValueError("persistent frontier transition generation has a gap")

        list_fields = (
            "ready_add", "ready_remove", "leases_add", "leases_remove",
            "done_add",
        )
        if any(not isinstance(raw.get(field), list) for field in list_fields):
            raise ValueError("persistent frontier transition sets are invalid")
        ready_add = tuple(
            _digest(value, "frontier transition ready checkpoint")
            for value in raw["ready_add"]
        )
        ready_remove = tuple(
            _digest(value, "frontier transition removed checkpoint")
            for value in raw["ready_remove"]
        )
        leases_remove = tuple(
            _digest(value, "frontier transition removed lease")
            for value in raw["leases_remove"]
        )
        done_add = tuple(
            _digest(value, "frontier transition completed checkpoint")
            for value in raw["done_add"]
        )
        if any(
            len(values) != len(set(values))
            for values in (ready_add, ready_remove, leases_remove, done_add)
        ):
            raise ValueError("persistent frontier transition contains duplicates")
        leases_add: list[LiveStateFrontierLease] = []
        for entry in raw["leases_add"]:
            if not isinstance(entry, Mapping) or set(entry) != {
                "checkpoint_id", "token", "owner", "worker", "expires",
                "claim_generation",
            }:
                raise ValueError("persistent frontier transition lease is invalid")
            lease = LiveStateFrontierLease(
                checkpoint_id=_digest(
                    entry.get("checkpoint_id"), "transition leased checkpoint",
                ),
                token=_digest(entry.get("token"), "transition lease token"),
                owner=_owner(entry.get("owner")),
                worker=_integer(
                    entry.get("worker"), "transition lease worker", 0, _MAX_WORKER,
                ),
                expires=_timestamp(
                    entry.get("expires"), "transition lease expiry",
                ),
                claim_generation=_integer(
                    entry.get("claim_generation"),
                    "transition lease generation",
                    1,
                    _MAX_GENERATION,
                ),
            )
            if self._lease_payload(lease) != entry:
                raise ValueError(
                    "persistent frontier transition lease is not canonical"
                )
            leases_add.append(lease)
        if len({lease.checkpoint_id for lease in leases_add}) != len(leases_add):
            raise ValueError("persistent frontier transition leases are duplicated")

        if operation == "claim":
            if not (
                len(ready_remove) == len(leases_add) == 1
                and ready_remove[0] == leases_add[0].checkpoint_id
                and not ready_add and not leases_remove and not done_add
            ):
                raise ValueError("persistent frontier claim transition is invalid")
            if leases_add[0].claim_generation != after_generation:
                raise ValueError(
                    "persistent frontier transition fencing is invalid"
                )
        if operation == "complete" and not (
            len(leases_remove) == len(done_add) == 1
            and leases_remove == done_add
            and not ready_remove and not leases_add
        ):
            raise ValueError("persistent frontier completion transition is invalid")
        if operation == "abandon" and not (
            len(leases_remove) == len(ready_add) == 1
            and leases_remove == ready_add
            and not ready_remove and not leases_add and not done_add
        ):
            raise ValueError("persistent frontier abandonment transition is invalid")
        if operation == "recover" and not (
            set(leases_remove) == set(ready_add)
            and leases_remove and not ready_remove and not leases_add
            and not done_add
        ):
            raise ValueError("persistent frontier recovery transition is invalid")

        normalized_search = self._normalized_search(raw.get("search"))
        if normalized_search != raw.get("search"):
            raise ValueError("persistent frontier transition search is not canonical")
        if operation == "claim":
            LiveStateSearchPolicy.validate_selection_transition(
                current.search, normalized_search,
            )
        elif operation in {"complete", "abandon"} \
                and normalized_search != current.search:
            LiveStateSearchPolicy.validate_observation_transition(
                current.search, normalized_search,
            )
        elif operation == "recover" and normalized_search != current.search:
            raise ValueError("frontier recovery cannot change search state")

        ready = list(current.ready)
        for checkpoint in ready_remove:
            if checkpoint not in ready:
                raise ValueError("frontier transition removes a non-ready state")
            ready.remove(checkpoint)
        leases = {
            lease.checkpoint_id: lease for lease in current.leases
        }
        for checkpoint in leases_remove:
            if checkpoint not in leases:
                raise ValueError("frontier transition removes a missing lease")
            del leases[checkpoint]
        owned = {*ready, *current.done, *leases}
        for checkpoint in ready_add:
            if checkpoint in owned:
                raise ValueError("frontier transition duplicates state ownership")
            ready.append(checkpoint)
            owned.add(checkpoint)
        for lease in leases_add:
            if lease.checkpoint_id in owned:
                raise ValueError("frontier transition duplicates lease ownership")
            leases[lease.checkpoint_id] = lease
            owned.add(lease.checkpoint_id)
        done = set(current.done)
        for checkpoint in done_add:
            if checkpoint in owned:
                raise ValueError("frontier transition duplicates completed ownership")
            done.add(checkpoint)
            owned.add(checkpoint)
        if len(owned) > self.max_states or current.root_checkpoint not in owned:
            raise ValueError("persistent frontier transition violates state bounds")
        return LiveStateFrontierSnapshot(
            root_checkpoint=current.root_checkpoint,
            program_root=current.program_root,
            generation=after_generation,
            ready=tuple(ready),
            leases=tuple(sorted(
                leases.values(), key=lambda lease: lease.checkpoint_id,
            )),
            done=tuple(sorted(done)),
            search=normalized_search,
        )

    def _read(self) -> LiveStateFrontierSnapshot:
        base_identity = self._base_identity()
        transitions = self._transition_paths()
        transition_identities = {
            generation: identity
            for generation, _path, identity in transitions
        }
        if (
            self._cached_base_identity == base_identity
            and any(
                generation not in transition_identities
                for generation in self._cached_transition_identities
            )
        ):
            raise ValueError("persistent frontier journal lost a transition")
        changed = False
        if (
            self._cached_snapshot is not None
            and self._cached_base_identity == base_identity
            and all(
                transition_identities.get(generation) == identity
                for generation, identity
                in self._cached_transition_identities.items()
            )
        ):
            snapshot = self._cached_snapshot
        else:
            snapshot = self._read_base()
            changed = True
        for generation, path, _identity in transitions:
            if generation <= snapshot.generation:
                continue
            payload = self._read_transition(path)
            if payload.get("after_generation") != generation:
                raise ValueError(
                    "persistent frontier transition filename is invalid"
                )
            snapshot = self._apply_transition(snapshot, payload)
            changed = True
        if changed or self._cached_snapshot_bytes is None:
            normalized = self._validate_payload(self._payload(snapshot))
            if normalized != snapshot:
                raise ValueError("persistent frontier journal is not canonical")
            snapshot_bytes = self._encoded_snapshot_size(snapshot)
            if snapshot_bytes > self.max_bytes:
                raise ValueError("persistent live-state frontier exceeds byte limit")
            self._cached_snapshot = snapshot
            self._cached_base_identity = base_identity
            self._cached_transition_identities = transition_identities
            self._cached_snapshot_bytes = snapshot_bytes
        return snapshot

    def _write_transition(
        self,
        before: LiveStateFrontierSnapshot,
        after: LiveStateFrontierSnapshot,
        operation: str,
    ) -> None:
        if after.generation != before.generation + 1:
            raise ValueError("frontier transition must advance one generation")
        payload = self._transition_payload(before, after, operation)
        if self._apply_transition(before, payload) != after:
            raise ValueError("frontier transition does not reproduce its snapshot")
        envelope = {
            "schema": _TRANSITION_ENVELOPE_SCHEMA,
            "payload": payload,
            "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        }
        content = _canonical(envelope) + b"\n"
        if len(content) > _MAX_TRANSITION_BYTES:
            raise ValueError("persistent frontier transition exceeds byte limit")
        path = self._transition_path(after.generation)
        if os.path.lexists(path):
            raise ValueError("persistent frontier transition already exists")
        temporary = os.path.join(
            self.transition_root,
            f".{after.generation:020d}.{os.getpid()}.{time.time_ns()}.tmp",
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        published = False
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short frontier transition write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            durable_replace(temporary, path)
            published = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _compact_transitions(
        self, snapshot: LiveStateFrontierSnapshot,
    ) -> None:
        transitions = self._transition_paths()
        if len(transitions) < self.compaction_records:
            return
        # Publishing the newer base first makes cleanup crash-safe: if old
        # records survive, replay skips generations already represented there.
        self._write(snapshot)
        removed = False
        for generation, path, _identity in transitions:
            if generation > snapshot.generation:
                continue
            try:
                os.unlink(path)
                removed = True
            except FileNotFoundError:
                continue
        if removed:
            fsync_directory(self.transition_root)

    def _commit(
        self,
        before: LiveStateFrontierSnapshot,
        after: LiveStateFrontierSnapshot,
        operation: str,
    ) -> None:
        snapshot_bytes = self._next_encoded_snapshot_size(before, after)
        if snapshot_bytes > self.max_bytes:
            raise ValueError("persistent live-state frontier exceeds byte limit")
        self._write_transition(before, after, operation)
        self._cached_snapshot = after
        self._cached_snapshot_bytes = snapshot_bytes
        self._cached_base_identity = None
        try:
            self._cached_base_identity = self._base_identity()
            self._cached_transition_identities = {
                generation: identity
                for generation, _path, identity in self._transition_paths()
            }
            self._compact_transitions(after)
            self._cached_transition_identities = {
                generation: identity
                for generation, _path, identity in self._transition_paths()
            }
            self._last_maintenance_error = ""
        except (OSError, ValueError) as error:
            # The transition is already durable. Compaction is an amortized
            # maintenance operation and may be retried by the next mutation.
            # Do not report the already committed state transition as failed.
            self._maintenance_failures += 1
            self._last_maintenance_error = str(error)[:4096]
            self._cached_transition_identities = {}

    def _cleanup_committed_lease(self, checkpoint_id: str) -> None:
        """Best-effort cleanup after a durable frontier transition."""
        self._forget_lease_expiry(checkpoint_id)
        try:
            self._remove_lease_state(checkpoint_id)
        except OSError as error:
            self._maintenance_failures += 1
            self._last_maintenance_error = str(error)[:4096]

    def _write(self, snapshot: LiveStateFrontierSnapshot) -> None:
        payload = self._payload(snapshot)
        normalized = self._validate_payload(payload)
        if normalized != snapshot:
            raise ValueError("persistent live-state frontier is not canonical")
        envelope = {
            "schema": _ENVELOPE_SCHEMA,
            "payload": payload,
            "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        }
        content = _canonical(envelope) + b"\n"
        if len(content) > self.max_bytes:
            raise ValueError("persistent live-state frontier exceeds byte limit")
        temporary = os.path.join(
            self.root,
            f".frontier.{os.getpid()}.{time.time_ns()}.tmp",
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        published = False
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short frontier write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            durable_replace(temporary, self.path)
            published = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._cached_snapshot = snapshot
        self._cached_snapshot_bytes = len(content)
        try:
            self._cached_base_identity = self._base_identity()
        except (OSError, ValueError) as error:
            # The base snapshot was already atomically published. A failed
            # identity refresh only disables the read cache.
            self._cached_base_identity = None
            self._maintenance_failures += 1
            self._last_maintenance_error = str(error)[:4096]
        self._cached_transition_identities = {}

    @staticmethod
    def _next_generation(snapshot: LiveStateFrontierSnapshot) -> int:
        if snapshot.generation >= _MAX_GENERATION:
            raise ValueError("persistent live-state frontier generation is exhausted")
        return snapshot.generation + 1

    @staticmethod
    def _now(value: float | None) -> float:
        return _timestamp(time.time() if value is None else value, "frontier time")

    def initialize(
        self,
        root_checkpoint: str,
        program_root: str,
        search: Mapping[str, Any],
    ) -> LiveStateFrontierSnapshot:
        root_checkpoint = _digest(root_checkpoint, "frontier root checkpoint")
        program_root = _digest(program_root, "frontier program root")
        normalized_search = self._normalized_search(search)
        with self._lock():
            try:
                existing = self._read()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing.root_checkpoint != root_checkpoint \
                        or existing.program_root != program_root:
                    raise ValueError("persistent frontier is already bound")
                return existing
            snapshot = LiveStateFrontierSnapshot(
                root_checkpoint=root_checkpoint,
                program_root=program_root,
                generation=0,
                ready=(root_checkpoint,),
                leases=(),
                done=(),
                search=normalized_search,
            )
            self._write(snapshot)
            return snapshot

    def snapshot(self) -> LiveStateFrontierSnapshot:
        with self._lock():
            return self._overlay_lease_states(self._read())

    def maintenance_snapshot(self) -> dict[str, int | str | bool]:
        """Report post-commit maintenance degradation without changing state."""
        with self._expiry_index_lock:
            expiration_hints = len(self._lease_expiry_hints)
            expiration_heap_entries = len(self._lease_expiry_heap)
        return {
            "degraded": bool(self._last_maintenance_error),
            "failures": self._maintenance_failures,
            "last_error": self._last_maintenance_error,
            "expiration_hints": expiration_hints,
            "expiration_heap_entries": expiration_heap_entries,
        }

    @staticmethod
    def _token(checkpoint: str, owner: str, worker: int) -> str:
        material = (
            f"{checkpoint}\x00{owner}\x00{worker}\x00{os.getpid()}\x00"
            f"{time.time_ns()}\x00{os.urandom(32).hex()}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def claim(
        self,
        checkpoint_id: str,
        *,
        expected_generation: int,
        search: Mapping[str, Any],
        owner: str,
        worker: int = 0,
        now: float | None = None,
    ) -> LiveStateFrontierLease | None:
        checkpoint_id = _digest(checkpoint_id, "claimed checkpoint")
        expected_generation = _integer(
            expected_generation, "expected frontier generation", 0, _MAX_GENERATION,
        )
        owner = _owner(owner)
        worker = _integer(worker, "frontier worker", 0, _MAX_WORKER)
        normalized_search = self._normalized_search(search)
        with self._lock():
            timestamp = self._now(now)
            current = self._read()
            if current.generation != expected_generation \
                    or checkpoint_id not in current.ready:
                return None
            LiveStateSearchPolicy.validate_selection_transition(
                current.search, normalized_search,
            )
            generation = self._next_generation(current)
            lease = LiveStateFrontierLease(
                checkpoint_id=checkpoint_id,
                token=self._token(checkpoint_id, owner, worker),
                owner=owner,
                worker=worker,
                expires=timestamp + self.lease_ttl,
                claim_generation=generation,
            )
            snapshot = LiveStateFrontierSnapshot(
                root_checkpoint=current.root_checkpoint,
                program_root=current.program_root,
                generation=generation,
                ready=tuple(
                    value for value in current.ready if value != checkpoint_id
                ),
                leases=tuple(sorted(
                    (*current.leases, lease),
                    key=lambda value: value.checkpoint_id,
                )),
                done=current.done,
                search=normalized_search,
            )
            with self._lease_lock(checkpoint_id):
                self._write_lease_state(lease)
                try:
                    self._commit(current, snapshot, "claim")
                except BaseException:
                    self._remove_lease_state(checkpoint_id)
                    raise
            self._remember_lease_expiry(lease)
            return lease

    def heartbeat(
        self,
        lease: LiveStateFrontierLease,
        *,
        now: float | None = None,
    ) -> LiveStateFrontierLease | None:
        if not isinstance(lease, LiveStateFrontierLease):
            raise TypeError("frontier heartbeat requires a lease")
        state_path, _lock_path = self._lease_paths(lease.checkpoint_id)
        with self._lease_lock(lease.checkpoint_id):
            if os.path.exists(state_path):
                timestamp = self._now(now)
                authoritative = self._read_lease_state(lease, strict_fence=False)
                if not self._lease_is_current(authoritative, lease, timestamp):
                    return None
                renewed = LiveStateFrontierLease(
                    checkpoint_id=authoritative.checkpoint_id,
                    token=authoritative.token,
                    owner=authoritative.owner,
                    worker=authoritative.worker,
                    expires=max(authoritative.expires, timestamp + self.lease_ttl),
                    claim_generation=authoritative.claim_generation,
                )
                self._write_lease_state(renewed)
                self._remember_lease_expiry(renewed)
                return renewed

        # Migrate a lease from an older frontier that predates heartbeat files.
        with self._lock():
            current = self._read()
            baseline = current.lease_for(lease.checkpoint_id)
            if baseline is None or not self._same_lease_identity(baseline, lease):
                return None
            with self._lease_lock(lease.checkpoint_id):
                timestamp = self._now(now)
                authoritative = self._read_lease_state(baseline)
                if not self._lease_is_current(authoritative, lease, timestamp):
                    return None
                renewed = LiveStateFrontierLease(
                    checkpoint_id=authoritative.checkpoint_id,
                    token=authoritative.token,
                    owner=authoritative.owner,
                    worker=authoritative.worker,
                    expires=max(authoritative.expires, timestamp + self.lease_ttl),
                    claim_generation=authoritative.claim_generation,
                )
                self._write_lease_state(renewed)
                self._remember_lease_expiry(renewed)
                return renewed

    def recover_expired(
        self,
        *,
        now: float | None = None,
    ) -> LiveStateFrontierUpdate:
        with self._lock():
            current = self._read()
            timestamp = self._now(now)
            candidates: list[LiveStateFrontierLease] = []
            for baseline in self._due_lease_hints(current.leases, timestamp):
                observed = self._read_lease_state(baseline)
                if observed.expires <= timestamp:
                    candidates.append(baseline)
                else:
                    self._remember_lease_expiry(observed)
            if not candidates:
                return LiveStateFrontierUpdate("unchanged", current)

            # Only likely-expired leases are locked. Re-read each while all
            # candidate locks are held so a concurrent heartbeat cannot race
            # the recovery commit.
            with ExitStack() as lease_locks:
                for lease in candidates:
                    lease_locks.enter_context(
                        self._lease_lock(lease.checkpoint_id)
                    )
                expired_values = []
                for baseline in candidates:
                    observed = self._read_lease_state(baseline)
                    if observed.expires <= timestamp:
                        expired_values.append(observed)
                    else:
                        self._remember_lease_expiry(observed)
                expired = tuple(expired_values)
                if not expired:
                    return LiveStateFrontierUpdate("unchanged", current)
                expired_ids = {lease.checkpoint_id for lease in expired}
                snapshot = LiveStateFrontierSnapshot(
                    root_checkpoint=current.root_checkpoint,
                    program_root=current.program_root,
                    generation=self._next_generation(current),
                    ready=(*current.ready, *(
                        lease.checkpoint_id
                        for lease in sorted(expired, key=lambda value: (
                            value.expires, value.checkpoint_id,
                        ))
                    )),
                    leases=tuple(
                        lease for lease in current.leases
                        if lease.checkpoint_id not in expired_ids
                    ),
                    done=current.done,
                    search=current.search,
                )
                self._commit(current, snapshot, "recover")
                for lease in expired:
                    self._cleanup_committed_lease(lease.checkpoint_id)
                return LiveStateFrontierUpdate("recovered", snapshot)

    def complete(
        self,
        lease: LiveStateFrontierLease,
        children: Sequence[str],
        *,
        expected_generation: int,
        search: Mapping[str, Any],
        now: float | None = None,
    ) -> LiveStateFrontierUpdate:
        if not isinstance(lease, LiveStateFrontierLease):
            raise TypeError("frontier completion requires a lease")
        if not isinstance(children, (list, tuple)) \
                or len(children) > self.max_states:
            raise ValueError("frontier children are invalid")
        normalized_children = tuple(
            _digest(value, "frontier child checkpoint") for value in children
        )
        if len(set(normalized_children)) != len(normalized_children):
            raise ValueError("frontier children contain duplicates")
        expected_generation = _integer(
            expected_generation, "expected frontier generation", 0, _MAX_GENERATION,
        )
        normalized_search = self._normalized_search(search)
        with self._lock():
            current = self._read()
            if current.generation != expected_generation:
                return LiveStateFrontierUpdate("conflict", current)
            baseline = current.lease_for(lease.checkpoint_id)
            if baseline is None or not self._same_lease_identity(baseline, lease):
                return LiveStateFrontierUpdate("stale", current)
            with self._lease_lock(lease.checkpoint_id):
                timestamp = self._now(now)
                authoritative = self._read_lease_state(baseline)
                if not self._lease_is_current(authoritative, lease, timestamp):
                    return LiveStateFrontierUpdate("stale", current)
                LiveStateSearchPolicy.validate_observation_transition(
                    current.search, normalized_search,
                )
                owned = {
                    *current.ready,
                    *current.done,
                    *(value.checkpoint_id for value in current.leases),
                }
                admitted = tuple(
                    child for child in normalized_children if child not in owned
                )
                if len(owned) + len(admitted) > self.max_states:
                    raise ValueError("persistent live-state frontier is full")
                snapshot = LiveStateFrontierSnapshot(
                    root_checkpoint=current.root_checkpoint,
                    program_root=current.program_root,
                    generation=self._next_generation(current),
                    ready=(*current.ready, *admitted),
                    leases=tuple(
                        value for value in current.leases
                        if value.checkpoint_id != lease.checkpoint_id
                    ),
                    done=tuple(sorted((*current.done, lease.checkpoint_id))),
                    search=normalized_search,
                )
                self._commit(current, snapshot, "complete")
                self._cleanup_committed_lease(lease.checkpoint_id)
                return LiveStateFrontierUpdate("completed", snapshot)

    def abandon(
        self,
        lease: LiveStateFrontierLease,
        *,
        expected_generation: int | None = None,
        search: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> LiveStateFrontierUpdate:
        if not isinstance(lease, LiveStateFrontierLease):
            raise TypeError("frontier abandonment requires a lease")
        if (expected_generation is None) != (search is None):
            raise ValueError(
                "frontier abandonment observation requires generation and search"
            )
        normalized_search = None
        if expected_generation is not None:
            expected_generation = _integer(
                expected_generation,
                "expected frontier generation",
                0,
                _MAX_GENERATION,
            )
            normalized_search = self._normalized_search(search)
        with self._lock():
            current = self._read()
            if expected_generation is not None \
                    and current.generation != expected_generation:
                return LiveStateFrontierUpdate("conflict", current)
            baseline = current.lease_for(lease.checkpoint_id)
            if baseline is None or not self._same_lease_identity(baseline, lease):
                return LiveStateFrontierUpdate("stale", current)
            with self._lease_lock(lease.checkpoint_id):
                timestamp = self._now(now)
                authoritative = self._read_lease_state(baseline)
                if not self._lease_is_current(authoritative, lease, timestamp):
                    return LiveStateFrontierUpdate("stale", current)
                if normalized_search is not None:
                    LiveStateSearchPolicy.validate_observation_transition(
                        current.search, normalized_search,
                    )
                snapshot = LiveStateFrontierSnapshot(
                    root_checkpoint=current.root_checkpoint,
                    program_root=current.program_root,
                    generation=self._next_generation(current),
                    ready=(*current.ready, lease.checkpoint_id),
                    leases=tuple(
                        value for value in current.leases
                        if value.checkpoint_id != lease.checkpoint_id
                    ),
                    done=current.done,
                    search=(
                        current.search
                        if normalized_search is None
                        else normalized_search
                    ),
                )
                self._commit(current, snapshot, "abandon")
                self._cleanup_committed_lease(lease.checkpoint_id)
                return LiveStateFrontierUpdate("abandoned", snapshot)
