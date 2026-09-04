#!/usr/bin/env python3
"""Content-addressed QF_BV prefix plans for cross-worker reconstruction."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbv_artifact_lifecycle import (
    LIFECYCLE_PROTOCOL as ARTIFACT_LIFECYCLE_PROTOCOL,
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)


CONTEXT_SCHEMA = "symcc-cross-worker-qfbv-context-v1"
CONTEXT_PROTOCOL = "smtlib-cross-worker-cas-push-pop-v1"
LOWERING_PROTOCOL = "symcc-qfbv-lowering-v1"
MAX_CONTEXT_DEPTH = 4096
MAX_TERM_BYTES = 1024 * 1024
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_OWNER_BYTES = 256
_INPUT_SYMBOL = re.compile(r"(?<![A-Za-z0-9_])symcc_input_([0-9]+)(?![A-Za-z0-9_])")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex_digest(value: Any, name: str) -> str:
    parsed = str(value)
    if _HEX64.fullmatch(parsed) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return parsed


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < lower or parsed > upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def _term_offsets(term: str) -> tuple[int, ...]:
    return tuple(sorted({int(match) for match in _INPUT_SYMBOL.findall(term)}))


@dataclass(frozen=True)
class ContextPublication:
    context_sha256: str
    parent_context_sha256: str
    depth: int
    exact_hit: bool
    created_count: int
    existing_count: int


@dataclass(frozen=True)
class ContextPlan:
    context_sha256: str
    parent_context_sha256: str
    capability_sha256: str
    root_hashes: tuple[str, ...]
    terms: tuple[str, ...]
    offsets: tuple[int, ...]
    formula_sha256: str

    @property
    def depth(self) -> int:
        return len(self.terms)


@dataclass(frozen=True)
class MaterializationLease:
    context_sha256: str
    owner: str
    token: int
    lease_until: float


def _validate_term_value(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("context term must be a non-empty string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("context term must be ASCII SMT-LIB") from error
    if len(encoded) > MAX_TERM_BYTES or "\x00" in value:
        raise ValueError("context term exceeds its transport contract")
    return value


def _context_digest(body: Mapping[str, Any]) -> str:
    normalized = dict(body)
    normalized.pop("context_sha256", None)
    return _digest(_canonical_json(normalized))


def build_context_manifests(
    root_hashes: Sequence[str],
    terms: Sequence[str],
    *,
    capability_sha256: str,
) -> tuple[dict[str, Any], ...]:
    """Build the canonical delta chain without accessing shared storage."""
    if len(root_hashes) != len(terms):
        raise ValueError("context roots and terms have different lengths")
    if not terms:
        return ()
    if len(terms) > MAX_CONTEXT_DEPTH:
        raise ValueError("context chain exceeds its depth bound")
    capability = _hex_digest(capability_sha256, "capability_sha256")
    roots = tuple(_hex_digest(value, "root_hash") for value in root_hashes)
    normalized_terms = tuple(_validate_term_value(value) for value in terms)
    term_offset_rows = tuple(
        tuple(
            _bounded_int(value, "input offset", 0, (1 << 32) - 1)
            for value in _term_offsets(term)
        )
        for term in normalized_terms
    )
    if sum(len(term.encode("ascii")) for term in normalized_terms) > MAX_CHAIN_BYTES:
        raise ValueError("context chain exceeds its byte bound")

    parent = ""
    previous_offsets: tuple[int, ...] = ()
    formula_rows: list[tuple[str, str]] = []
    manifests: list[dict[str, Any]] = []
    for depth, (root_hash, term, term_offsets) in enumerate(
        zip(roots, normalized_terms, term_offset_rows), start=1
    ):
        term_digest = _digest(term.encode("ascii"))
        formula_rows.append((root_hash, term_digest))
        offsets = tuple(sorted(set(previous_offsets).union(term_offsets)))
        delta_offsets = tuple(
            value for value in offsets if value not in previous_offsets
        )
        body: dict[str, Any] = {
            "schema": CONTEXT_SCHEMA,
            "protocol": CONTEXT_PROTOCOL,
            "lowering_protocol": LOWERING_PROTOCOL,
            "logic": "QF_BV",
            "parent_context_sha256": parent,
            "capability_sha256": capability,
            "root_hash": root_hash,
            "term": term,
            "term_sha256": term_digest,
            "delta_offsets": list(delta_offsets),
            "offsets": list(offsets),
            "offsets_sha256": _digest(_canonical_json(offsets)),
            "depth": depth,
            "formula_sha256": _digest(_canonical_json(formula_rows)),
        }
        digest = _context_digest(body)
        body["context_sha256"] = digest
        manifests.append(body)
        parent = digest
        previous_offsets = offsets
    return tuple(manifests)


def context_chain_identity(
    root_hashes: Sequence[str],
    terms: Sequence[str],
    *,
    capability_sha256: str,
) -> dict[str, Any] | None:
    manifests = build_context_manifests(
        root_hashes,
        terms,
        capability_sha256=capability_sha256,
    )
    if not manifests:
        return None
    terminal = manifests[-1]
    return {
        "context_sha256": terminal["context_sha256"],
        "parent_context_sha256": terminal["parent_context_sha256"],
        "capability_sha256": terminal["capability_sha256"],
        "formula_sha256": terminal["formula_sha256"],
        "depth": terminal["depth"],
        "offsets": list(terminal["offsets"]),
    }


class CrossWorkerContextStore:
    """Shared CAS plus fenced, expiring materialization leases."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_contexts: int = 1_000_000,
        max_active_materializations: int = 64,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ):
        self.root = Path(root).resolve()
        self.object_dir = self.root / "objects"
        self.db_path = self.root / "index.sqlite3"
        self.initialize_lock_path = self.root / ".initialize.lock"
        self.max_contexts = _bounded_int(
            max_contexts, "max_contexts", 1, 10_000_000
        )
        self.max_active_materializations = _bounded_int(
            max_active_materializations,
            "max_active_materializations",
            1,
            4096,
        )
        if lifecycle_lease is not None and lifecycle is None:
            raise ValueError("context lifecycle lease requires a registry")
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self.object_dir.mkdir(parents=True, exist_ok=True)
        if self.lifecycle is None:
            self._initialize()
        else:
            with self.lifecycle.maintenance():
                self._initialize()

    def _record_lifecycle_context(
        self,
        manifest: Mapping[str, Any],
        encoded_bytes: int,
        *,
        now: float | None = None,
    ) -> None:
        if self.lifecycle is None:
            return
        parent = str(manifest["parent_context_sha256"])
        edges = (ArtifactRef("context", parent),) if parent else ()
        self.lifecycle.record_artifact(
            ArtifactRef("context", str(manifest["context_sha256"])),
            encoded_bytes=encoded_bytes,
            edges=edges,
            now=now,
        )

    def _touch_lifecycle_context(self, digest: str) -> None:
        if self.lifecycle is not None:
            self.lifecycle.touch(
                (ArtifactRef("context", digest),),
                lease=self.lifecycle_lease,
            )

    def _assert_lifecycle_mode(self) -> None:
        if self.lifecycle is not None:
            return
        with self._connect() as database:
            managed = database.execute(
                "SELECT value FROM store_metadata "
                "WHERE key = 'lifecycle_protocol'"
            ).fetchone()
        if managed is not None:
            raise ValueError(
                "managed context store requires its artifact lifecycle"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for context store initialization")
        descriptor = os.open(
            self.initialize_lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            path_state = os.stat(
                self.initialize_lock_path, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_state.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (path_state.st_dev, path_state.st_ino)
            ):
                raise ValueError("context initialization lock is not stable")
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except InterruptedError:
                    continue
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "context store initialization lock timeout"
                        ) from error
                    time.sleep(min(0.01, remaining))
            locked_state = os.stat(
                self.initialize_lock_path, follow_symlinks=False
            )
            if (opened.st_dev, opened.st_ino) != (
                locked_state.st_dev,
                locked_state.st_ino,
            ):
                raise ValueError(
                    "context initialization lock changed while waiting"
                )
            self._initialize_locked()
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _initialize_locked(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS contexts (
                    context_sha256 TEXT PRIMARY KEY,
                    parent_context_sha256 TEXT NOT NULL,
                    capability_sha256 TEXT NOT NULL,
                    formula_sha256 TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS context_parent
                    ON contexts(parent_context_sha256, capability_sha256, depth);
                CREATE TABLE IF NOT EXISTS materialization_leases (
                    context_sha256 TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    token INTEGER NOT NULL,
                    lease_until REAL NOT NULL,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    FOREIGN KEY(context_sha256) REFERENCES contexts(context_sha256)
                );
                CREATE INDEX IF NOT EXISTS active_materializations
                    ON materialization_leases(lease_until, cancelled, owner);
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            expected = {
                "schema": CONTEXT_SCHEMA,
                "protocol": CONTEXT_PROTOCOL,
                "max_contexts": str(self.max_contexts),
                "max_active_materializations": str(
                    self.max_active_materializations
                ),
            }
            if self.lifecycle is not None:
                expected.update(
                    {
                        "lifecycle_protocol": ARTIFACT_LIFECYCLE_PROTOCOL,
                        "lifecycle_root_sha256": (
                            self.lifecycle.identity_sha256
                        ),
                    }
                )
            for key, value in expected.items():
                row = database.execute(
                    "SELECT value FROM store_metadata WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    database.execute(
                        "INSERT INTO store_metadata(key, value) VALUES(?, ?)",
                        (key, value),
                    )
                elif str(row["value"]) != value:
                    raise ValueError(
                        f"shared context store metadata mismatch for {key}"
                    )
            if self.lifecycle is None:
                managed = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key = 'lifecycle_protocol'"
                ).fetchone()
                if managed is not None:
                    raise ValueError(
                        "managed context store requires its artifact lifecycle"
                    )

    def _object_path(self, context_sha256: str) -> Path:
        digest = _hex_digest(context_sha256, "context_sha256")
        return self.object_dir / digest[:2] / f"{digest}.json"

    @staticmethod
    def _validate_term(value: Any) -> str:
        return _validate_term_value(value)

    @staticmethod
    def _context_digest(body: Mapping[str, Any]) -> str:
        return _context_digest(body)

    def _validate_manifest(
        self,
        raw: Mapping[str, Any],
        *,
        expected_digest: str,
    ) -> dict[str, Any]:
        if set(raw) != {
            "schema",
            "protocol",
            "lowering_protocol",
            "logic",
            "context_sha256",
            "parent_context_sha256",
            "capability_sha256",
            "root_hash",
            "term",
            "term_sha256",
            "delta_offsets",
            "offsets",
            "offsets_sha256",
            "depth",
            "formula_sha256",
        }:
            raise ValueError("context manifest has an unexpected field set")
        if (
            raw.get("schema") != CONTEXT_SCHEMA
            or raw.get("protocol") != CONTEXT_PROTOCOL
            or raw.get("lowering_protocol") != LOWERING_PROTOCOL
            or raw.get("logic") != "QF_BV"
        ):
            raise ValueError("context manifest protocol mismatch")
        digest = _hex_digest(raw.get("context_sha256"), "context_sha256")
        if digest != expected_digest or digest != self._context_digest(raw):
            raise ValueError("context manifest digest mismatch")
        parent = str(raw.get("parent_context_sha256", ""))
        if parent and _HEX64.fullmatch(parent) is None:
            raise ValueError("invalid parent context digest")
        _hex_digest(raw.get("capability_sha256"), "capability_sha256")
        _hex_digest(raw.get("root_hash"), "root_hash")
        term = self._validate_term(raw.get("term"))
        if _hex_digest(raw.get("term_sha256"), "term_sha256") != _digest(
            term.encode("ascii")
        ):
            raise ValueError("context term digest mismatch")
        depth = _bounded_int(raw.get("depth"), "context depth", 1, MAX_CONTEXT_DEPTH)
        if (depth == 1) != (parent == ""):
            raise ValueError("root and parent depth are inconsistent")
        offsets_raw = raw.get("offsets")
        delta_raw = raw.get("delta_offsets")
        if not isinstance(offsets_raw, list) or not isinstance(delta_raw, list):
            raise ValueError("context offsets must be lists")
        offsets = tuple(
            _bounded_int(value, "input offset", 0, (1 << 32) - 1)
            for value in offsets_raw
        )
        delta = tuple(
            _bounded_int(value, "delta input offset", 0, (1 << 32) - 1)
            for value in delta_raw
        )
        if tuple(sorted(set(offsets))) != offsets or tuple(sorted(set(delta))) != delta:
            raise ValueError("context offsets must be sorted and unique")
        if any(value not in offsets for value in delta):
            raise ValueError("delta offsets are not a subset of context offsets")
        if tuple(sorted(_term_offsets(term))) != tuple(
            value for value in offsets if value in _term_offsets(term)
        ):
            raise ValueError("context term contains an invalid input symbol")
        if _hex_digest(raw.get("offsets_sha256"), "offsets_sha256") != _digest(
            _canonical_json(offsets)
        ):
            raise ValueError("context offsets digest mismatch")
        _hex_digest(raw.get("formula_sha256"), "formula_sha256")
        return dict(raw)

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            try:
                written = os.write(descriptor, content[offset:])
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short context artifact write")
            offset += written

    @staticmethod
    def _read_regular(path: Path, max_bytes: int) -> bytes:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for shared context objects")
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ValueError("context object is not a bounded regular file")
            content = bytearray()
            while len(content) <= max_bytes:
                try:
                    chunk = os.read(
                        descriptor,
                        min(1024 * 1024, max_bytes + 1 - len(content)),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
            path_metadata = os.stat(path, follow_symlinks=False)
            if (
                len(content) > max_bytes
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
                or not stat.S_ISREG(path_metadata.st_mode)
            ):
                raise ValueError("context object identity changed during read")
            return bytes(content)
        finally:
            os.close(descriptor)

    def _publish_object(self, digest: str, encoded: bytes) -> bool:
        path = self._object_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = self._read_regular(path, len(encoded))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != encoded:
                raise ValueError(
                    "context artifact pathname has conflicting content"
                )
            return False
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            self._write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(temporary, path)
                created = True
            except FileExistsError:
                created = False
            if self._read_regular(path, len(encoded)) != encoded:
                raise ValueError("context artifact pathname has conflicting content")
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return created
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def publish_chain(
        self,
        root_hashes: Sequence[str],
        terms: Sequence[str],
        *,
        capability_sha256: str,
    ) -> ContextPublication | None:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._publish_chain(
                root_hashes,
                terms,
                capability_sha256=capability_sha256,
            )
        with self.lifecycle.operation():
            return self._publish_chain(
                root_hashes,
                terms,
                capability_sha256=capability_sha256,
            )

    def _publish_chain(
        self,
        root_hashes: Sequence[str],
        terms: Sequence[str],
        *,
        capability_sha256: str,
    ) -> ContextPublication | None:
        manifests = build_context_manifests(
            root_hashes,
            terms,
            capability_sha256=capability_sha256,
        )
        if not manifests:
            return None
        created_count = 0
        existing_count = 0
        terminal_created = False
        rows: list[tuple[Any, ...]] = []
        now = time.time()
        for body in manifests:
            digest = str(body["context_sha256"])
            encoded = _canonical_json(body) + b"\n"
            self._record_lifecycle_context(body, len(encoded), now=now)
            created = self._publish_object(digest, encoded)
            created_count += int(created)
            existing_count += int(not created)
            terminal_created = created
            relative = str(self._object_path(digest).relative_to(self.root))
            rows.append((
                digest,
                body["parent_context_sha256"],
                body["capability_sha256"],
                body["formula_sha256"],
                body["depth"],
                len(encoded),
                relative,
                now,
                now,
            ))
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            count = int(database.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])
            for row in rows:
                digest = str(row[0])
                known = database.execute(
                    "SELECT 1 FROM contexts WHERE context_sha256 = ?", (digest,)
                ).fetchone()
                if known is None and count >= self.max_contexts:
                    raise ValueError("shared context quota is exhausted")
                database.execute(
                    "INSERT OR IGNORE INTO contexts("
                    "context_sha256, parent_context_sha256, capability_sha256, "
                    "formula_sha256, depth, encoded_bytes, relative_path, "
                    "created, last_access) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                count += int(known is None)
                database.execute(
                    "UPDATE contexts SET last_access = ? WHERE context_sha256 = ?",
                    (now, digest),
                )
        terminal = manifests[-1]
        self._touch_lifecycle_context(str(terminal["context_sha256"]))
        return ContextPublication(
            context_sha256=str(terminal["context_sha256"]),
            parent_context_sha256=str(terminal["parent_context_sha256"]),
            depth=len(manifests),
            exact_hit=not terminal_created and created_count == 0,
            created_count=created_count,
            existing_count=existing_count,
        )

    def load_manifest(self, context_sha256: str) -> dict[str, Any]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._load_manifest(context_sha256)
        with self.lifecycle.operation():
            return self._load_manifest(context_sha256)

    def _load_manifest(self, context_sha256: str) -> dict[str, Any]:
        digest = _hex_digest(context_sha256, "context_sha256")
        path = self._object_path(digest)
        encoded = self._read_regular(path, MAX_TERM_BYTES + 64 * 1024)
        if len(encoded) > MAX_TERM_BYTES + 64 * 1024:
            raise ValueError("context manifest exceeds its byte bound")
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("context manifest is not valid JSON") from error
        if not isinstance(parsed, Mapping):
            raise ValueError("context manifest must be an object")
        manifest = self._validate_manifest(parsed, expected_digest=digest)
        self._record_lifecycle_context(manifest, len(encoded))
        self._touch_lifecycle_context(digest)
        return manifest

    def resolve(
        self,
        context_sha256: str,
        *,
        expected_capability_sha256: str | None = None,
    ) -> ContextPlan:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._resolve(
                context_sha256,
                expected_capability_sha256=expected_capability_sha256,
            )
        with self.lifecycle.operation():
            return self._resolve(
                context_sha256,
                expected_capability_sha256=expected_capability_sha256,
            )

    def _resolve(
        self,
        context_sha256: str,
        *,
        expected_capability_sha256: str | None = None,
    ) -> ContextPlan:
        terminal = _hex_digest(context_sha256, "context_sha256")
        expected_capability = (
            _hex_digest(expected_capability_sha256, "capability_sha256")
            if expected_capability_sha256 is not None
            else ""
        )
        reverse: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = terminal
        encoded_bytes = 0
        while current:
            if current in seen or len(reverse) >= MAX_CONTEXT_DEPTH:
                raise ValueError("context chain is cyclic or too deep")
            seen.add(current)
            manifest = self.load_manifest(current)
            encoded_bytes += len(_canonical_json(manifest))
            if encoded_bytes > MAX_CHAIN_BYTES:
                raise ValueError("context chain exceeds its byte bound")
            if expected_capability and manifest["capability_sha256"] != expected_capability:
                raise ValueError("context capability identity mismatch")
            reverse.append(manifest)
            current = str(manifest["parent_context_sha256"])
        chain = list(reversed(reverse))
        if not chain:
            raise ValueError("context chain is empty")
        capability = str(chain[0]["capability_sha256"])
        formula_rows: list[tuple[str, str]] = []
        offsets: set[int] = set()
        previous = ""
        for depth, manifest in enumerate(chain, start=1):
            if (
                int(manifest["depth"]) != depth
                or manifest["parent_context_sha256"] != previous
                or manifest["capability_sha256"] != capability
            ):
                raise ValueError("context parent chain is inconsistent")
            formula_rows.append((manifest["root_hash"], manifest["term_sha256"]))
            previous_offsets = set(offsets)
            offsets.update(_term_offsets(str(manifest["term"])))
            if manifest["offsets"] != sorted(offsets):
                raise ValueError("context cumulative offsets are inconsistent")
            if manifest["delta_offsets"] != sorted(offsets - previous_offsets):
                raise ValueError("context delta offsets are inconsistent")
            if manifest["formula_sha256"] != _digest(_canonical_json(formula_rows)):
                raise ValueError("context cumulative formula identity mismatch")
            previous = str(manifest["context_sha256"])
        return ContextPlan(
            context_sha256=terminal,
            parent_context_sha256=str(chain[-1]["parent_context_sha256"]),
            capability_sha256=capability,
            root_hashes=tuple(str(row["root_hash"]) for row in chain),
            terms=tuple(str(row["term"]) for row in chain),
            offsets=tuple(sorted(offsets)),
            formula_sha256=str(chain[-1]["formula_sha256"]),
        )

    @staticmethod
    def _owner(value: Any) -> str:
        owner = str(value)
        if not owner or "\x00" in owner or len(owner.encode("utf-8")) > MAX_OWNER_BYTES:
            raise ValueError("materialization owner is invalid")
        return owner

    def claim_materialization(
        self,
        context_sha256: str,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        max_active: int | None = None,
    ) -> MaterializationLease | None:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._claim_materialization(
                context_sha256,
                owner,
                lease_seconds=lease_seconds,
                max_active=max_active,
            )
        with self.lifecycle.operation():
            self._touch_lifecycle_context(context_sha256)
            return self._claim_materialization(
                context_sha256,
                owner,
                lease_seconds=lease_seconds,
                max_active=max_active,
            )

    def _claim_materialization(
        self,
        context_sha256: str,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        max_active: int | None = None,
    ) -> MaterializationLease | None:
        digest = _hex_digest(context_sha256, "context_sha256")
        normalized_owner = self._owner(owner)
        duration = float(lease_seconds)
        if not 0.1 <= duration <= 3600.0:
            raise ValueError("materialization lease_seconds must be in [0.1, 3600]")
        quota = self.max_active_materializations
        if max_active is not None and _bounded_int(
            max_active, "max_active", 1, 4096
        ) != quota:
            raise ValueError("materialization quota disagrees with store metadata")
        now = time.time()
        until = now + duration
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(
                "SELECT 1 FROM contexts WHERE context_sha256 = ?", (digest,)
            ).fetchone() is None:
                raise ValueError("cannot lease an unknown context")
            row = database.execute(
                "SELECT owner, token, lease_until, cancelled "
                "FROM materialization_leases WHERE context_sha256 = ?",
                (digest,),
            ).fetchone()
            if row is not None and float(row["lease_until"]) > now and not int(
                row["cancelled"]
            ):
                return None
            active = int(
                database.execute(
                    "SELECT COUNT(*) FROM materialization_leases "
                    "WHERE lease_until > ? AND cancelled = 0",
                    (now,),
                ).fetchone()[0]
            )
            if active >= quota:
                return None
            token = (int(row["token"]) + 1) if row is not None else 1
            database.execute(
                "INSERT INTO materialization_leases("
                "context_sha256, owner, token, lease_until, cancelled, updated"
                ") VALUES(?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(context_sha256) DO UPDATE SET "
                "owner=excluded.owner, token=excluded.token, "
                "lease_until=excluded.lease_until, cancelled=0, "
                "updated=excluded.updated",
                (digest, normalized_owner, token, until, now),
            )
        return MaterializationLease(digest, normalized_owner, token, until)

    def renew_materialization(
        self,
        lease: MaterializationLease,
        *,
        lease_seconds: float = 30.0,
    ) -> MaterializationLease | None:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._renew_materialization(
                lease, lease_seconds=lease_seconds
            )
        with self.lifecycle.operation():
            self._touch_lifecycle_context(lease.context_sha256)
            return self._renew_materialization(
                lease, lease_seconds=lease_seconds
            )

    def _renew_materialization(
        self,
        lease: MaterializationLease,
        *,
        lease_seconds: float = 30.0,
    ) -> MaterializationLease | None:
        duration = float(lease_seconds)
        if not 0.1 <= duration <= 3600.0:
            raise ValueError("materialization lease_seconds must be in [0.1, 3600]")
        now = time.time()
        until = now + duration
        with self._connect() as database:
            cursor = database.execute(
                "UPDATE materialization_leases SET lease_until = ?, updated = ? "
                "WHERE context_sha256 = ? AND owner = ? AND token = ? "
                "AND lease_until > ? AND cancelled = 0",
                (
                    until,
                    now,
                    lease.context_sha256,
                    lease.owner,
                    lease.token,
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return MaterializationLease(
            lease.context_sha256, lease.owner, lease.token, until
        )

    def release_materialization(self, lease: MaterializationLease) -> bool:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._release_materialization(lease)
        with self.lifecycle.operation():
            return self._release_materialization(lease)

    def _release_materialization(self, lease: MaterializationLease) -> bool:
        now = time.time()
        with self._connect() as database:
            cursor = database.execute(
                "UPDATE materialization_leases "
                "SET lease_until = ?, updated = ? "
                "WHERE context_sha256 = ? AND owner = ? AND token = ?",
                (
                    now,
                    now,
                    lease.context_sha256,
                    lease.owner,
                    lease.token,
                ),
            )
        return cursor.rowcount == 1

    def cancel_materialization(self, lease: MaterializationLease) -> bool:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._cancel_materialization(lease)
        with self.lifecycle.operation():
            return self._cancel_materialization(lease)

    def _cancel_materialization(self, lease: MaterializationLease) -> bool:
        now = time.time()
        with self._connect() as database:
            cursor = database.execute(
                "UPDATE materialization_leases "
                "SET cancelled = 1, lease_until = ?, updated = ? "
                "WHERE context_sha256 = ? AND owner = ? AND token = ?",
                (
                    now,
                    now,
                    lease.context_sha256,
                    lease.owner,
                    lease.token,
                ),
            )
        return cursor.rowcount == 1

    def delete_lifecycle_artifact(
        self,
        kind: str,
        digest: str,
        expected_bytes: int,
    ) -> int:
        """Idempotently remove one unreachable context under the GC lock."""
        if kind != "context":
            raise ValueError("context store cannot delete another artifact kind")
        value = _hex_digest(digest, "context_sha256")
        size = _bounded_int(
            expected_bytes,
            "expected context bytes",
            0,
            MAX_TERM_BYTES + 64 * 1024,
        )
        path = self._object_path(value)
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(
                "SELECT 1 FROM contexts WHERE parent_context_sha256 = ? LIMIT 1",
                (value,),
            ).fetchone() is not None:
                raise ValueError("context still has an indexed child")
            if database.execute(
                "SELECT 1 FROM materialization_leases WHERE "
                "context_sha256 = ? AND lease_until > ? AND cancelled = 0",
                (value, now),
            ).fetchone() is not None:
                raise ValueError("context still has an active materialization")
            row = database.execute(
                "SELECT encoded_bytes, relative_path FROM contexts "
                "WHERE context_sha256 = ?",
                (value,),
            ).fetchone()
            if row is not None:
                if int(row["encoded_bytes"]) != size:
                    raise ValueError("context lifecycle size disagrees with index")
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise ValueError("context lifecycle path disagrees with index")
                database.execute(
                    "DELETE FROM materialization_leases WHERE context_sha256 = ?",
                    (value,),
                )
                database.execute(
                    "DELETE FROM contexts WHERE context_sha256 = ?", (value,)
                )
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise ValueError("context lifecycle object is not the expected regular file")
        encoded = self._read_regular(path, MAX_TERM_BYTES + 64 * 1024)
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("context lifecycle object is not valid JSON") from error
        if not isinstance(parsed, Mapping):
            raise ValueError("context lifecycle object must be an object")
        self._validate_manifest(parsed, expected_digest=value)
        path.unlink()
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return len(encoded)

    def synchronize_lifecycle(self, *, max_entries: int) -> dict[str, int | bool]:
        """Import and validate every indexed context before a collection."""
        if self.lifecycle is None:
            raise ValueError("context lifecycle synchronization requires a registry")
        limit = _bounded_int(
            max_entries, "context lifecycle scan limit", 1, 10_000_000
        )
        with self.lifecycle.operation():
            with self._connect() as database:
                count = int(
                    database.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]
                )
                if count > limit:
                    return {"complete": False, "scanned": 0, "total": count}
                rows = database.execute(
                    "SELECT context_sha256, parent_context_sha256, "
                    "capability_sha256, formula_sha256, depth, encoded_bytes, "
                    "relative_path, last_access FROM contexts "
                    "ORDER BY context_sha256"
                ).fetchall()
            for row in rows:
                digest = _hex_digest(row["context_sha256"], "context_sha256")
                path = self._object_path(digest)
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise ValueError("context lifecycle inventory path mismatch")
                encoded = self._read_regular(path, MAX_TERM_BYTES + 64 * 1024)
                if len(encoded) != int(row["encoded_bytes"]):
                    raise ValueError("context lifecycle inventory size mismatch")
                try:
                    parsed = json.loads(encoded)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise ValueError(
                        "context lifecycle inventory is not valid JSON"
                    ) from error
                if not isinstance(parsed, Mapping):
                    raise ValueError("context lifecycle inventory must be an object")
                manifest = self._validate_manifest(
                    parsed, expected_digest=digest
                )
                if (
                    manifest["parent_context_sha256"]
                    != row["parent_context_sha256"]
                    or manifest["capability_sha256"]
                    != row["capability_sha256"]
                    or manifest["formula_sha256"] != row["formula_sha256"]
                    or int(manifest["depth"]) != int(row["depth"])
                ):
                    raise ValueError(
                        "context lifecycle inventory disagrees with index"
                    )
                self._record_lifecycle_context(
                    manifest,
                    len(encoded),
                    now=float(row["last_access"]),
                )
        return {"complete": True, "scanned": count, "total": count}

    def stats(self) -> dict[str, int]:
        now = time.time()
        with self._connect() as database:
            return {
                "contexts": int(database.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]),
                "active_materializations": int(
                    database.execute(
                        "SELECT COUNT(*) FROM materialization_leases "
                        "WHERE lease_until > ? AND cancelled = 0",
                        (now,),
                    ).fetchone()[0]
                ),
                "cancelled_materializations": int(
                    database.execute(
                        "SELECT COUNT(*) FROM materialization_leases "
                        "WHERE cancelled != 0"
                    ).fetchone()[0]
                ),
            }
