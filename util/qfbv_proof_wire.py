#!/usr/bin/env python3
"""External LIDRUP and PalRUP wire interoperability for QF_BV proofs.

The persistent proof store uses a project-native, recursively imported LRUP
DAG.  It deliberately remains separate from both external formats here:

* LIDRUP is exported as a matched ``.icnf`` interaction and ``.lidrup`` proof
  and checked with the independent checker in strict mode.
* PalRUP proof fragments use the SAT 2026 tracer's actual binary directives
  and signed base-128 encoding.  Fragment conversion is an interoperability
  oracle, not by itself a global PalRUP UNSAT confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbv_incremental_proof import (
    CLAUSE_PROTOCOL,
    CLAUSE_RECORD_SCHEMA,
    PROJECT_LRUP_DAG_SCHEMA,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    check_lrup,
    make_unsat_result_receipt,
    normalize_clause,
    normalize_clause_record,
)
from qfbv_incremental_sat import BitBlastPlan


LIDRUP_WIRE_PROTOCOL = "symcc-qfbv-lidrup-wire-v1"
LIDRUP_ARTIFACT_SCHEMA = "symcc-qfbv-lidrup-artifacts-v1"
LIDRUP_CHECKER_POLICY_SCHEMA = "symcc-qfbv-lidrup-checker-policy-v1"
LIDRUP_RECEIPT_SCHEMA = "symcc-qfbv-lidrup-checker-receipt-v1"
LIDRUP_STORE_SCHEMA = "symcc-qfbv-lidrup-wire-store-v1"
PALRUP_BINARY_PROTOCOL = "palrup-sat2026-binary-proof-fragment-v1"
PALRUP_ORACLE_RECEIPT_SCHEMA = "symcc-palrup-fragment-oracle-receipt-v1"

LIDRUP_CHECKER_VERSION = "0.0.7"
LIDRUP_CHECKER_COMMIT = "3ae8c23cd978c313ee14472327bf0f9560601015"
PALRUP_CHECKER_COMMIT = "d9382fb4b0acf094034ee91e2ed0a22b1b479c1d"

MAX_WIRE_BYTES = 256 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_WIRE_LINES = 2_000_000
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_PALRUP_DIRECTIVES = 2_000_000
MAX_PALRUP_VALUES = 32_000_000
MAX_PALRUP_ID = (1 << 63) - 1
MAX_PALRUP_LITERAL = (1 << 31) - 1
MAX_STORE_RECORDS = 10_000_000
MAX_STORE_BYTES = 1 << 44
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")


class ProofWireError(ValueError):
    """A proof wire artifact, checker, policy, or receipt failed closed."""


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
        raise ProofWireError(f"{name} must be a lowercase SHA-256 digest")
    return parsed


def _git_commit(value: Any, name: str) -> str:
    parsed = str(value)
    if _HEX40.fullmatch(parsed) is None:
        raise ProofWireError(f"{name} must be a full lowercase Git object ID")
    return parsed


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ProofWireError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ProofWireError(f"{name} must be a string")
    encoded = value.encode("utf-8")
    if (
        not encoded
        or len(encoded) > 256
        or any(character < 32 or character == 127 for character in encoded)
    ):
        raise ProofWireError(f"{name} is invalid")
    return value


class _BoundedAsciiWriter:
    def __init__(self, maximum: int) -> None:
        self.maximum = _integer(maximum, "wire byte budget", 1, MAX_WIRE_BYTES)
        self.content = bytearray()
        self.lines = 0

    def line(self, fields: Sequence[Any]) -> None:
        encoded = (" ".join(str(field) for field in fields) + "\n").encode("ascii")
        if len(encoded) > MAX_LINE_BYTES:
            raise ProofWireError("proof wire line exceeds its byte bound")
        if (
            self.lines >= MAX_WIRE_LINES
            or len(self.content) + len(encoded) > self.maximum
        ):
            raise ProofWireError("proof wire artifact exceeds its bound")
        self.content.extend(encoded)
        self.lines += 1

    def finish(self) -> bytes:
        return bytes(self.content)


@dataclass(frozen=True)
class LidrupArtifacts:
    interaction: bytes
    proof: bytes
    root_record_sha256: str
    result_receipt_sha256: str
    failed_assumptions: tuple[int, ...]
    learned_clause_count: int

    def metadata(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": LIDRUP_ARTIFACT_SCHEMA,
            "protocol": LIDRUP_WIRE_PROTOCOL,
            "root_record_sha256": self.root_record_sha256,
            "result_receipt_sha256": self.result_receipt_sha256,
            "failed_assumptions": list(self.failed_assumptions),
            "learned_clause_count": self.learned_clause_count,
            "interaction_sha256": _digest(self.interaction),
            "interaction_bytes": len(self.interaction),
            "proof_sha256": _digest(self.proof),
            "proof_bytes": len(self.proof),
        }
        body["artifact_sha256"] = _digest(_canonical_json(body))
        return body


def _clause_fields(prefix: Sequence[Any], clause: Sequence[int]) -> list[Any]:
    return [*prefix, *clause, 0]


def export_lidrup_artifacts(
    plan: BitBlastPlan,
    result_receipt: Mapping[str, Any],
    store: IncrementalProofStore,
    *,
    max_artifact_bytes: int = MAX_WIRE_BYTES,
) -> LidrupArtifacts:
    """Flatten an authorized project proof DAG into canonical strict LIDRUP."""
    maximum = _integer(
        max_artifact_bytes, "LIDRUP artifact byte budget", 1, MAX_WIRE_BYTES
    )
    checker = IncrementalProofChecker(store)
    try:
        result = checker.verify_result_receipt(plan, result_receipt)
    except IncrementalProofError as error:
        raise ProofWireError(str(error)) from error

    interaction = _BoundedAsciiWriter(maximum)
    interaction.line(("p", "icnf"))
    for clause in plan.clauses:
        interaction.line(_clause_fields(("i",), clause))
    interaction.line(_clause_fields(("q",), plan.assumptions))
    interaction.line(("s", "UNSATISFIABLE"))
    interaction.line(_clause_fields(("u",), result.failed_assumptions))

    proof = _BoundedAsciiWriter(maximum)
    proof.line(("p", "lidrup"))
    clause_table: dict[int, tuple[int, ...]] = {}
    for clause_id, clause in enumerate(plan.clauses, 1):
        normalized = tuple(clause)
        clause_table[clause_id] = normalized
        proof.line(_clause_fields(("i", clause_id), normalized))
    proof.line(_clause_fields(("q",), plan.assumptions))

    next_clause_id = len(plan.clauses) + 1
    emitted: dict[str, tuple[int, tuple[int, ...]]] = {}
    visiting: set[str] = set()
    learned = 0

    def emit_record(digest: str) -> tuple[int, tuple[int, ...]]:
        nonlocal learned, next_clause_id
        existing = emitted.get(digest)
        if existing is not None:
            return existing
        if digest in visiting:
            raise ProofWireError("project proof DAG contains a cycle")
        visiting.add(digest)
        try:
            raw = store.load(digest)
            record = normalize_clause_record(raw, max_variable=plan.max_variable)
            if record["record_sha256"] != digest:
                raise ProofWireError("proof-store key differs from record identity")
            base_count = int(record["base_clause_count"])
            if base_count > len(plan.clauses):
                raise ProofWireError("proof fragment exceeds the target CNF prefix")
            local_ids: dict[int, int] = {
                clause_id: clause_id for clause_id in range(1, base_count + 1)
            }
            for imported in record["imports"]:
                child_id, child_clause = emit_record(str(imported["receipt_sha256"]))
                if child_clause != tuple(imported["clause"]):
                    raise ProofWireError("flattened import content changed")
                local_ids[int(imported["local_clause_id"])] = child_id
            final_id = 0
            final_clause: tuple[int, ...] = ()
            for step in record["proof_steps"]:
                try:
                    hints = tuple(local_ids[int(hint)] for hint in step["hints"])
                except KeyError as error:
                    raise ProofWireError(
                        "proof step references an unmapped local clause"
                    ) from error
                clause = tuple(step["clause"])
                output_id = next_clause_id
                next_clause_id += 1
                try:
                    check_lrup(
                        clause_table,
                        clause,
                        hints,
                        max_variable=plan.max_variable,
                    )
                except IncrementalProofError as error:
                    raise ProofWireError(
                        "flattened LIDRUP step is not LRUP: " + str(error)
                    ) from error
                proof.line(["l", output_id, *clause, 0, *hints, 0])
                clause_table[output_id] = clause
                local_ids[int(step["clause_id"])] = output_id
                final_id = output_id
                final_clause = clause
                learned += 1
            if final_clause != tuple(record["shared_clause"]):
                raise ProofWireError(
                    "flattened record does not end in its shared clause"
                )
            emitted[digest] = (final_id, final_clause)
            return emitted[digest]
        finally:
            visiting.discard(digest)

    root_id, root_clause = emit_record(result.clause_receipt_sha256)
    expected = normalize_clause(
        tuple(-literal for literal in result.failed_assumptions),
        max_variable=plan.max_variable,
    )
    if root_clause != expected:
        raise ProofWireError("root proof clause does not encode the UNSAT core")
    proof.line(("s", "UNSATISFIABLE"))
    proof.line(["u", *result.failed_assumptions, 0, root_id, 0])

    return LidrupArtifacts(
        interaction=interaction.finish(),
        proof=proof.finish(),
        root_record_sha256=result.clause_receipt_sha256,
        result_receipt_sha256=result.receipt_sha256,
        failed_assumptions=result.failed_assumptions,
        learned_clause_count=learned,
    )


def _decode_ascii(content: bytes, name: str, maximum: int) -> list[list[str]]:
    if not content or len(content) > maximum or b"\x00" in content:
        raise ProofWireError(f"{name} is empty, oversized, or contains NUL")
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProofWireError(f"{name} is not ASCII") from error
    raw_lines = text.splitlines()
    if len(raw_lines) > MAX_WIRE_LINES:
        raise ProofWireError(f"{name} has too many lines")
    lines: list[list[str]] = []
    for raw in raw_lines:
        if len(raw.encode("ascii")) > MAX_LINE_BYTES:
            raise ProofWireError(f"{name} has an oversized line")
        stripped = raw.strip()
        if not stripped or stripped.startswith("c"):
            continue
        lines.append(stripped.split())
    return lines


def _parse_int(token: str, name: str, lower: int, upper: int) -> int:
    if not token or token.startswith("+"):
        raise ProofWireError(f"{name} has a non-canonical integer")
    try:
        value = int(token, 10)
    except ValueError as error:
        raise ProofWireError(f"{name} has an invalid integer") from error
    if str(value) != token or not lower <= value <= upper:
        raise ProofWireError(f"{name} integer is outside its canonical bound")
    return value


def _terminated_values(
    tokens: Sequence[str],
    name: str,
    *,
    lower: int,
    upper: int,
) -> tuple[int, ...]:
    if not tokens or tokens[-1] != "0" or "0" in tokens[:-1]:
        raise ProofWireError(f"{name} is not terminated exactly once")
    return tuple(_parse_int(token, name, lower, upper) for token in tokens[:-1])


def _two_terminated_values(
    tokens: Sequence[str], name: str
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    zeros = [index for index, token in enumerate(tokens) if token == "0"]
    if len(zeros) != 2 or zeros[-1] != len(tokens) - 1:
        raise ProofWireError(f"{name} requires two terminated integer lists")
    split = zeros[0]
    left = tuple(
        _parse_int(token, name, -MAX_PALRUP_LITERAL, MAX_PALRUP_LITERAL)
        for token in tokens[:split]
    )
    right = tuple(
        _parse_int(token, name, 1, MAX_PALRUP_ID) for token in tokens[split + 1 : -1]
    )
    return left, right


def import_lidrup_artifacts(
    plan: BitBlastPlan,
    interaction: bytes,
    proof: bytes,
    *,
    source_worker: str,
    worker_epoch: int,
    sequence: int,
    max_artifact_bytes: int = MAX_WIRE_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Import the canonical strict subset and recheck it with the LRUP core."""
    maximum = _integer(
        max_artifact_bytes, "LIDRUP import byte budget", 1, MAX_WIRE_BYTES
    )
    interaction_lines = _decode_ascii(interaction, "ICNF interaction", maximum)
    proof_lines = _decode_ascii(proof, "LIDRUP proof", maximum)
    if not interaction_lines or interaction_lines.pop(0) != ["p", "icnf"]:
        raise ProofWireError("ICNF header is missing")
    if not proof_lines or proof_lines.pop(0) != ["p", "lidrup"]:
        raise ProofWireError("LIDRUP header is missing")

    for clause_id, expected_clause in enumerate(plan.clauses, 1):
        if not interaction_lines:
            raise ProofWireError("ICNF input sequence is incomplete")
        interaction_line = interaction_lines.pop(0)
        if not interaction_line or interaction_line[0] != "i":
            raise ProofWireError("ICNF input sequence is incomplete")
        parsed_interaction = _terminated_values(
            interaction_line[1:],
            "ICNF input clause",
            lower=-plan.max_variable,
            upper=plan.max_variable,
        )
        if not proof_lines:
            raise ProofWireError("LIDRUP input sequence is incomplete")
        proof_line = proof_lines.pop(0)
        if len(proof_line) < 3 or proof_line[0] != "i":
            raise ProofWireError("LIDRUP input sequence is incomplete")
        parsed_id = _parse_int(proof_line[1], "LIDRUP input ID", 1, MAX_PALRUP_ID)
        parsed_proof = _terminated_values(
            proof_line[2:],
            "LIDRUP input clause",
            lower=-plan.max_variable,
            upper=plan.max_variable,
        )
        if (
            parsed_id != clause_id
            or parsed_interaction != tuple(expected_clause)
            or parsed_proof != tuple(expected_clause)
        ):
            raise ProofWireError("LIDRUP input differs from the bit-blast plan")

    if not interaction_lines or interaction_lines.pop(0) != [
        "q",
        *map(str, plan.assumptions),
        "0",
    ]:
        raise ProofWireError("ICNF query differs from the bit-blast assumptions")
    if not proof_lines or proof_lines.pop(0) != ["q", *map(str, plan.assumptions), "0"]:
        raise ProofWireError("LIDRUP query differs from the bit-blast assumptions")

    clause_table: dict[int, tuple[int, ...]] = {
        index: tuple(clause) for index, clause in enumerate(plan.clauses, 1)
    }
    steps: list[dict[str, Any]] = []
    expected_id = len(plan.clauses) + 1
    while proof_lines and proof_lines[0][0] == "l":
        line = proof_lines.pop(0)
        if len(line) < 4:
            raise ProofWireError("LIDRUP lemma is incomplete")
        clause_id = _parse_int(line[1], "LIDRUP lemma ID", 1, MAX_PALRUP_ID)
        clause, hints = _two_terminated_values(line[2:], "LIDRUP lemma")
        if clause_id != expected_id or not hints:
            raise ProofWireError("LIDRUP lemma IDs or hints are not canonical")
        normalized = normalize_clause(clause, max_variable=plan.max_variable)
        if tuple(clause) != normalized:
            raise ProofWireError("LIDRUP lemma clause is not canonically ordered")
        try:
            check_lrup(
                clause_table,
                normalized,
                hints,
                max_variable=plan.max_variable,
            )
        except IncrementalProofError as error:
            raise ProofWireError("LIDRUP lemma is not LRUP: " + str(error)) from error
        steps.append(
            {
                "clause_id": clause_id,
                "clause": list(normalized),
                "hints": list(hints),
            }
        )
        clause_table[clause_id] = normalized
        expected_id += 1
    if not steps:
        raise ProofWireError("LIDRUP proof contains no learned clause")
    if not interaction_lines or interaction_lines.pop(0) != ["s", "UNSATISFIABLE"]:
        raise ProofWireError("ICNF UNSAT conclusion is missing")
    if not proof_lines or proof_lines.pop(0) != ["s", "UNSATISFIABLE"]:
        raise ProofWireError("LIDRUP UNSAT conclusion is missing")
    if len(interaction_lines) != 1 or interaction_lines[0][0] != "u":
        raise ProofWireError("ICNF UNSAT core is missing or has trailing commands")
    failed = _terminated_values(
        interaction_lines[0][1:],
        "ICNF UNSAT core",
        lower=-plan.max_variable,
        upper=plan.max_variable,
    )
    if 0 in failed:
        raise ProofWireError("ICNF UNSAT core contains a zero literal")
    if len(proof_lines) != 1 or proof_lines[0][0] != "u":
        raise ProofWireError("LIDRUP UNSAT core is missing or has trailing commands")
    proof_failed, core_hints = _two_terminated_values(
        proof_lines[0][1:], "LIDRUP UNSAT core"
    )
    canonical_failed = tuple(sorted(set(failed)))
    if (
        failed != canonical_failed
        or proof_failed != failed
        or not failed
        or not set(failed) <= set(plan.assumptions)
        or core_hints != (steps[-1]["clause_id"],)
    ):
        raise ProofWireError("LIDRUP UNSAT core is outside the canonical contract")
    expected_final = normalize_clause(
        tuple(-literal for literal in failed), max_variable=plan.max_variable
    )
    if tuple(steps[-1]["clause"]) != expected_final:
        raise ProofWireError("LIDRUP final lemma does not encode its UNSAT core")

    prefix_sha256 = _digest(_canonical_json(plan.clauses))
    body: dict[str, Any] = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROJECT_LRUP_DAG_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": plan.formula_sha256,
        "cnf_sha256": prefix_sha256,
        "base_clause_count": len(plan.clauses),
        "max_variable": plan.max_variable,
        "source_worker": _identity(source_worker, "LIDRUP source worker"),
        "worker_epoch": _integer(worker_epoch, "worker epoch", 0, MAX_PALRUP_ID),
        "sequence": _integer(sequence, "proof sequence", 0, MAX_PALRUP_ID),
        "dependency_assumptions": list(failed),
        "imports": [],
        "proof_steps": steps,
        "shared_clause": list(expected_final),
    }
    body["record_sha256"] = _digest(_canonical_json(body))
    try:
        record = normalize_clause_record(body, max_variable=plan.max_variable)
        IncrementalProofChecker().verify_clause_record(plan, record)
    except IncrementalProofError as error:
        raise ProofWireError(
            "imported LIDRUP proof failed replay: " + str(error)
        ) from error
    result = make_unsat_result_receipt(plan, record["record_sha256"], failed)
    return record, result


def _read_regular(path: Path, maximum: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ProofWireError("O_NOFOLLOW is required for checker identity")
    descriptor = os.open(path, os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ProofWireError("checker artifact is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ProofWireError("checker artifact exceeds its byte bound")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProofWireError("checker artifact changed during identity read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _executable_identity(
    path: str | os.PathLike[str],
) -> tuple[Path, dict[str, Any], bytes]:
    raw = str(path)
    discovered = raw if os.path.sep in raw else shutil.which(raw)
    if not discovered:
        raise ProofWireError(f"checker executable {raw!r} was not found")
    resolved = Path(discovered).resolve(strict=True)
    content = _read_regular(resolved, MAX_EXECUTABLE_BYTES)
    mode = resolved.stat(follow_symlinks=False).st_mode
    if mode & 0o111 == 0:
        raise ProofWireError("checker artifact is not executable")
    return (
        resolved,
        {"sha256": _digest(content), "bytes": len(content)},
        content,
    )


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_us: int


def _interrupt(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=1.0)


def _run_bounded(command: Sequence[str], *, timeout_ms: int) -> _ProcessResult:
    started = time.monotonic_ns()
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
        )
    except OSError as error:
        raise ProofWireError(str(error)[:512]) from error
    assert process.stdout is not None and process.stderr is not None
    stdout_descriptor = process.stdout.fileno()
    stderr_descriptor = process.stderr.fileno()
    streams = {
        stdout_descriptor: bytearray(),
        stderr_descriptor: bytearray(),
    }
    selector = selectors.DefaultSelector()
    try:
        for descriptor in streams:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _interrupt(process)
                raise ProofWireError("proof checker timed out")
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                events = [
                    (key, selectors.EVENT_READ)
                    for key in tuple(selector.get_map().values())
                ]
            for key, _mask in events:
                descriptor = int(key.fd)
                try:
                    chunk = os.read(descriptor, 16 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                streams[descriptor].extend(chunk)
                if len(streams[descriptor]) > MAX_COMMAND_OUTPUT_BYTES:
                    _interrupt(process)
                    raise ProofWireError("proof checker output exceeds its byte bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _interrupt(process)
            raise ProofWireError("proof checker timed out")
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _interrupt(process)
        raise ProofWireError("proof checker timed out") from error
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return _ProcessResult(
        returncode=int(process.returncode),
        stdout=bytes(streams[stdout_descriptor]),
        stderr=bytes(streams[stderr_descriptor]),
        elapsed_us=(time.monotonic_ns() - started) // 1000,
    )


def _write_executable_snapshot(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o700,
    )
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short checker snapshot write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


class LidrupExternalChecker:
    """Pinned independent LIDRUP checker using the matched strict interface."""

    def __init__(
        self,
        checker: str | os.PathLike[str],
        *,
        checker_sha256: str,
        checker_version: str = LIDRUP_CHECKER_VERSION,
        source_commit: str = LIDRUP_CHECKER_COMMIT,
        timeout_ms: int = 30_000,
    ) -> None:
        self.path, identity, content = _executable_identity(checker)
        expected_sha256 = _hex_digest(checker_sha256, "LIDRUP checker")
        if identity["sha256"] != expected_sha256:
            raise ProofWireError("LIDRUP checker content identity differs from policy")
        self.checker_version = _identity(checker_version, "LIDRUP checker version")
        self.source_commit = _git_commit(source_commit, "LIDRUP checker source commit")
        self.timeout_ms = _integer(timeout_ms, "checker timeout", 1, 3_600_000)
        self._content = content
        with tempfile.TemporaryDirectory(prefix="symcc-lidrup-version-") as directory:
            snapshot = _write_executable_snapshot(
                Path(directory), "lidrup-check", self._content
            )
            version = _run_bounded([str(snapshot), "--version"], timeout_ms=5_000)
        try:
            reported_version = version.stdout.decode("ascii", "strict").strip()
        except UnicodeDecodeError as error:
            raise ProofWireError("LIDRUP checker version is not ASCII") from error
        if (
            version.returncode != 0
            or version.stderr
            or reported_version != self.checker_version
        ):
            raise ProofWireError("LIDRUP checker version probe failed")
        self._identity = identity
        policy: dict[str, Any] = {
            "schema": LIDRUP_CHECKER_POLICY_SCHEMA,
            "protocol": LIDRUP_WIRE_PROTOCOL,
            "checker_sha256": identity["sha256"],
            "checker_bytes": identity["bytes"],
            "checker_version": self.checker_version,
            "source_commit": self.source_commit,
            "mode": "strict",
        }
        self.policy_sha256 = _digest(_canonical_json(policy))

    def _verify_identity(self) -> None:
        current_path, current, _content = _executable_identity(self.path)
        if current_path != self.path or current != self._identity:
            raise ProofWireError("LIDRUP checker identity changed after initialization")

    def verify(
        self,
        plan: BitBlastPlan,
        result_receipt: Mapping[str, Any],
        store: IncrementalProofStore,
        *,
        max_artifact_bytes: int = MAX_WIRE_BYTES,
    ) -> tuple[LidrupArtifacts, dict[str, Any]]:
        self._verify_identity()
        artifacts = export_lidrup_artifacts(
            plan,
            result_receipt,
            store,
            max_artifact_bytes=max_artifact_bytes,
        )
        with tempfile.TemporaryDirectory(prefix="symcc-lidrup-") as directory:
            root = Path(directory)
            checker_path = _write_executable_snapshot(
                root, "lidrup-check", self._content
            )
            interaction_path = root / "interaction.icnf"
            proof_path = root / "proof.lidrup"
            interaction_path.write_bytes(artifacts.interaction)
            proof_path.write_bytes(artifacts.proof)
            result = _run_bounded(
                [
                    str(checker_path),
                    "--strict",
                    str(interaction_path),
                    str(proof_path),
                ],
                timeout_ms=self.timeout_ms,
            )
        verified_lines = {
            line.strip() for line in result.stdout.splitlines() if line.strip()
        }
        if (
            result.returncode != 0
            or result.stderr
            or b"s VERIFIED" not in verified_lines
        ):
            raise ProofWireError("independent LIDRUP checker did not verify the proof")
        metadata = artifacts.metadata()
        receipt: dict[str, Any] = {
            "schema": LIDRUP_RECEIPT_SCHEMA,
            "protocol": LIDRUP_WIRE_PROTOCOL,
            "status": "verified",
            "formula_sha256": plan.formula_sha256,
            "assumption_sha256": plan.assumption_sha256,
            "artifact_sha256": metadata["artifact_sha256"],
            "root_record_sha256": artifacts.root_record_sha256,
            "result_receipt_sha256": artifacts.result_receipt_sha256,
            "interaction_sha256": metadata["interaction_sha256"],
            "interaction_bytes": metadata["interaction_bytes"],
            "proof_sha256": metadata["proof_sha256"],
            "proof_bytes": metadata["proof_bytes"],
            "learned_clause_count": artifacts.learned_clause_count,
            "checker_policy_sha256": self.policy_sha256,
            "checker_sha256": self._identity["sha256"],
            "checker_version": self.checker_version,
            "checker_source_commit": self.source_commit,
            "checker_mode": "strict",
            "checker_stdout_sha256": _digest(result.stdout),
            "checker_stderr_sha256": _digest(result.stderr),
            "checker_elapsed_us": result.elapsed_us,
        }
        receipt["receipt_sha256"] = _digest(_canonical_json(receipt))
        return artifacts, receipt

    def validate_receipt(
        self,
        plan: BitBlastPlan,
        result_receipt: Mapping[str, Any],
        store: IncrementalProofStore,
        artifacts: LidrupArtifacts,
        receipt: Mapping[str, Any],
        *,
        recheck: bool = True,
    ) -> dict[str, Any]:
        """Validate a receipt and, by default, independently rerun its checker."""
        if type(recheck) is not bool:
            raise ProofWireError("LIDRUP receipt recheck flag must be boolean")
        self._verify_identity()
        expected_artifacts = export_lidrup_artifacts(plan, result_receipt, store)
        if expected_artifacts != artifacts:
            raise ProofWireError("LIDRUP artifacts are not the canonical DAG export")
        if not isinstance(receipt, Mapping):
            raise ProofWireError("LIDRUP checker receipt must be an object")
        normalized = dict(receipt)
        digest = _hex_digest(normalized.pop("receipt_sha256", None), "LIDRUP receipt")
        if _digest(_canonical_json(normalized)) != digest:
            raise ProofWireError("LIDRUP checker receipt identity changed")
        metadata = artifacts.metadata()
        expected = {
            "schema": LIDRUP_RECEIPT_SCHEMA,
            "protocol": LIDRUP_WIRE_PROTOCOL,
            "status": "verified",
            "formula_sha256": plan.formula_sha256,
            "assumption_sha256": plan.assumption_sha256,
            "artifact_sha256": metadata["artifact_sha256"],
            "root_record_sha256": artifacts.root_record_sha256,
            "result_receipt_sha256": artifacts.result_receipt_sha256,
            "interaction_sha256": metadata["interaction_sha256"],
            "interaction_bytes": metadata["interaction_bytes"],
            "proof_sha256": metadata["proof_sha256"],
            "proof_bytes": metadata["proof_bytes"],
            "learned_clause_count": artifacts.learned_clause_count,
            "checker_policy_sha256": self.policy_sha256,
            "checker_sha256": self._identity["sha256"],
            "checker_version": self.checker_version,
            "checker_source_commit": self.source_commit,
            "checker_mode": "strict",
        }
        for key, value in expected.items():
            if normalized.get(key) != value:
                raise ProofWireError(f"LIDRUP receipt field {key} changed")
        if set(normalized) != set(expected) | {
            "checker_stdout_sha256",
            "checker_stderr_sha256",
            "checker_elapsed_us",
        }:
            raise ProofWireError("LIDRUP receipt fields differ from the protocol")
        _hex_digest(normalized["checker_stdout_sha256"], "checker stdout")
        if normalized["checker_stderr_sha256"] != _digest(b""):
            raise ProofWireError("LIDRUP receipt records checker diagnostics")
        _integer(
            normalized["checker_elapsed_us"],
            "checker elapsed time",
            0,
            (1 << 63) - 1,
        )
        if recheck:
            rechecked_artifacts, _new_receipt = self.verify(plan, result_receipt, store)
            if rechecked_artifacts != artifacts:
                raise ProofWireError("LIDRUP receipt recheck changed its artifacts")
        normalized["receipt_sha256"] = digest
        return normalized


def _reject_duplicate_json_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProofWireError("proof wire JSON contains duplicate keys")
        result[key] = value
    return result


class LidrupWireStore:
    """Bounded immutable CAS for LIDRUP artifacts and checker receipts."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_records: int = 100_000,
        max_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self.max_records = _integer(
            max_records, "proof wire store record quota", 1, MAX_STORE_RECORDS
        )
        self.max_bytes = _integer(
            max_bytes, "proof wire store byte quota", 1, MAX_STORE_BYTES
        )
        candidate = Path(root)
        if candidate.is_symlink():
            raise ProofWireError("proof wire store root must not be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = candidate.resolve(strict=True)
        if not self.root.is_dir():
            raise ProofWireError("proof wire store root is not a directory")
        for name in ("objects", "artifacts", "receipts"):
            path = self.root / name
            if path.is_symlink():
                raise ProofWireError("proof wire store bucket must not be a symlink")
            path.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.root / "wire.sqlite3"
        self.lock_path = self.root / ".wire.lock"
        self._thread_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts(
                    artifact_sha256 TEXT PRIMARY KEY,
                    interaction_sha256 TEXT NOT NULL,
                    proof_sha256 TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    receipt_sha256 TEXT PRIMARY KEY,
                    artifact_sha256 TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    created REAL NOT NULL,
                    last_access REAL NOT NULL,
                    FOREIGN KEY(artifact_sha256) REFERENCES artifacts(artifact_sha256)
                );
                CREATE INDEX IF NOT EXISTS receipts_by_artifact
                    ON receipts(artifact_sha256, receipt_sha256);
                """
            )
            expected = {
                "schema": LIDRUP_STORE_SCHEMA,
                "max_records": str(self.max_records),
                "max_bytes": str(self.max_bytes),
            }
            found = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            if found and found != expected:
                raise ProofWireError("proof wire store metadata differs from policy")
            if not found:
                connection.executemany(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    tuple(expected.items()),
                )

    @contextmanager
    def _locked(self):
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ProofWireError("O_NOFOLLOW is required for proof wire locks")
        with self._thread_lock:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProofWireError("proof wire lock is not a regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _blob_location(self, bucket: str, digest: str, suffix: str) -> tuple[Path, str]:
        identity = _hex_digest(digest, "proof wire object")
        base = self.root / bucket
        shard = base / identity[:2]
        if shard.is_symlink():
            raise ProofWireError("proof wire shard must not be a symlink")
        shard.mkdir(mode=0o700, exist_ok=True)
        return shard, identity + suffix

    def _publish_blob(
        self, bucket: str, digest: str, suffix: str, content: bytes
    ) -> None:
        shard, name = self._blob_location(bucket, digest, suffix)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = os.open(
            shard,
            os.O_RDONLY
            | os.O_DIRECTORY
            | (no_follow or 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            staging = (
                f".{name}.tmp-{os.getpid()}-{threading.get_ident()}-"
                f"{secrets.token_hex(8)}"
            )
            try:
                descriptor = os.open(
                    staging,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | (no_follow or 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory,
                )
                try:
                    offset = 0
                    while offset < len(content):
                        written = os.write(descriptor, content[offset:])
                        if written <= 0:
                            raise OSError("short proof wire object write")
                        offset += written
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    os.link(
                        staging,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                    os.fsync(directory)
                except FileExistsError:
                    if (
                        self._read_blob(bucket, digest, suffix, len(content))
                        != content
                    ):
                        raise ProofWireError("immutable proof wire object changed")
            finally:
                try:
                    os.unlink(staging, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory)

    def _read_blob(self, bucket: str, digest: str, suffix: str, maximum: int) -> bytes:
        shard, name = self._blob_location(bucket, digest, suffix)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ProofWireError("O_NOFOLLOW is required for proof wire objects")
        directory = os.open(
            shard,
            os.O_RDONLY | os.O_DIRECTORY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory,
            )
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                    raise ProofWireError("proof wire object is not a bounded file")
                content = bytearray()
                while True:
                    chunk = os.read(
                        descriptor, min(1024 * 1024, maximum + 1 - len(content))
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise ProofWireError("proof wire object exceeds its byte bound")
                after = os.fstat(descriptor)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ProofWireError("proof wire object changed during read")
                return bytes(content)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)

    @staticmethod
    def _json_bytes(value: Mapping[str, Any]) -> bytes:
        return _canonical_json(value) + b"\n"

    def publish(
        self,
        artifacts: LidrupArtifacts,
        receipt: Mapping[str, Any],
    ) -> tuple[str, str, bool]:
        if not isinstance(artifacts, LidrupArtifacts) or not isinstance(
            receipt, Mapping
        ):
            raise ProofWireError("proof wire publication has invalid objects")
        metadata = artifacts.metadata()
        artifact_digest = _hex_digest(metadata["artifact_sha256"], "LIDRUP artifact")
        normalized_receipt = dict(receipt)
        receipt_digest = _hex_digest(
            normalized_receipt.get("receipt_sha256"), "LIDRUP checker receipt"
        )
        receipt_body = dict(normalized_receipt)
        receipt_body.pop("receipt_sha256")
        if (
            normalized_receipt.get("schema") != LIDRUP_RECEIPT_SCHEMA
            or normalized_receipt.get("artifact_sha256") != artifact_digest
            or _digest(_canonical_json(receipt_body)) != receipt_digest
        ):
            raise ProofWireError("proof wire receipt is not bound to its artifact")
        metadata_bytes = self._json_bytes(metadata)
        receipt_bytes = self._json_bytes(normalized_receipt)
        artifact_bytes = (
            len(artifacts.interaction) + len(artifacts.proof) + len(metadata_bytes)
        )
        now = time.time()
        with self._locked():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                artifact_row = connection.execute(
                    "SELECT interaction_sha256,proof_sha256,encoded_bytes "
                    "FROM artifacts WHERE artifact_sha256=?",
                    (artifact_digest,),
                ).fetchone()
                receipt_row = connection.execute(
                    "SELECT artifact_sha256,encoded_bytes FROM receipts "
                    "WHERE receipt_sha256=?",
                    (receipt_digest,),
                ).fetchone()
                created = receipt_row is None
                added_records = int(artifact_row is None) + int(receipt_row is None)
                added_bytes = (artifact_bytes if artifact_row is None else 0) + (
                    len(receipt_bytes) if receipt_row is None else 0
                )
                counts = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM artifacts) + "
                    "(SELECT COUNT(*) FROM receipts), "
                    "COALESCE((SELECT SUM(encoded_bytes) FROM artifacts),0) + "
                    "COALESCE((SELECT SUM(encoded_bytes) FROM receipts),0)"
                ).fetchone()
                if (
                    int(counts[0]) + added_records > self.max_records
                    or int(counts[1]) + added_bytes > self.max_bytes
                ):
                    raise ProofWireError("proof wire store quota exceeded")
                if artifact_row is not None and (
                    artifact_row["interaction_sha256"] != metadata["interaction_sha256"]
                    or artifact_row["proof_sha256"] != metadata["proof_sha256"]
                    or int(artifact_row["encoded_bytes"]) != artifact_bytes
                ):
                    raise ProofWireError("proof wire artifact index changed")
                if receipt_row is not None and (
                    receipt_row["artifact_sha256"] != artifact_digest
                    or int(receipt_row["encoded_bytes"]) != len(receipt_bytes)
                ):
                    raise ProofWireError("proof wire receipt index changed")
                self._publish_blob(
                    "objects",
                    str(metadata["interaction_sha256"]),
                    ".icnf",
                    artifacts.interaction,
                )
                self._publish_blob(
                    "objects",
                    str(metadata["proof_sha256"]),
                    ".lidrup",
                    artifacts.proof,
                )
                self._publish_blob(
                    "artifacts", artifact_digest, ".json", metadata_bytes
                )
                self._publish_blob("receipts", receipt_digest, ".json", receipt_bytes)
                connection.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)",
                    (
                        artifact_digest,
                        metadata["interaction_sha256"],
                        metadata["proof_sha256"],
                        artifact_bytes,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)",
                    (receipt_digest, artifact_digest, len(receipt_bytes), now, now),
                )
                connection.execute(
                    "UPDATE artifacts SET last_access=? WHERE artifact_sha256=?",
                    (now, artifact_digest),
                )
                connection.execute(
                    "UPDATE receipts SET last_access=? WHERE receipt_sha256=?",
                    (now, receipt_digest),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return artifact_digest, receipt_digest, created

    def load(
        self, artifact_sha256: str, receipt_sha256: str
    ) -> tuple[LidrupArtifacts, dict[str, Any]]:
        artifact_digest = _hex_digest(artifact_sha256, "LIDRUP artifact")
        receipt_digest = _hex_digest(receipt_sha256, "LIDRUP checker receipt")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT artifacts.interaction_sha256,artifacts.proof_sha256,"
                "receipts.artifact_sha256 FROM artifacts JOIN receipts ON "
                "receipts.artifact_sha256=artifacts.artifact_sha256 WHERE "
                "artifacts.artifact_sha256=? AND receipts.receipt_sha256=?",
                (artifact_digest, receipt_digest),
            ).fetchone()
            if row is None or row["artifact_sha256"] != artifact_digest:
                raise FileNotFoundError("proof wire artifact receipt is unavailable")
            now = time.time()
            connection.execute(
                "UPDATE artifacts SET last_access=? WHERE artifact_sha256=?",
                (now, artifact_digest),
            )
            connection.execute(
                "UPDATE receipts SET last_access=? WHERE receipt_sha256=?",
                (now, receipt_digest),
            )
        metadata_bytes = self._read_blob(
            "artifacts", artifact_digest, ".json", MAX_COMMAND_OUTPUT_BYTES
        )
        receipt_bytes = self._read_blob(
            "receipts", receipt_digest, ".json", MAX_COMMAND_OUTPUT_BYTES
        )
        try:
            metadata = json.loads(
                metadata_bytes.decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            receipt = json.loads(
                receipt_bytes.decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProofWireError("proof wire metadata is not canonical JSON") from error
        if not isinstance(metadata, Mapping) or not isinstance(receipt, Mapping):
            raise ProofWireError("proof wire metadata is not an object")
        interaction = self._read_blob(
            "objects", str(row["interaction_sha256"]), ".icnf", MAX_WIRE_BYTES
        )
        proof = self._read_blob(
            "objects", str(row["proof_sha256"]), ".lidrup", MAX_WIRE_BYTES
        )
        artifacts = LidrupArtifacts(
            interaction=interaction,
            proof=proof,
            root_record_sha256=_hex_digest(
                metadata.get("root_record_sha256"), "root proof record"
            ),
            result_receipt_sha256=_hex_digest(
                metadata.get("result_receipt_sha256"), "result proof receipt"
            ),
            failed_assumptions=tuple(
                _integer(
                    value,
                    "failed assumption",
                    -MAX_PALRUP_LITERAL,
                    MAX_PALRUP_LITERAL,
                )
                for value in metadata.get("failed_assumptions", ())
            ),
            learned_clause_count=_integer(
                metadata.get("learned_clause_count"),
                "learned clause count",
                1,
                MAX_WIRE_LINES,
            ),
        )
        if artifacts.metadata() != dict(metadata):
            raise ProofWireError("stored proof wire artifact identity changed")
        receipt_body = dict(receipt)
        supplied_receipt = _hex_digest(
            receipt_body.pop("receipt_sha256", None), "stored checker receipt"
        )
        if (
            supplied_receipt != receipt_digest
            or _digest(_canonical_json(receipt_body)) != receipt_digest
            or receipt.get("artifact_sha256") != artifact_digest
        ):
            raise ProofWireError("stored proof wire receipt identity changed")
        return artifacts, dict(receipt)

    def stats(self) -> dict[str, int | str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT (SELECT COUNT(*) FROM artifacts),"
                "(SELECT COUNT(*) FROM receipts),"
                "COALESCE((SELECT SUM(encoded_bytes) FROM artifacts),0) + "
                "COALESCE((SELECT SUM(encoded_bytes) FROM receipts),0)"
            ).fetchone()
        return {
            "schema": LIDRUP_STORE_SCHEMA,
            "artifacts": int(row[0]),
            "receipts": int(row[1]),
            "encoded_bytes": int(row[2]),
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class PalrupProduce:
    external_id: int
    literals: tuple[int, ...]
    hints: tuple[int, ...]


@dataclass(frozen=True)
class PalrupImport:
    external_id: int
    literals: tuple[int, ...]


@dataclass(frozen=True)
class PalrupDelete:
    external_ids: tuple[int, ...]


PalrupDirective = PalrupProduce | PalrupImport | PalrupDelete


def _encode_signed_varint(value: int, *, lower: int, upper: int) -> bytes:
    _integer(value, "PalRUP varint", lower, upper)
    unsigned = 2 * abs(value) + (1 if value < 0 else 0)
    output = bytearray()
    while unsigned & ~0x7F:
        output.append((unsigned & 0x7F) | 0x80)
        unsigned >>= 7
    output.append(unsigned)
    return bytes(output)


def _normalized_palrup_directive(raw: PalrupDirective) -> PalrupDirective:
    if isinstance(raw, PalrupProduce):
        external_id = _integer(raw.external_id, "PalRUP produced ID", 1, MAX_PALRUP_ID)
        literals = tuple(
            _integer(value, "PalRUP literal", -MAX_PALRUP_LITERAL, MAX_PALRUP_LITERAL)
            for value in raw.literals
        )
        hints = tuple(
            _integer(value, "PalRUP hint", 1, MAX_PALRUP_ID) for value in raw.hints
        )
        if 0 in literals:
            raise ProofWireError("PalRUP clause literals cannot contain zero")
        return PalrupProduce(external_id, literals, hints)
    if isinstance(raw, PalrupImport):
        external_id = _integer(raw.external_id, "PalRUP imported ID", 1, MAX_PALRUP_ID)
        literals = tuple(
            _integer(value, "PalRUP literal", -MAX_PALRUP_LITERAL, MAX_PALRUP_LITERAL)
            for value in raw.literals
        )
        if 0 in literals:
            raise ProofWireError("PalRUP clause literals cannot contain zero")
        return PalrupImport(external_id, literals)
    if isinstance(raw, PalrupDelete):
        external_ids = tuple(
            _integer(value, "PalRUP deleted ID", 1, MAX_PALRUP_ID)
            for value in raw.external_ids
        )
        if not external_ids:
            raise ProofWireError("PalRUP deletion requires at least one ID")
        return PalrupDelete(external_ids)
    raise ProofWireError("unsupported PalRUP directive")


def encode_palrup_fragment(
    directives: Sequence[PalrupDirective],
    *,
    max_bytes: int = MAX_WIRE_BYTES,
) -> bytes:
    if (
        isinstance(directives, (str, bytes))
        or not 1 <= len(directives) <= MAX_PALRUP_DIRECTIVES
    ):
        raise ProofWireError("PalRUP fragment has an invalid directive count")
    maximum = _integer(max_bytes, "PalRUP byte budget", 1, MAX_WIRE_BYTES)
    output = bytearray()
    values = 0

    def append(content: bytes) -> None:
        if len(output) + len(content) > maximum:
            raise ProofWireError("PalRUP fragment exceeds its byte bound")
        output.extend(content)

    for raw in directives:
        directive = _normalized_palrup_directive(raw)
        if isinstance(directive, PalrupProduce):
            append(b"a")
            append(
                _encode_signed_varint(
                    directive.external_id, lower=1, upper=MAX_PALRUP_ID
                )
            )
            for literal in directive.literals:
                append(
                    _encode_signed_varint(
                        literal,
                        lower=-MAX_PALRUP_LITERAL,
                        upper=MAX_PALRUP_LITERAL,
                    )
                )
            append(b"\x00")
            for hint in directive.hints:
                append(_encode_signed_varint(hint, lower=1, upper=MAX_PALRUP_ID))
            append(b"\x00")
            values += 1 + len(directive.literals) + len(directive.hints)
        elif isinstance(directive, PalrupImport):
            append(b"i")
            append(
                _encode_signed_varint(
                    directive.external_id, lower=1, upper=MAX_PALRUP_ID
                )
            )
            for literal in directive.literals:
                append(
                    _encode_signed_varint(
                        literal,
                        lower=-MAX_PALRUP_LITERAL,
                        upper=MAX_PALRUP_LITERAL,
                    )
                )
            append(b"\x00")
            values += 1 + len(directive.literals)
        else:
            assert isinstance(directive, PalrupDelete)
            append(b"d")
            for external_id in directive.external_ids:
                append(_encode_signed_varint(external_id, lower=1, upper=MAX_PALRUP_ID))
            append(b"\x00")
            values += len(directive.external_ids)
        if values > MAX_PALRUP_VALUES:
            raise ProofWireError("PalRUP fragment exceeds its value bound")
    return bytes(output)


def _decode_signed_varint(content: bytes, offset: int, *, bits: int) -> tuple[int, int]:
    start = offset
    unsigned = 0
    shift = 0
    maximum_bytes = 5 if bits == 32 else 10
    for _ in range(maximum_bytes):
        if offset >= len(content):
            raise ProofWireError("PalRUP fragment ends inside a varint")
        byte = content[offset]
        offset += 1
        unsigned |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            value = -(unsigned // 2) if unsigned & 1 else unsigned // 2
            bound = MAX_PALRUP_LITERAL if bits == 32 else MAX_PALRUP_ID
            if not -bound <= value <= bound:
                raise ProofWireError("PalRUP varint exceeds its signed bound")
            canonical = _encode_signed_varint(value, lower=-bound, upper=bound)
            if content[start:offset] != canonical:
                raise ProofWireError("PalRUP varint is not canonically encoded")
            return value, offset
        shift += 7
    raise ProofWireError("PalRUP varint exceeds its byte bound")


def decode_palrup_fragment(
    content: bytes,
    *,
    max_bytes: int = MAX_WIRE_BYTES,
) -> tuple[PalrupDirective, ...]:
    maximum = _integer(max_bytes, "PalRUP byte budget", 1, MAX_WIRE_BYTES)
    if not content or len(content) > maximum:
        raise ProofWireError("PalRUP fragment is empty or oversized")
    directives: list[PalrupDirective] = []
    offset = 0
    values = 0

    def read_list(*, bits: int, positive: bool) -> tuple[int, ...]:
        nonlocal offset, values
        result: list[int] = []
        while True:
            value, offset = _decode_signed_varint(content, offset, bits=bits)
            if value == 0:
                return tuple(result)
            if positive and value < 1:
                raise ProofWireError("PalRUP clause ID must be positive")
            result.append(value)
            values += 1
            if values > MAX_PALRUP_VALUES:
                raise ProofWireError("PalRUP fragment exceeds its value bound")

    while offset < len(content):
        if len(directives) >= MAX_PALRUP_DIRECTIVES:
            raise ProofWireError("PalRUP fragment has too many directives")
        directive = content[offset]
        offset += 1
        if directive == ord("a"):
            external_id, offset = _decode_signed_varint(content, offset, bits=64)
            literals = read_list(bits=32, positive=False)
            hints = read_list(bits=64, positive=True)
            parsed: PalrupDirective = PalrupProduce(external_id, literals, hints)
        elif directive == ord("i"):
            external_id, offset = _decode_signed_varint(content, offset, bits=64)
            literals = read_list(bits=32, positive=False)
            parsed = PalrupImport(external_id, literals)
        elif directive == ord("d"):
            parsed = PalrupDelete(read_list(bits=64, positive=True))
        else:
            raise ProofWireError("PalRUP fragment contains an unknown directive")
        directives.append(_normalized_palrup_directive(parsed))
    return tuple(directives)


def palrup_fragment_text(directives: Sequence[PalrupDirective]) -> bytes:
    writer = _BoundedAsciiWriter(MAX_WIRE_BYTES)
    for raw in directives:
        directive = _normalized_palrup_directive(raw)
        if isinstance(directive, PalrupProduce):
            writer.line(
                [
                    "a",
                    directive.external_id,
                    *directive.literals,
                    0,
                    *directive.hints,
                    0,
                ]
            )
        elif isinstance(directive, PalrupImport):
            writer.line(["i", directive.external_id, *directive.literals, 0])
        else:
            assert isinstance(directive, PalrupDelete)
            writer.line(["d", *directive.external_ids, 0])
    return writer.finish()


class PalrupFragmentOracle:
    """Cross-check binary fragments with the official SAT 2026 converter."""

    def __init__(
        self,
        converter: str | os.PathLike[str],
        *,
        converter_sha256: str,
        source_commit: str = PALRUP_CHECKER_COMMIT,
        timeout_ms: int = 30_000,
    ) -> None:
        self.path, self._identity, self._content = _executable_identity(converter)
        if self._identity["sha256"] != _hex_digest(
            converter_sha256, "PalRUP converter"
        ):
            raise ProofWireError(
                "PalRUP converter content identity differs from policy"
            )
        self.source_commit = _git_commit(source_commit, "PalRUP source commit")
        self.timeout_ms = _integer(timeout_ms, "PalRUP timeout", 1, 3_600_000)

    def verify(self, fragment: bytes) -> tuple[bytes, dict[str, Any]]:
        current_path, current, _content = _executable_identity(self.path)
        if current_path != self.path or current != self._identity:
            raise ProofWireError(
                "PalRUP converter identity changed after initialization"
            )
        parsed = decode_palrup_fragment(fragment)
        expected = palrup_fragment_text(parsed)
        with tempfile.TemporaryDirectory(prefix="symcc-palrup-") as directory:
            root = Path(directory)
            converter_path = _write_executable_snapshot(
                root, "proof_fragment_to_txt", self._content
            )
            binary_path = root / "fragment.palrup"
            text_path = root / "fragment.txt"
            binary_path.write_bytes(fragment)
            result = _run_bounded(
                [str(converter_path), str(binary_path), str(text_path)],
                timeout_ms=self.timeout_ms,
            )
            actual = _read_regular(text_path, MAX_WIRE_BYTES)
        if result.returncode != 0 or result.stderr or actual != expected:
            raise ProofWireError("official PalRUP converter disagrees with the codec")
        receipt: dict[str, Any] = {
            "schema": PALRUP_ORACLE_RECEIPT_SCHEMA,
            "protocol": PALRUP_BINARY_PROTOCOL,
            "fragment_sha256": _digest(fragment),
            "fragment_bytes": len(fragment),
            "text_sha256": _digest(actual),
            "directive_count": len(parsed),
            "converter_sha256": self._identity["sha256"],
            "converter_source_commit": self.source_commit,
            "converter_stdout_sha256": _digest(result.stdout),
            "converter_stderr_sha256": _digest(result.stderr),
            "converter_elapsed_us": result.elapsed_us,
            "scope": "fragment-syntax-interoperability-not-global-unsat",
        }
        receipt["receipt_sha256"] = _digest(_canonical_json(receipt))
        return actual, receipt
