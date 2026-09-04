#!/usr/bin/env python3
"""Fenced cross-job roots and bounded GC for shared QF_BV artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence


LIFECYCLE_SCHEMA = "symcc-qfbv-artifact-lifecycle-v1"
LIFECYCLE_PROTOCOL = "symcc-qfbv-fenced-root-mark-sweep-v1"
ARTIFACT_KINDS = frozenset({
    "context", "proof", "receipt", "lemma", "core", "sat-proof", "partition",
    "partition-execution",
})
_LEGACY_ARTIFACT_KINDS = frozenset({
    "context,lemma,proof,receipt",
    "context,core,lemma,proof,receipt",
    "context,core,lemma,proof,receipt,sat-proof",
    "context,core,lemma,partition,proof,receipt,sat-proof",
})
MAX_JOB_ID_BYTES = 256
MAX_OWNER_BYTES = 256
# One 4096-cube execution can reference its partition plus one proof per cube.
MAX_REFERENCES_PER_CALL = 4097
DEFAULT_MAX_JOBS = 1_000_000
MAX_JOBS_LIMIT = 10_000_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}")


class ArtifactLifecycleError(ValueError):
    """Lifecycle metadata, fencing, locking, or collection failed closed."""


def _bounded_int(value: object, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ArtifactLifecycleError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ArtifactLifecycleError(f"{name} must be an integer") from error
    if parsed < lower or parsed > upper:
        raise ArtifactLifecycleError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def _finite_float(
    value: object,
    name: str,
    lower: float,
    upper: float,
) -> float:
    if isinstance(value, bool):
        raise ArtifactLifecycleError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ArtifactLifecycleError(f"{name} must be finite") from error
    if not math.isfinite(parsed) or parsed < lower or parsed > upper:
        raise ArtifactLifecycleError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def _wall_time(value: object | None) -> float:
    return (
        time.time()
        if value is None
        else _finite_float(value, "artifact lifecycle wall time", 0.0, 1.0e12)
    )


def _kind(value: object) -> str:
    parsed = str(value)
    if parsed not in ARTIFACT_KINDS:
        raise ArtifactLifecycleError(f"unsupported artifact kind {parsed!r}")
    return parsed


def _digest(value: object) -> str:
    parsed = str(value)
    if _HEX64.fullmatch(parsed) is None:
        raise ArtifactLifecycleError("artifact digest must be lowercase SHA-256")
    return parsed


def _job_id(value: object) -> str:
    parsed = str(value)
    if (
        _JOB_ID.fullmatch(parsed) is None
        or len(parsed.encode("ascii")) > MAX_JOB_ID_BYTES
    ):
        raise ArtifactLifecycleError("artifact job ID is invalid")
    return parsed


def _owner(value: object) -> str:
    parsed = str(value)
    try:
        encoded = parsed.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ArtifactLifecycleError("artifact job owner is invalid") from error
    if not parsed or b"\x00" in encoded or len(encoded) > MAX_OWNER_BYTES:
        raise ArtifactLifecycleError("artifact job owner is invalid")
    return parsed


@dataclass(frozen=True, order=True)
class ArtifactRef:
    kind: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _kind(self.kind))
        object.__setattr__(self, "digest", _digest(self.digest))


@dataclass(frozen=True)
class ArtifactJobLease:
    job_id: str
    owner: str
    generation: int
    lease_until: float


@dataclass(frozen=True)
class ArtifactCollection:
    deleted: tuple[ArtifactRef, ...]
    deleted_bytes: int
    examined: int
    protected: int
    expired_jobs: int
    stop_reason: str


DeleteArtifact = Callable[[str, str, int], int]


class ArtifactLifecycleRegistry:
    """Durable root graph guarded against concurrent mutator/collector races."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_jobs: int = DEFAULT_MAX_JOBS,
    ):
        self.max_jobs = _bounded_int(
            max_jobs, "artifact lifecycle max_jobs", 1, MAX_JOBS_LIMIT
        )
        root_path = Path(root)
        if root_path.is_symlink():
            raise ArtifactLifecycleError(
                "artifact lifecycle root must not be a symlink"
            )
        self.root = root_path.resolve()
        self.identity_sha256 = hashlib.sha256(
            (
                f"{LIFECYCLE_SCHEMA}\x00{LIFECYCLE_PROTOCOL}\x00{self.root}"
            ).encode("utf-8")
        ).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "lifecycle.sqlite3"
        self.lock_path = self.root / ".lifecycle.lock"
        self._local = threading.local()
        with self._file_lock(exclusive=True, timeout_ms=30_000):
            self._initialize_locked()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_locked(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    lease_until REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    created REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY(kind, digest)
                );
                CREATE INDEX IF NOT EXISTS artifacts_by_age
                    ON artifacts(last_seen, kind, digest);
                CREATE TABLE IF NOT EXISTS artifact_edges (
                    source_kind TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    PRIMARY KEY(
                        source_kind, source_digest, target_kind, target_digest
                    )
                );
                CREATE INDEX IF NOT EXISTS artifact_edges_by_target
                    ON artifact_edges(target_kind, target_digest);
                CREATE TABLE IF NOT EXISTS job_refs (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    referenced REAL NOT NULL,
                    PRIMARY KEY(job_id, generation, kind, digest)
                );
                CREATE INDEX IF NOT EXISTS job_refs_by_artifact
                    ON job_refs(kind, digest, job_id, generation);
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            expected = {
                "schema": LIFECYCLE_SCHEMA,
                "protocol": LIFECYCLE_PROTOCOL,
                "artifact_kinds": ",".join(sorted(ARTIFACT_KINDS)),
                "max_jobs": str(self.max_jobs),
            }
            for key, value in expected.items():
                row = database.execute(
                    "SELECT value FROM store_metadata WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    database.execute(
                        "INSERT INTO store_metadata(key, value) VALUES(?, ?)",
                        (key, value),
                    )
                elif (
                    key == "artifact_kinds"
                    and str(row["value"]) in _LEGACY_ARTIFACT_KINDS
                ):
                    database.execute(
                        "UPDATE store_metadata SET value = ? WHERE key = ?",
                        (value, key),
                    )
                elif str(row["value"]) != value:
                    raise ArtifactLifecycleError(
                        f"artifact lifecycle metadata mismatch for {key}"
                    )

    @contextmanager
    def _file_lock(self, *, exclusive: bool, timeout_ms: int) -> Iterator[None]:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for artifact lifecycle locking")
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            path_state = os.stat(self.lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_state.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (path_state.st_dev, path_state.st_ino)
            ):
                raise ArtifactLifecycleError(
                    "artifact lifecycle lock is not a stable regular file"
                )
            deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    break
                except InterruptedError:
                    continue
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArtifactLifecycleError(
                            "artifact lifecycle lock timeout"
                        ) from error
                    time.sleep(min(0.01, remaining))
            locked = os.stat(self.lock_path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (
                locked.st_dev,
                locked.st_ino,
            ):
                raise ArtifactLifecycleError(
                    "artifact lifecycle lock changed while waiting"
                )
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def operation(self, *, timeout_ms: int = 30_000) -> Iterator[None]:
        """Exclude collection while one store operation reads or publishes."""
        depth = int(getattr(self._local, "operation_depth", 0))
        if depth:
            self._local.operation_depth = depth + 1
            try:
                yield
            finally:
                self._local.operation_depth = depth
            return
        with self._file_lock(exclusive=False, timeout_ms=timeout_ms):
            self._local.operation_depth = 1
            try:
                yield
            finally:
                self._local.operation_depth = 0

    @contextmanager
    def maintenance(self, *, timeout_ms: int = 30_000) -> Iterator[None]:
        """Serialize schema changes and other store-wide maintenance."""
        if int(getattr(self._local, "operation_depth", 0)):
            raise ArtifactLifecycleError(
                "artifact lifecycle maintenance cannot nest in an operation"
            )
        with self._file_lock(exclusive=True, timeout_ms=timeout_ms):
            yield

    def start_job(
        self,
        job_id: str,
        owner: str,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> ArtifactJobLease:
        identity = _job_id(job_id)
        normalized_owner = _owner(owner)
        duration = _finite_float(
            lease_seconds, "artifact job lease_seconds", 0.1, 86_400.0
        )
        current = _wall_time(now)
        until = current + duration
        with self.operation():
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT generation FROM jobs WHERE job_id = ?", (identity,)
                ).fetchone()
                if row is None:
                    job_count = int(
                        database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                    )
                    if job_count >= self.max_jobs:
                        raise ArtifactLifecycleError(
                            "artifact lifecycle job ID capacity is exhausted"
                        )
                generation = int(row["generation"]) + 1 if row is not None else 1
                database.execute("DELETE FROM job_refs WHERE job_id = ?", (identity,))
                database.execute(
                    "INSERT INTO jobs(job_id, owner, generation, lease_until, updated) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                    "owner=excluded.owner, generation=excluded.generation, "
                    "lease_until=excluded.lease_until, updated=excluded.updated",
                    (identity, normalized_owner, generation, until, current),
                )
        return ArtifactJobLease(
            identity, normalized_owner, generation, until
        )

    @staticmethod
    def _validate_lease(
        database: sqlite3.Connection,
        lease: ArtifactJobLease,
        now: float,
    ) -> None:
        row = database.execute(
            "SELECT owner, generation, lease_until FROM jobs WHERE job_id = ?",
            (lease.job_id,),
        ).fetchone()
        if (
            row is None
            or str(row["owner"]) != lease.owner
            or int(row["generation"]) != lease.generation
            or float(row["lease_until"]) <= now
        ):
            raise ArtifactLifecycleError("artifact job lease is stale or expired")

    def heartbeat(
        self,
        lease: ArtifactJobLease,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> ArtifactJobLease:
        duration = _finite_float(
            lease_seconds, "artifact job lease_seconds", 0.1, 86_400.0
        )
        current = _wall_time(now)
        until = current + duration
        with self.operation():
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                self._validate_lease(database, lease, current)
                cursor = database.execute(
                    "UPDATE jobs SET lease_until = ?, updated = ? "
                    "WHERE job_id = ? AND owner = ? AND generation = ?",
                    (
                        until,
                        current,
                        lease.job_id,
                        lease.owner,
                        lease.generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ArtifactLifecycleError("artifact job heartbeat was fenced")
        return ArtifactJobLease(
            lease.job_id, lease.owner, lease.generation, until
        )

    def release_job(
        self,
        lease: ArtifactJobLease,
        *,
        now: float | None = None,
    ) -> bool:
        current = _wall_time(now)
        with self.operation():
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                cursor = database.execute(
                    "UPDATE jobs SET lease_until = ?, updated = ? "
                    "WHERE job_id = ? AND owner = ? AND generation = ?",
                    (
                        current,
                        current,
                        lease.job_id,
                        lease.owner,
                        lease.generation,
                    ),
                )
                if cursor.rowcount == 1:
                    database.execute(
                        "DELETE FROM job_refs WHERE job_id = ? AND generation = ?",
                        (lease.job_id, lease.generation),
                    )
                return cursor.rowcount == 1

    @staticmethod
    def _normalize_refs(values: Sequence[ArtifactRef]) -> tuple[ArtifactRef, ...]:
        if len(values) > MAX_REFERENCES_PER_CALL:
            raise ArtifactLifecycleError("too many artifact references in one call")
        return tuple(dict.fromkeys(ArtifactRef(item.kind, item.digest) for item in values))

    def record_artifact(
        self,
        artifact: ArtifactRef,
        *,
        encoded_bytes: int,
        edges: Sequence[ArtifactRef] = (),
        lease: ArtifactJobLease | None = None,
        now: float | None = None,
    ) -> None:
        source = ArtifactRef(artifact.kind, artifact.digest)
        size = _bounded_int(encoded_bytes, "artifact encoded_bytes", 0, 1 << 40)
        targets = self._normalize_refs(edges)
        current = _wall_time(now)
        with self.operation():
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                if lease is not None:
                    self._validate_lease(database, lease, current)
                row = database.execute(
                    "SELECT encoded_bytes FROM artifacts WHERE kind = ? AND digest = ?",
                    (source.kind, source.digest),
                ).fetchone()
                if row is not None and int(row["encoded_bytes"]) != size:
                    raise ArtifactLifecycleError("artifact size identity changed")
                existing_edges = {
                    ArtifactRef(str(item["target_kind"]), str(item["target_digest"]))
                    for item in database.execute(
                        "SELECT target_kind, target_digest FROM artifact_edges "
                        "WHERE source_kind = ? AND source_digest = ?",
                        (source.kind, source.digest),
                    ).fetchall()
                }
                if row is not None and existing_edges != set(targets):
                    raise ArtifactLifecycleError("artifact dependency identity changed")
                database.execute(
                    "INSERT INTO artifacts(kind, digest, encoded_bytes, created, "
                    "last_seen) VALUES(?, ?, ?, ?, ?) ON CONFLICT(kind, digest) "
                    "DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen)",
                    (source.kind, source.digest, size, current, current),
                )
                for target in targets:
                    database.execute(
                        "INSERT OR IGNORE INTO artifact_edges(" 
                        "source_kind, source_digest, target_kind, target_digest"
                        ") VALUES(?, ?, ?, ?)",
                        (source.kind, source.digest, target.kind, target.digest),
                    )
                if lease is not None:
                    database.execute(
                        "INSERT OR REPLACE INTO job_refs(" 
                        "job_id, generation, kind, digest, referenced"
                        ") VALUES(?, ?, ?, ?, ?)",
                        (
                            lease.job_id,
                            lease.generation,
                            source.kind,
                            source.digest,
                            current,
                        ),
                    )

    def touch(
        self,
        artifacts: Sequence[ArtifactRef],
        *,
        lease: ArtifactJobLease | None = None,
        now: float | None = None,
    ) -> None:
        refs = self._normalize_refs(artifacts)
        if not refs:
            return
        current = _wall_time(now)
        with self.operation():
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                if lease is not None:
                    self._validate_lease(database, lease, current)
                for artifact in refs:
                    cursor = database.execute(
                        "UPDATE artifacts SET last_seen = MAX(last_seen, ?) "
                        "WHERE kind = ? AND digest = ?",
                        (current, artifact.kind, artifact.digest),
                    )
                    if cursor.rowcount != 1:
                        raise ArtifactLifecycleError("unknown lifecycle artifact")
                    if lease is not None:
                        database.execute(
                            "INSERT OR REPLACE INTO job_refs(" 
                            "job_id, generation, kind, digest, referenced"
                            ") VALUES(?, ?, ?, ?, ?)",
                            (
                                lease.job_id,
                                lease.generation,
                                artifact.kind,
                                artifact.digest,
                                current,
                            ),
                        )

    def collect(
        self,
        delete_artifact: DeleteArtifact,
        *,
        grace_seconds: float,
        max_objects: int,
        max_bytes: int,
        time_budget_ms: int,
        now: float | None = None,
    ) -> ArtifactCollection:
        """Delete a bounded dependency-closed prefix of unreachable artifacts."""
        if int(getattr(self._local, "operation_depth", 0)):
            raise ArtifactLifecycleError("collection cannot nest inside an operation")
        grace = _finite_float(
            grace_seconds, "artifact GC grace_seconds", 0.0, 365 * 86_400.0
        )
        object_budget = _bounded_int(
            max_objects, "artifact GC max_objects", 1, 1_000_000
        )
        byte_budget = _bounded_int(
            max_bytes, "artifact GC max_bytes", 1, 1 << 40
        )
        duration_ms = _bounded_int(
            time_budget_ms, "artifact GC time_budget_ms", 1, 3_600_000
        )
        current = _wall_time(now)
        cutoff = current - grace
        deadline = time.monotonic() + duration_ms / 1000.0
        deleted: list[ArtifactRef] = []
        deleted_bytes = 0
        examined = 0
        stop_reason = "complete"
        with self._file_lock(exclusive=True, timeout_ms=duration_ms):
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                expired = int(
                    database.execute(
                        "SELECT COUNT(*) FROM jobs WHERE lease_until <= ?", (current,)
                    ).fetchone()[0]
                )
                database.execute(
                    "DELETE FROM job_refs WHERE EXISTS ("
                    "SELECT 1 FROM jobs WHERE jobs.job_id = job_refs.job_id "
                    "AND jobs.generation = job_refs.generation "
                    "AND jobs.lease_until <= ?)",
                    (current,),
                )
                incomplete_graph = database.execute(
                    "SELECT 1 FROM artifact_edges AS edge "
                    "LEFT JOIN artifacts AS source ON "
                    "source.kind = edge.source_kind AND "
                    "source.digest = edge.source_digest "
                    "LEFT JOIN artifacts AS target ON "
                    "target.kind = edge.target_kind AND "
                    "target.digest = edge.target_digest "
                    "WHERE source.digest IS NULL OR target.digest IS NULL "
                    "UNION ALL SELECT 1 FROM job_refs AS ref "
                    "LEFT JOIN jobs AS job ON job.job_id = ref.job_id AND "
                    "job.generation = ref.generation "
                    "LEFT JOIN artifacts AS artifact ON "
                    "artifact.kind = ref.kind AND artifact.digest = ref.digest "
                    "WHERE job.job_id IS NULL OR artifact.digest IS NULL LIMIT 1"
                ).fetchone()
                if incomplete_graph is not None:
                    raise ArtifactLifecycleError(
                        "artifact dependency graph is incomplete"
                    )
                database.execute("DROP TABLE IF EXISTS temp.live_artifacts")
                database.execute(
                    "CREATE TEMP TABLE live_artifacts(" 
                    "kind TEXT NOT NULL, digest TEXT NOT NULL, "
                    "PRIMARY KEY(kind, digest)) WITHOUT ROWID"
                )
                def interrupt_expired_query() -> int:
                    return int(time.monotonic() >= deadline)

                database.set_progress_handler(interrupt_expired_query, 1000)
                try:
                    try:
                        database.execute(
                            "WITH RECURSIVE live(kind, digest) AS ("
                            "SELECT refs.kind, refs.digest FROM job_refs AS refs "
                            "JOIN jobs ON jobs.job_id = refs.job_id "
                            "AND jobs.generation = refs.generation "
                            "WHERE jobs.lease_until > ? UNION "
                            "SELECT kind, digest FROM artifacts "
                            "WHERE last_seen >= ? UNION "
                            "SELECT edges.target_kind, edges.target_digest "
                            "FROM artifact_edges AS edges JOIN live ON "
                            "edges.source_kind = live.kind AND "
                            "edges.source_digest = live.digest) "
                            "INSERT OR IGNORE INTO live_artifacts "
                            "SELECT kind, digest FROM live",
                            (current, cutoff),
                        )
                        protected = int(
                            database.execute(
                                "SELECT COUNT(*) FROM live_artifacts"
                            ).fetchone()[0]
                        )
                    except sqlite3.OperationalError as error:
                        if "interrupted" not in str(error).lower():
                            raise
                        stop_reason = "time_budget"
                        protected = 0
                    while (
                        stop_reason != "time_budget"
                        and len(deleted) < object_budget
                    ):
                        if time.monotonic() >= deadline:
                            stop_reason = "time_budget"
                            break
                        try:
                            row = database.execute(
                                "SELECT candidate.kind, candidate.digest, "
                                "candidate.encoded_bytes "
                                "FROM artifacts AS candidate "
                                "LEFT JOIN live_artifacts AS live ON "
                                "live.kind = candidate.kind AND "
                                "live.digest = candidate.digest "
                                "WHERE live.digest IS NULL AND NOT EXISTS ("
                                "SELECT 1 FROM artifact_edges AS incoming "
                                "JOIN artifacts AS source ON "
                                "source.kind = incoming.source_kind AND "
                                "source.digest = incoming.source_digest "
                                "LEFT JOIN live_artifacts AS source_live ON "
                                "source_live.kind = source.kind AND "
                                "source_live.digest = source.digest "
                                "WHERE incoming.target_kind = candidate.kind AND "
                                "incoming.target_digest = candidate.digest AND "
                                "source_live.digest IS NULL) "
                                "ORDER BY candidate.last_seen, candidate.kind, "
                                "candidate.digest LIMIT 1"
                            ).fetchone()
                        except sqlite3.OperationalError as error:
                            if "interrupted" not in str(error).lower():
                                raise
                            stop_reason = "time_budget"
                            break
                        if row is None:
                            try:
                                unreachable = int(
                                    database.execute(
                                        "SELECT COUNT(*) FROM artifacts "
                                        "AS candidate LEFT JOIN live_artifacts "
                                        "AS live ON live.kind = candidate.kind "
                                        "AND live.digest = candidate.digest "
                                        "WHERE live.digest IS NULL"
                                    ).fetchone()[0]
                                )
                            except sqlite3.OperationalError as error:
                                if "interrupted" not in str(error).lower():
                                    raise
                                stop_reason = "time_budget"
                                break
                            if unreachable:
                                stop_reason = "dependency_cycle"
                            break
                        artifact = ArtifactRef(
                            str(row["kind"]), str(row["digest"])
                        )
                        encoded_bytes = _bounded_int(
                            row["encoded_bytes"],
                            "indexed artifact bytes",
                            0,
                            1 << 40,
                        )
                        examined += 1
                        if deleted_bytes + encoded_bytes > byte_budget:
                            stop_reason = "byte_budget"
                            break
                        # Once deletion begins, complete its recoverable DB/file
                        # unit even if the selection deadline expires.
                        database.set_progress_handler(None, 0)
                        removed = int(
                            delete_artifact(
                                artifact.kind,
                                artifact.digest,
                                encoded_bytes,
                            )
                        )
                        if removed < 0 or removed > encoded_bytes:
                            raise ArtifactLifecycleError(
                                "artifact deletion callback returned invalid bytes"
                            )
                        database.execute(
                            "DELETE FROM artifact_edges WHERE "
                            "(source_kind = ? AND source_digest = ?) OR "
                            "(target_kind = ? AND target_digest = ?)",
                            (
                                artifact.kind,
                                artifact.digest,
                                artifact.kind,
                                artifact.digest,
                            ),
                        )
                        database.execute(
                            "DELETE FROM job_refs WHERE kind = ? AND digest = ?",
                            (artifact.kind, artifact.digest),
                        )
                        database.execute(
                            "DELETE FROM artifacts WHERE kind = ? AND digest = ?",
                            (artifact.kind, artifact.digest),
                        )
                        deleted.append(artifact)
                        deleted_bytes += removed
                        database.set_progress_handler(
                            interrupt_expired_query, 1000
                        )
                    if (
                        len(deleted) >= object_budget
                        and stop_reason == "complete"
                    ):
                        stop_reason = "object_budget"
                finally:
                    database.set_progress_handler(None, 0)
        return ArtifactCollection(
            tuple(deleted),
            deleted_bytes,
            examined,
            protected,
            expired,
            stop_reason,
        )

    def stats(self, *, now: float | None = None) -> dict[str, int]:
        current = _wall_time(now)
        with self.operation():
            with self._connect() as database:
                return {
                    "artifacts": int(
                        database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                    ),
                    "edges": int(
                        database.execute("SELECT COUNT(*) FROM artifact_edges").fetchone()[0]
                    ),
                    "jobs": int(
                        database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                    ),
                    "active_jobs": int(
                        database.execute(
                            "SELECT COUNT(*) FROM jobs WHERE lease_until > ?",
                            (current,),
                        ).fetchone()[0]
                    ),
                    "active_references": int(
                        database.execute(
                            "SELECT COUNT(*) FROM job_refs AS refs JOIN jobs ON "
                            "jobs.job_id = refs.job_id AND "
                            "jobs.generation = refs.generation "
                            "WHERE jobs.lease_until > ?",
                            (current,),
                        ).fetchone()[0]
                    ),
                    "artifact_bytes": int(
                        database.execute(
                            "SELECT COALESCE(SUM(encoded_bytes), 0) FROM artifacts"
                        ).fetchone()[0]
                    ),
                }


class ArtifactLeaseHeartbeat:
    """Renew one fenced job lease until explicitly closed."""

    def __init__(
        self,
        registry: ArtifactLifecycleRegistry,
        lease: ArtifactJobLease,
        *,
        lease_seconds: float,
        interval_seconds: float | None = None,
    ):
        self.registry = registry
        self.lease = lease
        self.lease_seconds = _finite_float(
            lease_seconds, "artifact job lease_seconds", 0.1, 86_400.0
        )
        default_interval = max(0.05, self.lease_seconds / 3.0)
        self.interval_seconds = _finite_float(
            default_interval if interval_seconds is None else interval_seconds,
            "artifact heartbeat interval_seconds",
            0.01,
            max(0.01, self.lease_seconds * 0.8),
        )
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"qfbv-artifact-heartbeat-{lease.job_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.registry.heartbeat(
                    self.lease,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as error:
                with self._lock:
                    self._error = error
                self._stop.set()
                return
            with self._lock:
                self.lease = renewed

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise ArtifactLifecycleError(
                f"artifact job heartbeat failed: {error}"
            ) from error

    def close(self, *, release: bool = True) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 2.0))
        if self._thread.is_alive():
            raise ArtifactLifecycleError("artifact job heartbeat did not stop")
        self.check()
        if release:
            self.registry.release_job(self.lease)

    def __enter__(self) -> ArtifactLeaseHeartbeat:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
