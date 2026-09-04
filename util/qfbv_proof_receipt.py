#!/usr/bin/env python3
"""Content-addressed, independently checked QF_BV UNSAT proof receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping, Sequence

from qfbv_artifact_lifecycle import (
    LIFECYCLE_PROTOCOL as ARTIFACT_LIFECYCLE_PROTOCOL,
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)


PROOF_RECEIPT_SCHEMA = "symcc-qfbv-unsat-proof-receipt-v1"
PROOF_PROTOCOL = "symcc-qfbv-cpc-ethos-reference-v1"
PROOF_STORE_SCHEMA = "symcc-qfbv-unsat-proof-store-v1"
PROOF_POLICY_SCHEMA = "symcc-qfbv-unsat-proof-policy-v1"
COMMAND_IDENTITY_SCHEMA = "symcc-command-content-identity-v1"
SIGNATURE_IDENTITY_SCHEMA = "symcc-cpc-signature-tree-identity-v1"
RESULT_KEY_SCHEMA = "symcc-qfbv-unsat-result-key-v1"

MAX_PROOF_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_BYTES = 64 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_SIGNATURE_FILES = 4096
MAX_SIGNATURE_BYTES = 64 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INPUT_DECLARATION = re.compile(
    r"^\(\s*declare-(?:const|fun)\s+symcc_input_([0-9]+)\s+"
    r"(?:\(\s*\)\s+)?\(\s*_\s+BitVec\s+8\s*\)\s*\)$"
)
_FINAL_FALSE = re.compile(r"^\(\s*step\s+[^\s()]+\s+false(?:\s|\))")
_TOP_LEVEL_HEAD = re.compile(r"^\(\s*([^\s()]+)")
_INCOMPLETE_RULE = re.compile(r":rule\s+(?:trust|hole)(?:\s|\))")
_PROOF_FORM_HEADS = frozenset({
    "declare-const",
    "define",
    "assume",
    "assume-push",
    "step",
    "step-pop",
})


class ProofVerificationError(ValueError):
    """A proof, receipt, checker, or content identity failed closed."""


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
        raise ProofVerificationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return parsed


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ProofVerificationError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProofVerificationError(f"{name} must be an integer") from error
    if parsed < lower or parsed > upper:
        raise ProofVerificationError(
            f"{name} must be in [{lower}, {upper}]"
        )
    return parsed


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("short proof artifact write")
        offset += written


def _read_regular(path: Path, max_bytes: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for proof artifacts")
    descriptor = os.open(
        path,
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ProofVerificationError(
                "proof artifact is not a bounded regular file"
            )
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
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if (
            len(content) > max_bytes
            or before_identity != after_identity
            or (after.st_dev, after.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or not stat.S_ISREG(path_metadata.st_mode)
        ):
            raise ProofVerificationError(
                "proof artifact identity changed during read"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _file_identity(path: Path, label: str, *, executable: bool) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    content = _read_regular(resolved, MAX_EXECUTABLE_BYTES)
    mode = resolved.stat(follow_symlinks=False).st_mode
    if executable and mode & 0o111 == 0:
        raise ProofVerificationError(f"{label} is not executable")
    return {
        "label": label,
        "sha256": _digest(content),
        "bytes": len(content),
        "executable": executable,
    }


def _resolve_executable(command: Sequence[str]) -> Path:
    if not command or not str(command[0]):
        raise ProofVerificationError("proof command must not be empty")
    raw = str(command[0])
    discovered = raw if os.path.sep in raw else shutil.which(raw)
    if not discovered:
        raise ProofVerificationError(f"proof executable {raw!r} was not found")
    return Path(discovered).resolve(strict=True)


def _command_identity(
    command: Sequence[str],
    trusted_files: Sequence[str | os.PathLike[str]],
) -> dict[str, Any]:
    if (
        not command
        or len(command) > 128
        or any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or len(item.encode("utf-8")) > 4096
            for item in command
        )
    ):
        raise ProofVerificationError("proof command has an invalid argument")
    executable = _file_identity(
        _resolve_executable(command), "executable", executable=True
    )
    artifacts = [
        _file_identity(Path(path), f"trusted-{index}", executable=False)
        for index, path in enumerate(trusted_files)
    ]
    body: dict[str, Any] = {
        "schema": COMMAND_IDENTITY_SCHEMA,
        "argv_template": ["{executable}", *list(command[1:])],
        "executable": executable,
        "trusted_artifacts": artifacts,
    }
    body["identity_sha256"] = _digest(_canonical_json(body))
    return body


def _signature_tree_identity(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ProofVerificationError("CPC signature root must be a directory")
    resolved = root.resolve(strict=True)
    entries = sorted(resolved.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ProofVerificationError("CPC signature tree contains a symlink")
    files = [
        path for path in entries if path.is_file() and path.suffix == ".eo"
    ]
    if (
        not files
        or len(files) > MAX_SIGNATURE_FILES
        or not (resolved / "Cpc.eo").is_file()
        or not (resolved / "expert" / "CpcExpert.eo").is_file()
    ):
        raise ProofVerificationError("CPC signature tree is incomplete")
    rows: list[dict[str, Any]] = []
    total = 0
    for path in files:
        content = _read_regular(path, MAX_SIGNATURE_BYTES)
        total += len(content)
        if total > MAX_SIGNATURE_BYTES:
            raise ProofVerificationError("CPC signature tree is too large")
        rows.append({
            "path": path.relative_to(resolved).as_posix(),
            "bytes": len(content),
            "sha256": _digest(content),
        })
    body: dict[str, Any] = {
        "schema": SIGNATURE_IDENTITY_SCHEMA,
        "files": rows,
        "file_count": len(rows),
        "total_bytes": total,
    }
    body["identity_sha256"] = _digest(_canonical_json(body))
    return body


def _context_identity(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {
            "context_sha256": "",
            "parent_context_sha256": "",
            "formula_sha256": "",
            "depth": 0,
        }
    depth = _bounded_int(raw.get("depth"), "proof context depth", 1, 4096)
    context = _hex_digest(raw.get("context_sha256"), "context_sha256")
    formula = _hex_digest(raw.get("formula_sha256"), "formula_sha256")
    parent = str(raw.get("parent_context_sha256", ""))
    if parent:
        parent = _hex_digest(parent, "parent_context_sha256")
    if (depth == 1) != (parent == ""):
        raise ProofVerificationError("proof context parent/depth mismatch")
    return {
        "context_sha256": context,
        "parent_context_sha256": parent,
        "formula_sha256": formula,
        "depth": depth,
    }


def _result_key_body(
    *,
    query_id: str,
    smt2_sha256: str,
    proof_query_smt2_sha256: str,
    reference_smt2_sha256: str,
    lowering_certificate_sha256: str,
    capability_sha256: str,
    context: Mapping[str, Any],
    generator_identity_sha256: str,
    checker_identity_sha256: str,
    signature_identity_sha256: str,
    checker_policy_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": RESULT_KEY_SCHEMA,
        "protocol": PROOF_PROTOCOL,
        "logic": "QF_BV",
        "proof_format": "cpc",
        "query_id": query_id,
        "smt2_sha256": smt2_sha256,
        "proof_query_smt2_sha256": proof_query_smt2_sha256,
        "reference_smt2_sha256": reference_smt2_sha256,
        "lowering_certificate_sha256": lowering_certificate_sha256,
        "capability_sha256": capability_sha256,
        "context": dict(context),
        "generator_identity_sha256": generator_identity_sha256,
        "checker_identity_sha256": checker_identity_sha256,
        "signature_identity_sha256": signature_identity_sha256,
        "checker_policy_sha256": checker_policy_sha256,
    }


def _normalize_command_identity(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema",
        "argv_template",
        "executable",
        "trusted_artifacts",
        "identity_sha256",
    }:
        raise ProofVerificationError("invalid command identity field set")
    body = dict(raw)
    digest = _hex_digest(body.pop("identity_sha256"), "command identity")
    if body.get("schema") != COMMAND_IDENTITY_SCHEMA:
        raise ProofVerificationError("invalid command identity schema")
    argv = body.get("argv_template")
    artifacts = body.get("trusted_artifacts")
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "{executable}"
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(artifacts, list)
    ):
        raise ProofVerificationError("invalid command identity payload")
    for expected_label, item in [
        ("executable", body.get("executable")),
        *[(f"trusted-{index}", value) for index, value in enumerate(artifacts)],
    ]:
        if not isinstance(item, Mapping) or set(item) != {
            "label",
            "sha256",
            "bytes",
            "executable",
        }:
            raise ProofVerificationError("invalid command artifact identity")
        if item.get("label") != expected_label:
            raise ProofVerificationError("command artifact order changed")
        _hex_digest(item.get("sha256"), "command artifact digest")
        _bounded_int(
            item.get("bytes"), "command artifact bytes", 1, MAX_EXECUTABLE_BYTES
        )
        if not isinstance(item.get("executable"), bool):
            raise ProofVerificationError("invalid executable identity bit")
    normalized = dict(body)
    normalized["identity_sha256"] = digest
    if digest != _digest(_canonical_json(body)):
        raise ProofVerificationError("command identity digest mismatch")
    return normalized


def _normalize_signature_identity(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema",
        "files",
        "file_count",
        "total_bytes",
        "identity_sha256",
    }:
        raise ProofVerificationError("invalid signature identity field set")
    body = dict(raw)
    digest = _hex_digest(body.pop("identity_sha256"), "signature identity")
    if body.get("schema") != SIGNATURE_IDENTITY_SCHEMA:
        raise ProofVerificationError("invalid signature identity schema")
    files = body.get("files")
    if not isinstance(files, list):
        raise ProofVerificationError("signature identity files must be a list")
    count = _bounded_int(
        body.get("file_count"), "signature file count", 1, MAX_SIGNATURE_FILES
    )
    total = _bounded_int(
        body.get("total_bytes"), "signature total bytes", 1, MAX_SIGNATURE_BYTES
    )
    if len(files) != count:
        raise ProofVerificationError("signature file count mismatch")
    seen: set[str] = set()
    observed_total = 0
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "bytes",
            "sha256",
        }:
            raise ProofVerificationError("invalid signature file identity")
        path = str(item.get("path", ""))
        if (
            not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or path in seen
        ):
            raise ProofVerificationError("invalid signature relative path")
        seen.add(path)
        observed_total += _bounded_int(
            item.get("bytes"), "signature file bytes", 1, MAX_SIGNATURE_BYTES
        )
        _hex_digest(item.get("sha256"), "signature file digest")
    if observed_total != total:
        raise ProofVerificationError("signature byte count mismatch")
    normalized = dict(body)
    normalized["identity_sha256"] = digest
    if digest != _digest(_canonical_json(body)):
        raise ProofVerificationError("signature identity digest mismatch")
    return normalized


def normalize_proof_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a proof receipt without trusting its checker verdict."""
    expected_fields = {
        "schema",
        "protocol",
        "logic",
        "proof_format",
        "verdict",
        "query_id",
        "smt2_sha256",
        "proof_query_smt2_sha256",
        "reference_smt2_sha256",
        "lowering_certificate_sha256",
        "capability_sha256",
        "context",
        "generator_identity",
        "checker_identity",
        "signature_identity",
        "checker_policy_sha256",
        "proof_sha256",
        "proof_bytes",
        "checker_stdout_sha256",
        "result_key_sha256",
        "receipt_sha256",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise ProofVerificationError("invalid proof receipt field set")
    receipt = dict(raw)
    if (
        receipt.get("schema") != PROOF_RECEIPT_SCHEMA
        or receipt.get("protocol") != PROOF_PROTOCOL
        or receipt.get("logic") != "QF_BV"
        or receipt.get("proof_format") != "cpc"
        or receipt.get("verdict") != "correct"
    ):
        raise ProofVerificationError("proof receipt protocol mismatch")
    query_id = str(receipt.get("query_id", ""))
    if not query_id or len(query_id.encode("utf-8")) > 128 or "\x00" in query_id:
        raise ProofVerificationError("invalid proof receipt query id")
    for name in (
        "smt2_sha256",
        "proof_query_smt2_sha256",
        "reference_smt2_sha256",
        "lowering_certificate_sha256",
        "capability_sha256",
        "checker_policy_sha256",
        "proof_sha256",
        "checker_stdout_sha256",
        "result_key_sha256",
        "receipt_sha256",
    ):
        _hex_digest(receipt.get(name), name)
    if receipt["checker_stdout_sha256"] != _digest(b"correct\n"):
        raise ProofVerificationError("proof checker verdict digest mismatch")
    receipt["proof_bytes"] = _bounded_int(
        receipt.get("proof_bytes"), "proof bytes", 1, MAX_PROOF_BYTES
    )
    receipt["context"] = _context_identity(receipt.get("context"))
    receipt["generator_identity"] = _normalize_command_identity(
        receipt.get("generator_identity")
    )
    receipt["checker_identity"] = _normalize_command_identity(
        receipt.get("checker_identity")
    )
    receipt["signature_identity"] = _normalize_signature_identity(
        receipt.get("signature_identity")
    )
    key_body = _result_key_body(
        query_id=query_id,
        smt2_sha256=str(receipt["smt2_sha256"]),
        proof_query_smt2_sha256=str(receipt["proof_query_smt2_sha256"]),
        reference_smt2_sha256=str(receipt["reference_smt2_sha256"]),
        lowering_certificate_sha256=str(
            receipt["lowering_certificate_sha256"]
        ),
        capability_sha256=str(receipt["capability_sha256"]),
        context=receipt["context"],
        generator_identity_sha256=str(
            receipt["generator_identity"]["identity_sha256"]
        ),
        checker_identity_sha256=str(
            receipt["checker_identity"]["identity_sha256"]
        ),
        signature_identity_sha256=str(
            receipt["signature_identity"]["identity_sha256"]
        ),
        checker_policy_sha256=str(receipt["checker_policy_sha256"]),
    )
    if receipt["result_key_sha256"] != _digest(_canonical_json(key_body)):
        raise ProofVerificationError("proof result key mismatch")
    receipt_body = dict(receipt)
    receipt_digest = str(receipt_body.pop("receipt_sha256"))
    if receipt_digest != _digest(_canonical_json(receipt_body)):
        raise ProofVerificationError("proof receipt digest mismatch")
    return receipt


class QfbvProofStore:
    """Shared CAS for portable CPC proof bodies and result receipts."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_objects: int = 1_000_000,
        max_proof_bytes: int = MAX_PROOF_BYTES,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ):
        self.root = Path(root).resolve()
        self.proof_dir = self.root / "proofs"
        self.receipt_dir = self.root / "receipts"
        self.db_path = self.root / "index.sqlite3"
        self.publish_lock_path = self.root / ".publish.lock"
        self.max_objects = _bounded_int(
            max_objects, "max proof objects", 1, 10_000_000
        )
        self.max_proof_bytes = _bounded_int(
            max_proof_bytes, "max proof bytes", 1024, MAX_PROOF_BYTES
        )
        if lifecycle_lease is not None and lifecycle is None:
            raise ProofVerificationError(
                "proof lifecycle lease requires a registry"
            )
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self.proof_dir.mkdir(parents=True, exist_ok=True)
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        if self.lifecycle is None:
            self._initialize()
        else:
            with self.lifecycle.maintenance():
                self._initialize()

    def _record_lifecycle_proof(
        self,
        digest: str,
        encoded_bytes: int,
        *,
        reference: bool,
        now: float | None = None,
    ) -> None:
        if self.lifecycle is None:
            return
        self.lifecycle.record_artifact(
            ArtifactRef("proof", digest),
            encoded_bytes=encoded_bytes,
            lease=self.lifecycle_lease if reference else None,
            now=now,
        )

    def _record_lifecycle_receipt(
        self,
        receipt: Mapping[str, Any],
        encoded_bytes: int,
        *,
        reference: bool,
        now: float | None = None,
    ) -> None:
        if self.lifecycle is None:
            return
        self.lifecycle.record_artifact(
            ArtifactRef("receipt", str(receipt["receipt_sha256"])),
            encoded_bytes=encoded_bytes,
            edges=(ArtifactRef("proof", str(receipt["proof_sha256"])),),
            lease=self.lifecycle_lease if reference else None,
            now=now,
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
            raise ProofVerificationError(
                "managed proof store requires its artifact lifecycle"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS proofs (
                    proof_sha256 TEXT PRIMARY KEY,
                    encoded_bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_sha256 TEXT PRIMARY KEY,
                    result_key_sha256 TEXT NOT NULL,
                    proof_sha256 TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS receipts_by_result
                    ON receipts(result_key_sha256, receipt_sha256);
                CREATE TABLE IF NOT EXISTS result_receipts (
                    result_key_sha256 TEXT PRIMARY KEY,
                    receipt_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            expected = {
                "schema": PROOF_STORE_SCHEMA,
                "protocol": PROOF_PROTOCOL,
                "max_objects": str(self.max_objects),
                "max_proof_bytes": str(self.max_proof_bytes),
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
                    raise ProofVerificationError(
                        f"proof store metadata mismatch for {key}"
                    )
            if self.lifecycle is None:
                managed = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key = 'lifecycle_protocol'"
                ).fetchone()
                if managed is not None:
                    raise ProofVerificationError(
                        "managed proof store requires its artifact lifecycle"
                    )

    def _proof_path(self, digest: str) -> Path:
        value = _hex_digest(digest, "proof_sha256")
        return self.proof_dir / value[:2] / f"{value}.cpc"

    def _receipt_path(self, digest: str) -> Path:
        value = _hex_digest(digest, "receipt_sha256")
        return self.receipt_dir / value[:2] / f"{value}.json"

    @staticmethod
    def _publish_object(path: Path, encoded: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = _read_regular(path, len(encoded))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != encoded:
                raise ProofVerificationError(
                    "proof CAS pathname has conflicting content"
                )
            return False
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(temporary, path)
                created = True
            except FileExistsError:
                created = False
            if _read_regular(path, len(encoded)) != encoded:
                raise ProofVerificationError(
                    "proof CAS pathname has conflicting content"
                )
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
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

    def _publish_locked(
        self,
        proof_body: bytes,
        receipt: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        normalized = normalize_proof_receipt(receipt)
        if len(proof_body) > self.max_proof_bytes:
            raise ProofVerificationError("proof exceeds configured store limit")
        proof_digest = _digest(proof_body)
        if (
            proof_digest != normalized["proof_sha256"]
            or len(proof_body) != normalized["proof_bytes"]
        ):
            raise ProofVerificationError("proof body disagrees with receipt")
        receipt_encoded = _canonical_json(normalized) + b"\n"
        proof_path = self._proof_path(proof_digest)
        receipt_digest = str(normalized["receipt_sha256"])
        receipt_path = self._receipt_path(receipt_digest)
        existing = self.lookup(str(normalized["result_key_sha256"]))
        if existing is not None:
            return existing, existing["receipt_sha256"] == receipt_digest
        now = time.time()
        self._record_lifecycle_proof(
            proof_digest, len(proof_body), reference=False, now=now
        )
        self._record_lifecycle_receipt(
            normalized, len(receipt_encoded), reference=True, now=now
        )
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            proof_known = database.execute(
                "SELECT 1 FROM proofs WHERE proof_sha256 = ?", (proof_digest,)
            ).fetchone()
            receipt_known = database.execute(
                "SELECT 1 FROM receipts WHERE receipt_sha256 = ?",
                (receipt_digest,),
            ).fetchone()
            count = int(
                database.execute(
                    "SELECT (SELECT COUNT(*) FROM proofs) + "
                    "(SELECT COUNT(*) FROM receipts)"
                ).fetchone()[0]
            )
            needed = int(proof_known is None) + int(receipt_known is None)
            if count + needed > self.max_objects:
                raise ProofVerificationError("proof store quota is exhausted")
            self._publish_object(proof_path, proof_body)
            self._publish_object(receipt_path, receipt_encoded)
            database.execute(
                "INSERT OR IGNORE INTO proofs(" 
                "proof_sha256, encoded_bytes, relative_path, created, last_access"
                ") VALUES(?, ?, ?, ?, ?)",
                (
                    proof_digest,
                    len(proof_body),
                    str(proof_path.relative_to(self.root)),
                    now,
                    now,
                ),
            )
            database.execute(
                "INSERT OR IGNORE INTO receipts(" 
                "receipt_sha256, result_key_sha256, proof_sha256, encoded_bytes, "
                "relative_path, created, last_access) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_digest,
                    normalized["result_key_sha256"],
                    proof_digest,
                    len(receipt_encoded),
                    str(receipt_path.relative_to(self.root)),
                    now,
                    now,
                ),
            )
            database.execute(
                "INSERT OR IGNORE INTO result_receipts(" 
                "result_key_sha256, receipt_sha256) VALUES(?, ?)",
                (normalized["result_key_sha256"], receipt_digest),
            )
            winner = database.execute(
                "SELECT receipt_sha256 FROM result_receipts "
                "WHERE result_key_sha256 = ?",
                (normalized["result_key_sha256"],),
            ).fetchone()
        if winner is None:
            raise ProofVerificationError("proof receipt index publication failed")
        winner_digest = str(winner["receipt_sha256"])
        return self.load_receipt(winner_digest), winner_digest == receipt_digest

    def publish(
        self,
        proof_body: bytes,
        receipt: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._publish(
                proof_body, receipt, timeout_ms=timeout_ms
            )
        with self.lifecycle.operation(timeout_ms=timeout_ms or 30_000):
            return self._publish(
                proof_body, receipt, timeout_ms=timeout_ms
            )

    def _publish(
        self,
        proof_body: bytes,
        receipt: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Publish one result under a store-wide cross-process quota lock."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for proof store locking")
        descriptor = os.open(
            self.publish_lock_path,
            os.O_RDWR
            | os.O_CREAT
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            path_metadata = os.stat(
                self.publish_lock_path, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_metadata.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                raise ProofVerificationError(
                    "proof store publication lock is not a stable regular file"
                )
            if timeout_ms is None:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                        break
                    except InterruptedError:
                        continue
            else:
                deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
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
                            raise ProofVerificationError(
                                "proof store publication lock timeout"
                            ) from error
                        time.sleep(min(0.01, remaining))
            locked_path = os.stat(
                self.publish_lock_path, follow_symlinks=False
            )
            if (opened.st_dev, opened.st_ino) != (
                locked_path.st_dev,
                locked_path.st_ino,
            ):
                raise ProofVerificationError(
                    "proof store publication lock changed while waiting"
                )
            return self._publish_locked(proof_body, receipt)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load_proof(self, proof_sha256: str) -> bytes:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._load_proof(proof_sha256)
        with self.lifecycle.operation():
            return self._load_proof(proof_sha256)

    def _load_proof(self, proof_sha256: str) -> bytes:
        digest = _hex_digest(proof_sha256, "proof_sha256")
        with self._connect() as database:
            row = database.execute(
                "SELECT encoded_bytes, relative_path FROM proofs "
                "WHERE proof_sha256 = ?",
                (digest,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"unknown proof object {digest}")
        expected_path = self._proof_path(digest)
        if str(row["relative_path"]) != str(expected_path.relative_to(self.root)):
            raise ProofVerificationError("proof index path mismatch")
        size = _bounded_int(
            row["encoded_bytes"], "indexed proof bytes", 1, self.max_proof_bytes
        )
        content = _read_regular(expected_path, self.max_proof_bytes)
        if len(content) != size or _digest(content) != digest:
            raise ProofVerificationError("proof CAS object digest mismatch")
        with self._connect() as database:
            database.execute(
                "UPDATE proofs SET last_access = ? WHERE proof_sha256 = ?",
                (time.time(), digest),
            )
        self._record_lifecycle_proof(
            digest, len(content), reference=True
        )
        return content

    def load_receipt(self, receipt_sha256: str) -> dict[str, Any]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._load_receipt(receipt_sha256)
        with self.lifecycle.operation():
            return self._load_receipt(receipt_sha256)

    def _load_receipt(self, receipt_sha256: str) -> dict[str, Any]:
        digest = _hex_digest(receipt_sha256, "receipt_sha256")
        with self._connect() as database:
            row = database.execute(
                "SELECT encoded_bytes, relative_path FROM receipts "
                "WHERE receipt_sha256 = ?",
                (digest,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"unknown proof receipt {digest}")
        expected_path = self._receipt_path(digest)
        if str(row["relative_path"]) != str(expected_path.relative_to(self.root)):
            raise ProofVerificationError("proof receipt index path mismatch")
        size = _bounded_int(
            row["encoded_bytes"], "indexed receipt bytes", 1, 1024 * 1024
        )
        encoded = _read_regular(expected_path, 1024 * 1024)
        if len(encoded) != size:
            raise ProofVerificationError("proof receipt size mismatch")
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProofVerificationError("proof receipt is not valid JSON") from error
        normalized = normalize_proof_receipt(parsed)
        if normalized["receipt_sha256"] != digest:
            raise ProofVerificationError("proof receipt pathname mismatch")
        with self._connect() as database:
            database.execute(
                "UPDATE receipts SET last_access = ? WHERE receipt_sha256 = ?",
                (time.time(), digest),
            )
        self._record_lifecycle_receipt(
            normalized, len(encoded), reference=True
        )
        return normalized

    def lookup(self, result_key_sha256: str) -> dict[str, Any] | None:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._lookup(result_key_sha256)
        with self.lifecycle.operation():
            return self._lookup(result_key_sha256)

    def _lookup(self, result_key_sha256: str) -> dict[str, Any] | None:
        key = _hex_digest(result_key_sha256, "result_key_sha256")
        with self._connect() as database:
            row = database.execute(
                "SELECT receipt_sha256 FROM result_receipts "
                "WHERE result_key_sha256 = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        receipt = self.load_receipt(str(row["receipt_sha256"]))
        if receipt["result_key_sha256"] != key:
            raise ProofVerificationError("proof result index mismatch")
        return receipt

    def delete_lifecycle_artifact(
        self,
        kind: str,
        digest: str,
        expected_bytes: int,
    ) -> int:
        """Idempotently remove one unreachable proof or receipt under GC lock."""
        if kind not in {"proof", "receipt"}:
            raise ProofVerificationError(
                "proof store cannot delete another artifact kind"
            )
        value = _hex_digest(digest, f"{kind}_sha256")
        maximum = self.max_proof_bytes if kind == "proof" else 1024 * 1024
        size = _bounded_int(expected_bytes, f"expected {kind} bytes", 0, maximum)
        path = self._proof_path(value) if kind == "proof" else self._receipt_path(value)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if kind == "proof":
                if database.execute(
                    "SELECT 1 FROM receipts WHERE proof_sha256 = ? LIMIT 1",
                    (value,),
                ).fetchone() is not None:
                    raise ProofVerificationError(
                        "proof still has an indexed receipt"
                    )
                row = database.execute(
                    "SELECT encoded_bytes, relative_path FROM proofs "
                    "WHERE proof_sha256 = ?",
                    (value,),
                ).fetchone()
                table = "proofs"
                column = "proof_sha256"
            else:
                row = database.execute(
                    "SELECT encoded_bytes, relative_path FROM receipts "
                    "WHERE receipt_sha256 = ?",
                    (value,),
                ).fetchone()
                table = "receipts"
                column = "receipt_sha256"
            if row is not None:
                if int(row["encoded_bytes"]) != size:
                    raise ProofVerificationError(
                        f"{kind} lifecycle size disagrees with index"
                    )
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise ProofVerificationError(
                        f"{kind} lifecycle path disagrees with index"
                    )
                if kind == "receipt":
                    database.execute(
                        "DELETE FROM result_receipts WHERE receipt_sha256 = ?",
                        (value,),
                    )
                database.execute(
                    f"DELETE FROM {table} WHERE {column} = ?", (value,)
                )
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise ProofVerificationError(
                f"{kind} lifecycle object is not the expected regular file"
            )
        encoded = _read_regular(path, maximum)
        if kind == "proof":
            if _digest(encoded) != value:
                raise ProofVerificationError("proof lifecycle digest mismatch")
        else:
            try:
                parsed = json.loads(encoded)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ProofVerificationError(
                    "receipt lifecycle object is not valid JSON"
                ) from error
            normalized = normalize_proof_receipt(parsed)
            if normalized["receipt_sha256"] != value:
                raise ProofVerificationError("receipt lifecycle digest mismatch")
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
        """Import and validate every indexed proof/receipt before collection."""
        if self.lifecycle is None:
            raise ProofVerificationError(
                "proof lifecycle synchronization requires a registry"
            )
        limit = _bounded_int(
            max_entries, "proof lifecycle scan limit", 1, 10_000_000
        )
        with self.lifecycle.operation():
            with self._connect() as database:
                proof_count = int(
                    database.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
                )
                receipt_count = int(
                    database.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                )
                total = proof_count + receipt_count
                if total > limit:
                    return {"complete": False, "scanned": 0, "total": total}
                invalid_mapping = database.execute(
                    "SELECT 1 FROM ("
                    "SELECT mapping.result_key_sha256 AS mapping_key, "
                    "mapping.receipt_sha256 AS mapping_receipt, "
                    "receipt.result_key_sha256 AS receipt_key, "
                    "receipt.receipt_sha256 AS receipt_digest "
                    "FROM result_receipts AS mapping LEFT JOIN receipts AS receipt "
                    "ON receipt.receipt_sha256 = mapping.receipt_sha256 "
                    "UNION ALL SELECT mapping.result_key_sha256, "
                    "mapping.receipt_sha256, receipt.result_key_sha256, "
                    "receipt.receipt_sha256 FROM receipts AS receipt "
                    "LEFT JOIN result_receipts AS mapping ON "
                    "mapping.receipt_sha256 = receipt.receipt_sha256) AS joined "
                    "WHERE joined.mapping_receipt IS NULL OR "
                    "joined.receipt_digest IS NULL OR "
                    "joined.mapping_key != joined.receipt_key LIMIT 1"
                ).fetchone()
                invalid_dependency = database.execute(
                    "SELECT 1 FROM receipts AS receipt LEFT JOIN proofs AS proof "
                    "ON proof.proof_sha256 = receipt.proof_sha256 "
                    "WHERE proof.proof_sha256 IS NULL LIMIT 1"
                ).fetchone()
                if invalid_mapping is not None or invalid_dependency is not None:
                    raise ProofVerificationError(
                        "proof lifecycle inventory graph is incomplete"
                    )
                proof_rows = database.execute(
                    "SELECT proof_sha256, encoded_bytes, relative_path, last_access "
                    "FROM proofs ORDER BY proof_sha256"
                ).fetchall()
                receipt_rows = database.execute(
                    "SELECT receipt_sha256, result_key_sha256, proof_sha256, "
                    "encoded_bytes, relative_path, last_access FROM receipts "
                    "ORDER BY receipt_sha256"
                ).fetchall()
            for row in proof_rows:
                digest = _hex_digest(row["proof_sha256"], "proof_sha256")
                path = self._proof_path(digest)
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise ProofVerificationError(
                        "proof lifecycle inventory path mismatch"
                    )
                encoded = _read_regular(path, self.max_proof_bytes)
                if (
                    len(encoded) != int(row["encoded_bytes"])
                    or _digest(encoded) != digest
                ):
                    raise ProofVerificationError(
                        "proof lifecycle inventory digest mismatch"
                    )
                self._record_lifecycle_proof(
                    digest,
                    len(encoded),
                    reference=False,
                    now=float(row["last_access"]),
                )
            for row in receipt_rows:
                digest = _hex_digest(row["receipt_sha256"], "receipt_sha256")
                path = self._receipt_path(digest)
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise ProofVerificationError(
                        "receipt lifecycle inventory path mismatch"
                    )
                encoded = _read_regular(path, 1024 * 1024)
                if len(encoded) != int(row["encoded_bytes"]):
                    raise ProofVerificationError(
                        "receipt lifecycle inventory size mismatch"
                    )
                try:
                    parsed = json.loads(encoded)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise ProofVerificationError(
                        "receipt lifecycle inventory is not valid JSON"
                    ) from error
                receipt = normalize_proof_receipt(parsed)
                if (
                    receipt["receipt_sha256"] != digest
                    or receipt["result_key_sha256"]
                    != row["result_key_sha256"]
                    or receipt["proof_sha256"] != row["proof_sha256"]
                ):
                    raise ProofVerificationError(
                        "receipt lifecycle inventory disagrees with index"
                    )
                self._record_lifecycle_receipt(
                    receipt,
                    len(encoded),
                    reference=False,
                    now=float(row["last_access"]),
                )
        return {"complete": True, "scanned": total, "total": total}

    def stats(self) -> dict[str, int]:
        with self._connect() as database:
            return {
                "proofs": int(
                    database.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
                ),
                "receipts": int(
                    database.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                ),
                "result_keys": int(
                    database.execute(
                        "SELECT COUNT(*) FROM result_receipts"
                    ).fetchone()[0]
                ),
                "proof_bytes": int(
                    database.execute(
                        "SELECT COALESCE(SUM(encoded_bytes), 0) FROM proofs"
                    ).fetchone()[0]
                ),
            }


@dataclass(frozen=True)
class ProofAuthorization:
    receipt: dict[str, Any]
    reused: bool
    generator_elapsed_us: int
    checker_elapsed_us: int


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_us: int
    cancelled: bool


ProcessRegister = Callable[
    [subprocess.Popen[bytes]], tuple[int, Event]
]
ProcessUnregister = Callable[[int], None]


def _interrupt_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _split_top_level_forms(text: str) -> list[str]:
    forms: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length:
            if text[index].isspace():
                index += 1
                continue
            if text[index] == ";":
                newline = text.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            break
        if index >= length:
            break
        if text[index] != "(":
            raise ProofVerificationError("CPC output has a non-form token")
        start = index
        depth = 0
        in_string = False
        in_symbol = False
        while index < length:
            character = text[index]
            if in_string:
                if character == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        index += 2
                        continue
                    in_string = False
                index += 1
                continue
            if in_symbol:
                if character == "\\":
                    index += 2
                    continue
                if character == "|":
                    in_symbol = False
                index += 1
                continue
            if character == ";":
                newline = text.find("\n", index)
                if newline < 0:
                    index = length
                    break
                index = newline + 1
                continue
            if character == '"':
                in_string = True
            elif character == "|":
                in_symbol = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    index += 1
                    forms.append(text[start:index].strip())
                    break
                if depth < 0:
                    raise ProofVerificationError("CPC output is unbalanced")
            index += 1
        else:
            raise ProofVerificationError("CPC output is unterminated")
        if depth != 0 or in_string or in_symbol:
            raise ProofVerificationError("CPC output is unterminated")
    return forms


def _extract_cpc_body(output: bytes) -> bytes:
    try:
        text = output.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProofVerificationError("proof generator output is not ASCII") from error
    first_newline = text.find("\n")
    if first_newline < 0 or text[:first_newline].strip() != "unsat":
        raise ProofVerificationError("proof generator did not return UNSAT")
    wrappers = _split_top_level_forms(text[first_newline + 1 :])
    if len(wrappers) != 1:
        raise ProofVerificationError("proof generator returned multiple objects")
    wrapper = wrappers[0]
    if not wrapper.startswith("(") or not wrapper.endswith(")"):
        raise ProofVerificationError("proof generator returned an invalid wrapper")
    forms = _split_top_level_forms(wrapper[1:-1])
    if not forms:
        raise ProofVerificationError("proof generator returned an empty proof")
    return ("\n".join(forms) + "\n").encode("ascii")


def _sanitize_cpc_body(proof_body: bytes, offsets: Sequence[int]) -> bytes:
    if not proof_body or len(proof_body) > MAX_PROOF_BYTES:
        raise ProofVerificationError("CPC proof body is empty or oversized")
    try:
        text = proof_body.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProofVerificationError("CPC proof body is not ASCII") from error
    lowered = text.lower()
    if (
        "\x00" in text
        or "; warning:" in lowered
        or _INCOMPLETE_RULE.search(lowered) is not None
    ):
        raise ProofVerificationError("CPC proof contains an incomplete step")
    expected_offsets = {int(value) for value in offsets}
    retained: list[str] = []
    declared: set[int] = set()
    for form in _split_top_level_forms(text):
        head = form.lstrip().lower()
        head_match = _TOP_LEVEL_HEAD.match(head)
        if head_match is None or head_match.group(1) not in _PROOF_FORM_HEADS:
            raise ProofVerificationError(
                "CPC proof contains a forbidden top-level command"
            )
        declaration = _INPUT_DECLARATION.fullmatch(form)
        if declaration is not None:
            offset = int(declaration.group(1))
            if offset not in expected_offsets or offset in declared:
                raise ProofVerificationError("CPC input declaration is inconsistent")
            declared.add(offset)
            continue
        retained.append(form)
    if (
        not retained
        or not any(form.lstrip().startswith("(assume ") for form in retained)
        or not any(form.lstrip().startswith("(step ") for form in retained)
        or _FINAL_FALSE.match(retained[-1]) is None
    ):
        raise ProofVerificationError(
            "CPC proof is not an explicit refutation ending in false"
        )
    return ("\n".join(retained) + "\n").encode("ascii")


def normalize_qfbv_proof_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a service portfolio proof checker configuration."""
    if not isinstance(raw, Mapping):
        raise ProofVerificationError("UNSAT proof configuration must be an object")

    def command(name: str, placeholder: str) -> list[str]:
        value = raw.get(name)
        if isinstance(value, str):
            import shlex

            parsed = shlex.split(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parsed = [str(item) for item in value]
        else:
            parsed = []
        if not parsed or not any(placeholder in item for item in parsed):
            raise ProofVerificationError(
                f"{name} must contain the {placeholder} placeholder"
            )
        return parsed

    signature_root = str(raw.get("signature_root", ""))
    if not signature_root or "\x00" in signature_root:
        raise ProofVerificationError("CPC signature_root is required")

    def files(name: str) -> list[str]:
        value = raw.get(name, ())
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or len(value) > 64
        ):
            raise ProofVerificationError(f"{name} must be a bounded list")
        return [str(item) for item in value]

    proof_format = str(raw.get("format", "cpc"))
    if proof_format != "cpc":
        raise ProofVerificationError("only CPC proof receipts are supported")
    return {
        "format": "cpc",
        "generator_command": command("generator_command", "{query}"),
        "checker_command": command("checker_command", "{proof}"),
        "signature_root": signature_root,
        "generator_trusted_files": files("generator_trusted_files"),
        "checker_trusted_files": files("checker_trusted_files"),
        "timeout_ms": _bounded_int(
            raw.get("timeout_ms", 30_000),
            "proof timeout_ms",
            1,
            3_600_000,
        ),
    }


class QfbvProofVerifier:
    """Generate, bind, independently check, and reuse CPC UNSAT proofs."""

    def __init__(
        self,
        store: QfbvProofStore,
        *,
        generator_command: Sequence[str],
        checker_command: Sequence[str],
        signature_root: str | os.PathLike[str],
        generator_trusted_files: Sequence[str | os.PathLike[str]] = (),
        checker_trusted_files: Sequence[str | os.PathLike[str]] = (),
        timeout_ms: int = 30_000,
    ):
        self.store = store
        self.generator_command = tuple(str(item) for item in generator_command)
        self.checker_command = tuple(str(item) for item in checker_command)
        if not any("{query}" in item for item in self.generator_command):
            raise ProofVerificationError(
                "proof generator command requires {query}"
            )
        if not any("{proof}" in item for item in self.checker_command):
            raise ProofVerificationError("proof checker command requires {proof}")
        signature_path = Path(signature_root)
        if signature_path.is_symlink():
            raise ProofVerificationError("CPC signature root must not be a symlink")
        self.signature_root = signature_path.resolve(strict=True)
        self.generator_trusted_files = tuple(generator_trusted_files)
        self.checker_trusted_files = tuple(checker_trusted_files)
        self.timeout_ms = _bounded_int(
            timeout_ms, "proof verifier timeout_ms", 1, 3_600_000
        )
        generator, checker, signature = self._current_identities()
        self._generator_identity = generator
        self._checker_identity = checker
        self._signature_identity = signature
        policy_body: dict[str, Any] = {
            "schema": PROOF_POLICY_SCHEMA,
            "protocol": PROOF_PROTOCOL,
            "proof_format": "cpc",
            "generator_identity_sha256": generator["identity_sha256"],
            "checker_identity_sha256": checker["identity_sha256"],
            "signature_identity_sha256": signature["identity_sha256"],
            "max_proof_bytes": store.max_proof_bytes,
        }
        self.policy_sha256 = _digest(_canonical_json(policy_body))

    def _current_identities(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (
            _command_identity(
                self.generator_command, self.generator_trusted_files
            ),
            _command_identity(self.checker_command, self.checker_trusted_files),
            _signature_tree_identity(self.signature_root),
        )

    def _verify_local_policy(self) -> None:
        current = self._current_identities()
        if current != (
            self._generator_identity,
            self._checker_identity,
            self._signature_identity,
        ):
            raise ProofVerificationError(
                "proof checker policy identity changed after initialization"
            )

    def _deadline(self, timeout_ms: int) -> float:
        bounded = max(1, min(int(timeout_ms), self.timeout_ms))
        return time.monotonic() + bounded / 1000.0

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        remaining = int((deadline - time.monotonic()) * 1000.0)
        if remaining <= 0:
            raise ProofVerificationError("proof verification deadline expired")
        return remaining

    @staticmethod
    def _render_command(
        template: Sequence[str],
        *,
        query: Path | None = None,
        proof: Path | None = None,
        timeout_ms: int,
    ) -> list[str]:
        rendered: list[str] = []
        for item in template:
            value = item.replace("{timeout_ms}", str(timeout_ms))
            if query is not None:
                value = value.replace("{query}", str(query))
            if proof is not None:
                value = value.replace("{proof}", str(proof))
            if "{query}" in value or "{proof}" in value:
                raise ProofVerificationError("proof command has an unresolved path")
            rendered.append(value)
        return rendered

    @staticmethod
    def _run_command(
        command: Sequence[str],
        *,
        timeout_ms: int,
        max_stdout: int,
        register_process: ProcessRegister | None,
        unregister_process: ProcessUnregister | None,
    ) -> _CommandResult:
        started = time.monotonic_ns()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process: subprocess.Popen[bytes] = subprocess.Popen(
                    list(command),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as error:
                raise ProofVerificationError(str(error)[:512]) from error
            token = -1
            cancelled = Event()
            if register_process is not None:
                token, cancelled = register_process(process)
            try:
                try:
                    process.wait(timeout=max(0.001, timeout_ms / 1000.0))
                except subprocess.TimeoutExpired as error:
                    _interrupt_process(process)
                    raise ProofVerificationError("proof command timeout") from error
            finally:
                if unregister_process is not None and token >= 0:
                    unregister_process(token)
            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            if stdout_size > max_stdout or stderr_size > MAX_COMMAND_OUTPUT_BYTES:
                raise ProofVerificationError("proof command output exceeds its bound")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return _CommandResult(
                returncode=int(process.returncode),
                stdout=stdout_file.read(max_stdout + 1),
                stderr=stderr_file.read(MAX_COMMAND_OUTPUT_BYTES + 1),
                elapsed_us=(time.monotonic_ns() - started) // 1000,
                cancelled=cancelled.is_set(),
            )

    @staticmethod
    def _path_literal(path: Path) -> str:
        value = str(path)
        if any(character in value for character in ('"', "\n", "\r", "\x00")):
            raise ProofVerificationError("proof checker path is not representable")
        return value

    def _check_proof(
        self,
        proof_body: bytes,
        reference_smt2: bytes,
        offsets: Sequence[int],
        *,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> int:
        deadline = self._deadline(timeout_ms)
        self._verify_local_policy()
        sanitized = _sanitize_cpc_body(proof_body, offsets)
        if (
            not reference_smt2
            or len(reference_smt2) > MAX_REFERENCE_BYTES
            or b"\x00" in reference_smt2
        ):
            raise ProofVerificationError("invalid proof reference SMT2")
        try:
            reference_smt2.decode("ascii")
        except UnicodeDecodeError as error:
            raise ProofVerificationError("proof reference SMT2 is not ASCII") from error
        reference_lower = reference_smt2.lower()
        if any(
            token in reference_lower
            for token in (
                b"(exit",
                b"(reset",
                b"(push",
                b"(pop",
                b"(check-sat",
                b"(get-",
            )
        ):
            raise ProofVerificationError(
                "proof reference SMT2 is not a declaration/assertion problem"
            )
        with tempfile.TemporaryDirectory(prefix="symcc-qfbv-proof-") as directory:
            root = Path(directory)
            reference_path = root / "query.smt2"
            proof_path = root / "proof.cpc"
            reference_path.write_bytes(reference_smt2)
            cpc = (
                f'(include "{self._path_literal(self.signature_root / "Cpc.eo")}")\n'
                f'(include "{self._path_literal(self.signature_root / "expert" / "CpcExpert.eo")}")\n'
                f'(reference "{self._path_literal(reference_path)}")\n'
            ).encode("ascii") + sanitized
            proof_path.write_bytes(cpc)
            remaining = self._remaining_ms(deadline)
            command = self._render_command(
                self.checker_command,
                proof=proof_path,
                timeout_ms=remaining,
            )
            result = self._run_command(
                command,
                timeout_ms=remaining,
                max_stdout=MAX_COMMAND_OUTPUT_BYTES,
                register_process=register_process,
                unregister_process=unregister_process,
            )
        if result.cancelled:
            raise ProofVerificationError("proof checker was cancelled")
        if (
            result.returncode != 0
            or result.stdout != b"correct\n"
            or result.stderr != b""
        ):
            diagnostic = (result.stderr + b"\n" + result.stdout)[-512:]
            raise ProofVerificationError(
                "proof checker did not return a complete refutation: "
                + diagnostic.decode("utf-8", errors="replace")
            )
        return result.elapsed_us

    def check_proof_body(
        self,
        proof_body: bytes,
        reference_smt2: bytes,
        offsets: Sequence[int],
        *,
        timeout_ms: int | None = None,
    ) -> int:
        """Check a raw portable body; used by independent oracles and audits."""
        return self._check_proof(
            proof_body,
            reference_smt2,
            offsets,
            timeout_ms=(self.timeout_ms if timeout_ms is None else timeout_ms),
        )

    def _expected_receipt_inputs(
        self,
        *,
        query_id: str,
        smt2: bytes,
        proof_query_smt2: bytes,
        reference_smt2: bytes,
        lowering_certificate_sha256: str,
        capability_sha256: str,
        context: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        normalized_context = _context_identity(context)
        key_body = _result_key_body(
            query_id=str(query_id),
            smt2_sha256=_digest(smt2),
            proof_query_smt2_sha256=_digest(proof_query_smt2),
            reference_smt2_sha256=_digest(reference_smt2),
            lowering_certificate_sha256=_hex_digest(
                lowering_certificate_sha256,
                "lowering_certificate_sha256",
            ),
            capability_sha256=_hex_digest(
                capability_sha256, "capability_sha256"
            ),
            context=normalized_context,
            generator_identity_sha256=str(
                self._generator_identity["identity_sha256"]
            ),
            checker_identity_sha256=str(
                self._checker_identity["identity_sha256"]
            ),
            signature_identity_sha256=str(
                self._signature_identity["identity_sha256"]
            ),
            checker_policy_sha256=self.policy_sha256,
        )
        return key_body, _digest(_canonical_json(key_body))

    def verify_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        query_id: str,
        smt2: bytes,
        proof_query_smt2: bytes,
        reference_smt2: bytes,
        offsets: Sequence[int],
        lowering_certificate_sha256: str,
        capability_sha256: str,
        context: Mapping[str, Any] | None,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> int:
        deadline = self._deadline(timeout_ms)
        self._verify_local_policy()
        normalized = normalize_proof_receipt(receipt)
        key_body, result_key = self._expected_receipt_inputs(
            query_id=query_id,
            smt2=smt2,
            proof_query_smt2=proof_query_smt2,
            reference_smt2=reference_smt2,
            lowering_certificate_sha256=lowering_certificate_sha256,
            capability_sha256=capability_sha256,
            context=context,
        )
        if (
            normalized["result_key_sha256"] != result_key
            or normalized["query_id"] != str(query_id)
            or normalized["generator_identity"] != self._generator_identity
            or normalized["checker_identity"] != self._checker_identity
            or normalized["signature_identity"] != self._signature_identity
            or normalized["checker_policy_sha256"] != self.policy_sha256
        ):
            raise ProofVerificationError("proof receipt does not match local query policy")
        if result_key != _digest(_canonical_json(key_body)):
            raise ProofVerificationError("proof result identity is unstable")
        proof_body = self.store.load_proof(str(normalized["proof_sha256"]))
        if len(proof_body) != normalized["proof_bytes"]:
            raise ProofVerificationError("proof receipt byte count mismatch")
        return self._check_proof(
            proof_body,
            reference_smt2,
            offsets,
            timeout_ms=self._remaining_ms(deadline),
            register_process=register_process,
            unregister_process=unregister_process,
        )

    def try_reuse(
        self,
        *,
        query_id: str,
        smt2: bytes,
        proof_query_smt2: bytes,
        reference_smt2: bytes,
        offsets: Sequence[int],
        lowering_certificate_sha256: str,
        capability_sha256: str,
        context: Mapping[str, Any] | None,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> ProofAuthorization | None:
        deadline = self._deadline(timeout_ms)
        _, result_key = self._expected_receipt_inputs(
            query_id=query_id,
            smt2=smt2,
            proof_query_smt2=proof_query_smt2,
            reference_smt2=reference_smt2,
            lowering_certificate_sha256=lowering_certificate_sha256,
            capability_sha256=capability_sha256,
            context=context,
        )
        receipt = self.store.lookup(result_key)
        if receipt is None:
            return None
        checker_elapsed = self.verify_receipt(
            receipt,
            query_id=query_id,
            smt2=smt2,
            proof_query_smt2=proof_query_smt2,
            reference_smt2=reference_smt2,
            offsets=offsets,
            lowering_certificate_sha256=lowering_certificate_sha256,
            capability_sha256=capability_sha256,
            context=context,
            timeout_ms=self._remaining_ms(deadline),
            register_process=register_process,
            unregister_process=unregister_process,
        )
        return ProofAuthorization(receipt, True, 0, checker_elapsed)

    def authorize(
        self,
        *,
        query_id: str,
        smt2: bytes,
        proof_query_smt2: bytes,
        reference_smt2: bytes,
        offsets: Sequence[int],
        lowering_certificate_sha256: str,
        capability_sha256: str,
        context: Mapping[str, Any] | None,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> ProofAuthorization:
        deadline = self._deadline(timeout_ms)
        reused = self.try_reuse(
            query_id=query_id,
            smt2=smt2,
            proof_query_smt2=proof_query_smt2,
            reference_smt2=reference_smt2,
            offsets=offsets,
            lowering_certificate_sha256=lowering_certificate_sha256,
            capability_sha256=capability_sha256,
            context=context,
            timeout_ms=self._remaining_ms(deadline),
            register_process=register_process,
            unregister_process=unregister_process,
        )
        if reused is not None:
            return reused
        with tempfile.TemporaryDirectory(prefix="symcc-qfbv-generate-") as directory:
            query_path = Path(directory) / "proof-query.smt2"
            query_path.write_bytes(proof_query_smt2)
            remaining = self._remaining_ms(deadline)
            command = self._render_command(
                self.generator_command,
                query=query_path,
                timeout_ms=remaining,
            )
            generated = self._run_command(
                command,
                timeout_ms=remaining,
                max_stdout=self.store.max_proof_bytes + MAX_COMMAND_OUTPUT_BYTES,
                register_process=register_process,
                unregister_process=unregister_process,
            )
        if generated.cancelled:
            raise ProofVerificationError("proof generator was cancelled")
        if generated.returncode != 0 or generated.stderr.strip():
            diagnostic = (generated.stderr + b"\n" + generated.stdout)[-512:]
            raise ProofVerificationError(
                "proof generator failed: "
                + diagnostic.decode("utf-8", errors="replace")
            )
        proof_body = _sanitize_cpc_body(
            _extract_cpc_body(generated.stdout), offsets
        )
        checker_elapsed = self._check_proof(
            proof_body,
            reference_smt2,
            offsets,
            timeout_ms=self._remaining_ms(deadline),
            register_process=register_process,
            unregister_process=unregister_process,
        )
        key_body, result_key = self._expected_receipt_inputs(
            query_id=query_id,
            smt2=smt2,
            proof_query_smt2=proof_query_smt2,
            reference_smt2=reference_smt2,
            lowering_certificate_sha256=lowering_certificate_sha256,
            capability_sha256=capability_sha256,
            context=context,
        )
        receipt: dict[str, Any] = {
            "schema": PROOF_RECEIPT_SCHEMA,
            "protocol": PROOF_PROTOCOL,
            "logic": "QF_BV",
            "proof_format": "cpc",
            "verdict": "correct",
            "query_id": str(query_id),
            "smt2_sha256": key_body["smt2_sha256"],
            "proof_query_smt2_sha256": key_body["proof_query_smt2_sha256"],
            "reference_smt2_sha256": key_body["reference_smt2_sha256"],
            "lowering_certificate_sha256": lowering_certificate_sha256,
            "capability_sha256": capability_sha256,
            "context": key_body["context"],
            "generator_identity": self._generator_identity,
            "checker_identity": self._checker_identity,
            "signature_identity": self._signature_identity,
            "checker_policy_sha256": self.policy_sha256,
            "proof_sha256": _digest(proof_body),
            "proof_bytes": len(proof_body),
            "checker_stdout_sha256": _digest(b"correct\n"),
            "result_key_sha256": result_key,
        }
        receipt["receipt_sha256"] = _digest(_canonical_json(receipt))
        selected, selected_current = self.store.publish(
            proof_body,
            receipt,
            timeout_ms=self._remaining_ms(deadline),
        )
        if not selected_current:
            checker_elapsed += self.verify_receipt(
                selected,
                query_id=query_id,
                smt2=smt2,
                proof_query_smt2=proof_query_smt2,
                reference_smt2=reference_smt2,
                offsets=offsets,
                lowering_certificate_sha256=lowering_certificate_sha256,
                capability_sha256=capability_sha256,
                context=context,
                timeout_ms=self._remaining_ms(deadline),
                register_process=register_process,
                unregister_process=unregister_process,
            )
        return ProofAuthorization(
            selected,
            not selected_current,
            generated.elapsed_us,
            checker_elapsed,
        )
