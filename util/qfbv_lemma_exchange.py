#!/usr/bin/env python3
"""Proof-carrying QF_BV learned literals for descendant prefix contexts."""

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
from typing import Any, Callable, Mapping, Sequence

from cross_worker_context import (
    CONTEXT_PROTOCOL,
    ContextPlan,
    CrossWorkerContextStore,
)
from qfbv_artifact_lifecycle import (
    LIFECYCLE_PROTOCOL as ARTIFACT_LIFECYCLE_PROTOCOL,
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from qfbv_proof_receipt import (
    ProofVerificationError,
    QfbvProofVerifier,
)


LEMMA_PROTOCOL = "symcc-qfbv-verified-prefix-lemma-v1"
LEMMA_RECORD_SCHEMA = "symcc-qfbv-lemma-record-v1"
LEMMA_STORE_SCHEMA = "symcc-qfbv-lemma-store-v1"
LEMMA_POLICY_SCHEMA = "symcc-qfbv-lemma-policy-v1"
LEMMA_ENTAILMENT_SCHEMA = "symcc-qfbv-lemma-entailment-v1"
MAX_LEMMA_BYTES = 64 * 1024
MAX_LEMMA_NODES = 4096
MAX_LEMMA_DEPTH = 128
MAX_RECORD_BYTES = 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INPUT_SYMBOL = re.compile(r"symcc_input_([0-9]+)")
_BV_BINARY = re.compile(r"#b[01]+")
_BV_HEX = re.compile(r"#x[0-9A-Fa-f]+")
_BV_INDEXED = re.compile(r"bv[0-9]+")
_NUMERAL = re.compile(r"[0-9]+")
_SAFE_ATOMS = frozenset(
    {
        "_",
        "true",
        "false",
        "=",
        "distinct",
        "not",
        "and",
        "or",
        "xor",
        "ite",
        "concat",
        "extract",
        "zero_extend",
        "sign_extend",
        "repeat",
        "rotate_left",
        "rotate_right",
        "bvnot",
        "bvneg",
        "bvand",
        "bvor",
        "bvxor",
        "bvnand",
        "bvnor",
        "bvxnor",
        "bvcomp",
        "bvadd",
        "bvsub",
        "bvmul",
        "bvudiv",
        "bvsdiv",
        "bvurem",
        "bvsrem",
        "bvsmod",
        "bvshl",
        "bvlshr",
        "bvashr",
        "bvult",
        "bvule",
        "bvugt",
        "bvuge",
        "bvslt",
        "bvsle",
        "bvsgt",
        "bvsge",
    }
)
_BOOLEAN_ROOTS = frozenset(
    {
        "=",
        "distinct",
        "not",
        "and",
        "or",
        "xor",
        "bvult",
        "bvule",
        "bvugt",
        "bvuge",
        "bvslt",
        "bvsle",
        "bvsgt",
        "bvsge",
    }
)

ProcessRegister = Callable[[Any], Any]
ProcessUnregister = Callable[[Any], None]


class LemmaExchangeError(ValueError):
    """A lemma artifact, proof, context, or policy failed closed."""


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
        raise LemmaExchangeError(f"{name} must be a lowercase SHA-256 digest")
    return parsed


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise LemmaExchangeError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LemmaExchangeError(f"{name} must be an integer") from error
    if not lower <= parsed <= upper:
        raise LemmaExchangeError(f"{name} must be in [{lower}, {upper}]")
    return parsed


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing lemma artifact")
        view = view[written:]


def _read_regular(path: Path, max_bytes: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for lemma artifact reads")
    descriptor = os.open(
        path,
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise LemmaExchangeError("lemma artifact is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        path_state = os.stat(path, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            len(content) > max_bytes
            or identity
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (before.st_dev, before.st_ino)
            != (path_state.st_dev, path_state.st_ino)
            or not stat.S_ISREG(path_state.st_mode)
        ):
            raise LemmaExchangeError("lemma artifact changed during stable read")
        return content
    finally:
        os.close(descriptor)


def _parse_one_term(text: str) -> Any:
    try:
        encoded = text.encode("ascii")
    except UnicodeEncodeError as error:
        raise LemmaExchangeError("lemma must be ASCII SMT-LIB") from error
    if not encoded or len(encoded) > MAX_LEMMA_BYTES or b"\x00" in encoded:
        raise LemmaExchangeError("lemma exceeds its byte contract")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char in {'"', "|"}:
            raise LemmaExchangeError("quoted lemma atoms are not supported")
        start = index
        while (
            index < len(text)
            and not text[index].isspace()
            and text[index] not in "();"
        ):
            index += 1
        tokens.append(text[start:index])
        if len(tokens) > MAX_LEMMA_NODES * 3:
            raise LemmaExchangeError("lemma has too many tokens")
    stack: list[list[Any]] = []
    forms: list[Any] = []
    for token in tokens:
        if token == "(":
            if len(stack) >= MAX_LEMMA_DEPTH:
                raise LemmaExchangeError("lemma nesting is too deep")
            stack.append([])
        elif token == ")":
            if not stack:
                raise LemmaExchangeError("lemma parentheses are unbalanced")
            completed = stack.pop()
            if not completed:
                raise LemmaExchangeError("empty lemma form is invalid")
            if stack:
                stack[-1].append(completed)
            else:
                forms.append(completed)
        elif stack:
            stack[-1].append(token)
        else:
            forms.append(token)
    if stack or len(forms) != 1 or not isinstance(forms[0], list):
        raise LemmaExchangeError("lemma must contain exactly one compound term")
    return forms[0]


def _canonical_term(value: Any) -> str:
    if isinstance(value, list):
        return "(" + " ".join(_canonical_term(item) for item in value) + ")"
    return str(value)


def _ethos_binary_term(text: str) -> str:
    """Render indexed bit-vector constants in Ethos-referenceable form."""
    parsed = _parse_one_term(text)

    def rewrite(node: Any) -> Any:
        if (
            isinstance(node, list)
            and len(node) == 3
            and node[0] == "_"
            and isinstance(node[1], str)
            and _BV_INDEXED.fullmatch(node[1]) is not None
            and isinstance(node[2], str)
            and _NUMERAL.fullmatch(node[2]) is not None
        ):
            number = int(node[1][2:])
            width = int(node[2])
            if width <= 0 or number < 0 or number >= (1 << width):
                raise LemmaExchangeError("indexed bit-vector literal is out of range")
            return "#b" + format(number, f"0{width}b")
        if isinstance(node, list):
            return [rewrite(child) for child in node]
        return node

    return _canonical_term(rewrite(parsed))


def normalize_lemma_term(
    value: Any,
    *,
    allowed_offsets: Sequence[int],
) -> tuple[str, tuple[int, ...]]:
    parsed = _parse_one_term(str(value))
    if not isinstance(parsed[0], str) or parsed[0] not in _BOOLEAN_ROOTS:
        raise LemmaExchangeError("lemma root is not a supported Boolean operator")
    offsets: set[int] = set()
    node_count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_LEMMA_NODES or depth > MAX_LEMMA_DEPTH:
            raise LemmaExchangeError("lemma exceeds its structural bounds")
        if isinstance(node, list):
            if not node:
                raise LemmaExchangeError("empty lemma form is invalid")
            for child in node:
                visit(child, depth + 1)
            return
        atom = str(node)
        match = _INPUT_SYMBOL.fullmatch(atom)
        if match is not None:
            offsets.add(int(match.group(1)))
            return
        if (
            atom in _SAFE_ATOMS
            or _BV_BINARY.fullmatch(atom) is not None
            or _BV_HEX.fullmatch(atom) is not None
            or _BV_INDEXED.fullmatch(atom) is not None
            or _NUMERAL.fullmatch(atom) is not None
        ):
            return
        raise LemmaExchangeError(f"unsupported lemma atom {atom!r}")

    visit(parsed, 1)
    allowed = {int(offset) for offset in allowed_offsets}
    if not offsets or not offsets <= allowed:
        raise LemmaExchangeError("lemma input symbols escape the source context")
    canonical = _canonical_term(parsed)
    if len(canonical.encode("ascii")) > MAX_LEMMA_BYTES:
        raise LemmaExchangeError("canonical lemma exceeds its byte contract")
    return canonical, tuple(sorted(offsets))


def parse_learned_literal_response(
    output: str,
    *,
    allowed_offsets: Sequence[int],
    max_lemmas: int,
) -> tuple[str, ...]:
    """Parse one exact SAT status followed by one learned-literal list."""
    limit = _bounded_int(max_lemmas, "max_lemmas", 1, 64)
    parsed = _parse_many_forms(output)
    if len(parsed) != 2 or parsed[0] != "sat" or not isinstance(parsed[1], list):
        raise LemmaExchangeError(
            "learned-literal response must be exactly SAT and one literal list"
        )
    learned = parsed[1]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in learned:
        term, _offsets = normalize_lemma_term(
            _canonical_term(raw),
            allowed_offsets=allowed_offsets,
        )
        if term not in seen:
            seen.add(term)
            normalized.append(term)
        if len(normalized) >= limit:
            break
    return tuple(normalized)


def _parse_many_forms(text: str) -> list[Any]:
    """Bounded SMT-LIB response parser used only for solver-produced terms."""
    if len(text.encode("utf-8", errors="replace")) > 8 * 1024 * 1024:
        raise LemmaExchangeError("learned-literal response exceeds 8 MiB")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char in {'"', "|"}:
            quote = char
            start = index
            index += 1
            while index < len(text):
                if text[index] == quote:
                    if quote == '"' and index + 1 < len(text) and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                if quote == "|" and text[index] == "\\":
                    index += 1
                index += 1
            else:
                raise LemmaExchangeError("unterminated solver response atom")
            tokens.append(text[start:index])
            continue
        start = index
        while (
            index < len(text)
            and not text[index].isspace()
            and text[index] not in "();"
        ):
            index += 1
        tokens.append(text[start:index])
        if len(tokens) > 1_000_000:
            raise LemmaExchangeError("learned-literal response has too many tokens")
    forms: list[Any] = []
    stack: list[list[Any]] = []
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            if not stack:
                raise LemmaExchangeError("solver response parentheses are unbalanced")
            completed = stack.pop()
            if stack:
                stack[-1].append(completed)
            else:
                forms.append(completed)
        elif stack:
            stack[-1].append(token)
        else:
            forms.append(token)
    if stack:
        raise LemmaExchangeError("solver response parentheses are unbalanced")
    return forms


def _context_identity(plan: ContextPlan) -> dict[str, Any]:
    return {
        "context_sha256": plan.context_sha256,
        "parent_context_sha256": plan.parent_context_sha256,
        "capability_sha256": plan.capability_sha256,
        "formula_sha256": plan.formula_sha256,
        "depth": plan.depth,
        "offsets": list(plan.offsets),
    }


def _entailment_inputs(
    plan: ContextPlan,
    lemma: str,
    category: str,
) -> dict[str, Any]:
    canonical, offsets = normalize_lemma_term(
        lemma,
        allowed_offsets=plan.offsets,
    )
    body = {
        "schema": LEMMA_ENTAILMENT_SCHEMA,
        "protocol": LEMMA_PROTOCOL,
        "source_context": _context_identity(plan),
        "source_root_hashes_sha256": _digest(_canonical_json(plan.root_hashes)),
        "source_terms_sha256": _digest(_canonical_json(plan.terms)),
        "lemma": canonical,
        "lemma_sha256": _digest(canonical.encode("ascii")),
        "lemma_offsets": list(offsets),
        "category": category,
    }
    identity = _digest(_canonical_json(body))
    declarations = [
        f"(declare-const symcc_input_{offset} (_ BitVec 8))"
        for offset in plan.offsets
    ]
    rows = ["(set-logic QF_BV)", *declarations]
    rows.extend(
        f"(assert {_ethos_binary_term(term)})" for term in plan.terms
    )
    rows.append(f"(assert (not {_ethos_binary_term(canonical)}))")
    reference = ("\n".join(rows) + "\n").encode("ascii")
    proof_query = reference + b"(check-sat)\n(exit)\n"
    return {
        "body": body,
        "identity": identity,
        "query_id": _digest(
            _canonical_json(
                {
                    "schema": LEMMA_ENTAILMENT_SCHEMA,
                    "entailment_sha256": identity,
                }
            )
        ),
        "smt2": proof_query,
        "proof_query_smt2": proof_query,
        "reference_smt2": reference,
        "offsets": plan.offsets,
        "lowering_certificate_sha256": identity,
        "capability_sha256": plan.capability_sha256,
        "context": _context_identity(plan),
    }


def normalize_lemma_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "protocol",
        "lemma",
        "lemma_sha256",
        "lemma_offsets",
        "category",
        "source_context",
        "source_root_hashes_sha256",
        "source_terms_sha256",
        "entailment_sha256",
        "proof_receipt_sha256",
        "checker_policy_sha256",
        "exchange_policy_sha256",
        "result_key_sha256",
        "record_sha256",
    }
    if set(raw) != required:
        raise LemmaExchangeError("lemma record fields are not canonical")
    if raw.get("schema") != LEMMA_RECORD_SCHEMA or raw.get("protocol") != LEMMA_PROTOCOL:
        raise LemmaExchangeError("invalid lemma record schema or protocol")
    source_raw = raw.get("source_context")
    if not isinstance(source_raw, Mapping):
        raise LemmaExchangeError("lemma source context must be an object")
    source_offsets_raw = source_raw.get("offsets")
    if not isinstance(source_offsets_raw, list):
        raise LemmaExchangeError("lemma source offsets must be a list")
    source = {
        "context_sha256": _hex_digest(source_raw.get("context_sha256"), "context_sha256"),
        "parent_context_sha256": str(source_raw.get("parent_context_sha256", "")),
        "capability_sha256": _hex_digest(source_raw.get("capability_sha256"), "capability_sha256"),
        "formula_sha256": _hex_digest(source_raw.get("formula_sha256"), "formula_sha256"),
        "depth": _bounded_int(source_raw.get("depth"), "source depth", 1, 4096),
        "offsets": [
            _bounded_int(value, "source offset", 0, (1 << 63) - 1)
            for value in source_offsets_raw
        ],
    }
    if set(source_raw) != set(source) or source["offsets"] != sorted(set(source["offsets"])):
        raise LemmaExchangeError("lemma source context is not canonical")
    parent = source["parent_context_sha256"]
    if parent and _hex_digest(parent, "parent_context_sha256") != parent:
        raise LemmaExchangeError("invalid lemma source parent context")
    category = str(raw.get("category"))
    if category not in {"preprocess", "input", "solvable", "internal"}:
        raise LemmaExchangeError("unsupported learned literal category")
    lemma, offsets = normalize_lemma_term(
        raw.get("lemma"),
        allowed_offsets=source["offsets"],
    )
    if not isinstance(raw.get("lemma_offsets"), list) or list(offsets) != raw.get(
        "lemma_offsets"
    ):
        raise LemmaExchangeError("lemma offset identity mismatch")
    normalized = {
        "schema": LEMMA_RECORD_SCHEMA,
        "protocol": LEMMA_PROTOCOL,
        "lemma": lemma,
        "lemma_sha256": _hex_digest(raw.get("lemma_sha256"), "lemma_sha256"),
        "lemma_offsets": list(offsets),
        "category": category,
        "source_context": source,
        "source_root_hashes_sha256": _hex_digest(
            raw.get("source_root_hashes_sha256"), "source_root_hashes_sha256"
        ),
        "source_terms_sha256": _hex_digest(
            raw.get("source_terms_sha256"), "source_terms_sha256"
        ),
        "entailment_sha256": _hex_digest(raw.get("entailment_sha256"), "entailment_sha256"),
        "proof_receipt_sha256": _hex_digest(
            raw.get("proof_receipt_sha256"), "proof_receipt_sha256"
        ),
        "checker_policy_sha256": _hex_digest(
            raw.get("checker_policy_sha256"), "checker_policy_sha256"
        ),
        "exchange_policy_sha256": _hex_digest(
            raw.get("exchange_policy_sha256"), "exchange_policy_sha256"
        ),
        "result_key_sha256": _hex_digest(raw.get("result_key_sha256"), "result_key_sha256"),
        "record_sha256": _hex_digest(raw.get("record_sha256"), "record_sha256"),
    }
    if normalized["lemma_sha256"] != _digest(lemma.encode("ascii")):
        raise LemmaExchangeError("lemma content digest mismatch")
    result_body = {
        key: normalized[key]
        for key in (
            "protocol",
            "lemma_sha256",
            "category",
            "source_context",
            "entailment_sha256",
            "checker_policy_sha256",
            "exchange_policy_sha256",
        )
    }
    if normalized["result_key_sha256"] != _digest(_canonical_json(result_body)):
        raise LemmaExchangeError("lemma result key mismatch")
    record_body = dict(normalized)
    record_body.pop("record_sha256")
    if normalized["record_sha256"] != _digest(_canonical_json(record_body)):
        raise LemmaExchangeError("lemma record digest mismatch")
    return normalized


class QfbvLemmaStore:
    """Content-addressed lemma records indexed by their source context."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_records: int = 1_000_000,
        max_bytes: int = 256 * 1024 * 1024,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ):
        root_path = Path(root)
        if root_path.is_symlink():
            raise LemmaExchangeError("lemma store root must not be a symlink")
        self.root = root_path.resolve()
        self.object_dir = self.root / "records"
        self.db_path = self.root / "index.sqlite3"
        self.publish_lock_path = self.root / ".publish.lock"
        self.max_records = _bounded_int(max_records, "max lemma records", 1, 10_000_000)
        self.max_bytes = _bounded_int(
            max_bytes,
            "max lemma store bytes",
            MAX_RECORD_BYTES,
            1 << 40,
        )
        if lifecycle_lease is not None and lifecycle is None:
            raise LemmaExchangeError(
                "lemma lifecycle lease requires a registry"
            )
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self.object_dir.mkdir(parents=True, exist_ok=True)
        if self.lifecycle is None:
            self._initialize()
        else:
            with self.lifecycle.maintenance():
                self._initialize()

    def _record_lifecycle_record(
        self,
        record: Mapping[str, Any],
        encoded_bytes: int,
        *,
        now: float | None = None,
    ) -> None:
        if self.lifecycle is None:
            return
        self.lifecycle.record_artifact(
            ArtifactRef("lemma", str(record["record_sha256"])),
            encoded_bytes=encoded_bytes,
            edges=(
                ArtifactRef(
                    "context",
                    str(record["source_context"]["context_sha256"]),
                ),
                ArtifactRef(
                    "receipt", str(record["proof_receipt_sha256"])
                ),
            ),
            lease=self.lifecycle_lease,
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
            raise LemmaExchangeError(
                "managed lemma store requires its artifact lifecycle"
            )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.db_path, timeout=30.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA synchronous = FULL")
        database.execute("PRAGMA busy_timeout = 30000")
        return database

    def _initialize(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    record_sha256 TEXT PRIMARY KEY,
                    result_key_sha256 TEXT NOT NULL,
                    source_context_sha256 TEXT NOT NULL,
                    source_depth INTEGER NOT NULL,
                    lemma_sha256 TEXT NOT NULL,
                    checker_policy_sha256 TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS records_by_result
                    ON records(result_key_sha256);
                CREATE INDEX IF NOT EXISTS records_by_source
                    ON records(source_context_sha256, source_depth, record_sha256);
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            expected = {
                "schema": LEMMA_STORE_SCHEMA,
                "protocol": LEMMA_PROTOCOL,
                "max_records": str(self.max_records),
                "max_bytes": str(self.max_bytes),
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
                    raise LemmaExchangeError(f"lemma store metadata mismatch for {key}")
            if self.lifecycle is None:
                managed = database.execute(
                    "SELECT value FROM store_metadata "
                    "WHERE key = 'lifecycle_protocol'"
                ).fetchone()
                if managed is not None:
                    raise LemmaExchangeError(
                        "managed lemma store requires its artifact lifecycle"
                    )

    def _path(self, digest: str) -> Path:
        value = _hex_digest(digest, "record_sha256")
        return self.object_dir / value[:2] / f"{value}.json"

    @staticmethod
    def _publish_object(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = _read_regular(path, len(encoded))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != encoded:
                raise LemmaExchangeError("lemma CAS pathname has conflicting content")
            return
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
            except FileExistsError:
                pass
            if _read_regular(path, len(encoded)) != encoded:
                raise LemmaExchangeError("lemma CAS pathname has conflicting content")
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def publish(
        self,
        record: Mapping[str, Any],
        *,
        timeout_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._publish(record, timeout_ms=timeout_ms)
        with self.lifecycle.operation(timeout_ms=timeout_ms):
            return self._publish(record, timeout_ms=timeout_ms)

    def _publish(
        self,
        record: Mapping[str, Any],
        *,
        timeout_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = normalize_lemma_record(record)
        encoded = _canonical_json(normalized) + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise LemmaExchangeError("lemma record exceeds its byte contract")
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for lemma store locking")
        descriptor = os.open(
            self.publish_lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            path_state = os.stat(self.publish_lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_state.st_mode)
                or (opened.st_dev, opened.st_ino) != (path_state.st_dev, path_state.st_ino)
            ):
                raise LemmaExchangeError("lemma publication lock is not stable")
            deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except InterruptedError:
                    continue
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LemmaExchangeError("lemma publication lock timeout") from error
                    time.sleep(min(0.01, remaining))
            locked = os.stat(self.publish_lock_path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (locked.st_dev, locked.st_ino):
                raise LemmaExchangeError("lemma publication lock changed while waiting")
            return self._publish_locked(normalized, encoded)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _publish_locked(
        self,
        record: Mapping[str, Any],
        encoded: bytes,
    ) -> tuple[dict[str, Any], bool]:
        result_key = str(record["result_key_sha256"])
        with self._connect() as database:
            row = database.execute(
                "SELECT record_sha256 FROM records WHERE result_key_sha256 = ?",
                (result_key,),
            ).fetchone()
            if row is not None:
                return self.load(str(row["record_sha256"])), True
            count, total = database.execute(
                "SELECT COUNT(*), COALESCE(SUM(encoded_bytes), 0) FROM records"
            ).fetchone()
            if int(count) >= self.max_records or int(total) + len(encoded) > self.max_bytes:
                raise LemmaExchangeError("lemma store quota is exhausted")
            path = self._path(str(record["record_sha256"]))
            now = time.time()
            self._record_lifecycle_record(record, len(encoded), now=now)
            self._publish_object(path, encoded)
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO records(record_sha256, result_key_sha256, "
                "source_context_sha256, source_depth, lemma_sha256, "
                "checker_policy_sha256, encoded_bytes, relative_path, created, "
                "last_access) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["record_sha256"],
                    result_key,
                    record["source_context"]["context_sha256"],
                    record["source_context"]["depth"],
                    record["lemma_sha256"],
                    record["checker_policy_sha256"],
                    len(encoded),
                    str(path.relative_to(self.root)),
                    now,
                    now,
                ),
            )
        return dict(record), False

    def load(self, record_sha256: str) -> dict[str, Any]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._load(record_sha256)
        with self.lifecycle.operation():
            return self._load(record_sha256)

    def _load(self, record_sha256: str) -> dict[str, Any]:
        digest = _hex_digest(record_sha256, "record_sha256")
        with self._connect() as database:
            row = database.execute(
                "SELECT encoded_bytes, relative_path FROM records WHERE record_sha256 = ?",
                (digest,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"unknown lemma record {digest}")
        path = self._path(digest)
        if str(row["relative_path"]) != str(path.relative_to(self.root)):
            raise LemmaExchangeError("lemma index path mismatch")
        size = _bounded_int(row["encoded_bytes"], "lemma record bytes", 1, MAX_RECORD_BYTES)
        encoded = _read_regular(path, MAX_RECORD_BYTES)
        if len(encoded) != size:
            raise LemmaExchangeError("lemma record size mismatch")
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise LemmaExchangeError("lemma record is not valid JSON") from error
        normalized = normalize_lemma_record(parsed)
        if normalized["record_sha256"] != digest:
            raise LemmaExchangeError("lemma record pathname mismatch")
        with self._connect() as database:
            database.execute(
                "UPDATE records SET last_access = ? WHERE record_sha256 = ?",
                (time.time(), digest),
            )
        self._record_lifecycle_record(normalized, len(encoded))
        return normalized

    def records_for_contexts(
        self,
        contexts: Sequence[str],
        *,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        if self.lifecycle is None:
            self._assert_lifecycle_mode()
            return self._records_for_contexts(contexts, limit=limit)
        with self.lifecycle.operation():
            return self._records_for_contexts(contexts, limit=limit)

    def _records_for_contexts(
        self,
        contexts: Sequence[str],
        *,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        maximum = _bounded_int(limit, "lemma lookup limit", 1, 256)
        normalized_contexts = tuple(
            dict.fromkeys(_hex_digest(value, "context_sha256") for value in contexts)
        )
        digests: list[str] = []
        with self._connect() as database:
            for start in range(0, len(normalized_contexts), 200):
                chunk = normalized_contexts[start : start + 200]
                placeholders = ",".join("?" for _ in chunk)
                rows = database.execute(
                    "SELECT record_sha256 FROM records WHERE source_context_sha256 "
                    f"IN ({placeholders}) ORDER BY source_depth DESC, record_sha256",
                    chunk,
                ).fetchall()
                digests.extend(str(row["record_sha256"]) for row in rows)
        ordered = tuple(dict.fromkeys(digests))
        return tuple(self.load(digest) for digest in ordered[:maximum])

    def delete_lifecycle_artifact(
        self,
        kind: str,
        digest: str,
        expected_bytes: int,
    ) -> int:
        """Idempotently remove one unreachable lemma under the GC lock."""
        if kind != "lemma":
            raise LemmaExchangeError(
                "lemma store cannot delete another artifact kind"
            )
        value = _hex_digest(digest, "record_sha256")
        size = _bounded_int(
            expected_bytes,
            "expected lemma bytes",
            0,
            MAX_RECORD_BYTES,
        )
        path = self._path(value)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT encoded_bytes, relative_path FROM records "
                "WHERE record_sha256 = ?",
                (value,),
            ).fetchone()
            if row is not None:
                if int(row["encoded_bytes"]) != size:
                    raise LemmaExchangeError(
                        "lemma lifecycle size disagrees with index"
                    )
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise LemmaExchangeError(
                        "lemma lifecycle path disagrees with index"
                    )
                database.execute(
                    "DELETE FROM records WHERE record_sha256 = ?", (value,)
                )
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise LemmaExchangeError(
                "lemma lifecycle object is not the expected regular file"
            )
        encoded = _read_regular(path, MAX_RECORD_BYTES)
        try:
            parsed = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise LemmaExchangeError(
                "lemma lifecycle object is not valid JSON"
            ) from error
        normalized = normalize_lemma_record(parsed)
        if normalized["record_sha256"] != value:
            raise LemmaExchangeError("lemma lifecycle digest mismatch")
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
        """Import and validate every indexed lemma before a collection."""
        if self.lifecycle is None:
            raise LemmaExchangeError(
                "lemma lifecycle synchronization requires a registry"
            )
        limit = _bounded_int(
            max_entries, "lemma lifecycle scan limit", 1, 10_000_000
        )
        with self.lifecycle.operation():
            with self._connect() as database:
                count = int(
                    database.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                )
                if count > limit:
                    return {"complete": False, "scanned": 0, "total": count}
                rows = database.execute(
                    "SELECT record_sha256, result_key_sha256, "
                    "source_context_sha256, source_depth, lemma_sha256, "
                    "checker_policy_sha256, encoded_bytes, relative_path, "
                    "last_access FROM records ORDER BY record_sha256"
                ).fetchall()
            for row in rows:
                digest = _hex_digest(row["record_sha256"], "record_sha256")
                path = self._path(digest)
                if str(row["relative_path"]) != str(path.relative_to(self.root)):
                    raise LemmaExchangeError(
                        "lemma lifecycle inventory path mismatch"
                    )
                encoded = _read_regular(path, MAX_RECORD_BYTES)
                if len(encoded) != int(row["encoded_bytes"]):
                    raise LemmaExchangeError(
                        "lemma lifecycle inventory size mismatch"
                    )
                try:
                    parsed = json.loads(encoded)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise LemmaExchangeError(
                        "lemma lifecycle inventory is not valid JSON"
                    ) from error
                record = normalize_lemma_record(parsed)
                if (
                    record["record_sha256"] != digest
                    or record["result_key_sha256"]
                    != row["result_key_sha256"]
                    or record["source_context"]["context_sha256"]
                    != row["source_context_sha256"]
                    or int(record["source_context"]["depth"])
                    != int(row["source_depth"])
                    or record["lemma_sha256"] != row["lemma_sha256"]
                    or record["checker_policy_sha256"]
                    != row["checker_policy_sha256"]
                ):
                    raise LemmaExchangeError(
                        "lemma lifecycle inventory disagrees with index"
                    )
                self._record_lifecycle_record(
                    record,
                    len(encoded),
                    now=float(row["last_access"]),
                )
        return {"complete": True, "scanned": count, "total": count}

    def stats(self) -> dict[str, int]:
        with self._connect() as database:
            count, total = database.execute(
                "SELECT COUNT(*), COALESCE(SUM(encoded_bytes), 0) FROM records"
            ).fetchone()
        return {"records": int(count), "record_bytes": int(total)}


@dataclass(frozen=True)
class LemmaAuthorization:
    record: dict[str, Any]
    proof_reused: bool
    checker_elapsed_us: int


class QfbvLemmaExchange:
    """Certify solver-learned literals and admit them into descendant prefixes."""

    def __init__(
        self,
        store: QfbvLemmaStore,
        context_store: CrossWorkerContextStore,
        proof_verifier: QfbvProofVerifier,
        *,
        timeout_ms: int = 30_000,
    ):
        self.store = store
        self.context_store = context_store
        self.proof_verifier = proof_verifier
        self.timeout_ms = _bounded_int(timeout_ms, "lemma timeout_ms", 1, 3_600_000)
        policy_body = {
            "schema": LEMMA_POLICY_SCHEMA,
            "protocol": LEMMA_PROTOCOL,
            "context_protocol": CONTEXT_PROTOCOL,
            "checker_policy_sha256": proof_verifier.policy_sha256,
            "max_lemma_bytes": MAX_LEMMA_BYTES,
            "max_lemma_nodes": MAX_LEMMA_NODES,
            "max_lemma_depth": MAX_LEMMA_DEPTH,
        }
        self.policy_sha256 = _digest(_canonical_json(policy_body))

    def _deadline(self, timeout_ms: int) -> float:
        bounded = max(1, min(int(timeout_ms), self.timeout_ms))
        return time.monotonic() + bounded / 1000.0

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        remaining = int((deadline - time.monotonic()) * 1000.0)
        if remaining <= 0:
            raise LemmaExchangeError("lemma exchange deadline expired")
        return remaining

    def _resolve(self, digest: str, capability: str = "") -> ContextPlan:
        return self.context_store.resolve(
            digest,
            expected_capability_sha256=capability or None,
        )

    @staticmethod
    def _is_prefix(source: ContextPlan, target: ContextPlan) -> bool:
        return (
            source.capability_sha256 == target.capability_sha256
            and source.depth <= target.depth
            and target.root_hashes[: source.depth] == source.root_hashes
            and target.terms[: source.depth] == source.terms
        )

    def _record(
        self,
        inputs: Mapping[str, Any],
        proof_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        body = inputs["body"]
        result_body = {
            "protocol": LEMMA_PROTOCOL,
            "lemma_sha256": body["lemma_sha256"],
            "category": body["category"],
            "source_context": body["source_context"],
            "entailment_sha256": inputs["identity"],
            "checker_policy_sha256": self.proof_verifier.policy_sha256,
            "exchange_policy_sha256": self.policy_sha256,
        }
        record = {
            "schema": LEMMA_RECORD_SCHEMA,
            "protocol": LEMMA_PROTOCOL,
            "lemma": body["lemma"],
            "lemma_sha256": body["lemma_sha256"],
            "lemma_offsets": body["lemma_offsets"],
            "category": body["category"],
            "source_context": body["source_context"],
            "source_root_hashes_sha256": body["source_root_hashes_sha256"],
            "source_terms_sha256": body["source_terms_sha256"],
            "entailment_sha256": inputs["identity"],
            "proof_receipt_sha256": proof_receipt["receipt_sha256"],
            "checker_policy_sha256": self.proof_verifier.policy_sha256,
            "exchange_policy_sha256": self.policy_sha256,
            "result_key_sha256": _digest(_canonical_json(result_body)),
        }
        record["record_sha256"] = _digest(_canonical_json(record))
        return normalize_lemma_record(record)

    def certify_and_publish(
        self,
        source_context_sha256: str,
        lemma: str,
        *,
        category: str,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> LemmaAuthorization:
        deadline = self._deadline(timeout_ms)
        source = self._resolve(source_context_sha256)
        inputs = _entailment_inputs(source, lemma, category)
        try:
            authorization = self.proof_verifier.authorize(
                query_id=inputs["query_id"],
                smt2=inputs["smt2"],
                proof_query_smt2=inputs["proof_query_smt2"],
                reference_smt2=inputs["reference_smt2"],
                offsets=inputs["offsets"],
                lowering_certificate_sha256=inputs["lowering_certificate_sha256"],
                capability_sha256=inputs["capability_sha256"],
                context=inputs["context"],
                timeout_ms=self._remaining_ms(deadline),
                register_process=register_process,
                unregister_process=unregister_process,
            )
        except ProofVerificationError as error:
            raise LemmaExchangeError(f"lemma entailment proof failed: {error}") from error
        record = self._record(inputs, authorization.receipt)
        winner, record_reused = self.store.publish(
            record,
            timeout_ms=self._remaining_ms(deadline),
        )
        return LemmaAuthorization(
            record=winner,
            proof_reused=authorization.reused or record_reused,
            checker_elapsed_us=authorization.checker_elapsed_us,
        )

    def _ancestor_contexts(self, target: ContextPlan) -> tuple[str, ...]:
        contexts: list[str] = []
        current = target.context_sha256
        seen: set[str] = set()
        while current:
            if current in seen or len(contexts) >= target.depth:
                raise LemmaExchangeError("target context ancestry is inconsistent")
            seen.add(current)
            contexts.append(current)
            manifest = self.context_store.load_manifest(current)
            current = str(manifest["parent_context_sha256"])
        if len(contexts) != target.depth:
            raise LemmaExchangeError("target context depth does not match its ancestry")
        return tuple(contexts)

    def verify_record_for_target(
        self,
        record: Mapping[str, Any],
        target_context_sha256: str,
        *,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> LemmaAuthorization:
        deadline = self._deadline(timeout_ms)
        normalized = normalize_lemma_record(record)
        if normalized["exchange_policy_sha256"] != self.policy_sha256:
            raise LemmaExchangeError("lemma exchange policy does not match local policy")
        target = self._resolve(target_context_sha256)
        source = self._resolve(
            normalized["source_context"]["context_sha256"],
            target.capability_sha256,
        )
        if not self._is_prefix(source, target):
            raise LemmaExchangeError("lemma source is not an ancestor of target context")
        inputs = _entailment_inputs(source, normalized["lemma"], normalized["category"])
        body = inputs["body"]
        if (
            normalized["source_context"] != body["source_context"]
            or normalized["source_root_hashes_sha256"]
            != body["source_root_hashes_sha256"]
            or normalized["source_terms_sha256"] != body["source_terms_sha256"]
            or normalized["entailment_sha256"] != inputs["identity"]
            or normalized["lemma_sha256"] != body["lemma_sha256"]
            or normalized["checker_policy_sha256"]
            != self.proof_verifier.policy_sha256
        ):
            raise LemmaExchangeError("lemma record disagrees with reconstructed entailment")
        receipt = self.proof_verifier.store.load_receipt(
            normalized["proof_receipt_sha256"]
        )
        try:
            checker_elapsed = self.proof_verifier.verify_receipt(
                receipt,
                query_id=inputs["query_id"],
                smt2=inputs["smt2"],
                proof_query_smt2=inputs["proof_query_smt2"],
                reference_smt2=inputs["reference_smt2"],
                offsets=inputs["offsets"],
                lowering_certificate_sha256=inputs["lowering_certificate_sha256"],
                capability_sha256=inputs["capability_sha256"],
                context=inputs["context"],
                timeout_ms=self._remaining_ms(deadline),
                register_process=register_process,
                unregister_process=unregister_process,
            )
        except ProofVerificationError as error:
            raise LemmaExchangeError(f"lemma receipt verification failed: {error}") from error
        return LemmaAuthorization(
            record=normalized,
            proof_reused=True,
            checker_elapsed_us=checker_elapsed,
        )

    def applicable(
        self,
        target_context_sha256: str,
        *,
        limit: int,
        timeout_ms: int,
        register_process: ProcessRegister | None = None,
        unregister_process: ProcessUnregister | None = None,
    ) -> tuple[tuple[LemmaAuthorization, ...], int]:
        deadline = self._deadline(timeout_ms)
        target = self._resolve(target_context_sha256)
        records = self.store.records_for_contexts(
            self._ancestor_contexts(target),
            limit=limit,
        )
        accepted: list[LemmaAuthorization] = []
        rejected = 0
        for record in records:
            try:
                accepted.append(
                    self.verify_record_for_target(
                        record,
                        target.context_sha256,
                        timeout_ms=self._remaining_ms(deadline),
                        register_process=register_process,
                        unregister_process=unregister_process,
                    )
                )
            except (FileNotFoundError, OSError, sqlite3.Error, LemmaExchangeError):
                rejected += 1
        return tuple(accepted), rejected
