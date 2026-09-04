#!/usr/bin/env python3
"""Persistent, content-addressed query store for asynchronous concolic solving.

The runtime emits self-contained query envelopes.  This module validates and
normalizes them, interns expression nodes by SHA-256, builds a persistent prefix
trie, and provides fenced work leases for independent solver processes.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from array import array
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from solution_generator import (
    derive_converter_chains,
    generator_hash,
    normalize_generator,
    sample_generator,
)
from schedule_exploration import (
    JOINT_PATH_SCHEDULE_SCHEMA,
    solve_joint_path_schedule_query,
    verify_joint_path_schedule_result,
)
from qf_bv_backend import (
    CAPABILITY_SCHEMA as QFBV_CAPABILITY_SCHEMA,
    LOWERING_SCHEMA as QFBV_LOWERING_SCHEMA,
    lower_qfbv_proof_problem,
    normalize_qfbv_capabilities,
    qfbv_full_context_identity,
    qfbv_prefix_context_identity,
)
from qfbv_proof_receipt import (
    PROOF_PROTOCOL as QFBV_PROOF_PROTOCOL,
    ProofVerificationError,
    QfbvProofVerifier,
    normalize_proof_receipt,
)
from qfbv_lemma_exchange import (
    LEMMA_PROTOCOL as QFBV_LEMMA_PROTOCOL,
    LemmaExchangeError,
    QfbvLemmaExchange,
    normalize_lemma_record,
)
from qfbv_substitution_core import (
    CORE_PROTOCOL as QFBV_SUBSTITUTION_CORE_PROTOCOL,
    QfbvSubstitutionCoreExchange,
    SubstitutionCoreError,
)
from qfbv_incremental_sat import (
    BITBLAST_SCHEMA as QFBV_BITBLAST_SCHEMA,
    QfbvBitBlastError,
    bitblast_qfbv_query,
)
from qfbv_incremental_proof import (
    CLAUSE_PROTOCOL as QFBV_INCREMENTAL_PROOF_PROTOCOL,
    EVENT_STREAM_PROTOCOL as QFBV_PROOF_EVENT_STREAM_PROTOCOL,
    RESULT_RECEIPT_SCHEMA as QFBV_INCREMENTAL_RESULT_SCHEMA,
    IncrementalProofChecker,
    IncrementalProofError,
)
from qfbv_proof_wire import (
    LIDRUP_WIRE_PROTOCOL as QFBV_LIDRUP_WIRE_PROTOCOL,
    LidrupExternalChecker,
    LidrupWireStore,
    ProofWireError,
)
from qfbv_realtime_stream import (
    CLAUSE_ACTIVITY_PROTOCOL as QFBV_CLAUSE_ACTIVITY_PROTOCOL,
    CLAUSE_ACTIVITY_SCHEMA as QFBV_CLAUSE_ACTIVITY_SCHEMA,
    CLAUSE_COMPRESSION_PROTOCOL as QFBV_CLAUSE_COMPRESSION_PROTOCOL,
    CHECKED_IMPORT_ACK_SCHEMA as QFBV_CHECKED_IMPORT_ACK_SCHEMA,
    REALTIME_STREAM_PROTOCOL as QFBV_REALTIME_STREAM_PROTOCOL,
    RealtimeStreamError,
    verify_clause_activity_receipt,
    verify_checked_import_ack,
)
from qfbv_adaptive_exchange import (
    ADAPTIVE_EXCHANGE_PROTOCOL as QFBV_ADAPTIVE_EXCHANGE_PROTOCOL,
    verify_adaptive_stream_result,
)
from qfbv_utility_pairing import (
    UTILITY_PAIRING_PROTOCOL as QFBV_UTILITY_PAIRING_PROTOCOL,
    UtilityPairingError,
    UtilityPairingPolicy,
    formula_family_sha256,
    validate_pairing_worker_identity,
    verify_pairing_snapshot,
    verify_pairing_stream_result,
)
from qfbv_malleable_workers import (
    MalleableWorkerError,
    MalleableWorkerPolicy,
    validate_malleable_identity,
    verify_malleable_snapshot,
)
from qfbv_online_cubing import (
    OnlineCubingError,
    normalize_online_cubing_result,
)
from mpi_ulfm_recovery import (
    UlfmRecoveryError,
    UlfmRecoveryPolicy,
    verify_recovery_receipt,
    verify_recovery_snapshot,
)
from cross_worker_context import (
    CONTEXT_PROTOCOL as QFBV_SHARED_CONTEXT_PROTOCOL,
)
from distributed_state import (
    ContentAddressedInputStore,
    bounded_advisory_lock,
    durable_makedirs,
    durable_replace,
    stable_regular_file_snapshot,
)
from constraint_shape import (
    AlphaConstraintShapeIndex,
    SHAPE_SCHEMA as CONSTRAINT_SHAPE_SCHEMA,
)


ENVELOPE_SCHEMA = "symcc-query-ir-v1"
NODE_SCHEMA = "symcc-expr-node-v1"
QUERY_SCHEMA = "symcc-query-v1"
RESULT_SCHEMA = "symcc-query-result-v1"
PORTFOLIO_SCHEMA = "symcc-solver-portfolio-v1"
PARTIAL_SOLUTION_SCHEMA = "symcc-partial-solution-v1"
CONFLICT_SOLUTION_SCHEMA = "symcc-solver-conflict-solution-v1"
SCHEDULE_CONSTRAINT_SCHEMA = "symcc-schedule-constraint-v1"
JOINT_SCHEDULE_QUERY_SCHEMA = "symcc-joint-schedule-query-v1"

_OPS = {
    "bool",
    "constant",
    "read",
    "concat",
    "extract",
    "zext",
    "sext",
    "add",
    "sub",
    "mul",
    "udiv",
    "sdiv",
    "urem",
    "srem",
    "neg",
    "not",
    "and",
    "or",
    "xor",
    "shl",
    "lshr",
    "ashr",
    "equal",
    "distinct",
    "ult",
    "ule",
    "ugt",
    "uge",
    "slt",
    "sle",
    "sgt",
    "sge",
    "land",
    "lor",
    "lnot",
    "ite",
    "rol",
    "ror",
}
_RESULT_STATUSES = {"sat", "unsat", "unknown", "error"}
_QUERY_EVAL_MAX_BITS = 4096
_MAX_ENVELOPE_BYTES = 256 * 1024 * 1024
_MAX_QUERY_BODY_BYTES = _MAX_ENVELOPE_BYTES
_MAX_QUERY_IR_NODES = 250_000
_QUERY_SHAPE_CONTEXT_ROOTS = 8
_MAX_SAT_VALIDATION_WITNESSES = 64
_MAX_SAT_VALIDATION_CANDIDATES = 4096
_MAX_SAT_VALIDATION_NODE_EVALUATIONS = 8_000_000
_ENVELOPE_READ_CHUNK_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_SOLVER_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_SOLVER_DIAGNOSTIC_BYTES = 1024 * 1024
_MAX_SOLVER_REQUEST_BYTES = 2 * _MAX_ARTIFACT_BYTES + 64 * 1024
_MAX_DESCRIPTOR_REQUEST_ID_BYTES = 256
_MAX_PERSISTENT_GENERATION_REQUESTS = 65536
_DEFAULT_ARTIFACT_AUDIT_MAX_ENTRIES = 100_000
_MAX_ARTIFACT_AUDIT_ENTRIES = 10_000_000
_ARTIFACT_AUDIT_LOCK_TIMEOUT_SECONDS = 30.0
_DEFAULT_STARTUP_PUBLICATION_LIMIT = 64
_DEFAULT_STARTUP_PUBLICATION_SECONDS = 2.0
_DEFAULT_COMPLETION_PUBLICATION_LIMIT = 64
_DEFAULT_COMPLETION_PUBLICATION_SECONDS = 0.1
_DEFAULT_PUBLICATION_MAX_ATTEMPTS = 8
_DEFAULT_PUBLICATION_RETRY_BASE_SECONDS = 1.0
_DEFAULT_PUBLICATION_RETRY_MAX_SECONDS = 300.0
_MAX_QUERY_LEASE_SECONDS = 7 * 24 * 60 * 60
_PERSISTENT_SOLVER_GRACE_SECONDS = 5.0
_ARTIFACT_KINDS = ("smt2", "smt2-prefix", "smt2-target")
_ARTIFACT_ROLES = ("full", "prefix", "target")
_SEALED_ARTIFACT_REQUIRED_SEALS = (
    getattr(fcntl, "F_SEAL_WRITE", 0)
    | getattr(fcntl, "F_SEAL_GROW", 0)
    | getattr(fcntl, "F_SEAL_SHRINK", 0)
    | getattr(fcntl, "F_SEAL_SEAL", 0)
)
_FD_PROTOCOL_PREFIX = "@symcc-fd:"


class QueryAdmissionError(ValueError):
    """An invalid envelope rejected before QueryStore persistence begins."""


_ValidatedEnvelope = tuple[
    list[dict[str, Any]],
    list[int],
    int,
    str,
    str,
    str,
    str,
    dict[str, Any],
    int,
    float,
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object member {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not supported")


def _parse_solver_response(response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            response,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise RuntimeError(
            f"solver returned invalid JSON: {error}; prefix={response[:2048]!r}"
        ) from error
    if not isinstance(parsed, dict):
        raise RuntimeError("solver result must be a JSON object")
    return parsed


def _regular_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _load_query_envelope(path: Path) -> dict[str, Any]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "O_NOFOLLOW is required for query envelopes",
            path,
        )
    flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError(f"cannot read query envelope: {error}") from error
        raise
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise ValueError("query envelope must be a regular file")
        identity = _regular_file_identity(metadata_before)
        encoded = bytearray()
        while len(encoded) <= _MAX_ENVELOPE_BYTES:
            remaining = _MAX_ENVELOPE_BYTES + 1 - len(encoded)
            try:
                chunk = os.read(
                    descriptor,
                    min(_ENVELOPE_READ_CHUNK_BYTES, remaining),
                )
            except InterruptedError:
                continue
            if not chunk:
                break
            encoded.extend(chunk)
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise ValueError(f"query envelope exceeds {_MAX_ENVELOPE_BYTES} bytes")
        metadata_after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or _regular_file_identity(metadata_after) != identity
            or _regular_file_identity(path_metadata) != identity
        ):
            raise ValueError("query envelope identity changed while reading")
    finally:
        os.close(descriptor)
    try:
        envelope = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"cannot parse query envelope: {error}") from error
    if not isinstance(envelope, dict):
        raise ValueError("query envelope must be a JSON object")
    return envelope


def _normalize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValueError("metadata nesting exceeds eight levels")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if value < -(1 << 63) or value > (1 << 64) - 1:
            raise ValueError("metadata integer is outside int64/uint64")
        return value
    if isinstance(value, float):
        if not (-1e300 < value < 1e300):
            raise ValueError("metadata float is not finite")
        return value
    if isinstance(value, list):
        if len(value) > 4096:
            raise ValueError("metadata list is too large")
        return [_normalize_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 1024:
            raise ValueError("metadata object is too large")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("metadata keys must be bounded strings")
            result[key] = _normalize_json(item, depth=depth + 1)
        return result
    raise ValueError(f"unsupported JSON value {type(value).__name__}")


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < lower or value > upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return value


def _validate_hex(value: Any, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or len(value) % 2:
        raise ValueError(f"{name} must be an even-length hex string")
    if len(value) // 2 > maximum_bytes:
        raise ValueError(f"{name} exceeds {maximum_bytes} bytes")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not valid hex") from exc
    return value.lower()


def _normalize_assignments(
    value: Any,
    name: str,
    *,
    maximum: int = 4096,
) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded object")
    assignments: dict[str, int] = {}
    for raw_index, raw_value in value.items():
        if isinstance(raw_index, bool):
            raise ValueError(f"{name} offsets must be integers")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} offsets must be integers") from exc
        if index < 0 or index > (1 << 32) - 1:
            raise ValueError(f"{name} offset is outside uint32")
        canonical_index = str(index)
        if canonical_index in assignments:
            raise ValueError(f"{name} contains duplicate offsets")
        assignments[canonical_index] = _bounded_int(raw_value, f"{name} value", 0, 255)
    return assignments


def _atomic_write(path: Path, data: bytes) -> None:
    durable_makedirs(str(path.parent))
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        durable_replace(temporary, str(path))
    finally:
        if "temporary" in locals():
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _atomic_write_exact(path: Path, data: bytes) -> None:
    """Publish data unless a stable regular file already contains it exactly."""
    try:
        snapshot = stable_regular_file_snapshot(
            str(path), max_bytes=max(1, len(data)), retain_content=True,
        )
        if snapshot.content == data:
            return
    except (OSError, ValueError):
        pass
    _atomic_write(path, data)


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no"}


def _env_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(upper, max(lower, value))


def _interrupt_process(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 0.2,
) -> bool:
    """Terminate one helper process group and escalate after a short grace."""
    leader_running = process.poll() is None
    signalled = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        signalled = True
    except (OSError, ProcessLookupError):
        if leader_running:
            try:
                process.terminate()
                signalled = True
            except OSError:
                pass
    if not signalled:
        return False
    if leader_running:
        try:
            process.wait(timeout=max(0.01, grace_seconds))
        except subprocess.TimeoutExpired:
            pass

    deadline = time.monotonic() + max(0.01, grace_seconds)
    group_exists = True
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            group_exists = False
            break
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if group_exists:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return False
    return True


def _communicate_solver_bounded(
    process: subprocess.Popen[str],
    command: Sequence[str],
    timeout_seconds: float,
) -> tuple[str, str]:
    """Drain both helper pipes under one deadline and fixed memory bounds."""
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("solver output pipes are unavailable")
    deadline = time.monotonic() + max(0.001, timeout_seconds)
    outputs = {
        process.stdout.fileno(): (
            "stdout",
            bytearray(),
            _MAX_SOLVER_RESPONSE_BYTES,
        ),
        process.stderr.fileno(): (
            "stderr",
            bytearray(),
            _MAX_SOLVER_DIAGNOSTIC_BYTES,
        ),
    }
    with selectors.DefaultSelector() as selector:
        for descriptor in outputs:
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                readable = selector.select(remaining)
            except InterruptedError:
                continue
            if not readable:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _events in readable:
                descriptor = int(key.fd)
                label, output, limit = outputs[descriptor]
                try:
                    chunk = os.read(
                        descriptor,
                        min(65536, limit + 1 - len(output)),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    raise RuntimeError(f"solver {label} exceeds {limit} bytes")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    process.wait(timeout=remaining)
    decoded: list[str] = []
    for descriptor in (process.stdout.fileno(), process.stderr.fileno()):
        label, output, _limit = outputs[descriptor]
        try:
            decoded.append(output.decode("utf-8", errors="strict"))
        except UnicodeError as error:
            raise RuntimeError(f"solver returned invalid UTF-8 on {label}") from error
    return decoded[0], decoded[1]


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return max(0, int(default))
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_prefix(raw: Any, *, max_len: int = 256) -> list[int]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    result: list[int] = []
    for item in raw:
        value = _optional_nonnegative_int(item)
        if value is None:
            continue
        result.append(value)
        if len(result) >= max_len:
            break
    return result


def _hex_digest(value: Any) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
        return digest
    return ""


def _required_hex_digest(value: Any, name: str) -> str:
    digest = _hex_digest(value)
    if not digest or not isinstance(value, str) or value != digest:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _normalize_realtime_stream_result(
    result: Mapping[str, Any],
    certificate: Mapping[str, Any],
    policy_sha256: str,
    status: str,
) -> dict[str, Any]:
    if result.get("backend_realtime_stream_protocol") != QFBV_REALTIME_STREAM_PROTOCOL:
        raise ValueError("invalid realtime proof-stream protocol")
    if (
        result.get("backend_realtime_event_stream_protocol")
        != QFBV_PROOF_EVENT_STREAM_PROTOCOL
    ):
        raise ValueError("invalid realtime proof event-stream protocol")
    stream_id = _required_hex_digest(
        result.get("backend_realtime_stream_id"), "realtime stream identity"
    )
    native_signature_raw = result.get("backend_realtime_native_signature", "")
    native_signature = str(native_signature_raw)[:256]
    if not isinstance(native_signature_raw, str) or (
        native_signature_raw != native_signature
    ):
        raise ValueError("realtime CaDiCaL signature must not be truncated")
    if (
        re.fullmatch(
            r"symcc-qfbv-realtime-v1\|cadical-3\.0\.[0-9]+(?:[^\s]*)?",
            native_signature,
        )
        is None
    ):
        raise ValueError("invalid realtime CaDiCaL signature")
    if native_signature != (
        "symcc-qfbv-realtime-v1|" + str(result.get("backend_native_signature", ""))
    ):
        raise ValueError("realtime and native CaDiCaL signatures disagree")
    generation = _bounded_int(
        result.get("backend_realtime_solve_generation"),
        "realtime solve generation",
        1,
        (1 << 63) - 1,
    )
    polls = _bounded_int(
        result.get("backend_realtime_polls"),
        "realtime stream polls",
        1,
        (1 << 63) - 1,
    )
    events = _bounded_int(
        result.get("backend_realtime_events_observed"),
        "realtime proof events",
        0,
        1_000_000,
    )
    candidates = _bounded_int(
        result.get("backend_realtime_import_candidates"),
        "realtime import candidates",
        0,
        events,
    )
    settled_fields: dict[str, int] = {}
    settled_key = "backend_realtime_import_settled_candidates"
    duplicate_key = "backend_realtime_import_duplicate_clauses"
    if (settled_key in result) != (duplicate_key in result):
        raise ValueError("realtime settled-candidate telemetry is incomplete")
    if settled_key in result:
        settled = _bounded_int(
            result.get(settled_key),
            "realtime settled import candidates",
            0,
            candidates,
        )
        duplicates = _bounded_int(
            result.get(duplicate_key),
            "realtime duplicate import clauses",
            0,
            settled,
        )
        settled_fields = {
            settled_key: settled,
            duplicate_key: duplicates,
        }
    authorized = _bounded_int(
        result.get("backend_realtime_import_authorized"),
        "realtime authorized imports",
        0,
        min(candidates, 4096),
    )
    delivered = _bounded_int(
        result.get("backend_realtime_import_delivered"),
        "realtime delivered imports",
        0,
        authorized,
    )
    pending = _bounded_int(
        result.get("backend_realtime_import_pending"),
        "realtime pending imports",
        0,
        authorized,
    )
    if delivered + pending != authorized:
        raise ValueError("realtime import delivery accounting is inconsistent")
    rejected = _bounded_int(
        result.get("backend_realtime_import_rejected"),
        "realtime rejected imports",
        0,
        events,
    )
    backpressure = _bounded_int(
        result.get("backend_realtime_import_backpressure"),
        "realtime import backpressure",
        0,
        events,
    )
    import_ids_raw = result.get("backend_realtime_import_record_sha256", ())
    if not isinstance(import_ids_raw, list) or len(import_ids_raw) != delivered:
        raise ValueError("realtime import identities are inconsistent")
    import_ids = [
        _required_hex_digest(value, "realtime import identity")
        for value in import_ids_raw
    ]
    if len(set(import_ids)) != delivered:
        raise ValueError("realtime import identities are duplicated")
    raw_acks = result.get("backend_realtime_import_acks", ())
    if not isinstance(raw_acks, list) or len(raw_acks) != delivered:
        raise ValueError("realtime import ACK count is inconsistent")
    acks: list[dict[str, Any]] = []
    tokens: set[int] = set()
    event_sequences: set[int] = set()
    for expected_ordinal, (raw_ack, record_id) in enumerate(
        zip(raw_acks, import_ids), 1
    ):
        if not isinstance(raw_ack, Mapping):
            raise ValueError("realtime import ACK must be an object")
        ack = _normalize_json(dict(raw_ack))
        if not isinstance(ack, dict):
            raise ValueError("realtime import ACK normalization failed")
        ack_digest = _required_hex_digest(
            ack.get("ack_sha256"), "realtime ACK identity"
        )
        ack_body = dict(ack)
        ack_body.pop("ack_sha256")
        if _digest(_canonical_json(ack_body)) != ack_digest:
            raise ValueError("realtime import ACK digest mismatch")
        if (
            ack.get("schema") != QFBV_CHECKED_IMPORT_ACK_SCHEMA
            or ack.get("protocol") != QFBV_REALTIME_STREAM_PROTOCOL
            or ack.get("event_stream_protocol") != QFBV_PROOF_EVENT_STREAM_PROTOCOL
            or ack.get("clause_protocol") != QFBV_INCREMENTAL_PROOF_PROTOCOL
            or ack.get("stream_id") != stream_id
            or ack.get("record_sha256") != record_id
            or ack.get("cnf_sha256") != certificate["cnf_sha256"]
            or ack.get("checker_policy_sha256") != policy_sha256
            or ack.get("native_signature") != native_signature
        ):
            raise ValueError("realtime import ACK scope is inconsistent")
        _required_hex_digest(ack.get("formula_sha256"), "realtime ACK formula identity")
        _required_hex_digest(ack.get("clause_sha256"), "realtime ACK clause identity")
        token = _bounded_int(
            ack.get("token"), "realtime import token", 1, (1 << 64) - 1
        )
        event_sequence = _bounded_int(
            ack.get("event_sequence"),
            "realtime proof event sequence",
            1,
            (1 << 63) - 1,
        )
        if token in tokens or event_sequence in event_sequences:
            raise ValueError("realtime import ACK identity is duplicated")
        tokens.add(token)
        event_sequences.add(event_sequence)
        if (
            _bounded_int(
                ack.get("solve_generation"),
                "realtime ACK solve generation",
                1,
                (1 << 63) - 1,
            )
            != generation
            or _bounded_int(
                ack.get("delivery_ordinal"),
                "realtime ACK delivery ordinal",
                1,
                4096,
            )
            != expected_ordinal
        ):
            raise ValueError("realtime native ACK ordering is inconsistent")
        _bounded_int(
            ack.get("authorized_monotonic_ns"),
            "realtime import authorization time",
            1,
            (1 << 63) - 1,
        )
        acks.append(ack)
    activity_fields: dict[str, Any] = {}
    activity_keys = {
        "backend_realtime_clause_activity_enabled",
        "backend_realtime_clause_activity_protocol",
        "backend_realtime_clause_activity_unit",
        "backend_realtime_clause_activity_conflict",
        "backend_realtime_clause_activity_unactivated",
        "backend_realtime_clause_activity_receipts",
        "backend_realtime_native_imports_activated_unit",
        "backend_realtime_native_imports_activated_conflict",
    }
    present_activity_keys = activity_keys & set(result)
    if present_activity_keys and present_activity_keys != activity_keys:
        raise ValueError("realtime clause-activity telemetry is incomplete")
    if present_activity_keys:
        activity_enabled = result.get("backend_realtime_clause_activity_enabled")
        if not isinstance(activity_enabled, bool):
            raise ValueError("realtime clause-activity state is invalid")
        activity_protocol = result.get("backend_realtime_clause_activity_protocol")
        if activity_protocol != (
            QFBV_CLAUSE_ACTIVITY_PROTOCOL if activity_enabled else ""
        ):
            raise ValueError("realtime clause-activity protocol is inconsistent")
        activity_unit = _bounded_int(
            result.get("backend_realtime_clause_activity_unit"),
            "realtime unit activities",
            0,
            delivered,
        )
        activity_conflict = _bounded_int(
            result.get("backend_realtime_clause_activity_conflict"),
            "realtime conflict activities",
            0,
            delivered,
        )
        activity_unactivated = _bounded_int(
            result.get("backend_realtime_clause_activity_unactivated"),
            "realtime unactivated imports",
            0,
            delivered,
        )
        if activity_unit + activity_conflict + activity_unactivated != delivered:
            raise ValueError("realtime clause-activity accounting is inconsistent")
        raw_activity_receipts = result.get("backend_realtime_clause_activity_receipts")
        if (
            not isinstance(raw_activity_receipts, list)
            or len(raw_activity_receipts) != activity_unit + activity_conflict
        ):
            raise ValueError("realtime clause-activity receipts are incomplete")
        normalized_receipts: list[dict[str, Any]] = []
        activity_records: set[str] = set()
        ack_by_digest = {str(ack["ack_sha256"]): ack for ack in acks}
        for expected_ordinal, raw_receipt in enumerate(raw_activity_receipts, 1):
            if not isinstance(raw_receipt, Mapping):
                raise ValueError("realtime clause-activity receipt is malformed")
            receipt = _normalize_json(dict(raw_receipt))
            if not isinstance(receipt, dict):
                raise ValueError("realtime clause-activity normalization failed")
            receipt_digest = _required_hex_digest(
                receipt.get("activity_sha256"), "clause-activity identity"
            )
            receipt_body = dict(receipt)
            receipt_body.pop("activity_sha256")
            if _digest(_canonical_json(receipt_body)) != receipt_digest:
                raise ValueError("realtime clause-activity digest mismatch")
            if (
                receipt.get("schema") != QFBV_CLAUSE_ACTIVITY_SCHEMA
                or receipt.get("protocol") != QFBV_CLAUSE_ACTIVITY_PROTOCOL
                or receipt.get("stream_id") != stream_id
                or receipt.get("native_signature") != native_signature
                or receipt.get("ack_sha256") not in ack_by_digest
                or _bounded_int(
                    receipt.get("activity_ordinal"),
                    "clause-activity ordinal",
                    1,
                    4096,
                )
                != expected_ordinal
            ):
                raise ValueError("realtime clause-activity scope is inconsistent")
            record = _required_hex_digest(
                receipt.get("record_sha256"), "clause-activity record"
            )
            if record in activity_records:
                raise ValueError("realtime clause activity is duplicated")
            activity_records.add(record)
            normalized_receipts.append(receipt)
        native_activity_unit = _bounded_int(
            result.get("backend_realtime_native_imports_activated_unit"),
            "native realtime unit activities",
            0,
            delivered,
        )
        native_activity_conflict = _bounded_int(
            result.get("backend_realtime_native_imports_activated_conflict"),
            "native realtime conflict activities",
            0,
            delivered,
        )
        if (
            native_activity_unit != activity_unit
            or native_activity_conflict != activity_conflict
            or (not activity_enabled and normalized_receipts)
        ):
            raise ValueError("native clause-activity accounting is inconsistent")
        activity_fields = {
            "backend_realtime_clause_activity_enabled": activity_enabled,
            "backend_realtime_clause_activity_protocol": activity_protocol,
            "backend_realtime_clause_activity_unit": activity_unit,
            "backend_realtime_clause_activity_conflict": activity_conflict,
            "backend_realtime_clause_activity_unactivated": activity_unactivated,
            "backend_realtime_clause_activity_receipts": normalized_receipts,
            "backend_realtime_native_imports_activated_unit": (native_activity_unit),
            "backend_realtime_native_imports_activated_conflict": (
                native_activity_conflict
            ),
        }
    learned_candidates = _bounded_int(
        result.get("backend_realtime_learned_candidates"),
        "realtime learned candidates",
        0,
        65_536,
    )
    learned_published = _bounded_int(
        result.get("backend_realtime_learned_published"),
        "realtime learned clauses published",
        0,
        learned_candidates,
    )
    learned_created = _bounded_int(
        result.get("backend_realtime_learned_created"),
        "realtime learned records created",
        0,
        learned_published,
    )
    learned_rejected = _bounded_int(
        result.get("backend_realtime_learned_rejected"),
        "realtime learned clauses rejected",
        0,
        learned_candidates,
    )
    if learned_published + learned_rejected != learned_candidates:
        raise ValueError("realtime learned-clause accounting is inconsistent")
    learned_ids_raw = result.get("backend_realtime_learned_record_sha256", ())
    if (
        not isinstance(learned_ids_raw, list)
        or len(learned_ids_raw) != learned_published
    ):
        raise ValueError("realtime learned-record identities are inconsistent")
    learned_ids = [
        _required_hex_digest(value, "realtime learned-record identity")
        for value in learned_ids_raw
    ]
    if len(set(learned_ids)) != learned_published:
        raise ValueError("realtime learned-record identities are duplicated")
    native_enqueued = _bounded_int(
        result.get("backend_realtime_native_imports_enqueued"),
        "native realtime imports enqueued",
        0,
        4096,
    )
    native_delivered = _bounded_int(
        result.get("backend_realtime_native_imports_delivered"),
        "native realtime imports delivered",
        0,
        native_enqueued,
    )
    native_rejected = _bounded_int(
        result.get("backend_realtime_native_imports_rejected"),
        "native realtime imports rejected",
        0,
        1_000_000,
    )
    if (
        native_enqueued != authorized
        or native_delivered != delivered
        or native_rejected != backpressure
    ):
        raise ValueError("native realtime import accounting is inconsistent")
    native_learned_exported = _bounded_int(
        result.get("backend_realtime_native_learned_exported"),
        "native realtime learned clauses exported",
        0,
        (1 << 63) - 1,
    )
    if native_learned_exported != learned_candidates:
        raise ValueError("native realtime learned-clause accounting is incomplete")
    native_learned_dropped = _bounded_int(
        result.get("backend_realtime_native_learned_dropped"),
        "native realtime learned clauses dropped",
        0,
        (1 << 63) - 1,
    )
    stream_error = str(result.get("backend_realtime_stream_error", ""))[:512]
    if status in {"sat", "unsat"} and stream_error:
        raise ValueError("decisive result carries a realtime stream error")
    compression_fields: dict[str, Any] = {}
    compression_keys = {
        "backend_realtime_clause_compression_protocol",
        "backend_realtime_clause_uncompressed_bytes",
        "backend_realtime_clause_compressed_bytes",
        "backend_realtime_clause_inline_clauses",
        "backend_realtime_clause_heap_clauses",
        "backend_realtime_clause_queued_encoded_bytes",
        "backend_realtime_clause_decoded_literals",
        "backend_realtime_clause_compression_failures",
    }
    present_compression_keys = compression_keys & set(result)
    if present_compression_keys and present_compression_keys != compression_keys:
        raise ValueError("realtime clause-compression telemetry is incomplete")
    if present_compression_keys:
        if (
            result.get("backend_realtime_clause_compression_protocol")
            != QFBV_CLAUSE_COMPRESSION_PROTOCOL
        ):
            raise ValueError("invalid realtime clause-compression protocol")
        uncompressed_bytes = _bounded_int(
            result.get("backend_realtime_clause_uncompressed_bytes"),
            "realtime uncompressed clause bytes",
            0,
            (1 << 63) - 1,
        )
        compressed_bytes = _bounded_int(
            result.get("backend_realtime_clause_compressed_bytes"),
            "realtime compressed clause bytes",
            0,
            (1 << 63) - 1,
        )
        inline_clauses = _bounded_int(
            result.get("backend_realtime_clause_inline_clauses"),
            "realtime inline compressed clauses",
            0,
            native_enqueued,
        )
        heap_clauses = _bounded_int(
            result.get("backend_realtime_clause_heap_clauses"),
            "realtime heap compressed clauses",
            0,
            native_enqueued,
        )
        queued_bytes = _bounded_int(
            result.get("backend_realtime_clause_queued_encoded_bytes"),
            "realtime queued compressed clause bytes",
            0,
            compressed_bytes,
        )
        decoded_literals = _bounded_int(
            result.get("backend_realtime_clause_decoded_literals"),
            "realtime decoded compressed literals",
            0,
            (1 << 63) - 1,
        )
        failures = _bounded_int(
            result.get("backend_realtime_clause_compression_failures"),
            "realtime clause-compression failures",
            0,
            0,
        )
        if (
            uncompressed_bytes % 4
            or inline_clauses + heap_clauses != native_enqueued
            or compressed_bytes < native_enqueued
        ):
            raise ValueError("realtime clause-compression accounting is inconsistent")
        compression_fields = {
            "backend_realtime_clause_compression_protocol": (
                QFBV_CLAUSE_COMPRESSION_PROTOCOL
            ),
            "backend_realtime_clause_uncompressed_bytes": uncompressed_bytes,
            "backend_realtime_clause_compressed_bytes": compressed_bytes,
            "backend_realtime_clause_inline_clauses": inline_clauses,
            "backend_realtime_clause_heap_clauses": heap_clauses,
            "backend_realtime_clause_queued_encoded_bytes": queued_bytes,
            "backend_realtime_clause_decoded_literals": decoded_literals,
            "backend_realtime_clause_compression_failures": failures,
        }
    adaptive_fields: dict[str, Any] = {}
    adaptive_keys = {
        str(key) for key in result if str(key).startswith("backend_realtime_adaptive_")
    }
    if result.get("backend_realtime_adaptive_protocol") is not None:
        adaptive_fields = verify_adaptive_stream_result(
            result,
            stream_id=stream_id,
            delivered_records=tuple(import_ids),
            authorized=authorized,
            backpressure=backpressure,
        )
    elif adaptive_keys:
        raise ValueError("adaptive proof fields lack their protocol identity")
    pairing_fields: dict[str, Any] = {}
    pairing_keys = {
        str(key) for key in result if str(key).startswith("backend_realtime_pairing_")
    }
    if result.get("backend_realtime_pairing_protocol") is not None:
        if not activity_fields.get("backend_realtime_clause_activity_enabled", False):
            raise ValueError("utility pairing requires clause-activity evidence")
        activity_kinds = {
            str(receipt["record_sha256"]): str(receipt["kind"])
            for receipt in activity_fields["backend_realtime_clause_activity_receipts"]
        }
        pairing_fields = verify_pairing_stream_result(
            result,
            stream_id=stream_id,
            consumer_worker=result.get("backend_realtime_pairing_consumer_worker", ""),
            formula_family=formula_family_sha256(certificate),
            delivered_records=tuple(import_ids),
            activity_kinds=activity_kinds,
        )
    elif pairing_keys:
        raise ValueError("utility pairing fields lack their protocol identity")
    return {
        "backend_realtime_stream_protocol": QFBV_REALTIME_STREAM_PROTOCOL,
        "backend_realtime_event_stream_protocol": (QFBV_PROOF_EVENT_STREAM_PROTOCOL),
        "backend_realtime_stream_id": stream_id,
        "backend_realtime_native_signature": native_signature,
        "backend_realtime_solve_generation": generation,
        "backend_realtime_polls": polls,
        "backend_realtime_events_observed": events,
        "backend_realtime_import_candidates": candidates,
        **settled_fields,
        "backend_realtime_import_authorized": authorized,
        "backend_realtime_import_delivered": delivered,
        "backend_realtime_import_pending": pending,
        "backend_realtime_import_rejected": rejected,
        "backend_realtime_import_backpressure": backpressure,
        "backend_realtime_import_checker_elapsed_us": _bounded_int(
            result.get("backend_realtime_import_checker_elapsed_us"),
            "realtime checker elapsed_us",
            0,
            (1 << 63) - 1,
        ),
        "backend_realtime_import_record_sha256": import_ids,
        "backend_realtime_import_acks": acks,
        **activity_fields,
        "backend_realtime_learned_candidates": learned_candidates,
        "backend_realtime_learned_published": learned_published,
        "backend_realtime_learned_created": learned_created,
        "backend_realtime_learned_rejected": learned_rejected,
        "backend_realtime_learned_record_sha256": learned_ids,
        "backend_realtime_native_imports_enqueued": native_enqueued,
        "backend_realtime_native_imports_delivered": native_delivered,
        "backend_realtime_native_imports_rejected": native_rejected,
        "backend_realtime_native_learned_exported": native_learned_exported,
        "backend_realtime_native_learned_dropped": native_learned_dropped,
        "backend_realtime_stream_error": stream_error,
        **compression_fields,
        **adaptive_fields,
        **pairing_fields,
    }


def _sealed_memfd(content: bytes, *, role: str, digest: str) -> int:
    """Create an immutable, anonymous SMT2 snapshot for one work lease."""
    required = (
        "memfd_create",
        "MFD_CLOEXEC",
        "MFD_ALLOW_SEALING",
    )
    if any(not hasattr(os, name) for name in required) or any(
        not hasattr(fcntl, name)
        for name in (
            "F_ADD_SEALS",
            "F_GET_SEALS",
            "F_SEAL_WRITE",
            "F_SEAL_GROW",
            "F_SEAL_SHRINK",
            "F_SEAL_SEAL",
        )
    ):
        raise OSError(
            errno.EOPNOTSUPP,
            "sealed query artifacts require Linux memfd seals",
        )
    if role not in _ARTIFACT_ROLES or _hex_digest(digest) != digest:
        raise ValueError("invalid sealed query artifact identity")
    if len(content) > _MAX_ARTIFACT_BYTES or _digest(content) != digest:
        raise ValueError("sealed query artifact content does not match its digest")
    descriptor = os.memfd_create(
        f"symcc-{role}-{digest[:16]}",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    try:
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            try:
                written = os.write(descriptor, view[offset:])
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError(errno.EIO, "short sealed query artifact write")
            offset += written
        os.fsync(descriptor)
        fcntl.fcntl(
            descriptor,
            fcntl.F_ADD_SEALS,
            _SEALED_ARTIFACT_REQUIRED_SEALS,
        )
        observed_seals = int(fcntl.fcntl(descriptor, fcntl.F_GET_SEALS))
        if (
            observed_seals & _SEALED_ARTIFACT_REQUIRED_SEALS
        ) != _SEALED_ARTIFACT_REQUIRED_SEALS:
            raise OSError(errno.EIO, "query artifact memfd is not fully sealed")
        if os.pread(descriptor, len(content) + 1, 0) != content:
            raise OSError(errno.EIO, "sealed query artifact verification failed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _SealedArtifactBundle:
    """Thread-safe owner for the three immutable descriptors in a lease."""

    def __init__(self, descriptors: Mapping[str, int]):
        if tuple(descriptors) != _ARTIFACT_ROLES or any(
            type(descriptor) is not int or descriptor < 0
            for descriptor in descriptors.values()
        ):
            raise ValueError("sealed artifact bundle is incomplete")
        self._descriptors = dict(descriptors)
        self._lock = threading.Lock()
        self._closed = False

    def duplicate(self, roles: Sequence[str]) -> tuple[int, ...]:
        if not roles or any(role not in _ARTIFACT_ROLES for role in roles):
            raise ValueError("invalid query artifact role")
        duplicated: list[int] = []
        with self._lock:
            if self._closed:
                raise ValueError("query artifact lease is closed")
            try:
                for role in roles:
                    duplicated.append(os.dup(self._descriptors[role]))
            except BaseException:
                for descriptor in duplicated:
                    os.close(descriptor)
                raise
        return tuple(duplicated)

    @property
    def is_open(self) -> bool:
        with self._lock:
            return not self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            descriptors = tuple(self._descriptors.values())
            self._descriptors.clear()
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True)
class WorkLease:
    query_id: str
    token: int
    smt2_path: Path
    prefix_key: str
    prefix_smt2_path: Path
    target_smt2_path: Path
    timeout_ms: int
    input_hex: str = ""
    _sealed_artifacts: _SealedArtifactBundle | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def has_sealed_artifacts(self) -> bool:
        return self._sealed_artifacts is not None and self._sealed_artifacts.is_open

    def duplicate_artifacts(self, *roles: str) -> tuple[int, ...]:
        if self._sealed_artifacts is None:
            return ()
        return self._sealed_artifacts.duplicate(roles)

    def close_artifacts(self) -> None:
        if self._sealed_artifacts is not None:
            self._sealed_artifacts.close()


class QueryStore:
    """SQLite-indexed query trie with immutable content-addressed artifacts."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        startup_reconcile_limit: int = _DEFAULT_STARTUP_PUBLICATION_LIMIT,
        startup_reconcile_seconds: float = _DEFAULT_STARTUP_PUBLICATION_SECONDS,
    ):
        if (
            isinstance(startup_reconcile_limit, bool)
            or not isinstance(startup_reconcile_limit, int)
            or not 0 <= startup_reconcile_limit <= 1_000_000
        ):
            raise ValueError("startup publication limit is invalid")
        try:
            startup_seconds = float(startup_reconcile_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("startup publication time budget is invalid") from error
        if (
            not math.isfinite(startup_seconds)
            or not 0.0 <= startup_seconds <= 3600.0
        ):
            raise ValueError("startup publication time budget is invalid")
        self.root = Path(root).resolve()
        self.object_dir = self.root / "objects"
        self.query_dir = self.root / "queries"
        self.result_dir = self.root / "results"
        self.generator_dir = self.root / "generators"
        self.candidate_dir = self.root / "candidates"
        self.joint_schedule_dir = self.root / "joint_schedule"
        self.db_path = self.root / "index.sqlite3"
        self._artifact_audit_lock_path = self.root / ".artifact-audit.lock"
        self._qfbv_proof_verifier_lock = threading.Lock()
        self._qfbv_proof_verifiers: dict[str, QfbvProofVerifier] = {}
        self._qfbv_lemma_exchange_lock = threading.Lock()
        self._qfbv_lemma_exchanges: dict[str, QfbvLemmaExchange] = {}
        self._qfbv_substitution_core_exchange_lock = threading.Lock()
        self._qfbv_substitution_core_exchanges: dict[
            str, QfbvSubstitutionCoreExchange
        ] = {}
        self._qfbv_incremental_proof_checker_lock = threading.Lock()
        self._qfbv_incremental_proof_checkers: dict[str, IncrementalProofChecker] = {}
        self._qfbv_proof_wire_lock = threading.Lock()
        self._qfbv_proof_wires: dict[
            str, tuple[LidrupExternalChecker, LidrupWireStore]
        ] = {}
        self._last_publication_reconciliation: dict[str, Any] = {
            "selected": 0,
            "attempted": 0,
            "published": 0,
            "failed": 0,
            "dead_lettered": 0,
            "remaining_budget": startup_reconcile_limit,
            "time_budget_exhausted": False,
            "elapsed_seconds": 0.0,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifact_stores = {
            kind: ContentAddressedInputStore(
                str(self.object_dir / kind),
                _MAX_ARTIFACT_BYTES,
                object_suffix=".smt2",
                full_digest_leaf=True,
            )
            for kind in _ARTIFACT_KINDS
        }
        self._initialize()
        if startup_reconcile_limit and startup_seconds:
            self.reconcile_result_publications(
                limit=startup_reconcile_limit,
                time_budget_seconds=startup_seconds,
                continue_on_error=True,
                respect_retry_after=True,
            )

    def register_qfbv_proof_verifier(
        self,
        verifier: QfbvProofVerifier,
    ) -> None:
        """Register a locally trusted checker policy for commit-time replay."""
        policy = str(verifier.policy_sha256)
        if _hex_digest(policy) != policy:
            raise ValueError("invalid QF_BV proof verifier policy identity")
        with self._qfbv_proof_verifier_lock:
            existing = self._qfbv_proof_verifiers.get(policy)
            if (
                existing is not None
                and existing is not verifier
                and existing.store.root != verifier.store.root
            ):
                raise ValueError("QF_BV proof verifier policy has conflicting stores")
            if existing is None:
                self._qfbv_proof_verifiers[policy] = verifier

    def register_qfbv_lemma_exchange(
        self,
        exchange: QfbvLemmaExchange,
    ) -> None:
        """Register a locally trusted lemma checker for commit-time replay."""
        policy = str(exchange.policy_sha256)
        if _hex_digest(policy) != policy:
            raise ValueError("invalid QF_BV lemma exchange policy identity")
        with self._qfbv_lemma_exchange_lock:
            existing = self._qfbv_lemma_exchanges.get(policy)
            if (
                existing is not None
                and existing is not exchange
                and (
                    existing.store.root != exchange.store.root
                    or existing.context_store.root != exchange.context_store.root
                    or existing.proof_verifier.store.root
                    != exchange.proof_verifier.store.root
                )
            ):
                raise ValueError("QF_BV lemma exchange policy has conflicting stores")
            if existing is None:
                self._qfbv_lemma_exchanges[policy] = exchange

    def register_qfbv_substitution_core_exchange(
        self,
        exchange: QfbvSubstitutionCoreExchange,
    ) -> None:
        """Register the local proof/matching policy used at result commit."""
        policy = str(exchange.policy_sha256)
        if _hex_digest(policy) != policy:
            raise ValueError("invalid QF_BV substitution-core policy identity")
        with self._qfbv_substitution_core_exchange_lock:
            existing = self._qfbv_substitution_core_exchanges.get(policy)
            if (
                existing is not None
                and existing is not exchange
                and (
                    existing.store.root != exchange.store.root
                    or existing.proof_verifier.store.root
                    != exchange.proof_verifier.store.root
                )
            ):
                raise ValueError(
                    "QF_BV substitution-core policy has conflicting stores"
                )
            if existing is None:
                self._qfbv_substitution_core_exchanges[policy] = exchange

    def register_qfbv_incremental_proof_checker(
        self,
        checker: IncrementalProofChecker,
    ) -> None:
        """Register the LRUP replay policy used by the result commit path."""
        policy = str(checker.policy_sha256)
        if _hex_digest(policy) != policy or checker.store is None:
            raise ValueError("invalid incremental proof checker policy")
        with self._qfbv_incremental_proof_checker_lock:
            existing = self._qfbv_incremental_proof_checkers.get(policy)
            if (
                existing is not None
                and existing is not checker
                and (
                    existing.store is None or existing.store.root != checker.store.root
                )
            ):
                raise ValueError(
                    "incremental proof checker policy has conflicting stores"
                )
            if existing is None:
                self._qfbv_incremental_proof_checkers[policy] = checker

    def register_qfbv_proof_wire(
        self,
        checker: LidrupExternalChecker,
        store: LidrupWireStore,
    ) -> None:
        """Register the pinned external LIDRUP policy used at result commit."""
        if not isinstance(checker, LidrupExternalChecker) or not isinstance(
            store, LidrupWireStore
        ):
            raise ValueError("invalid LIDRUP proof wire registration")
        policy = str(checker.policy_sha256)
        if _hex_digest(policy) != policy:
            raise ValueError("invalid LIDRUP checker policy identity")
        with self._qfbv_proof_wire_lock:
            existing = self._qfbv_proof_wires.get(policy)
            if existing is not None and (
                existing[0] is not checker or existing[1].root != store.root
            ):
                raise ValueError("LIDRUP checker policy has conflicting stores")
            if existing is None:
                self._qfbv_proof_wires[policy] = (checker, store)

    def load_qfbv_utility_pairing_snapshot(
        self,
        consumer_worker: str,
        policy: UtilityPairingPolicy,
    ) -> dict[str, Any] | None:
        """Load the last independently verified pairing state for one worker."""
        if not isinstance(policy, UtilityPairingPolicy):
            raise ValueError("invalid utility-pairing persistence scope")
        try:
            worker = validate_pairing_worker_identity(
                consumer_worker, "pairing consumer worker"
            )
        except UtilityPairingError as error:
            raise ValueError("invalid utility-pairing persistence scope") from error
        with self._connect() as db:
            row = db.execute(
                "SELECT snapshot_sha256, next_decision_ordinal, "
                "next_event_ordinal, snapshot_json FROM "
                "utility_pairing_snapshots WHERE consumer_worker = ? "
                "AND policy_sha256 = ?",
                (worker, policy.sha256),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(str(row["snapshot_json"]))
            snapshot = verify_pairing_snapshot(raw, policy=policy)
        except (json.JSONDecodeError, UtilityPairingError) as error:
            raise ValueError("stored utility-pairing snapshot is invalid") from error
        if (
            snapshot["snapshot_sha256"] != row["snapshot_sha256"]
            or snapshot["next_decision_ordinal"] != int(row["next_decision_ordinal"])
            or snapshot["next_controller_event_ordinal"]
            != int(row["next_event_ordinal"])
            or any(
                state["consumer_worker"] != worker
                for state in snapshot["pairs"].values()
            )
        ):
            raise ValueError("stored utility-pairing snapshot scope changed")
        return snapshot

    def load_qfbv_malleable_worker_snapshot(
        self,
        pool_id: str,
        policy: MalleableWorkerPolicy,
    ) -> dict[str, Any] | None:
        """Load one independently verified logical-worker allocation state."""
        if not isinstance(policy, MalleableWorkerPolicy):
            raise ValueError("invalid malleable-worker persistence scope")
        try:
            pool = validate_malleable_identity(pool_id, "malleable pool")
        except MalleableWorkerError as error:
            raise ValueError("invalid malleable-worker persistence scope") from error
        with self._connect() as db:
            row = db.execute(
                "SELECT snapshot_sha256, allocation_generation, "
                "next_event_ordinal, snapshot_json FROM "
                "malleable_worker_snapshots WHERE pool_id = ? "
                "AND policy_sha256 = ?",
                (pool, policy.sha256),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = verify_malleable_snapshot(
                json.loads(str(row["snapshot_json"])), policy=policy
            )
        except (json.JSONDecodeError, MalleableWorkerError) as error:
            raise ValueError("stored malleable-worker snapshot is invalid") from error
        if (
            snapshot["pool_id"] != pool
            or snapshot["snapshot_sha256"] != row["snapshot_sha256"]
            or snapshot["allocation_generation"] != int(row["allocation_generation"])
            or snapshot["next_event_ordinal"] != int(row["next_event_ordinal"])
        ):
            raise ValueError("stored malleable-worker snapshot scope changed")
        return snapshot

    def commit_qfbv_malleable_worker_snapshot(
        self,
        pool_id: str,
        policy: MalleableWorkerPolicy,
        snapshot: Mapping[str, Any],
    ) -> str:
        """Persist a monotonic, fork-detecting controller checkpoint."""
        if not isinstance(policy, MalleableWorkerPolicy):
            raise ValueError("invalid malleable-worker persistence scope")
        try:
            pool = validate_malleable_identity(pool_id, "malleable pool")
            verified = verify_malleable_snapshot(snapshot, policy=policy)
        except MalleableWorkerError as error:
            raise ValueError("invalid malleable-worker checkpoint") from error
        if verified["pool_id"] != pool:
            raise ValueError("malleable-worker checkpoint scope changed")
        next_event = int(verified["next_event_ordinal"])
        generation = int(verified["allocation_generation"])
        identity = str(verified["snapshot_sha256"])
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT snapshot_sha256, allocation_generation, "
                "next_event_ordinal FROM malleable_worker_snapshots "
                "WHERE pool_id = ? AND policy_sha256 = ?",
                (pool, policy.sha256),
            ).fetchone()
            if existing is not None:
                old_event = int(existing["next_event_ordinal"])
                old_generation = int(existing["allocation_generation"])
                if next_event < old_event:
                    db.rollback()
                    return "stale"
                if next_event == old_event:
                    if (
                        generation != old_generation
                        or identity != existing["snapshot_sha256"]
                    ):
                        raise ValueError(
                            "malleable-worker checkpoint forked at one ordinal"
                        )
                    db.rollback()
                    return "idempotent"
                if generation < old_generation:
                    raise ValueError(
                        "malleable-worker allocation generation moved backwards"
                    )
            db.execute(
                "INSERT INTO malleable_worker_snapshots(pool_id, policy_sha256, "
                "snapshot_sha256, allocation_generation, next_event_ordinal, "
                "snapshot_json, updated) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pool_id, policy_sha256) DO UPDATE SET "
                "snapshot_sha256=excluded.snapshot_sha256, "
                "allocation_generation=excluded.allocation_generation, "
                "next_event_ordinal=excluded.next_event_ordinal, "
                "snapshot_json=excluded.snapshot_json, updated=excluded.updated",
                (
                    pool,
                    policy.sha256,
                    identity,
                    generation,
                    next_event,
                    _canonical_json(verified).decode("ascii"),
                    now,
                ),
            )
            db.commit()
        return "advanced"

    def load_ulfm_recovery_snapshot(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
    ) -> dict[str, Any] | None:
        """Load and independently verify the latest communicator recovery state."""
        if not isinstance(policy, UlfmRecoveryPolicy):
            raise ValueError("invalid ULFM recovery persistence policy")
        if not isinstance(run_id, str) or not run_id or len(run_id.encode()) > 256:
            raise ValueError("invalid ULFM recovery persistence run")
        state = self.load_ulfm_recovery_state(run_id, policy)
        return None if state is None else state[0]

    def load_ulfm_recovery_state(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
    ) -> tuple[dict[str, Any], int] | None:
        """Load the verified snapshot together with its monotonic state ordinal."""
        if not isinstance(policy, UlfmRecoveryPolicy):
            raise ValueError("invalid ULFM recovery persistence policy")
        if not isinstance(run_id, str) or not run_id or len(run_id.encode()) > 256:
            raise ValueError("invalid ULFM recovery persistence run")
        with self._connect() as db:
            row = db.execute(
                "SELECT snapshot_sha256, generation, recovery_count, "
                "state_ordinal, snapshot_json FROM ulfm_recovery_snapshots "
                "WHERE run_id = ? AND policy_sha256 = ?",
                (run_id, policy.sha256),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = verify_recovery_snapshot(json.loads(str(row["snapshot_json"])))
        except (json.JSONDecodeError, UlfmRecoveryError) as error:
            raise ValueError("stored ULFM recovery snapshot is invalid") from error
        if (
            snapshot["run_id"] != run_id
            or snapshot["policy_sha256"] != policy.sha256
            or snapshot["policy"] != policy.as_dict()
            or snapshot["snapshot_sha256"] != row["snapshot_sha256"]
            or snapshot["generation"] != int(row["generation"])
            or snapshot["recovery_count"] != int(row["recovery_count"])
            or int(row["state_ordinal"]) < 0
        ):
            raise ValueError("stored ULFM recovery snapshot scope changed")
        return snapshot, int(row["state_ordinal"])

    def commit_ulfm_recovery_snapshot(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
        snapshot: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
        state_ordinal: int | None = None,
    ) -> str:
        """Persist one ordered runtime or recovery transition atomically.

        Dispatch, finish and cancellation use an explicit consecutive state
        ordinal. Recovery additionally requires one pending plan followed by
        the exact independently verified generation receipt. Same-ordinal
        identities are idempotent; different identities are rejected as a
        fork so concurrent survivors cannot silently split one recovery epoch.
        """
        if not isinstance(policy, UlfmRecoveryPolicy):
            raise ValueError("invalid ULFM recovery persistence policy")
        if not isinstance(run_id, str) or not run_id or len(run_id.encode()) > 256:
            raise ValueError("invalid ULFM recovery persistence run")
        try:
            verified = verify_recovery_snapshot(snapshot)
        except UlfmRecoveryError as error:
            raise ValueError("invalid ULFM recovery checkpoint") from error
        if (
            verified["run_id"] != run_id
            or verified["policy_sha256"] != policy.sha256
            or verified["policy"] != policy.as_dict()
        ):
            raise ValueError("ULFM recovery checkpoint scope changed")
        normalized_receipt: dict[str, Any] | None = None
        if receipt is not None:
            try:
                normalized_receipt = verify_recovery_receipt(
                    receipt, post_snapshot=verified
                )
            except UlfmRecoveryError as error:
                raise ValueError("invalid ULFM recovery receipt") from error
            if (
                normalized_receipt["run_id"] != run_id
                or normalized_receipt["policy_sha256"] != policy.sha256
            ):
                raise ValueError("ULFM recovery receipt scope changed")

        identity = str(verified["snapshot_sha256"])
        generation = int(verified["generation"])
        recovery_count = int(verified["recovery_count"])
        pending = verified["pending_recovery"] is not None
        explicit_ordinal = state_ordinal is not None
        if state_ordinal is not None and (
            isinstance(state_ordinal, bool)
            or not isinstance(state_ordinal, int)
            or not 0 <= state_ordinal < (1 << 63)
        ):
            raise ValueError("invalid ULFM recovery state ordinal")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT snapshot_sha256, generation, recovery_count, "
                "state_ordinal, pending, snapshot_json FROM ulfm_recovery_snapshots "
                "WHERE run_id = ? AND policy_sha256 = ?",
                (run_id, policy.sha256),
            ).fetchone()
            if existing is not None:
                old_generation = int(existing["generation"])
                old_count = int(existing["recovery_count"])
                old_ordinal = int(existing["state_ordinal"])
                old_pending = bool(existing["pending"])
                if (
                    not explicit_ordinal
                    and identity == existing["snapshot_sha256"]
                ):
                    db.rollback()
                    return "idempotent"
                next_ordinal = (
                    int(state_ordinal) if explicit_ordinal else old_ordinal + 1
                )
                if next_ordinal < old_ordinal:
                    db.rollback()
                    return "stale"
                if next_ordinal == old_ordinal:
                    if identity != existing["snapshot_sha256"]:
                        raise ValueError(
                            "ULFM recovery checkpoint forked at one state ordinal"
                        )
                    db.rollback()
                    return "idempotent"
                if next_ordinal != old_ordinal + 1:
                    raise ValueError("ULFM recovery state ordinal is not consecutive")
                if identity == existing["snapshot_sha256"]:
                    raise ValueError(
                        "ULFM recovery state ordinal advanced without a state change"
                    )
                if generation < old_generation or recovery_count < old_count:
                    db.rollback()
                    return "stale"
                if generation == old_generation:
                    if recovery_count != old_count:
                        raise ValueError(
                            "ULFM recovery count changed without a generation change"
                        )
                    if old_pending or normalized_receipt is not None:
                        raise ValueError(
                            "ULFM recovery checkpoint forked within one generation"
                        )
                    if not explicit_ordinal and not pending:
                        raise ValueError(
                            "ULFM runtime checkpoint requires an explicit state ordinal"
                        )
                else:
                    if generation != old_generation + 1 or recovery_count != old_count + 1:
                        raise ValueError("ULFM recovery generation is not consecutive")
                    if not old_pending or pending or normalized_receipt is None:
                        raise ValueError(
                            "ULFM recovery generation advanced without prepare/receipt"
                        )
                    if (
                        normalized_receipt["base_generation"] != old_generation
                        or normalized_receipt["target_generation"] != generation
                    ):
                        raise ValueError("ULFM recovery receipt generation changed")
                    try:
                        old_snapshot = verify_recovery_snapshot(
                            json.loads(str(existing["snapshot_json"]))
                        )
                    except (json.JSONDecodeError, UlfmRecoveryError) as error:
                        raise ValueError(
                            "stored pending ULFM recovery snapshot is invalid"
                        ) from error
                    old_plan = old_snapshot["pending_recovery"]
                    if (
                        old_plan is None
                        or normalized_receipt["plan_sha256"]
                        != old_plan["plan_sha256"]
                    ):
                        raise ValueError("ULFM recovery receipt plan changed")
            else:
                next_ordinal = 0 if state_ordinal is None else int(state_ordinal)
                if (
                    generation != 0
                    or recovery_count != 0
                    or pending
                    or receipt is not None
                    or next_ordinal != 0
                ):
                    raise ValueError(
                        "ULFM recovery persistence must start at generation/ordinal zero"
                    )

            if normalized_receipt is not None:
                existing_receipt = db.execute(
                    "SELECT receipt_sha256 FROM ulfm_recovery_receipts "
                    "WHERE run_id = ? AND policy_sha256 = ? "
                    "AND target_generation = ?",
                    (run_id, policy.sha256, generation),
                ).fetchone()
                if existing_receipt is not None and (
                    existing_receipt["receipt_sha256"]
                    != normalized_receipt["receipt_sha256"]
                ):
                    raise ValueError("ULFM recovery receipt forked at one generation")
                db.execute(
                    "INSERT OR IGNORE INTO ulfm_recovery_receipts("
                    "run_id, policy_sha256, base_generation, target_generation, "
                    "receipt_sha256, receipt_json, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        policy.sha256,
                        int(normalized_receipt["base_generation"]),
                        int(normalized_receipt["target_generation"]),
                        str(normalized_receipt["receipt_sha256"]),
                        _canonical_json(normalized_receipt).decode("ascii"),
                        now,
                    ),
                )
            db.execute(
                "INSERT INTO ulfm_recovery_snapshots(run_id, policy_sha256, "
                "snapshot_sha256, generation, recovery_count, state_ordinal, pending, "
                "snapshot_json, updated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, policy_sha256) DO UPDATE SET "
                "snapshot_sha256=excluded.snapshot_sha256, "
                "generation=excluded.generation, "
                "recovery_count=excluded.recovery_count, pending=excluded.pending, "
                "state_ordinal=excluded.state_ordinal, "
                "snapshot_json=excluded.snapshot_json, updated=excluded.updated",
                (
                    run_id,
                    policy.sha256,
                    identity,
                    generation,
                    recovery_count,
                    next_ordinal,
                    int(pending),
                    _canonical_json(verified).decode("ascii"),
                    now,
                ),
            )
            db.commit()
        return "advanced"

    @staticmethod
    def _commit_qfbv_utility_pairing_snapshot(
        db: sqlite3.Connection,
        *,
        consumer_worker: str,
        policy_sha256: str,
        snapshot: Mapping[str, Any],
        query_id: str,
        updated: float,
    ) -> str:
        next_decision = int(snapshot["next_decision_ordinal"])
        next_event = int(snapshot["next_controller_event_ordinal"])
        snapshot_sha256 = str(snapshot["snapshot_sha256"])
        existing = db.execute(
            "SELECT snapshot_sha256, next_decision_ordinal, "
            "next_event_ordinal FROM utility_pairing_snapshots "
            "WHERE consumer_worker = ? AND policy_sha256 = ?",
            (consumer_worker, policy_sha256),
        ).fetchone()
        if existing is not None:
            old_event = int(existing["next_event_ordinal"])
            old_decision = int(existing["next_decision_ordinal"])
            if next_event < old_event:
                return "stale"
            if next_event == old_event:
                if (
                    next_decision != old_decision
                    or snapshot_sha256 != existing["snapshot_sha256"]
                ):
                    raise ValueError("utility-pairing checkpoint forked at one ordinal")
                return "idempotent"
            if next_decision < old_decision:
                raise ValueError("utility-pairing decision ordinal moved backwards")
        db.execute(
            "INSERT INTO utility_pairing_snapshots(consumer_worker, "
            "policy_sha256, snapshot_sha256, next_decision_ordinal, "
            "next_event_ordinal, snapshot_json, source_query_id, updated) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(consumer_worker, "
            "policy_sha256) DO UPDATE SET snapshot_sha256=excluded.snapshot_sha256, "
            "next_decision_ordinal=excluded.next_decision_ordinal, "
            "next_event_ordinal=excluded.next_event_ordinal, "
            "snapshot_json=excluded.snapshot_json, "
            "source_query_id=excluded.source_query_id, updated=excluded.updated",
            (
                consumer_worker,
                policy_sha256,
                snapshot_sha256,
                next_decision,
                next_event,
                _canonical_json(snapshot).decode("ascii"),
                query_id,
                updated,
            ),
        )
        return "advanced"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS expressions (
                    hash TEXT PRIMARY KEY,
                    body_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prefix_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER,
                    clause_hash TEXT,
                    depth INTEGER NOT NULL,
                    UNIQUE(parent_id, clause_hash),
                    FOREIGN KEY(parent_id) REFERENCES prefix_nodes(id)
                );
                CREATE TABLE IF NOT EXISTS queries (
                    query_id TEXT PRIMARY KEY,
                    prefix_id INTEGER NOT NULL,
                    prefix_hash TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    smt2_hash TEXT NOT NULL,
                    prefix_smt2_hash TEXT NOT NULL,
                    target_smt2_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority REAL NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    timeout_ms INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    FOREIGN KEY(prefix_id) REFERENCES prefix_nodes(id)
                );
                CREATE TABLE IF NOT EXISTS witnesses (
                    query_id TEXT NOT NULL,
                    witness_hash TEXT NOT NULL,
                    input_hex TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(query_id, witness_hash),
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS results (
                    query_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    completed REAL NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS result_publications (
                    query_id TEXT PRIMARY KEY,
                    published INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    next_retry REAL NOT NULL DEFAULT 0,
                    dead_letter INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(query_id) REFERENCES results(query_id)
                );
                CREATE TABLE IF NOT EXISTS utility_pairing_snapshots (
                    consumer_worker TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    next_decision_ordinal INTEGER NOT NULL,
                    next_event_ordinal INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    source_query_id TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY(consumer_worker, policy_sha256),
                    FOREIGN KEY(source_query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS malleable_worker_snapshots (
                    pool_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    allocation_generation INTEGER NOT NULL,
                    next_event_ordinal INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY(pool_id, policy_sha256)
                );
                CREATE TABLE IF NOT EXISTS ulfm_recovery_snapshots (
                    run_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    recovery_count INTEGER NOT NULL,
                    state_ordinal INTEGER NOT NULL DEFAULT 0,
                    pending INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY(run_id, policy_sha256)
                );
                CREATE TABLE IF NOT EXISTS ulfm_recovery_receipts (
                    run_id TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    base_generation INTEGER NOT NULL,
                    target_generation INTEGER NOT NULL,
                    receipt_sha256 TEXT NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    PRIMARY KEY(run_id, policy_sha256, target_generation)
                );
                CREATE TABLE IF NOT EXISTS generators (
                    query_id TEXT PRIMARY KEY,
                    generator_hash TEXT NOT NULL,
                    generator_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS query_clauses (
                    query_id TEXT NOT NULL,
                    clause_hash TEXT NOT NULL,
                    PRIMARY KEY(query_id, clause_hash),
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS query_literals (
                    query_id TEXT NOT NULL,
                    literal_hash TEXT NOT NULL,
                    PRIMARY KEY(query_id, literal_hash),
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS unsat_sets (
                    query_id TEXT PRIMARY KEY,
                    clause_count INTEGER NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS partial_solutions (
                    solution_hash TEXT PRIMARY KEY,
                    source_query_id TEXT NOT NULL,
                    assignment_json TEXT NOT NULL,
                    offsets_json TEXT NOT NULL,
                    clause_hashes_json TEXT NOT NULL,
                    model_index INTEGER NOT NULL,
                    generator_hash TEXT NOT NULL,
                    provenance TEXT NOT NULL DEFAULT 'solver-model',
                    proof_json TEXT NOT NULL DEFAULT '{}',
                    source_verified INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL,
                    FOREIGN KEY(source_query_id) REFERENCES queries(query_id)
                );
                CREATE TABLE IF NOT EXISTS partial_solution_candidates (
                    query_id TEXT NOT NULL,
                    witness_hash TEXT NOT NULL,
                    solution_hash TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    created REAL NOT NULL,
                    PRIMARY KEY(
                        query_id,
                        witness_hash,
                        solution_hash,
                        candidate_hash
                    ),
                    FOREIGN KEY(query_id) REFERENCES queries(query_id),
                    FOREIGN KEY(solution_hash)
                        REFERENCES partial_solutions(solution_hash)
                );
                CREATE TABLE IF NOT EXISTS partial_solution_clauses (
                    solution_hash TEXT NOT NULL,
                    clause_hash TEXT NOT NULL,
                    PRIMARY KEY(solution_hash, clause_hash),
                    FOREIGN KEY(solution_hash)
                        REFERENCES partial_solutions(solution_hash)
                );
                CREATE TABLE IF NOT EXISTS partial_solution_literals (
                    solution_hash TEXT NOT NULL,
                    literal_hash TEXT NOT NULL,
                    PRIMARY KEY(solution_hash, literal_hash),
                    FOREIGN KEY(solution_hash)
                        REFERENCES partial_solutions(solution_hash)
                );
                CREATE INDEX IF NOT EXISTS query_queue
                    ON queries(status, priority DESC, created, query_id);
                CREATE INDEX IF NOT EXISTS query_clause_lookup
                    ON query_clauses(clause_hash, query_id);
                CREATE INDEX IF NOT EXISTS query_literal_lookup
                    ON query_literals(literal_hash, query_id);
                CREATE TABLE IF NOT EXISTS query_shapes (
                    query_id TEXT PRIMARY KEY,
                    shape_hash TEXT NOT NULL,
                    context_hash TEXT NOT NULL,
                    read_count INTEGER NOT NULL,
                    node_count INTEGER NOT NULL,
                    duplicate_rank INTEGER NOT NULL,
                    representative_query_id TEXT NOT NULL,
                    created REAL NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE INDEX IF NOT EXISTS query_shape_class
                    ON query_shapes(
                        context_hash, shape_hash, duplicate_rank, query_id
                    );
                CREATE INDEX IF NOT EXISTS partial_solution_recent
                    ON partial_solutions(created DESC, solution_hash);
                CREATE INDEX IF NOT EXISTS partial_solution_candidate_query
                    ON partial_solution_candidates(query_id, created DESC);
                CREATE INDEX IF NOT EXISTS partial_solution_clause_lookup
                    ON partial_solution_clauses(clause_hash, solution_hash);
                CREATE INDEX IF NOT EXISTS partial_solution_literal_lookup
                    ON partial_solution_literals(
                        literal_hash, solution_hash);
                CREATE TABLE IF NOT EXISTS joint_schedule_validations (
                    validation_hash TEXT PRIMARY KEY,
                    schedule_trace_digest TEXT NOT NULL,
                    query_id TEXT NOT NULL,
                    target_branch INTEGER NOT NULL,
                    joint_status TEXT NOT NULL,
                    path_status TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE INDEX IF NOT EXISTS joint_schedule_trace_lookup
                    ON joint_schedule_validations(
                        schedule_trace_digest, query_id, created
                    );
                CREATE INDEX IF NOT EXISTS joint_schedule_query_lookup
                    ON joint_schedule_validations(query_id, created);
                CREATE TABLE IF NOT EXISTS joint_smt_solves (
                    result_hash TEXT PRIMARY KEY,
                    schedule_trace_digest TEXT NOT NULL,
                    query_id TEXT NOT NULL,
                    query_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    query_ir_model_verified INTEGER NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES queries(query_id)
                );
                CREATE INDEX IF NOT EXISTS joint_smt_query_lookup
                    ON joint_smt_solves(query_id, created);
                """
            )
            ulfm_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(ulfm_recovery_snapshots)"
                )
            }
            if "state_ordinal" not in ulfm_columns:
                db.execute(
                    "ALTER TABLE ulfm_recovery_snapshots "
                    "ADD COLUMN state_ordinal INTEGER NOT NULL DEFAULT 0"
                )
            columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(queries)")
            }
            for name in (
                "prefix_hash",
                "prefix_smt2_hash",
                "target_smt2_hash",
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE queries ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            partial_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(partial_solutions)")
            }
            for name, declaration in (
                ("provenance", "TEXT NOT NULL DEFAULT 'solver-model'"),
                ("proof_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("source_verified", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if name not in partial_columns:
                    db.execute(
                        f"ALTER TABLE partial_solutions ADD COLUMN {name} {declaration}"
                    )
            publication_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(result_publications)")
            }
            for name, declaration in (
                ("created", "REAL NOT NULL DEFAULT 0"),
                ("next_retry", "REAL NOT NULL DEFAULT 0"),
                ("dead_letter", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in publication_columns:
                    db.execute(
                        "ALTER TABLE result_publications "
                        f"ADD COLUMN {name} {declaration}"
                    )
            db.execute(
                "UPDATE result_publications SET created = updated WHERE created = 0"
            )
            db.execute(
                "INSERT OR IGNORE INTO prefix_nodes"
                "(id, parent_id, clause_hash, depth) VALUES(1, NULL, NULL, 0)"
            )
            db.execute(
                "INSERT OR IGNORE INTO result_publications("
                "query_id, published, attempts, last_error, created, updated, "
                "next_retry, dead_letter) "
                "SELECT query_id, 0, 0, '', completed, completed, 0, 0 FROM results"
            )

    @staticmethod
    def _literal_hash(
        root: str,
        expressions: Mapping[str, Mapping[str, Any]],
        truth: bool,
    ) -> str:
        normalized_root = str(root)
        polarity = bool(truth)
        visited: set[str] = set()
        while normalized_root not in visited:
            visited.add(normalized_root)
            node = expressions.get(normalized_root)
            if node is None or node.get("op") != "lnot":
                break
            children = node.get("children", ())
            if not isinstance(children, list) or len(children) != 1:
                break
            normalized_root = str(children[0])
            polarity = not polarity
        return _digest(
            _canonical_json(
                {
                    "schema": "symcc-query-literal-v1",
                    "root": normalized_root,
                    "polarity": polarity,
                }
            )
        )

    @staticmethod
    def _validate_envelope(
        envelope: Mapping[str, Any],
    ) -> _ValidatedEnvelope:
        if envelope.get("schema") != ENVELOPE_SCHEMA:
            raise ValueError(f"schema must be {ENVELOPE_SCHEMA}")
        nodes_raw = envelope.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise ValueError("nodes must be a non-empty list")
        if len(nodes_raw) > 250000:
            raise ValueError("query contains too many expression nodes")

        nodes: list[dict[str, Any]] = []
        seen: set[int] = set()
        for position, raw in enumerate(nodes_raw):
            if not isinstance(raw, dict):
                raise ValueError("each expression node must be an object")
            local_id = _bounded_int(raw.get("id"), "node id", 0, len(nodes_raw) - 1)
            if local_id in seen or local_id != position:
                raise ValueError("node ids must be unique and topologically ordered")
            seen.add(local_id)
            op = raw.get("op")
            if not isinstance(op, str) or op.lower() not in _OPS:
                raise ValueError(f"unsupported expression op {op!r}")
            bits = _bounded_int(raw.get("bits"), "node bits", 1, 1 << 20)
            children_raw = raw.get("children", [])
            if not isinstance(children_raw, list) or len(children_raw) > 3:
                raise ValueError("node children must be a list of at most three ids")
            children = [
                _bounded_int(child, "child id", 0, local_id - 1)
                for child in children_raw
            ]
            attrs_raw = raw.get("attrs", {})
            if not isinstance(attrs_raw, dict):
                raise ValueError("node attrs must be an object")
            attrs = _normalize_json(attrs_raw)
            if op.lower() == "constant":
                attrs["value_hex"] = _validate_hex(
                    attrs.get("value_hex"), "constant value", (bits + 7) // 8
                )
            elif op.lower() == "read":
                attrs["index"] = _bounded_int(
                    attrs.get("index"), "read index", 0, (1 << 32) - 1
                )
            elif op.lower() == "extract":
                attrs["index"] = _bounded_int(
                    attrs.get("index"), "extract index", 0, (1 << 20) - 1
                )
            elif op.lower() == "bool" and not isinstance(attrs.get("value"), bool):
                raise ValueError("bool node requires a Boolean value")
            nodes.append(
                {
                    "id": local_id,
                    "op": op.lower(),
                    "bits": bits,
                    "children": children,
                    "attrs": attrs,
                }
            )

        prefix_raw = envelope.get("prefix_roots", [])
        if not isinstance(prefix_raw, list) or len(prefix_raw) > 100000:
            raise ValueError("prefix_roots must be a bounded list")
        prefix = [
            _bounded_int(root, "prefix root", 0, len(nodes) - 1) for root in prefix_raw
        ]
        target = _bounded_int(
            envelope.get("target_root"), "target_root", 0, len(nodes) - 1
        )

        def validate_smt2(value: Any, name: str) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if "\x00" in value:
                raise ValueError(f"{name} must not contain NUL bytes")
            if len(value.encode("utf-8")) > 128 * 1024 * 1024:
                raise ValueError(f"{name} exceeds 128 MiB")
            return value

        smt2 = validate_smt2(envelope.get("smt2"), "smt2")
        prefix_smt2 = validate_smt2(envelope.get("prefix_smt2"), "prefix_smt2")
        target_smt2 = validate_smt2(envelope.get("target_smt2"), "target_smt2")
        input_hex = _validate_hex(
            envelope.get("input_hex", ""), "input_hex", 128 * 1024 * 1024
        )
        metadata_raw = envelope.get("metadata", {})
        if not isinstance(metadata_raw, dict):
            raise ValueError("metadata must be an object")
        metadata = _normalize_json(metadata_raw)
        timeout_ms = _bounded_int(
            envelope.get("timeout_ms", 10000), "timeout_ms", 1, 3600000
        )
        priority_raw = envelope.get("priority", 0.0)
        if isinstance(priority_raw, bool) or not isinstance(priority_raw, (int, float)):
            raise ValueError("priority must be numeric")
        priority = float(priority_raw)
        if not (-1e12 <= priority <= 1e12):
            raise ValueError("priority is outside the supported range")
        return (
            nodes,
            prefix,
            target,
            smt2,
            prefix_smt2,
            target_smt2,
            input_hex,
            metadata,
            timeout_ms,
            priority,
        )

    def _store_artifact(self, kind: str, content: bytes, suffix: str) -> str:
        if kind not in self._artifact_stores or suffix != ".smt2":
            raise ValueError("unsupported QueryStore artifact layout")
        digest = _digest(content)
        stored_digest, path = self._artifact_stores[kind].put(content, digest)
        if stored_digest != digest:
            raise OSError(errno.EIO, "artifact store returned the wrong digest")
        relative = Path(path).relative_to(self.object_dir)
        with self._connect() as db:
            db.execute(
                "INSERT INTO artifacts(hash, kind, relative_path, size) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(hash) DO UPDATE SET "
                "kind = excluded.kind, relative_path = excluded.relative_path, "
                "size = excluded.size",
                (digest, kind, str(relative), len(content)),
            )
        return digest

    def ingest(self, envelope: Mapping[str, Any]) -> tuple[str, bool]:
        return self._ingest_validated(self._validate_envelope(envelope))

    def _ingest_validated(
        self,
        validated: _ValidatedEnvelope,
    ) -> tuple[str, bool]:
        with bounded_advisory_lock(
            str(self._artifact_audit_lock_path),
            timeout=_ARTIFACT_AUDIT_LOCK_TIMEOUT_SECONDS,
            description="QueryStore artifact publication/audit lock",
            shared=True,
        ):
            return self._ingest_validated_locked(validated)

    def _ingest_validated_locked(
        self,
        validated: _ValidatedEnvelope,
    ) -> tuple[str, bool]:
        (
            nodes,
            prefix,
            target,
            smt2,
            prefix_smt2,
            target_smt2,
            input_hex,
            metadata,
            timeout_ms,
            priority,
        ) = validated

        local_to_hash: list[str] = []
        expression_rows: list[tuple[str, str]] = []
        normalized_expressions: dict[str, dict[str, Any]] = {}
        for node in nodes:
            normalized = {
                "schema": NODE_SCHEMA,
                "op": node["op"],
                "bits": node["bits"],
                "children": [local_to_hash[index] for index in node["children"]],
                "attrs": node["attrs"],
            }
            body = _canonical_json(normalized)
            node_hash = _digest(body)
            local_to_hash.append(node_hash)
            expression_rows.append((node_hash, body.decode("ascii")))
            normalized_expressions[node_hash] = normalized

        prefix_hashes = [local_to_hash[root] for root in prefix]
        target_hash = local_to_hash[target]
        query_key = {
            "schema": QUERY_SCHEMA,
            "theory": "QF_BV",
            "prefix": prefix_hashes,
            "target": target_hash,
        }
        query_id = _digest(_canonical_json(query_key))
        prefix_key = _digest(
            _canonical_json(
                {
                    "schema": "symcc-prefix-v1",
                    "theory": "QF_BV",
                    "prefix": prefix_hashes,
                }
            )
        )
        smt2_bytes = smt2.encode("utf-8")
        prefix_smt2_bytes = prefix_smt2.encode("utf-8")
        target_smt2_bytes = target_smt2.encode("utf-8")
        incoming_artifact_hashes = (
            _digest(smt2_bytes),
            _digest(prefix_smt2_bytes),
            _digest(target_smt2_bytes),
        )
        with self._connect() as db:
            existing_artifacts = db.execute(
                "SELECT smt2_hash, prefix_smt2_hash, target_smt2_hash "
                "FROM queries WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        if existing_artifacts is not None:
            stored_artifact_hashes = (
                str(existing_artifacts["smt2_hash"]),
                str(existing_artifacts["prefix_smt2_hash"]),
                str(existing_artifacts["target_smt2_hash"]),
            )
            if stored_artifact_hashes != incoming_artifact_hashes:
                raise QueryAdmissionError(
                    "query identity is already bound to different SMT2 artifacts"
                )

        smt2_hash = self._store_artifact("smt2", smt2_bytes, ".smt2")
        prefix_smt2_hash = self._store_artifact(
            "smt2-prefix", prefix_smt2_bytes, ".smt2"
        )
        target_smt2_hash = self._store_artifact(
            "smt2-target", target_smt2_bytes, ".smt2"
        )
        witness_key = {
            "input_hex": input_hex,
            "source": metadata.get("source", ""),
            "output_dir": metadata.get("output_dir", ""),
        }
        witness_hash = _digest(_canonical_json(witness_key))
        shape_index = AlphaConstraintShapeIndex(nodes)
        target_shape = shape_index.shape([target])
        context_roots = list(prefix[-_QUERY_SHAPE_CONTEXT_ROOTS:]) + [target]
        joint_context_shape = shape_index.shape(context_roots)
        site = metadata.get("site")
        site = (
            int(site)
            if isinstance(site, int)
            and not isinstance(site, bool)
            and 0 < site <= (1 << 64) - 1
            else 0
        )
        desired = metadata.get("desired")
        shape_context = {
            "schema": "symcc-query-shape-context-v2",
            "desired": desired if isinstance(desired, bool) else None,
            # Normalize suffix and target together so the class preserves whether
            # the target aliases input bytes already referenced by its prefix.
            "joint_suffix_target_shape": joint_context_shape.shape_hash,
            "location": (
                {"site": site} if site else {"branch": metadata.get("branch")}
            ),
        }
        shape_context_hash = _digest(_canonical_json(shape_context))
        now = time.time()

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT OR IGNORE INTO expressions(hash, body_json) VALUES(?, ?)",
                expression_rows,
            )
            row = db.execute(
                "SELECT id FROM prefix_nodes WHERE id = 1 "
                "AND parent_id IS NULL AND clause_hash IS NULL"
            ).fetchone()
            assert row is not None
            parent_id = int(row["id"])
            depth = 0
            for clause_hash in prefix_hashes:
                depth += 1
                child = db.execute(
                    "SELECT id FROM prefix_nodes "
                    "WHERE parent_id = ? AND clause_hash = ?",
                    (parent_id, clause_hash),
                ).fetchone()
                if child is None:
                    cursor = db.execute(
                        "INSERT INTO prefix_nodes"
                        "(parent_id, clause_hash, depth) VALUES(?, ?, ?)",
                        (parent_id, clause_hash, depth),
                    )
                    parent_id = int(cursor.lastrowid)
                else:
                    parent_id = int(child["id"])

            existing = db.execute(
                "SELECT status FROM queries WHERE query_id = ?", (query_id,)
            ).fetchone()
            created = existing is None
            if created:
                db.execute(
                    "INSERT INTO queries("
                    "query_id, prefix_id, prefix_hash, target_hash, smt2_hash, "
                    "prefix_smt2_hash, target_smt2_hash, status, "
                    "priority, timeout_ms, metadata_json, created, updated"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (
                        query_id,
                        parent_id,
                        prefix_key,
                        target_hash,
                        smt2_hash,
                        prefix_smt2_hash,
                        target_smt2_hash,
                        priority,
                        timeout_ms,
                        _canonical_json(metadata).decode("ascii"),
                        now,
                        now,
                    ),
                )
            else:
                db.execute(
                    "UPDATE queries SET priority = MAX(priority, ?), "
                    "timeout_ms = MAX(timeout_ms, ?), updated = ? "
                    "WHERE query_id = ?",
                    (priority, timeout_ms, now, query_id),
                )
            shape_row = db.execute(
                "SELECT shape_hash, context_hash, read_count, node_count "
                "FROM query_shapes WHERE query_id = ?",
                (query_id,),
            ).fetchone()
            if shape_row is None:
                representative = db.execute(
                    "SELECT query_id, duplicate_rank FROM query_shapes "
                    "WHERE context_hash = ? AND shape_hash = ? "
                    "ORDER BY duplicate_rank, query_id LIMIT 1",
                    (shape_context_hash, target_shape.shape_hash),
                ).fetchone()
                duplicate_rank = (
                    int(
                        db.execute(
                            "SELECT COUNT(*) FROM query_shapes "
                            "WHERE context_hash = ? AND shape_hash = ?",
                            (shape_context_hash, target_shape.shape_hash),
                        ).fetchone()[0]
                    )
                    + 1
                )
                representative_query_id = (
                    str(representative["query_id"])
                    if representative is not None
                    else query_id
                )
                db.execute(
                    "INSERT INTO query_shapes("
                    "query_id, shape_hash, context_hash, read_count, node_count, "
                    "duplicate_rank, representative_query_id, created"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        query_id,
                        target_shape.shape_hash,
                        shape_context_hash,
                        target_shape.read_count,
                        target_shape.node_count,
                        duplicate_rank,
                        representative_query_id,
                        now,
                    ),
                )
            elif (
                str(shape_row["shape_hash"]) != target_shape.shape_hash
                or int(shape_row["read_count"]) != target_shape.read_count
                or int(shape_row["node_count"]) != target_shape.node_count
            ):
                raise QueryAdmissionError(
                    "query identity is already bound to a different constraint shape"
                )
            db.execute(
                "INSERT OR IGNORE INTO witnesses("
                "query_id, witness_hash, input_hex, metadata_json"
                ") VALUES(?, ?, ?, ?)",
                (
                    query_id,
                    witness_hash,
                    input_hex,
                    _canonical_json(metadata).decode("ascii"),
                ),
            )
            clauses = sorted(set([*prefix_hashes, target_hash]))
            db.executemany(
                "INSERT OR IGNORE INTO query_clauses(query_id, clause_hash) "
                "VALUES(?, ?)",
                ((query_id, clause_hash) for clause_hash in clauses),
            )
            literal_hashes = sorted(
                {
                    self._literal_hash(root, normalized_expressions, True)
                    for root in [*prefix_hashes, target_hash]
                }
            )
            db.executemany(
                "INSERT OR IGNORE INTO query_literals("
                "query_id, literal_hash) VALUES(?, ?)",
                ((query_id, literal_hash) for literal_hash in literal_hashes),
            )
            reusable_unsat = db.execute(
                "SELECT u.query_id FROM unsat_sets u "
                "WHERE u.clause_count <= ? AND NOT EXISTS ("
                "SELECT 1 FROM query_clauses source "
                "WHERE source.query_id = u.query_id AND NOT EXISTS ("
                "SELECT 1 FROM query_clauses candidate "
                "WHERE candidate.query_id = ? "
                "AND candidate.clause_hash = source.clause_hash)) "
                "ORDER BY u.clause_count, u.query_id LIMIT 1",
                (len(clauses), query_id),
            ).fetchone()
            if reusable_unsat is not None and (
                existing is None or str(existing["status"]) != "done"
            ):
                reused_from = str(reusable_unsat["query_id"])
                cached = self._validate_result(
                    {
                        "status": "unsat",
                        "assignments": {},
                        "solver": "query-store-unsat-subset",
                        "elapsed_us": 0,
                        "reused_from": reused_from,
                    }
                )
                db.execute(
                    "UPDATE queries SET status = 'done', updated = ? "
                    "WHERE query_id = ?",
                    (now, query_id),
                )
                db.execute(
                    "INSERT OR REPLACE INTO results("
                    "query_id, result_json, completed) VALUES(?, ?, ?)",
                    (
                        query_id,
                        _canonical_json(cached).decode("ascii"),
                        now,
                    ),
                )
            result = db.execute(
                "SELECT result_json FROM results WHERE query_id = ?", (query_id,)
            ).fetchone()
            db.commit()

        query_body = {
            **query_key,
            "query_id": query_id,
            "smt2_hash": smt2_hash,
            "prefix_hash": prefix_key,
            "prefix_smt2_hash": prefix_smt2_hash,
            "target_smt2_hash": target_smt2_hash,
            "converter_chain": derive_converter_chains(nodes, [target, *prefix]),
        }
        query_path = self.query_dir / query_id[:2] / f"{query_id}.json"
        encoded_query_body = _canonical_json(query_body) + b"\n"
        query_body_matches = False
        try:
            query_snapshot = stable_regular_file_snapshot(
                str(query_path),
                max_bytes=_MAX_QUERY_BODY_BYTES,
                retain_content=True,
            )
            query_body_matches = query_snapshot.content == encoded_query_body
        except (OSError, ValueError):
            pass
        if not query_body_matches:
            _atomic_write(query_path, encoded_query_body)
        if result is not None:
            cached_result = json.loads(str(result["result_json"]))
            result_path = self.result_dir / query_id[:2] / f"{query_id}.json"
            _atomic_write_exact(
                result_path,
                _canonical_json(cached_result) + b"\n",
            )
            self.materialize_candidates(query_id, cached_result)
        else:
            self.materialize_partial_candidates(query_id)
        return query_id, created

    def _verified_query_body(
        self,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        query_id = str(row["query_id"])
        query_path = self.query_dir / query_id[:2] / f"{query_id}.json"
        try:
            snapshot = stable_regular_file_snapshot(
                str(query_path),
                max_bytes=_MAX_QUERY_BODY_BYTES,
                retain_content=True,
            )
            if snapshot.content is None:
                raise ValueError("query body content was not retained")
            body = json.loads(
                snapshot.content.decode("ascii"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonfinite_json,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                f"query {query_id} body failed stable validation"
            ) from error
        expected_fields = {
            "converter_chain",
            "prefix",
            "prefix_hash",
            "prefix_smt2_hash",
            "query_id",
            "schema",
            "smt2_hash",
            "target",
            "target_smt2_hash",
            "theory",
        }
        if not isinstance(body, dict) or set(body) != expected_fields:
            raise ValueError(f"query {query_id} body has an invalid field set")
        if snapshot.content != _canonical_json(body) + b"\n":
            raise ValueError(f"query {query_id} body is not canonically encoded")
        prefix = body["prefix"]
        target = body["target"]
        if (
            body["schema"] != QUERY_SCHEMA
            or body["theory"] != "QF_BV"
            or body["query_id"] != query_id
            or not isinstance(prefix, list)
            or any(_hex_digest(item) != item for item in prefix)
            or _hex_digest(target) != target
            or not isinstance(body["converter_chain"], list)
        ):
            raise ValueError(f"query {query_id} body has invalid typed fields")
        query_key = {
            "schema": QUERY_SCHEMA,
            "theory": "QF_BV",
            "prefix": prefix,
            "target": target,
        }
        prefix_key = _digest(
            _canonical_json(
                {
                    "schema": "symcc-prefix-v1",
                    "theory": "QF_BV",
                    "prefix": prefix,
                }
            )
        )
        if (
            _digest(_canonical_json(query_key)) != query_id
            or prefix_key != str(row["prefix_hash"])
            or target != str(row["target_hash"])
            or body["smt2_hash"] != str(row["smt2_hash"])
            or body["prefix_smt2_hash"] != str(row["prefix_smt2_hash"])
            or body["target_smt2_hash"] != str(row["target_smt2_hash"])
        ):
            raise ValueError(f"query {query_id} body disagrees with SQLite")
        return body

    def ingest_file(self, path: str | os.PathLike[str]) -> tuple[str, bool]:
        source = Path(path)
        try:
            envelope = _load_query_envelope(source)
            validated = self._validate_envelope(envelope)
        except ValueError as error:
            raise QueryAdmissionError(str(error)) from error
        return self._ingest_validated(validated)

    def _artifact_store_and_path(
        self,
        db: sqlite3.Connection,
        digest: str,
    ) -> tuple[ContentAddressedInputStore, Path]:
        if _hex_digest(digest) != digest:
            raise ValueError("artifact digest must be canonical SHA-256")
        row = db.execute(
            "SELECT kind, relative_path FROM artifacts WHERE hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact {digest}")
        kind = str(row["kind"])
        store = self._artifact_stores.get(kind)
        if store is None:
            raise ValueError(f"unknown stored artifact kind {kind!r}")
        expected = Path(store.object_path(digest))
        relative = Path(str(row["relative_path"]))
        if relative.is_absolute() or self.object_dir / relative != expected:
            raise ValueError(f"stored artifact {digest} has a non-canonical path")
        return store, expected

    def _verified_artifact_path(
        self,
        db: sqlite3.Connection,
        digest: str,
    ) -> Path:
        store, expected = self._artifact_store_and_path(db, digest)
        try:
            verified = Path(store.materialize(digest, None))
        except (OSError, ValueError) as error:
            raise ValueError(
                f"stored artifact {digest} failed integrity verification"
            ) from error
        if verified != expected:
            raise ValueError(f"stored artifact {digest} path identity mismatch")
        return verified

    def _sealed_verified_artifact(
        self,
        db: sqlite3.Connection,
        digest: str,
        role: str,
    ) -> tuple[Path, int]:
        store, expected = self._artifact_store_and_path(db, digest)
        try:
            snapshot = store.snapshot(digest, retain_content=True)
        except (OSError, ValueError) as error:
            raise ValueError(
                f"stored artifact {digest} failed integrity verification"
            ) from error
        if snapshot.sha256 != digest or snapshot.content is None:
            raise ValueError(f"stored artifact {digest} failed integrity verification")
        return expected, _sealed_memfd(
            snapshot.content,
            role=role,
            digest=digest,
        )

    def artifact_path(self, digest: str) -> Path:
        with self._connect() as db:
            return self._verified_artifact_path(db, digest)

    def audit_artifacts(
        self,
        *,
        max_entries: int = _DEFAULT_ARTIFACT_AUDIT_MAX_ENTRIES,
    ) -> dict[str, Any]:
        """Report SQLite-to-CAS reachability without mutating either namespace."""
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or not 1 <= max_entries <= _MAX_ARTIFACT_AUDIT_ENTRIES
        ):
            raise ValueError(
                "artifact audit max_entries must be an integer in [1, 10000000]"
            )

        with bounded_advisory_lock(
            str(self._artifact_audit_lock_path),
            timeout=_ARTIFACT_AUDIT_LOCK_TIMEOUT_SECONDS,
            description="QueryStore artifact publication/audit lock",
        ):
            with self._connect() as db:
                db.execute("BEGIN")
                referenced = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT smt2_hash FROM queries "
                        "UNION SELECT prefix_smt2_hash FROM queries "
                        "UNION SELECT target_smt2_hash FROM queries"
                    )
                }
                artifact_rows = [
                    {
                        "object_id": str(row["hash"]),
                        "kind": str(row["kind"]),
                        "relative_path": str(row["relative_path"]),
                        "size": (
                            int(row["size"])
                            if isinstance(row["size"], int)
                            and not isinstance(row["size"], bool)
                            else None
                        ),
                    }
                    for row in db.execute(
                        "SELECT hash, kind, relative_path, size "
                        "FROM artifacts ORDER BY hash"
                    )
                ]

                primary_by_digest: dict[str, tuple[str, str]] = {}
                invalid_rows: list[dict[str, Any]] = []
                rows_by_digest: dict[str, dict[str, Any]] = {}
                for row in artifact_rows:
                    digest = str(row["object_id"])
                    kind = str(row["kind"])
                    rows_by_digest[digest] = row
                    store = self._artifact_stores.get(kind)
                    canonical = False
                    if (
                        _hex_digest(digest) == digest
                        and store is not None
                        and isinstance(row["size"], int)
                        and not isinstance(row["size"], bool)
                        and 0 <= row["size"] <= _MAX_ARTIFACT_BYTES
                    ):
                        relative = Path(str(row["relative_path"]))
                        expected = Path(store.object_path(digest))
                        canonical = (
                            not relative.is_absolute()
                            and self.object_dir / relative == expected
                        )
                    if canonical:
                        primary_by_digest[digest] = (kind, digest)
                    else:
                        invalid_rows.append(dict(row))

                inventories: dict[str, dict[str, Any]] = {}
                physical: dict[tuple[str, str], dict[str, Any]] = {}
                scanned_entries = 0
                noncanonical_entries = 0
                scan_complete = True
                for kind in _ARTIFACT_KINDS:
                    remaining = max_entries - scanned_entries
                    if remaining < 1:
                        inventories[kind] = {
                            "canonical_objects": 0,
                            "complete": False,
                            "noncanonical_entries": 0,
                            "scanned": False,
                            "scanned_entries": 0,
                        }
                        scan_complete = False
                        continue
                    inventory = self._artifact_stores[kind].scan_objects(
                        max_entries=remaining
                    )
                    scanned_entries += inventory.scanned_entries
                    noncanonical_entries += inventory.noncanonical_entries
                    inventories[kind] = {
                        "canonical_objects": len(inventory.objects),
                        "complete": inventory.complete,
                        "noncanonical_entries": inventory.noncanonical_entries,
                        "scanned": True,
                        "scanned_entries": inventory.scanned_entries,
                    }
                    scan_complete = scan_complete and inventory.complete
                    for observation in inventory.objects:
                        physical[(kind, observation.object_id)] = {
                            "kind": kind,
                            "object_id": observation.object_id,
                            "size": observation.identity.size,
                        }
                db.commit()

        row_digests = set(rows_by_digest)
        primary_keys = set(primary_by_digest.values())
        physical_keys = set(physical)
        primary_size_mismatches = sorted(
            key
            for key in primary_keys & physical_keys
            if physical[key]["size"] != rows_by_digest[key[1]]["size"]
        )
        primary_size_mismatch_keys = set(primary_size_mismatches)
        unreferenced_rows = [
            rows_by_digest[digest] for digest in sorted(row_digests - referenced)
        ]
        referenced_invalid_rows = [
            row for row in invalid_rows if str(row["object_id"]) in referenced
        ]
        dangling_references = sorted(referenced - row_digests)
        observed_unindexed = sorted(physical_keys - primary_keys)
        observed_orphans = sorted(
            key for key in physical_keys if key[1] not in referenced
        )
        reachable_duplicates = sorted(
            key
            for key in physical_keys
            if key[1] in referenced and primary_by_digest.get(key[1]) != key
        )
        reachable_primary = {
            primary_by_digest[digest]
            for digest in referenced
            if digest in primary_by_digest
            and primary_by_digest[digest] in physical_keys
            and primary_by_digest[digest] not in primary_size_mismatch_keys
        }
        missing_primary = (
            sorted(
                primary_by_digest[digest]
                for digest in referenced
                if digest in primary_by_digest
                and primary_by_digest[digest] not in physical_keys
            )
            if scan_complete
            else None
        )

        def physical_records(keys: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
            return [physical[key] for key in keys]

        def size_mismatch_records(
            keys: Sequence[tuple[str, str]],
        ) -> list[dict[str, Any]]:
            return [
                {
                    **physical[key],
                    "expected_size": rows_by_digest[key[1]]["size"],
                }
                for key in keys
            ]

        return {
            "schema": "symcc-query-artifact-reachability-audit-v1",
            "mode": "report-only",
            "safe_to_sweep": False,
            "content_digests_verified": False,
            "limits": {
                "max_entries": max_entries,
                "maximum_configurable_entries": _MAX_ARTIFACT_AUDIT_ENTRIES,
            },
            "scan": {
                "complete": scan_complete,
                "inventories": inventories,
                "noncanonical_entries": noncanonical_entries,
                "scanned_entries": scanned_entries,
            },
            "database": {
                "artifact_rows": len(artifact_rows),
                "dangling_reference_count": len(dangling_references),
                "invalid_artifact_rows": len(invalid_rows),
                "query_reference_count": len(referenced),
                "reachable_artifact_rows": len(row_digests & referenced),
                "referenced_invalid_artifact_rows": len(referenced_invalid_rows),
                "unreferenced_artifact_rows": len(unreferenced_rows),
            },
            "physical": {
                "canonical_object_bytes_observed": sum(
                    int(record["size"]) for record in physical.values()
                ),
                "canonical_objects_observed": len(physical),
                "missing_primary_count": (
                    len(missing_primary) if missing_primary is not None else None
                ),
                "orphan_objects_observed": len(observed_orphans),
                "primary_size_mismatch_count": len(primary_size_mismatches),
                "reachable_duplicate_objects_observed": len(reachable_duplicates),
                "reachable_primary_objects_observed": len(reachable_primary),
                "unindexed_objects_observed": len(observed_unindexed),
            },
            "candidates": {
                "dangling_references": dangling_references,
                "invalid_artifact_rows": invalid_rows,
                "missing_primary": (
                    [
                        {"kind": kind, "object_id": digest}
                        for kind, digest in missing_primary
                    ]
                    if missing_primary is not None
                    else None
                ),
                "orphan_physical": physical_records(observed_orphans),
                "primary_size_mismatches": size_mismatch_records(
                    primary_size_mismatches
                ),
                "reachable_duplicates": physical_records(reachable_duplicates),
                "unindexed_physical": physical_records(observed_unindexed),
                "unreferenced_artifact_rows": unreferenced_rows,
            },
            "verdict": {
                "namespace_reference_closure": (
                    not dangling_references
                    and not referenced_invalid_rows
                    and not primary_size_mismatches
                    and not missing_primary
                    if missing_primary is not None
                    else None
                ),
                "physical_absence_claims_authorized": scan_complete,
                "sweep_authorized": False,
            },
        }

    def claim(
        self,
        owner: str,
        lease_seconds: float = 60.0,
        *,
        traversal: str = "dfs",
        shard_index: int = 0,
        shard_count: int = 1,
    ) -> WorkLease | None:
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a non-empty bounded string")
        try:
            encoded_owner = owner.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("owner must be valid UTF-8 text") from error
        if len(encoded_owner) > 256 or b"\0" in encoded_owner:
            raise ValueError("owner must be a non-empty bounded string")
        if isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be finite")
        try:
            lease_duration = float(lease_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("lease_seconds must be finite") from error
        if not math.isfinite(lease_duration):
            raise ValueError("lease_seconds must be finite")
        if lease_duration > _MAX_QUERY_LEASE_SECONDS:
            raise ValueError(
                f"lease_seconds must not exceed {_MAX_QUERY_LEASE_SECONDS}"
            )
        lease_duration = max(0.001, lease_duration)
        now = time.time()
        lease_until = now + lease_duration
        if not math.isfinite(lease_until):
            raise ValueError("lease deadline must be finite")
        if traversal not in {"dfs", "bfs", "priority", "structural"}:
            raise ValueError("traversal must be dfs, bfs, priority, or structural")
        shard_count = max(1, int(shard_count))
        shard_index = int(shard_index)
        if shard_index < 0 or shard_index >= shard_count:
            raise ValueError("shard_index must be within shard_count")
        depth_order = {
            "dfs": "p.depth DESC,",
            "bfs": "p.depth ASC,",
            "priority": "",
            "structural": "",
        }[traversal]
        shape_selection = os.environ.get("SYMCC_QUERY_SHAPE_SELECTION", "1") != "0"
        shape_join = (
            "LEFT JOIN query_shapes s ON s.query_id = q.query_id "
            if shape_selection
            else ""
        )
        shape_score = (
            "(CASE WHEN s.duplicate_rank IS NULL THEN 1.0 "
            "ELSE 1.0 / s.duplicate_rank + "
            "(1.0 - 1.0 / s.duplicate_rank) * "
            "MIN(1.0, MAX(0.0, (? - q.created) / 300.0)) END)"
            if shape_selection
            else ""
        )
        if traversal == "structural" and shape_selection:
            order = f"{shape_score} DESC, q.priority DESC, p.depth DESC, "
        else:
            shape_order = f"{shape_score} DESC, " if shape_selection else ""
            order = f"{depth_order} q.priority DESC, {shape_order}"
        sealed_descriptors: dict[str, int] = {}
        with self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT q.query_id, q.smt2_hash, q.prefix_hash, q.target_hash, "
                    "q.prefix_smt2_hash, q.target_smt2_hash, q.timeout_ms, "
                    "q.lease_token FROM queries q "
                    "JOIN prefix_nodes p ON p.id = q.prefix_id "
                    f"{shape_join}"
                    "WHERE (q.status = 'pending' "
                    "OR (q.status = 'leased' AND q.lease_until <= ?)) "
                    "AND (q.prefix_id % ?) = ? "
                    f"ORDER BY {order}q.created, "
                    "q.query_id LIMIT 1",
                    (
                        (now, shard_count, shard_index, now)
                        if shape_selection
                        else (now, shard_count, shard_index)
                    ),
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                self._verified_query_body(row)
                smt2_path, sealed_descriptors["full"] = self._sealed_verified_artifact(
                    db,
                    str(row["smt2_hash"]),
                    "full",
                )
                prefix_smt2_path, sealed_descriptors["prefix"] = (
                    self._sealed_verified_artifact(
                        db,
                        str(row["prefix_smt2_hash"]),
                        "prefix",
                    )
                )
                target_smt2_path, sealed_descriptors["target"] = (
                    self._sealed_verified_artifact(
                        db,
                        str(row["target_smt2_hash"]),
                        "target",
                    )
                )
                token = int(row["lease_token"]) + 1
                updated = db.execute(
                    "UPDATE queries SET status = 'leased', lease_owner = ?, "
                    "lease_until = ?, lease_token = ?, attempts = attempts + 1, "
                    "updated = ? WHERE query_id = ? AND lease_token = ?",
                    (
                        owner,
                        lease_until,
                        token,
                        now,
                        str(row["query_id"]),
                        int(row["lease_token"]),
                    ),
                )
                if updated.rowcount != 1:
                    db.rollback()
                    for descriptor in sealed_descriptors.values():
                        os.close(descriptor)
                    sealed_descriptors.clear()
                    return None
                witness = db.execute(
                    "SELECT input_hex FROM witnesses WHERE query_id = ? "
                    "ORDER BY witness_hash LIMIT 1",
                    (str(row["query_id"]),),
                ).fetchone()
                db.commit()
            except BaseException:
                for descriptor in sealed_descriptors.values():
                    os.close(descriptor)
                raise
        bundle = _SealedArtifactBundle(sealed_descriptors)
        return WorkLease(
            query_id=str(row["query_id"]),
            token=token,
            smt2_path=smt2_path,
            prefix_key=str(row["prefix_hash"]),
            prefix_smt2_path=prefix_smt2_path,
            target_smt2_path=target_smt2_path,
            timeout_ms=int(row["timeout_ms"]),
            input_hex=str(witness["input_hex"]) if witness is not None else "",
            _sealed_artifacts=bundle,
        )

    def query_lease_is_active(self, lease_id: str) -> bool:
        """Report whether an exact ``query_id:token`` lease still fences work."""
        if not isinstance(lease_id, str) or ":" not in lease_id:
            raise ValueError("invalid query lease identity")
        query_id, separator, raw_token = lease_id.rpartition(":")
        if not separator or _hex_digest(query_id) != query_id:
            raise ValueError("invalid query lease identity")
        try:
            token = int(raw_token, 10)
        except ValueError as error:
            raise ValueError("invalid query lease identity") from error
        if token < 1 or str(token) != raw_token:
            raise ValueError("invalid query lease identity")
        with self._connect() as db:
            row = db.execute(
                "SELECT status, lease_token, lease_until FROM queries "
                "WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["status"] == "leased"
            and int(row["lease_token"]) == token
            and row["lease_until"] is not None
            and float(row["lease_until"]) > time.time()
        )

    def renew(
        self,
        lease: WorkLease,
        owner: str,
        lease_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        """Extend an unexpired query lease without changing its fence token."""
        try:
            duration = float(lease_seconds)
            timestamp = time.time() if now is None else float(now)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("query lease renewal requires finite numbers") from error
        if (
            not math.isfinite(duration)
            or not math.isfinite(timestamp)
            or not 0.1 <= duration <= 86_400.0
        ):
            raise ValueError("query lease renewal duration must be in [0.1, 86400]")
        lease_until = timestamp + duration
        if not math.isfinite(lease_until):
            raise ValueError("query lease renewal deadline is not finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute(
                "UPDATE queries SET lease_until=?,updated=? WHERE query_id=? "
                "AND status='leased' AND lease_owner=? AND lease_token=? "
                "AND lease_until>?",
                (
                    lease_until,
                    timestamp,
                    lease.query_id,
                    str(owner),
                    int(lease.token),
                    timestamp,
                ),
            ).rowcount == 1

    @staticmethod
    def _validate_result(result: Mapping[str, Any]) -> dict[str, Any]:
        status = result.get("status")
        if status not in _RESULT_STATUSES:
            raise ValueError(f"invalid solver status {status!r}")
        assignments = _normalize_assignments(
            result.get("assignments", {}), "assignments"
        )
        conflicts_raw = result.get("solver_pscache_conflict_solutions", ())
        if (
            not isinstance(conflicts_raw, Sequence)
            or isinstance(conflicts_raw, (str, bytes))
            or len(conflicts_raw) > 64
        ):
            raise ValueError("solver conflict solutions must be a bounded list")
        conflicts: list[dict[str, Any]] = []
        for raw_conflict in conflicts_raw:
            if (
                not isinstance(raw_conflict, Mapping)
                or raw_conflict.get("schema") != CONFLICT_SOLUTION_SCHEMA
            ):
                raise ValueError("invalid solver conflict solution schema")
            conflict_assignments = _normalize_assignments(
                raw_conflict.get("assignments"), "conflict assignments"
            )
            core_assignments = _normalize_assignments(
                raw_conflict.get("core_assignments"), "conflict core assignments"
            )
            if not conflict_assignments or not core_assignments:
                raise ValueError("conflict solutions require assignments and a core")
            if any(
                conflict_assignments.get(index) != value
                for index, value in core_assignments.items()
            ):
                raise ValueError("conflict core must be a subset of assignments")
            if (
                raw_conflict.get("proof") != "z3-assumption-unsat-core-v1"
                or raw_conflict.get("proof_verified") is not True
            ):
                raise ValueError("conflict solution requires a verified Z3 core")
            source = str(raw_conflict.get("source", ""))
            if source not in {"concrete-witness", "cached-assignment"}:
                raise ValueError("invalid conflict assignment source")
            core_minimal = raw_conflict.get("core_minimal", False)
            if not isinstance(core_minimal, bool):
                raise ValueError("conflict core_minimal must be Boolean")
            proof_checks = _bounded_int(
                raw_conflict.get("proof_checks", 0), "conflict proof_checks", 1, 4097
            )
            proof_payload = {
                "schema": CONFLICT_SOLUTION_SCHEMA,
                "assignments": conflict_assignments,
                "core_assignments": core_assignments,
                "source": source,
                "proof": "z3-assumption-unsat-core-v1",
                "proof_verified": True,
                "core_minimal": core_minimal,
                "proof_checks": proof_checks,
            }
            proof_payload["certificate_sha256"] = _digest(
                _canonical_json(proof_payload)
            )
            conflicts.append(proof_payload)
        normalized = {
            "schema": RESULT_SCHEMA,
            "status": status,
            "assignments": assignments,
            "solver": str(result.get("solver", "unknown"))[:128],
            "elapsed_us": _bounded_int(
                result.get("elapsed_us", 0),
                "elapsed_us",
                0,
                (1 << 63) - 1,
            ),
            "prefix_cache_hit": bool(result.get("prefix_cache_hit", False)),
            "prefix_cache_entries": _bounded_int(
                result.get("prefix_cache_entries", 0),
                "prefix_cache_entries",
                0,
                65536,
            ),
            "solver_pscache_hit": bool(result.get("solver_pscache_hit", False)),
            "solver_pscache_probes": _bounded_int(
                result.get("solver_pscache_probes", 0),
                "solver_pscache_probes",
                0,
                65536,
            ),
            "solver_pscache_conflict_checks": _bounded_int(
                result.get("solver_pscache_conflict_checks", 0),
                "solver_pscache_conflict_checks",
                0,
                65536,
            ),
            "solver_pscache_conflict_solutions": conflicts,
            "selective_query_attempted": bool(
                result.get("selective_query_attempted", False)
            ),
            "selective_query_hit": bool(result.get("selective_query_hit", False)),
            "selective_query_eligible": bool(
                result.get("selective_query_eligible", False)
            ),
            "selective_query_symbolic": _bounded_int(
                result.get("selective_query_symbolic", 0),
                "selective_query_symbolic",
                0,
                65536,
            ),
            "selective_query_fixed": _bounded_int(
                result.get("selective_query_fixed", 0),
                "selective_query_fixed",
                0,
                65536,
            ),
            "selective_query_elapsed_us": _bounded_int(
                result.get("selective_query_elapsed_us", 0),
                "selective_query_elapsed_us",
                0,
                (1 << 63) - 1,
            ),
            "selective_query_completions": _bounded_int(
                result.get("selective_query_completions", 0),
                "selective_query_completions",
                0,
                8,
            ),
            "selective_query_hit_completion": _bounded_int(
                result.get("selective_query_hit_completion", 0),
                "selective_query_hit_completion",
                0,
                8,
            ),
            "selective_query_timeout_ms": _bounded_int(
                result.get("selective_query_timeout_ms", 0),
                "selective_query_timeout_ms",
                0,
                3600000,
            ),
            "selective_query_policy_observations": _bounded_int(
                result.get("selective_query_policy_observations", 0),
                "selective_query_policy_observations",
                0,
                (1 << 63) - 1,
            ),
            "selective_query_full_ewma_us": _bounded_int(
                result.get("selective_query_full_ewma_us", 0),
                "selective_query_full_ewma_us",
                0,
                (1 << 63) - 1,
            ),
            "selective_query_predicted_savings_us": _bounded_int(
                result.get("selective_query_predicted_savings_us", 0),
                "selective_query_predicted_savings_us",
                -(1 << 63),
                (1 << 63) - 1,
            ),
            "selective_query_policy_context": str(
                result.get("selective_query_policy_context", "")
            )[:128],
            "selective_query_policy_decision": str(
                result.get("selective_query_policy_decision", "ineligible")
            )[:32],
            "selective_query_partition_mode": str(
                result.get("selective_query_partition_mode", "none")
            )[:32],
            "selective_query_relation_edges": _bounded_int(
                result.get("selective_query_relation_edges", 0),
                "selective_query_relation_edges",
                0,
                8_388_608,
            ),
            "selective_query_cut_weight": _bounded_int(
                result.get("selective_query_cut_weight", 0),
                "selective_query_cut_weight",
                0,
                8_388_608,
            ),
            "selective_query_smt_assertions": _bounded_int(
                result.get("selective_query_smt_assertions", 0),
                "selective_query_smt_assertions",
                0,
                65_536,
            ),
            "selective_query_random_assertions": _bounded_int(
                result.get("selective_query_random_assertions", 0),
                "selective_query_random_assertions",
                0,
                65_536,
            ),
            "selective_query_shared_variables": _bounded_int(
                result.get("selective_query_shared_variables", 0),
                "selective_query_shared_variables",
                0,
                65_536,
            ),
            "selective_query_partial_elapsed_us": _bounded_int(
                result.get("selective_query_partial_elapsed_us", 0),
                "selective_query_partial_elapsed_us",
                0,
                (1 << 63) - 1,
            ),
            "selective_query_partial_status": str(
                result.get("selective_query_partial_status", "not-run")
            )[:16],
        }
        if normalized["selective_query_partition_mode"] not in {
            "none",
            "relation-graph-v1",
        }:
            raise ValueError("invalid selective query partition mode")
        if normalized["selective_query_partial_status"] not in {
            "not-run",
            "sat",
            "unsat",
            "unknown",
        }:
            raise ValueError("invalid selective query partial status")
        graph_metrics = (
            normalized["selective_query_relation_edges"],
            normalized["selective_query_cut_weight"],
            normalized["selective_query_smt_assertions"],
            normalized["selective_query_random_assertions"],
            normalized["selective_query_shared_variables"],
            normalized["selective_query_partial_elapsed_us"],
        )
        if normalized["selective_query_partition_mode"] == "none":
            if any(graph_metrics) or (
                normalized["selective_query_partial_status"] != "not-run"
            ):
                raise ValueError("unpartitioned selective telemetry is inconsistent")
        else:
            if (
                normalized["selective_query_smt_assertions"] == 0
                or normalized["selective_query_random_assertions"] == 0
            ):
                raise ValueError("partitioned selective telemetry is incomplete")
            if (
                normalized["selective_query_partial_status"] != "not-run"
                and not normalized["selective_query_attempted"]
            ):
                raise ValueError("partial solve requires a selective attempt")
            if normalized["solver"] == "z3-selective-graph" and (
                not normalized["selective_query_hit"]
                or normalized["selective_query_partial_status"] != "sat"
            ):
                raise ValueError("graph solver result lacks verified hit evidence")
            if (
                normalized["selective_query_hit"]
                and normalized["solver"] != "z3-selective-graph"
            ):
                raise ValueError("graph hit is not attributed to the graph solver")
        backend_kind = str(result.get("backend_kind", ""))[:64]
        if backend_kind:
            normalized["backend_kind"] = backend_kind
            capability_status = str(result.get("capability_status", "supported"))[:32]
            if capability_status not in {"supported", "unsupported"}:
                raise ValueError("invalid backend capability status")
            normalized["capability_status"] = capability_status
            normalized["backend_model_verified"] = (
                result.get("backend_model_verified") is True
            )
            normalized["backend_unsat_authorized"] = (
                result.get("backend_unsat_authorized") is True
            )
            unsat_confirmation = str(result.get("backend_unsat_confirmation", ""))[:64]
            if unsat_confirmation:
                if unsat_confirmation != "status-only-rerun-v1":
                    raise ValueError("invalid backend UNSAT confirmation")
                normalized["backend_unsat_confirmation"] = unsat_confirmation
            context_protocol = str(result.get("backend_context_protocol", ""))[:64]
            if context_protocol:
                if context_protocol not in {
                    "smtlib-prefix-process-push-pop-v1",
                    QFBV_SHARED_CONTEXT_PROTOCOL,
                }:
                    raise ValueError("invalid backend context protocol")
                normalized["backend_context_protocol"] = context_protocol
            backend_status = result.get("backend_status")
            if backend_status is not None:
                backend_status = str(backend_status)[:32]
                if backend_status not in _RESULT_STATUSES:
                    raise ValueError("invalid raw backend status")
                normalized["backend_status"] = backend_status

            capabilities_raw = result.get("backend_capabilities")
            if not isinstance(capabilities_raw, Mapping):
                raise ValueError("typed solver backend requires a capability contract")
            capabilities = _normalize_json(dict(capabilities_raw))
            if capabilities.get("schema") != QFBV_CAPABILITY_SCHEMA:
                raise ValueError("invalid QF_BV capability schema")
            capability_digest = str(capabilities.get("capability_sha256", ""))
            capability_body = dict(capabilities)
            capability_body.pop("capability_sha256", None)
            if capability_digest != _digest(_canonical_json(capability_body)):
                raise ValueError("QF_BV capability digest mismatch")
            try:
                expected_capabilities = normalize_qfbv_capabilities(capabilities)
            except ValueError as error:
                raise ValueError("invalid QF_BV capability contract") from error
            if capabilities != expected_capabilities:
                legacy_capabilities = dict(expected_capabilities)
                legacy_capabilities.pop("capability_sha256", None)
                legacy_capabilities.pop("incremental", None)
                legacy_capabilities["capability_sha256"] = _digest(
                    _canonical_json(legacy_capabilities)
                )
                if capabilities != legacy_capabilities:
                    raise ValueError("non-canonical QF_BV capability contract")
            if (
                "backend_context_protocol" in normalized
                and capabilities.get("incremental") is not True
            ):
                raise ValueError("context protocol requires incremental capability")
            normalized["backend_capabilities"] = capabilities

            if backend_kind == "bitblast-cadical-qfbv":
                if capabilities.get("incremental") is not True:
                    raise ValueError(
                        "bit-blasted CaDiCaL backend requires incremental capability"
                    )
                certificate_raw = result.get("bitblast_certificate")
                if not isinstance(certificate_raw, Mapping):
                    raise ValueError("bit-blasted backend requires a CNF certificate")
                certificate = _normalize_json(dict(certificate_raw))
                certificate_digest = str(certificate.get("certificate_sha256", ""))
                certificate_body = dict(certificate)
                certificate_body.pop("certificate_sha256", None)
                if (
                    certificate.get("schema") != QFBV_BITBLAST_SCHEMA
                    or certificate_digest != _digest(_canonical_json(certificate_body))
                    or certificate.get("activation_guarded") is not True
                    or certificate.get("bit_order") != "least-significant-first"
                    or certificate.get("cnf_protocol")
                    != "deterministic-tseitin-ripple-restoring-v1"
                ):
                    raise ValueError("invalid QF_BV bit-blast certificate")
                for digest_name in (
                    "formula_sha256",
                    "assumption_sha256",
                    "cnf_sha256",
                    "input_map_sha256",
                    "certificate_sha256",
                ):
                    if _hex_digest(certificate.get(digest_name)) != str(
                        certificate.get(digest_name, "")
                    ):
                        raise ValueError(f"invalid bit-blast {digest_name}")
                node_count = _bounded_int(
                    certificate.get("node_count"),
                    "bit-blast node count",
                    1,
                    int(capabilities["max_nodes"]),
                )
                _bounded_int(
                    certificate.get("root_count"),
                    "bit-blast root count",
                    1,
                    node_count,
                )
                _bounded_int(
                    certificate.get("variable_count"),
                    "bit-blast variable count",
                    1,
                    20_000_000,
                )
                _bounded_int(
                    certificate.get("clause_count"),
                    "bit-blast clause count",
                    1,
                    100_000_000,
                )
                _bounded_int(
                    certificate.get("input_offset_count"),
                    "bit-blast input count",
                    0,
                    int(capabilities["max_input_bytes"]),
                )
                _bounded_int(
                    certificate.get("maximum_width"),
                    "bit-blast maximum width",
                    1,
                    int(capabilities["max_bits"]),
                )
                operator_counts = certificate.get("operator_counts")
                if not isinstance(operator_counts, Mapping):
                    raise ValueError("bit-blast operator counts must be an object")
                checked_counts = {
                    str(operator): _bounded_int(
                        count, "bit-blast operator count", 1, node_count
                    )
                    for operator, count in operator_counts.items()
                }
                if (
                    set(checked_counts) - set(capabilities["operators"])
                    or sum(checked_counts.values()) != node_count
                ):
                    raise ValueError(
                        "bit-blast operator counts do not cover the Query IR"
                    )
                normalized["bitblast_certificate"] = certificate

                partition_protocol = str(
                    result.get("backend_partition_execution_protocol", "")
                )
                if partition_protocol:
                    if partition_protocol != (
                        "symcc-qfbv-proof-aware-execution-v1"
                    ):
                        raise ValueError("invalid partition execution protocol")
                    execution_sha256 = _required_hex_digest(
                        result.get("backend_partition_execution_sha256"),
                        "partition execution",
                    )
                    partition_sha256 = _required_hex_digest(
                        result.get("backend_partition_sha256"),
                        "partition certificate",
                    )
                    cube_count = _bounded_int(
                        result.get("backend_partition_cube_count"),
                        "partition cube count",
                        2,
                        4096,
                    )
                    completed_cubes = _bounded_int(
                        result.get("backend_partition_completed_cubes"),
                        "completed partition cubes",
                        0,
                        cube_count,
                    )
                    execution_result = str(
                        result.get("backend_partition_execution_result", "")
                    )
                    expected_status = {
                        "sat": "sat",
                        "unsat": "unsat",
                        "incomplete": "unknown",
                    }.get(execution_result)
                    if expected_status != status:
                        raise ValueError(
                            "partition execution state disagrees with solver status"
                        )
                    partition_fields: dict[str, Any] = {
                        "backend_partition_execution_protocol": partition_protocol,
                        "backend_partition_execution_sha256": execution_sha256,
                        "backend_partition_sha256": partition_sha256,
                        "backend_partition_cube_count": cube_count,
                        "backend_partition_completed_cubes": completed_cubes,
                        "backend_partition_execution_result": execution_result,
                    }
                    if execution_result == "sat":
                        if completed_cubes < 1:
                            raise ValueError("partition SAT lacks a completed cube")
                        partition_fields[
                            "backend_partition_winner_cube_sha256"
                        ] = _required_hex_digest(
                            result.get("backend_partition_winner_cube_sha256"),
                            "partition SAT cube",
                        )
                        if result.get(
                            "backend_partition_aggregate_proof_sha256"
                        ) is not None:
                            raise ValueError("partition SAT carries an UNSAT aggregate")
                    elif execution_result == "unsat":
                        aggregate = _required_hex_digest(
                            result.get("backend_partition_aggregate_proof_sha256"),
                            "partition aggregate proof",
                        )
                        if completed_cubes != cube_count:
                            raise ValueError(
                                "partition UNSAT does not cover every cube"
                            )
                        if aggregate != result.get(
                            "backend_incremental_proof_record_sha256"
                        ):
                            raise ValueError(
                                "partition aggregate and final proof disagree"
                            )
                        partition_fields[
                            "backend_partition_aggregate_proof_sha256"
                        ] = aggregate
                        if result.get(
                            "backend_partition_winner_cube_sha256"
                        ) is not None:
                            raise ValueError("partition UNSAT carries a SAT winner")
                    normalized.update(partition_fields)

                online_keys = {
                    str(key)
                    for key in result
                    if str(key).startswith("backend_online_cubing_")
                }
                if result.get("backend_online_cubing_protocol") is not None:
                    try:
                        online_fields = normalize_online_cubing_result(
                            result,
                            certificate=certificate,
                            status=str(status),
                            formula_family_sha256=formula_family_sha256(
                                certificate
                            ),
                        )
                    except (OnlineCubingError, UtilityPairingError) as error:
                        raise ValueError(
                            f"invalid online-cubing result: {error}"
                        ) from error
                    normalized.update(online_fields)
                elif online_keys:
                    raise ValueError(
                        "online-cubing fields lack their protocol identity"
                    )

                protocol = str(result.get("backend_incremental_proof_protocol", ""))
                policy = str(result.get("backend_incremental_proof_policy_sha256", ""))
                if protocol != QFBV_INCREMENTAL_PROOF_PROTOCOL:
                    raise ValueError("invalid incremental proof policy")
                policy = _required_hex_digest(policy, "incremental proof policy")
                candidates = _bounded_int(
                    result.get("backend_incremental_import_candidates", 0),
                    "incremental import candidates",
                    0,
                    4096,
                )
                imported = _bounded_int(
                    result.get("backend_incremental_imported_clauses", 0),
                    "incremental imported clauses",
                    0,
                    4096,
                )
                if imported > candidates:
                    raise ValueError("incremental import counts are inconsistent")
                import_ids_raw = result.get(
                    "backend_incremental_import_record_sha256", ()
                )
                if (
                    not isinstance(import_ids_raw, list)
                    or len(import_ids_raw) != imported
                ):
                    raise ValueError("incremental import identities are inconsistent")
                import_ids = [
                    _required_hex_digest(value, "incremental import identity")
                    for value in import_ids_raw
                ]
                if len(set(import_ids)) != len(import_ids):
                    raise ValueError("incremental import identities are duplicated")
                normalized.update(
                    {
                        "backend_incremental_proof_protocol": protocol,
                        "backend_incremental_proof_policy_sha256": policy,
                        "backend_incremental_import_candidates": candidates,
                        "backend_incremental_imported_clauses": imported,
                        "backend_incremental_import_checker_elapsed_us": _bounded_int(
                            result.get(
                                "backend_incremental_import_checker_elapsed_us", 0
                            ),
                            "incremental import checker elapsed_us",
                            0,
                            (1 << 63) - 1,
                        ),
                        "backend_incremental_import_record_sha256": import_ids,
                    }
                )
                native_protocol = str(result.get("backend_native_context_protocol", ""))
                if native_protocol:
                    if native_protocol not in {
                        "cadical-ipasir-assumptions-v1",
                        "cadical-ipasir-up-realtime-v1",
                    }:
                        raise ValueError("invalid native CaDiCaL context protocol")
                    native_result = _bounded_int(
                        result.get("backend_native_result"),
                        "native CaDiCaL result",
                        0,
                        20,
                    )
                    if (
                        native_result not in {0, 10, 20}
                        or (status == "sat" and native_result != 10)
                        or (status == "unsat" and native_result != 20)
                    ):
                        raise ValueError("native CaDiCaL result/status mismatch")
                    signature_raw = result.get("backend_native_signature", "")
                    signature = str(signature_raw)[:128]
                    if not isinstance(signature_raw, str) or signature_raw != signature:
                        raise ValueError(
                            "native CaDiCaL signature must not be truncated"
                        )
                    if (
                        re.fullmatch(r"cadical-3\.0\.[0-9]+(?:[^\s]*)?", signature)
                        is None
                    ):
                        raise ValueError("invalid native CaDiCaL 3.0 signature")
                    if not isinstance(
                        result.get("backend_native_context_cache_hit"), bool
                    ):
                        raise ValueError(
                            "native context cache-hit evidence must be Boolean"
                        )
                    normalized.update(
                        {
                            "backend_native_context_protocol": native_protocol,
                            "backend_native_context_cache_hit": bool(
                                result["backend_native_context_cache_hit"]
                            ),
                            "backend_native_context_solve_count": _bounded_int(
                                result.get("backend_native_context_solve_count"),
                                "native context solve count",
                                1,
                                (1 << 63) - 1,
                            ),
                            "backend_native_context_entries": _bounded_int(
                                result.get("backend_native_context_entries"),
                                "native context entry count",
                                1,
                                64,
                            ),
                            "backend_native_signature": signature,
                            "backend_native_result": native_result,
                        }
                    )
                    if native_protocol == "cadical-ipasir-up-realtime-v1":
                        normalized.update(
                            _normalize_realtime_stream_result(
                                result, certificate, policy, status
                            )
                        )
                    elif result.get("backend_realtime_stream_protocol") is not None:
                        raise ValueError(
                            "realtime evidence requires the IPASIR-UP context"
                        )
                receipt_raw = result.get("backend_incremental_result_receipt")
                if status == "unsat":
                    if (
                        not isinstance(receipt_raw, Mapping)
                        or receipt_raw.get("schema") != QFBV_INCREMENTAL_RESULT_SCHEMA
                        or receipt_raw.get("protocol") != protocol
                        or receipt_raw.get("status") != "unsat"
                        or result.get("backend_incremental_proof_verified") is not True
                        or not normalized["backend_unsat_authorized"]
                    ):
                        raise ValueError(
                            "bit-blasted UNSAT lacks incremental proof authorization"
                        )
                    receipt = _normalize_json(dict(receipt_raw))
                    receipt_digest = str(receipt.get("receipt_sha256", ""))
                    receipt_body = dict(receipt)
                    receipt_body.pop("receipt_sha256", None)
                    if receipt_digest != _digest(_canonical_json(receipt_body)):
                        raise ValueError("incremental result receipt digest mismatch")
                    failed_raw = receipt.get("failed_assumptions")
                    if (
                        not isinstance(failed_raw, list)
                        or not failed_raw
                        or len(failed_raw) > int(certificate["root_count"])
                    ):
                        raise ValueError("invalid failed-assumption receipt")
                    failed = [
                        _bounded_int(
                            literal,
                            "failed assumption",
                            1,
                            int(certificate["variable_count"]),
                        )
                        for literal in failed_raw
                    ]
                    if failed != sorted(set(failed)):
                        raise ValueError("failed assumptions are not canonical")
                    if (
                        receipt.get("formula_sha256") != certificate["formula_sha256"]
                        or receipt.get("assumption_sha256")
                        != certificate["assumption_sha256"]
                    ):
                        raise ValueError(
                            "incremental receipt scope differs from CNF certificate"
                        )
                    record_digest = _hex_digest(
                        result.get("backend_incremental_proof_record_sha256")
                    )
                    if receipt.get("clause_receipt_sha256") != record_digest:
                        raise ValueError(
                            "incremental result and clause receipt disagree"
                        )
                    normalized.update(
                        {
                            "backend_incremental_proof_verified": True,
                            "backend_incremental_proof_created": bool(
                                result.get("backend_incremental_proof_created", False)
                            ),
                            "backend_incremental_proof_record_sha256": record_digest,
                            "backend_incremental_proof_steps": _bounded_int(
                                result.get("backend_incremental_proof_steps"),
                                "incremental proof steps",
                                1,
                                1_000_000,
                            ),
                            "backend_incremental_proof_propagations": _bounded_int(
                                result.get("backend_incremental_proof_propagations"),
                                "incremental proof propagations",
                                0,
                                100_000_000,
                            ),
                            "backend_incremental_proof_checker_elapsed_us": _bounded_int(
                                result.get(
                                    "backend_incremental_proof_checker_elapsed_us"
                                ),
                                "incremental proof checker elapsed_us",
                                1,
                                (1 << 63) - 1,
                            ),
                            "backend_incremental_result_receipt": receipt,
                        }
                    )
                    wire_protocol = str(
                        result.get("backend_lidrup_wire_protocol", "")
                    )
                    if wire_protocol:
                        if (
                            wire_protocol != QFBV_LIDRUP_WIRE_PROTOCOL
                            or result.get("backend_lidrup_verified") is not True
                        ):
                            raise ValueError("invalid LIDRUP proof wire evidence")
                        normalized.update(
                            {
                                "backend_lidrup_wire_protocol": wire_protocol,
                                "backend_lidrup_checker_policy_sha256": (
                                    _required_hex_digest(
                                        result.get(
                                            "backend_lidrup_checker_policy_sha256"
                                        ),
                                        "LIDRUP checker policy",
                                    )
                                ),
                                "backend_lidrup_artifact_sha256": (
                                    _required_hex_digest(
                                        result.get("backend_lidrup_artifact_sha256"),
                                        "LIDRUP artifact",
                                    )
                                ),
                                "backend_lidrup_receipt_sha256": (
                                    _required_hex_digest(
                                        result.get("backend_lidrup_receipt_sha256"),
                                        "LIDRUP receipt",
                                    )
                                ),
                                "backend_lidrup_verified": True,
                                "backend_lidrup_created": bool(
                                    result.get("backend_lidrup_created", False)
                                ),
                                "backend_lidrup_learned_clauses": _bounded_int(
                                    result.get("backend_lidrup_learned_clauses"),
                                    "LIDRUP learned clause count",
                                    1,
                                    1_000_000,
                                ),
                                "backend_lidrup_checker_elapsed_us": _bounded_int(
                                    result.get("backend_lidrup_checker_elapsed_us"),
                                    "LIDRUP checker elapsed_us",
                                    0,
                                    (1 << 63) - 1,
                                ),
                            }
                        )
                    elif any(
                        name in result
                        for name in (
                            "backend_lidrup_checker_policy_sha256",
                            "backend_lidrup_artifact_sha256",
                            "backend_lidrup_receipt_sha256",
                            "backend_lidrup_verified",
                        )
                    ):
                        raise ValueError("partial LIDRUP proof wire evidence")
                elif receipt_raw is not None or any(
                    name in result
                    for name in (
                        "backend_incremental_proof_record_sha256",
                        "backend_incremental_proof_steps",
                        "backend_incremental_proof_propagations",
                        "backend_lidrup_wire_protocol",
                        "backend_lidrup_artifact_sha256",
                        "backend_lidrup_receipt_sha256",
                    )
                ):
                    raise ValueError(
                        "non-UNSAT result carries incremental proof evidence"
                    )

            substitution_core_protocol = str(
                result.get("backend_substitution_core_protocol", "")
            )
            if substitution_core_protocol:
                if substitution_core_protocol != QFBV_SUBSTITUTION_CORE_PROTOCOL:
                    raise ValueError("invalid QF_BV substitution-core protocol")
                substitution_core_policy = str(
                    result.get("backend_substitution_core_policy_sha256", "")
                )
                if _hex_digest(substitution_core_policy) != substitution_core_policy:
                    raise ValueError("invalid QF_BV substitution-core policy")
                boolean_fields = (
                    "backend_substitution_core_attempted",
                    "backend_substitution_core_hit",
                    "backend_substitution_core_proof_reused",
                    "backend_substitution_core_publish_attempted",
                    "backend_substitution_core_published",
                    "backend_substitution_core_publish_created",
                )
                for field_name in boolean_fields:
                    if not isinstance(result.get(field_name), bool):
                        raise ValueError(f"{field_name} must be Boolean")
                    normalized[field_name] = bool(result[field_name])
                attempted = normalized["backend_substitution_core_attempted"]
                hit = normalized["backend_substitution_core_hit"]
                publish_attempted = normalized[
                    "backend_substitution_core_publish_attempted"
                ]
                published = normalized["backend_substitution_core_published"]
                publish_created = normalized[
                    "backend_substitution_core_publish_created"
                ]
                proof_reused = normalized["backend_substitution_core_proof_reused"]
                metric_fields = (
                    ("backend_substitution_core_candidates", 0, 4096),
                    (
                        "backend_substitution_core_candidate_scan",
                        0,
                        1_000_000,
                    ),
                    (
                        "backend_substitution_core_checker_elapsed_us",
                        0,
                        (1 << 63) - 1,
                    ),
                    (
                        "backend_substitution_core_match_elapsed_us",
                        0,
                        (1 << 63) - 1,
                    ),
                    ("backend_substitution_core_clause_pairs", 0, 100_000_000),
                    ("backend_substitution_core_candidate_rows", 0, 100_000_000),
                    ("backend_substitution_core_join_states", 0, 100_000_000),
                )
                for field_name, lower, upper in metric_fields:
                    normalized[field_name] = _bounded_int(
                        result.get(field_name, 0), field_name, lower, upper
                    )
                normalized.update(
                    {
                        "backend_substitution_core_protocol": (
                            substitution_core_protocol
                        ),
                        "backend_substitution_core_policy_sha256": (
                            substitution_core_policy
                        ),
                    }
                )
                if not attempted and any(
                    normalized[field_name]
                    for field_name in (
                        "backend_substitution_core_candidates",
                        "backend_substitution_core_candidate_scan",
                        "backend_substitution_core_checker_elapsed_us",
                        "backend_substitution_core_match_elapsed_us",
                        "backend_substitution_core_clause_pairs",
                        "backend_substitution_core_candidate_rows",
                        "backend_substitution_core_join_states",
                    )
                ):
                    raise ValueError(
                        "unattempted substitution-core lookup carries metrics"
                    )
                if publish_attempted and status != "unsat":
                    raise ValueError(
                        "substitution-core publication requires an UNSAT result"
                    )
                if hit:
                    if (
                        not attempted
                        or status != "unsat"
                        or not normalized["backend_unsat_authorized"]
                        or normalized.get("backend_status") != "unsat"
                        or publish_attempted
                        or published
                        or publish_created
                    ):
                        raise ValueError(
                            "substitution-core hit authorization is inconsistent"
                        )
                    record_sha256 = _hex_digest(
                        result.get("backend_substitution_core_record_sha256")
                    )
                    source_query_id = _hex_digest(
                        result.get("backend_substitution_core_source_query_id")
                    )
                    mapping_raw = result.get("backend_substitution_core_mapping")
                    if not isinstance(mapping_raw, list) or len(mapping_raw) > 250_000:
                        raise ValueError(
                            "substitution-core mapping must be a bounded list"
                        )
                    mapping: list[list[int]] = []
                    sources: set[int] = set()
                    for pair in mapping_raw:
                        if not isinstance(pair, list) or len(pair) != 2:
                            raise ValueError("invalid substitution-core mapping pair")
                        source = _bounded_int(
                            pair[0], "substitution source offset", 0, (1 << 32) - 1
                        )
                        target = _bounded_int(
                            pair[1], "substitution target offset", 0, (1 << 32) - 1
                        )
                        if source in sources:
                            raise ValueError("duplicate substitution source offset")
                        sources.add(source)
                        mapping.append([source, target])
                    if mapping != sorted(mapping):
                        raise ValueError("substitution-core mapping is not canonical")
                    if (
                        normalized["backend_substitution_core_candidates"] < 1
                        or normalized["backend_substitution_core_candidate_scan"]
                        < normalized["backend_substitution_core_candidates"]
                        or (
                            not proof_reused
                            and normalized[
                                "backend_substitution_core_checker_elapsed_us"
                            ]
                            < 1
                        )
                        or normalized["backend_substitution_core_join_states"] < 1
                    ):
                        raise ValueError(
                            "substitution-core hit lacks verification metrics"
                        )
                    normalized.update(
                        {
                            "backend_substitution_core_record_sha256": record_sha256,
                            "backend_substitution_core_source_query_id": source_query_id,
                            "backend_substitution_core_mapping": mapping,
                        }
                    )
                elif any(
                    field_name in result
                    for field_name in (
                        "backend_substitution_core_record_sha256",
                        "backend_substitution_core_source_query_id",
                        "backend_substitution_core_mapping",
                    )
                ):
                    raise ValueError("substitution-core miss carries hit evidence")
                if proof_reused and not hit:
                    raise ValueError(
                        "substitution-core proof reuse requires a core hit"
                    )
                if published:
                    if (
                        not publish_attempted
                        or status != "unsat"
                        or not normalized["backend_unsat_authorized"]
                        or hit
                    ):
                        raise ValueError(
                            "substitution-core publication is inconsistent"
                        )
                    normalized.update(
                        {
                            "backend_substitution_core_published_record_sha256": (
                                _hex_digest(
                                    result.get(
                                        "backend_substitution_core_published_record_sha256"
                                    )
                                )
                            ),
                            "backend_substitution_core_published_clause_count": (
                                _bounded_int(
                                    result.get(
                                        "backend_substitution_core_published_clause_count"
                                    ),
                                    "published substitution-core clause count",
                                    1,
                                    64,
                                )
                            ),
                            "backend_substitution_core_extractor_elapsed_us": (
                                _bounded_int(
                                    result.get(
                                        "backend_substitution_core_extractor_elapsed_us"
                                    ),
                                    "substitution-core extractor elapsed_us",
                                    1,
                                    (1 << 63) - 1,
                                )
                            ),
                            "backend_substitution_core_publish_proof_elapsed_us": (
                                _bounded_int(
                                    result.get(
                                        "backend_substitution_core_publish_proof_elapsed_us"
                                    ),
                                    "substitution-core publication proof elapsed_us",
                                    1,
                                    (1 << 63) - 1,
                                )
                            ),
                        }
                    )
                elif publish_created:
                    raise ValueError(
                        "substitution-core creation requires successful publication"
                    )
                elif any(
                    field_name in result
                    for field_name in (
                        "backend_substitution_core_published_record_sha256",
                        "backend_substitution_core_published_clause_count",
                        "backend_substitution_core_extractor_elapsed_us",
                        "backend_substitution_core_publish_proof_elapsed_us",
                    )
                ):
                    raise ValueError(
                        "failed substitution-core publication carries success evidence"
                    )
                for source_name, target_name in (
                    (
                        "backend_substitution_core_reason",
                        "backend_substitution_core_reason",
                    ),
                    (
                        "backend_substitution_core_publish_reason",
                        "backend_substitution_core_publish_reason",
                    ),
                ):
                    reason = str(result.get(source_name, ""))[:512]
                    if reason:
                        normalized[target_name] = reason

            if context_protocol == QFBV_SHARED_CONTEXT_PROTOCOL:
                context_digest = str(result.get("backend_shared_context_sha256", ""))
                if _hex_digest(context_digest) != context_digest:
                    raise ValueError("invalid shared context digest")
                parent_digest = str(
                    result.get("backend_shared_parent_context_sha256", "")
                )
                if parent_digest and _hex_digest(parent_digest) != parent_digest:
                    raise ValueError("invalid shared parent context digest")
                for field_name in (
                    "backend_shared_context_exact_hit",
                    "backend_parent_context_reused",
                ):
                    if not isinstance(result.get(field_name), bool):
                        raise ValueError(f"{field_name} must be Boolean")
                    normalized[field_name] = bool(result[field_name])
                created = _bounded_int(
                    result.get("backend_shared_context_created"),
                    "shared context created count",
                    0,
                    4096,
                )
                existing = _bounded_int(
                    result.get("backend_shared_context_existing"),
                    "shared context existing count",
                    0,
                    4096,
                )
                depth = _bounded_int(
                    result.get("backend_shared_context_depth"),
                    "shared context depth",
                    1,
                    4096,
                )
                if created + existing != depth:
                    raise ValueError(
                        "shared context publication counts do not cover its depth"
                    )
                if (depth == 1) != (parent_digest == ""):
                    raise ValueError("shared context parent/depth mismatch")
                if bool(result["backend_shared_context_exact_hit"]) != (
                    existing > 0 and created == 0
                ):
                    raise ValueError(
                        "shared context exact-hit evidence is inconsistent"
                    )
                materialization = str(
                    result.get("backend_shared_context_materialization", "")
                )
                if materialization not in {
                    "unmaterialized",
                    "leased",
                    "quota-timeout",
                    "local-hit",
                }:
                    raise ValueError("invalid shared context materialization mode")
                if result["backend_parent_context_reused"] and (
                    not parent_digest or materialization != "leased"
                ):
                    raise ValueError("parent context reuse lacks a leased parent")
                normalized.update(
                    {
                        "backend_shared_context_sha256": context_digest,
                        "backend_shared_parent_context_sha256": parent_digest,
                        "backend_shared_context_created": created,
                        "backend_shared_context_existing": existing,
                        "backend_shared_context_depth": depth,
                        "backend_shared_context_materialization": materialization,
                    }
                )

            lemma_protocol = str(result.get("backend_lemma_protocol", ""))
            if lemma_protocol:
                if lemma_protocol != QFBV_LEMMA_PROTOCOL:
                    raise ValueError("invalid QF_BV lemma exchange protocol")
                lemma_policy = str(
                    result.get("backend_lemma_exchange_policy_sha256", "")
                )
                if _hex_digest(lemma_policy) != lemma_policy:
                    raise ValueError("invalid QF_BV lemma exchange policy")
                candidates = _bounded_int(
                    result.get("backend_lemma_candidates", 0),
                    "verified lemma candidates",
                    0,
                    64,
                )
                injected = _bounded_int(
                    result.get("backend_lemma_injected", 0),
                    "verified lemmas injected",
                    0,
                    64,
                )
                active = _bounded_int(
                    result.get("backend_lemma_active", 0),
                    "active verified lemmas",
                    0,
                    64,
                )
                rejected = _bounded_int(
                    result.get("backend_lemma_rejected", 0),
                    "rejected verified lemmas",
                    0,
                    256,
                )
                if injected > active or candidates < injected:
                    raise ValueError("verified lemma counts are inconsistent")
                records_raw = result.get("backend_verified_lemma_records", ())
                if not isinstance(records_raw, list) or len(records_raw) > 64:
                    raise ValueError("verified lemma records must be a bounded list")
                records: list[dict[str, Any]] = []
                record_ids: set[str] = set()
                lemma_ids: set[str] = set()
                for raw_record in records_raw:
                    if not isinstance(raw_record, Mapping):
                        raise ValueError("verified lemma record must be an object")
                    try:
                        record = normalize_lemma_record(raw_record)
                    except LemmaExchangeError as error:
                        raise ValueError("invalid verified lemma record") from error
                    if (
                        record["exchange_policy_sha256"] != lemma_policy
                        or record["source_context"]["capability_sha256"]
                        != capability_digest
                        or record["record_sha256"] in record_ids
                        or record["lemma_sha256"] in lemma_ids
                    ):
                        raise ValueError("verified lemma record identity mismatch")
                    record_ids.add(record["record_sha256"])
                    lemma_ids.add(record["lemma_sha256"])
                    records.append(record)
                if len(records) != active:
                    raise ValueError("active lemma count does not match records")
                normalized.update(
                    {
                        "backend_lemma_protocol": lemma_protocol,
                        "backend_lemma_exchange_policy_sha256": lemma_policy,
                        "backend_lemma_candidates": candidates,
                        "backend_lemma_injected": injected,
                        "backend_lemma_active": active,
                        "backend_lemma_rejected": rejected,
                        "backend_lemma_checker_elapsed_us": _bounded_int(
                            result.get("backend_lemma_checker_elapsed_us", 0),
                            "verified lemma checker elapsed_us",
                            0,
                            (1 << 63) - 1,
                        ),
                        "backend_verified_lemma_records": records,
                    }
                )
                reason = str(result.get("backend_lemma_reason", ""))[:512]
                if reason:
                    normalized["backend_lemma_reason"] = reason

                if "backend_lemma_publish_candidates" in result:
                    publish_candidates = _bounded_int(
                        result.get("backend_lemma_publish_candidates", 0),
                        "learned lemma publish candidates",
                        0,
                        64,
                    )
                    published = _bounded_int(
                        result.get("backend_lemma_published", 0),
                        "published learned lemmas",
                        0,
                        64,
                    )
                    publish_rejected = _bounded_int(
                        result.get("backend_lemma_publish_rejected", 0),
                        "rejected learned lemma publications",
                        0,
                        64,
                    )
                    proof_reuses = _bounded_int(
                        result.get("backend_lemma_proof_reuses", 0),
                        "learned lemma proof reuses",
                        0,
                        64,
                    )
                    published_raw = result.get(
                        "backend_published_lemma_record_sha256", ()
                    )
                    if not isinstance(published_raw, list):
                        raise ValueError("published lemma identities must be a list")
                    published_ids = [_hex_digest(value) for value in published_raw]
                    if (
                        len(published_ids) != published
                        or len(set(published_ids)) != len(published_ids)
                        or published + publish_rejected != publish_candidates
                        or proof_reuses > published
                        or status != "sat"
                    ):
                        raise ValueError("learned lemma publication counts mismatch")
                    normalized.update(
                        {
                            "backend_lemma_publish_candidates": publish_candidates,
                            "backend_lemma_published": published,
                            "backend_lemma_publish_rejected": publish_rejected,
                            "backend_lemma_proof_reuses": proof_reuses,
                            "backend_published_lemma_record_sha256": published_ids,
                        }
                    )
                    if "backend_lemma_extractor_elapsed_us" in result:
                        normalized["backend_lemma_extractor_elapsed_us"] = _bounded_int(
                            result["backend_lemma_extractor_elapsed_us"],
                            "learned lemma extractor elapsed_us",
                            1,
                            (1 << 63) - 1,
                        )
                    source_context = str(
                        result.get("backend_lemma_source_context_sha256", "")
                    )
                    if source_context:
                        normalized["backend_lemma_source_context_sha256"] = _hex_digest(
                            source_context
                        )
                    elif published:
                        raise ValueError("learned lemmas lack their source context")
                    publish_reason = str(
                        result.get("backend_lemma_publish_reason", "")
                    )[:512]
                    if publish_reason:
                        normalized["backend_lemma_publish_reason"] = publish_reason

            proof_receipt_raw = result.get("backend_unsat_proof_receipt")
            if proof_receipt_raw is not None:
                if not isinstance(proof_receipt_raw, Mapping):
                    raise ValueError("QF_BV UNSAT proof receipt must be an object")
                try:
                    proof_receipt = normalize_proof_receipt(proof_receipt_raw)
                except ProofVerificationError as error:
                    raise ValueError("invalid QF_BV UNSAT proof receipt") from error
                proof_protocol = str(result.get("backend_unsat_proof_protocol", ""))
                if (
                    proof_protocol != QFBV_PROOF_PROTOCOL
                    or proof_receipt["protocol"] != proof_protocol
                    or result.get("backend_unsat_proof_verified") is not True
                    or not isinstance(result.get("backend_unsat_proof_reused"), bool)
                    or status != "unsat"
                    or not normalized["backend_unsat_authorized"]
                ):
                    raise ValueError("inconsistent QF_BV UNSAT proof authorization")
                if proof_receipt["capability_sha256"] != capability_digest:
                    raise ValueError("proof receipt capability identity mismatch")
                normalized.update(
                    {
                        "backend_unsat_proof_protocol": proof_protocol,
                        "backend_unsat_proof_verified": True,
                        "backend_unsat_proof_reused": bool(
                            result["backend_unsat_proof_reused"]
                        ),
                        "backend_unsat_proof_generator_elapsed_us": _bounded_int(
                            result.get("backend_unsat_proof_generator_elapsed_us", 0),
                            "UNSAT proof generator elapsed_us",
                            0,
                            (1 << 63) - 1,
                        ),
                        "backend_unsat_proof_checker_elapsed_us": _bounded_int(
                            result.get("backend_unsat_proof_checker_elapsed_us", 0),
                            "UNSAT proof checker elapsed_us",
                            0,
                            (1 << 63) - 1,
                        ),
                        "backend_unsat_proof_receipt": proof_receipt,
                    }
                )

            lowering_raw = result.get("lowering_certificate")
            if lowering_raw is not None:
                if not isinstance(lowering_raw, Mapping):
                    raise ValueError("lowering certificate must be an object")
                lowering = _normalize_json(dict(lowering_raw))
                if (
                    lowering.get("schema") != QFBV_LOWERING_SCHEMA
                    or lowering.get("sort_verified") is not True
                ):
                    raise ValueError("invalid QF_BV lowering certificate")
                certificate_digest = str(lowering.get("certificate_sha256", ""))
                certificate_body = dict(lowering)
                certificate_body.pop("certificate_sha256", None)
                if certificate_digest != _digest(_canonical_json(certificate_body)):
                    raise ValueError("QF_BV lowering certificate mismatch")
                if lowering.get("capability_sha256") != capability_digest:
                    raise ValueError("lowering and capability certificates disagree")
                if (
                    lowering.get("logic") != "QF_BV"
                    or lowering.get("model_protocol")
                    != "smtlib-get-value-input-bytes-v1"
                ):
                    raise ValueError("invalid QF_BV lowering logic or model protocol")
                for digest_name in (
                    "input_offsets_sha256",
                    "smt2_sha256",
                    "certificate_sha256",
                ):
                    if _hex_digest(lowering.get(digest_name)) != str(
                        lowering.get(digest_name, "")
                    ):
                        raise ValueError(f"invalid lowering {digest_name}")
                node_count = _bounded_int(
                    lowering.get("node_count"),
                    "lowering node_count",
                    1,
                    int(capabilities["max_nodes"]),
                )
                _bounded_int(
                    lowering.get("root_count"),
                    "lowering root_count",
                    1,
                    node_count,
                )
                _bounded_int(
                    lowering.get("input_offset_count"),
                    "lowering input_offset_count",
                    0,
                    int(capabilities["max_input_bytes"]),
                )
                _bounded_int(
                    lowering.get("maximum_width"),
                    "lowering maximum_width",
                    1,
                    int(capabilities["max_bits"]),
                )
                operator_counts = lowering.get("operator_counts")
                if not isinstance(operator_counts, Mapping):
                    raise ValueError("lowering operator_counts must be an object")
                allowed_operators = set(capabilities["operators"])
                normalized_operator_counts: dict[str, int] = {}
                for operator, count in operator_counts.items():
                    if (
                        not isinstance(operator, str)
                        or operator not in allowed_operators
                    ):
                        raise ValueError("lowering used an unadvertised operator")
                    normalized_operator_counts[operator] = _bounded_int(
                        count,
                        "lowering operator count",
                        1,
                        node_count,
                    )
                if sum(normalized_operator_counts.values()) != node_count:
                    raise ValueError("lowering operator counts do not cover its nodes")
                normalized["lowering_certificate"] = lowering
                proof_receipt = normalized.get("backend_unsat_proof_receipt")
                if isinstance(proof_receipt, Mapping) and (
                    proof_receipt["lowering_certificate_sha256"]
                    != lowering["certificate_sha256"]
                ):
                    raise ValueError(
                        "proof receipt lowering certificate identity mismatch"
                    )
                if context_protocol == QFBV_SHARED_CONTEXT_PROTOCOL and (
                    int(lowering["root_count"])
                    != int(normalized["backend_shared_context_depth"]) + 1
                ):
                    raise ValueError(
                        "shared context depth disagrees with lowering roots"
                    )

            if backend_kind == "smtlib-qfbv":
                if (
                    normalized["capability_status"] == "supported"
                    and "lowering_certificate" not in normalized
                ):
                    raise ValueError(
                        "supported QF_BV result requires lowering evidence"
                    )
                if status == "sat" and not normalized["backend_model_verified"]:
                    raise ValueError("QF_BV SAT result requires model verification")
                if status == "unsat" and not normalized["backend_unsat_authorized"]:
                    raise ValueError("QF_BV UNSAT result is not capability-authorized")
            elif backend_kind == "bitblast-cadical-qfbv":
                if (
                    normalized["capability_status"] == "supported"
                    and "bitblast_certificate" not in normalized
                ):
                    raise ValueError(
                        "supported bit-blasted result requires CNF evidence"
                    )
                if status == "sat" and not normalized["backend_model_verified"]:
                    raise ValueError(
                        "bit-blasted SAT result requires model verification"
                    )
                if status == "unsat" and (
                    not normalized["backend_unsat_authorized"]
                    or normalized.get("backend_incremental_proof_verified") is not True
                ):
                    raise ValueError(
                        "bit-blasted UNSAT result lacks a checked LRUP receipt"
                    )
        partition_protocol = str(
            result.get("backend_partition_execution_protocol", "")
        )
        if (
            partition_protocol
            and "backend_partition_execution_protocol" not in normalized
        ):
            if (
                partition_protocol != "symcc-qfbv-proof-aware-execution-v1"
                or status != "unknown"
                or result.get("backend_partition_execution_result") != "incomplete"
                or result.get("backend_partition_winner_cube_sha256") is not None
                or result.get("backend_partition_aggregate_proof_sha256") is not None
            ):
                raise ValueError("invalid incomplete partition execution telemetry")
            cube_count = _bounded_int(
                result.get("backend_partition_cube_count"),
                "partition cube count",
                2,
                4096,
            )
            normalized.update(
                {
                    "backend_partition_execution_protocol": partition_protocol,
                    "backend_partition_execution_sha256": _required_hex_digest(
                        result.get("backend_partition_execution_sha256"),
                        "partition execution",
                    ),
                    "backend_partition_sha256": _required_hex_digest(
                        result.get("backend_partition_sha256"),
                        "partition certificate",
                    ),
                    "backend_partition_cube_count": cube_count,
                    "backend_partition_completed_cubes": _bounded_int(
                        result.get("backend_partition_completed_cubes"),
                        "completed partition cubes",
                        0,
                        cube_count,
                    ),
                    "backend_partition_execution_result": "incomplete",
                }
            )
        reason = result.get("reason")
        if reason is not None:
            normalized["reason"] = str(reason)[:4096]
        reused_from = result.get("reused_from")
        if reused_from is not None:
            normalized["reused_from"] = str(reused_from)[:128]
        generator = result.get("generator")
        if generator is not None:
            normalized_generator = normalize_generator(generator)
            normalized["generator"] = normalized_generator
            normalized["generator_hash"] = generator_hash(normalized_generator)
        portfolio = result.get("portfolio")
        if isinstance(portfolio, Mapping):
            attempts_raw = portfolio.get("attempts", ())
            if (
                not isinstance(attempts_raw, Sequence)
                or isinstance(attempts_raw, (str, bytes))
                or len(attempts_raw) > 64
            ):
                raise ValueError("portfolio attempts must be a bounded list")
            attempts: list[dict[str, Any]] = []
            for attempt in attempts_raw:
                if not isinstance(attempt, Mapping):
                    raise ValueError("portfolio attempts must be objects")
                attempt_status = str(attempt.get("status", "unknown"))
                if attempt_status not in _RESULT_STATUSES:
                    attempt_status = "unknown"
                attempts.append(
                    {
                        "name": str(attempt.get("name", "solver"))[:128],
                        "status": attempt_status,
                        "solver": str(attempt.get("solver", "unknown"))[:128],
                        "elapsed_us": _bounded_int(
                            attempt.get("elapsed_us", 0),
                            "portfolio elapsed_us",
                            0,
                            (1 << 63) - 1,
                        ),
                        "prefix_cache_hit": bool(
                            attempt.get("prefix_cache_hit", False)
                        ),
                        "backend_kind": str(attempt.get("backend_kind", ""))[:64],
                        "capability_status": str(
                            attempt.get("capability_status", "supported")
                        )[:32],
                        "backend_model_verified": bool(
                            attempt.get("backend_model_verified", False)
                        ),
                        "backend_unsat_authorized": bool(
                            attempt.get("backend_unsat_authorized", False)
                        ),
                        "backend_context_protocol": str(
                            attempt.get("backend_context_protocol", "")
                        )[:64],
                        "capability_sha256": str(attempt.get("capability_sha256", ""))[
                            :64
                        ],
                        "lowering_certificate_sha256": str(
                            attempt.get("lowering_certificate_sha256", "")
                        )[:64],
                        "cancelled": bool(attempt.get("cancelled", False)),
                        "cancel_reason": str(attempt.get("cancel_reason", ""))[:128],
                        "reason": str(attempt.get("reason", ""))[:512],
                    }
                )
            normalized["portfolio"] = {
                "schema": PORTFOLIO_SCHEMA,
                "winner": str(portfolio.get("winner", ""))[:128],
                "disagreement": bool(portfolio.get("disagreement", False)),
                "mode": str(portfolio.get("mode", "sequential"))[:32],
                "parallelism": _bounded_int(
                    portfolio.get("parallelism", 1),
                    "portfolio parallelism",
                    1,
                    64,
                ),
                "cancellation_enabled": bool(
                    portfolio.get("cancellation_enabled", False)
                ),
                "cancel_grace_ms": _bounded_int(
                    portfolio.get("cancel_grace_ms", 0),
                    "portfolio cancellation grace",
                    0,
                    60000,
                ),
                "cancel_requested": bool(portfolio.get("cancel_requested", False)),
                "cancelled_attempts": _bounded_int(
                    portfolio.get("cancelled_attempts", 0),
                    "portfolio cancelled attempts",
                    0,
                    64,
                ),
                "consensus_complete": bool(portfolio.get("consensus_complete", True)),
                "elapsed_us": _bounded_int(
                    portfolio.get("elapsed_us", 0),
                    "portfolio elapsed_us",
                    0,
                    (1 << 63) - 1,
                ),
                "attempts": attempts,
            }
        return normalized

    @staticmethod
    def _result_assignment_sets(
        normalized: Mapping[str, Any],
    ) -> list[tuple[int, dict[str, int]]]:
        primary = {
            str(index): int(value)
            for index, value in normalized.get("assignments", {}).items()
        }
        assignment_sets = [(0, primary)]
        generator = normalized.get("generator")
        if isinstance(generator, Mapping):
            for model_index, model in enumerate(
                generator.get("verified_models", ()), start=1
            ):
                if isinstance(model, Mapping):
                    assignment_sets.append(
                        (
                            model_index,
                            {str(index): int(value) for index, value in model.items()},
                        )
                    )
        return assignment_sets

    def _sat_result_has_verified_candidate(
        self,
        normalized: Mapping[str, Any],
        loaded_query_ir: tuple[list[str], dict[str, dict[str, Any]]],
        witnesses: Sequence[sqlite3.Row],
    ) -> bool:
        roots, expressions = loaded_query_ir
        decoded_witnesses: list[bytes] = []
        for witness in witnesses:
            try:
                decoded_witnesses.append(bytes.fromhex(str(witness["input_hex"])))
            except ValueError:
                continue
        remaining_nodes = [_MAX_SAT_VALIDATION_NODE_EVALUATIONS]
        candidate_attempts = 0

        def validate(candidate: bytes) -> bool:
            nonlocal candidate_attempts
            if (
                candidate_attempts >= _MAX_SAT_VALIDATION_CANDIDATES
                or remaining_nodes[0] <= 0
            ):
                return False
            candidate_attempts += 1
            return self._candidate_satisfies_query(
                roots,
                expressions,
                candidate,
                node_budget=remaining_nodes,
            )

        for _model_index, assignments in self._result_assignment_sets(normalized):
            for witness in decoded_witnesses:
                candidate = self._patched_candidate(witness.hex(), assignments)
                if candidate is not None and validate(candidate):
                    return True
                if (
                    candidate_attempts >= _MAX_SAT_VALIDATION_CANDIDATES
                    or remaining_nodes[0] <= 0
                ):
                    return False

        generator = normalized.get("generator")
        if not isinstance(generator, Mapping):
            return False
        for witness in decoded_witnesses:
            try:
                candidates, _metrics = sample_generator(
                    generator,
                    witness,
                    64,
                    validate,
                )
            except ValueError:
                continue
            if candidates:
                return True
            if (
                candidate_attempts >= _MAX_SAT_VALIDATION_CANDIDATES
                or remaining_nodes[0] <= 0
            ):
                return False
        return False

    def _store_partial_solutions(
        self,
        db: sqlite3.Connection,
        query_id: str,
        normalized: Mapping[str, Any],
        created: float,
    ) -> int:
        if not _env_flag("SYMCC_PARTIAL_SOLUTION_CACHE", True):
            return 0
        clauses = [
            str(row["clause_hash"])
            for row in db.execute(
                "SELECT clause_hash FROM query_clauses WHERE query_id = ? "
                "ORDER BY clause_hash",
                (query_id,),
            )
        ]
        literal_hashes = [
            str(row["literal_hash"])
            for row in db.execute(
                "SELECT literal_hash FROM query_literals "
                "WHERE query_id = ? ORDER BY literal_hash",
                (query_id,),
            )
        ]
        generator_hash_value = str(normalized.get("generator_hash", ""))
        rows: list[tuple[str, str, str, str, str, int, str, float, str, str, int]] = []
        row_clauses: dict[str, list[str]] = {}
        row_literals: dict[str, list[str]] = {}

        if normalized.get("status") == "sat":
            for model_index, assignments in self._result_assignment_sets(normalized):
                if not assignments:
                    continue
                offsets = sorted(int(offset) for offset in assignments)
                payload = {
                    "schema": PARTIAL_SOLUTION_SCHEMA,
                    "assignments": assignments,
                    "clause_hashes": clauses,
                    "provenance": "solver-model",
                }
                solution_hash = _digest(_canonical_json(payload))
                rows.append(
                    (
                        solution_hash,
                        query_id,
                        _canonical_json(assignments).decode("ascii"),
                        _canonical_json(offsets).decode("ascii"),
                        _canonical_json(clauses).decode("ascii"),
                        model_index,
                        generator_hash_value,
                        created,
                        "solver-model",
                        "{}",
                        1,
                    )
                )
                row_clauses[solution_hash] = clauses
                row_literals[solution_hash] = literal_hashes

        conflicts = normalized.get("solver_pscache_conflict_solutions", ())
        loaded = self._load_query_ir(query_id) if conflicts else None
        witness_row = (
            db.execute(
                "SELECT input_hex FROM witnesses WHERE query_id = ? "
                "ORDER BY witness_hash LIMIT 1",
                (query_id,),
            ).fetchone()
            if conflicts
            else None
        )
        if loaded is not None and witness_row is not None:
            roots, expressions = loaded
            for conflict_index, raw_conflict in enumerate(conflicts):
                if not isinstance(raw_conflict, Mapping):
                    continue
                assignments = {
                    str(index): int(value)
                    for index, value in raw_conflict.get("assignments", {}).items()
                }
                candidate = self._patched_candidate(
                    str(witness_row["input_hex"]), assignments
                )
                if candidate is None:
                    continue
                memo: dict[str, int] = {}
                root_values = [
                    self._evaluate_expression(root, expressions, candidate, memo)
                    for root in roots
                ]
                if any(value is None for value in root_values) or all(
                    int(value) != 0 for value in root_values
                ):
                    continue
                satisfied_clauses = sorted(
                    {root for root, value in zip(roots, root_values) if int(value) != 0}
                )
                satisfied_literals = sorted(
                    {
                        self._literal_hash(root, expressions, int(value) != 0)
                        for root, value in zip(roots, root_values)
                    }
                )
                proof = {
                    **dict(raw_conflict),
                    "query_id": query_id,
                    "query_ir_conflict_verified": True,
                    "root_values": [bool(int(value)) for value in root_values],
                    "satisfied_literal_hashes": satisfied_literals,
                }
                proof["store_certificate_sha256"] = _digest(_canonical_json(proof))
                payload = {
                    "schema": PARTIAL_SOLUTION_SCHEMA,
                    "assignments": assignments,
                    "clause_hashes": satisfied_clauses,
                    "literal_hashes": satisfied_literals,
                    "provenance": "z3-assumption-conflict",
                }
                solution_hash = _digest(_canonical_json(payload))
                offsets = sorted(int(offset) for offset in assignments)
                rows.append(
                    (
                        solution_hash,
                        query_id,
                        _canonical_json(assignments).decode("ascii"),
                        _canonical_json(offsets).decode("ascii"),
                        _canonical_json(satisfied_clauses).decode("ascii"),
                        -(conflict_index + 1),
                        "",
                        created,
                        "z3-assumption-conflict",
                        _canonical_json(proof).decode("ascii"),
                        1,
                    )
                )
                row_clauses[solution_hash] = satisfied_clauses
                row_literals[solution_hash] = satisfied_literals
        if not rows:
            return 0
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO partial_solutions("
            "solution_hash, source_query_id, assignment_json, offsets_json, "
            "clause_hashes_json, model_index, generator_hash, created, "
            "provenance, proof_json, source_verified"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        clause_rows = [
            (row[0], clause_hash) for row in rows for clause_hash in row_clauses[row[0]]
        ]
        db.executemany(
            "INSERT OR IGNORE INTO partial_solution_clauses("
            "solution_hash, clause_hash) VALUES(?, ?)",
            clause_rows,
        )
        literal_rows = [
            (row[0], literal_hash)
            for row in rows
            for literal_hash in row_literals[row[0]]
        ]
        db.executemany(
            "INSERT OR IGNORE INTO partial_solution_literals("
            "solution_hash, literal_hash) VALUES(?, ?)",
            literal_rows,
        )
        return db.total_changes - before

    def _publish_result_artifacts(
        self,
        query_id: str,
        result_json: str,
    ) -> bool:
        """Materialize one durable result and its idempotent derived artifacts."""
        query_id = _required_hex_digest(query_id, "result publication query")
        try:
            decoded = json.loads(
                result_json,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonfinite_json,
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("stored result publication JSON is invalid") from error
        if _canonical_json(decoded).decode("ascii") != result_json:
            raise ValueError("stored result publication is not canonical")
        normalized = self._validate_result(decoded)

        result_path = self.result_dir / query_id[:2] / f"{query_id}.json"
        _atomic_write_exact(result_path, result_json.encode("ascii") + b"\n")
        if "generator" in normalized:
            generator_path = (
                self.generator_dir
                / normalized["generator_hash"][:2]
                / f"{normalized['generator_hash']}.json"
            )
            _atomic_write_exact(
                generator_path,
                _canonical_json(normalized["generator"]) + b"\n",
            )
        self.materialize_candidates(query_id, normalized)
        return normalized["status"] == "sat"

    def reconcile_result_publications(
        self,
        query_ids: Sequence[str] | None = None,
        *,
        limit: int | None = None,
        time_budget_seconds: float | None = None,
        continue_on_error: bool = False,
        respect_retry_after: bool = False,
        max_attempts: int = _DEFAULT_PUBLICATION_MAX_ATTEMPTS,
        retry_base_seconds: float = _DEFAULT_PUBLICATION_RETRY_BASE_SECONDS,
        retry_max_seconds: float = _DEFAULT_PUBLICATION_RETRY_MAX_SECONDS,
    ) -> int:
        """Replay committed publications under an optional service budget."""
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= 1_000_000
        ):
            raise ValueError("result publication reconciliation limit is invalid")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 1_000_000
        ):
            raise ValueError("result publication max attempts is invalid")
        try:
            retry_base = float(retry_base_seconds)
            retry_max = float(retry_max_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("result publication retry delay is invalid") from error
        if (
            not math.isfinite(retry_base)
            or not math.isfinite(retry_max)
            or retry_base < 0.0
            or retry_max < retry_base
            or retry_max > 7 * 24 * 60 * 60
        ):
            raise ValueError("result publication retry delay is invalid")
        deadline = None
        admit_one_before_deadline = False
        if time_budget_seconds is not None:
            try:
                time_budget = float(time_budget_seconds)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(
                    "result publication reconciliation time budget is invalid"
                ) from error
            if not math.isfinite(time_budget) or not 0.0 <= time_budget <= 3600.0:
                raise ValueError(
                    "result publication reconciliation time budget is invalid"
                )
            deadline = time.monotonic() + time_budget
            admit_one_before_deadline = time_budget > 0.0
        requested_ids = None
        if query_ids is not None:
            if isinstance(query_ids, (str, bytes)) or not isinstance(
                query_ids, Sequence
            ):
                raise TypeError("result publication query ids must be a sequence")
            requested_ids = list(dict.fromkeys(
                _required_hex_digest(query_id, "result publication query")
                for query_id in query_ids
            ))
            if limit is not None:
                requested_ids = requested_ids[:limit]
        if limit == 0 or requested_ids == []:
            return 0

        started = time.monotonic()
        published = 0
        published_sat = False
        attempted = 0
        failed = 0
        dead_lettered = 0
        with self._connect() as db:
            if requested_ids is None:
                now = time.time()
                sql = (
                    "SELECT publication.query_id, publication.attempts, "
                    "result.result_json "
                    "FROM result_publications publication "
                    "JOIN results result "
                    "ON result.query_id = publication.query_id "
                    "WHERE publication.published = 0 "
                    "AND publication.dead_letter = 0 "
                )
                if respect_retry_after:
                    sql += "AND publication.next_retry <= ? "
                sql += "ORDER BY publication.updated, publication.query_id"
                parameters: tuple[Any, ...] = (
                    (now,) if respect_retry_after else ()
                )
                if limit is not None:
                    sql += " LIMIT ?"
                    parameters = (*parameters, limit)
                pending_rows = list(db.execute(sql, parameters))
            else:
                pending_rows = []
                for query_id in requested_ids:
                    sql = (
                        "SELECT publication.query_id, publication.attempts, "
                        "result.result_json "
                        "FROM result_publications publication "
                        "JOIN results result "
                        "ON result.query_id = publication.query_id "
                        "WHERE publication.query_id = ? "
                        "AND publication.published = 0 "
                        "AND publication.dead_letter = 0 "
                    )
                    parameters: tuple[Any, ...] = (query_id,)
                    if respect_retry_after:
                        sql += "AND publication.next_retry <= ?"
                        parameters = (*parameters, time.time())
                    row = db.execute(sql, parameters).fetchone()
                    if row is not None:
                        pending_rows.append(row)

            for row in pending_rows:
                if (
                    deadline is not None
                    and time.monotonic() >= deadline
                    and (attempted > 0 or not admit_one_before_deadline)
                ):
                    break
                query_id = str(row["query_id"])
                attempted += 1
                try:
                    published_sat = (
                        self._publish_result_artifacts(
                            query_id,
                            str(row["result_json"]),
                        )
                        or published_sat
                    )
                except Exception as error:
                    failed += 1
                    detail = str(error).replace("\x00", "?")[:4096]
                    failed_at = time.time()
                    # Publication itself is intentionally outside a database
                    # transaction, so concurrent janitors can select the same
                    # row.  Re-read attempts under the write lock rather than
                    # overwriting both failures with the same stale value.
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT attempts FROM result_publications "
                        "WHERE query_id = ? AND published = 0 "
                        "AND dead_letter = 0",
                        (query_id,),
                    ).fetchone()
                    if current is None:
                        db.rollback()
                        # A peer already published or dead-lettered this row;
                        # the desired terminal state is authoritative over this
                        # janitor's now-stale I/O failure.
                        continue
                    attempts = int(current["attempts"]) + 1
                    is_dead_letter = attempts >= max_attempts
                    delay = min(
                        retry_max,
                        retry_base * (2 ** min(max(0, attempts - 1), 20)),
                    )
                    updated = db.execute(
                        "UPDATE result_publications SET attempts = ?, "
                        "last_error = ?, updated = ?, next_retry = ?, "
                        "dead_letter = ? "
                        "WHERE query_id = ? AND published = 0 "
                        "AND dead_letter = 0",
                        (
                            attempts,
                            detail,
                            failed_at,
                            0.0 if is_dead_letter else failed_at + delay,
                            int(is_dead_letter),
                            query_id,
                        ),
                    )
                    db.commit()
                    dead_lettered += int(is_dead_letter and updated.rowcount == 1)
                    if not continue_on_error:
                        self._last_publication_reconciliation = {
                            "selected": len(pending_rows),
                            "attempted": attempted,
                            "published": published,
                            "failed": failed,
                            "dead_lettered": dead_lettered,
                            "remaining_budget": max(
                                0, (limit or attempted) - attempted,
                            ),
                            "time_budget_exhausted": bool(
                                deadline is not None
                                and attempted < len(pending_rows)
                                and time.monotonic() >= deadline
                            ),
                            "elapsed_seconds": time.monotonic() - started,
                        }
                        raise
                    continue
                updated = db.execute(
                    "UPDATE result_publications SET published = 1, "
                    "last_error = '', updated = ?, next_retry = 0, "
                    "dead_letter = 0 "
                    "WHERE query_id = ? AND published = 0",
                    (time.time(), query_id),
                )
                db.commit()
                published += int(updated.rowcount == 1)
        self._last_publication_reconciliation = {
            "selected": len(pending_rows),
            "attempted": attempted,
            "published": published,
            "failed": failed,
            "dead_lettered": dead_lettered,
            "remaining_budget": max(0, (limit or attempted) - attempted),
            "time_budget_exhausted": bool(
                deadline is not None
                and attempted < len(pending_rows)
                and time.monotonic() >= deadline
            ),
            "elapsed_seconds": time.monotonic() - started,
        }
        if published_sat:
            self.materialize_partial_candidates_for_pending()
        return published

    def requeue_result_publications(
        self,
        query_ids: Sequence[str] | None = None,
    ) -> int:
        """Move dead-lettered publications back to the active retry queue."""
        requested_ids = None
        if query_ids is not None:
            if isinstance(query_ids, (str, bytes)) or not isinstance(
                query_ids, Sequence
            ):
                raise TypeError("result publication query ids must be a sequence")
            requested_ids = sorted({
                _required_hex_digest(query_id, "result publication query")
                for query_id in query_ids
            })
            if not requested_ids:
                return 0
        now = time.time()
        with self._connect() as db:
            if requested_ids is None:
                updated = db.execute(
                    "UPDATE result_publications SET dead_letter = 0, "
                    "attempts = 0, last_error = '', next_retry = 0, updated = ? "
                    "WHERE published = 0 AND dead_letter = 1",
                    (now,),
                )
            else:
                placeholders = ",".join("?" for _ in requested_ids)
                updated = db.execute(
                    "UPDATE result_publications SET dead_letter = 0, "
                    "attempts = 0, last_error = '', next_retry = 0, updated = ? "
                    f"WHERE published = 0 AND dead_letter = 1 "
                    f"AND query_id IN ({placeholders})",
                    (now, *requested_ids),
                )
            db.commit()
            return int(updated.rowcount)

    def drain_result_publications(
        self,
        *,
        limit: int = _DEFAULT_STARTUP_PUBLICATION_LIMIT,
        time_budget_seconds: float = _DEFAULT_STARTUP_PUBLICATION_SECONDS,
        max_attempts: int = _DEFAULT_PUBLICATION_MAX_ATTEMPTS,
        retry_base_seconds: float = _DEFAULT_PUBLICATION_RETRY_BASE_SECONDS,
        retry_max_seconds: float = _DEFAULT_PUBLICATION_RETRY_MAX_SECONDS,
    ) -> int:
        """Drain every publication that is currently due in bounded passes.

        A failed row receives ``next_retry`` before the next pass and therefore
        cannot spin inside this drain. The hard pass bound protects callers if
        another process continuously adds due rows to the shared store.
        """
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1_000_000
        ):
            raise ValueError("result publication drain limit is invalid")
        max_passes = 1_000_001
        published = 0
        for _pass in range(max_passes):
            published += self.reconcile_result_publications(
                limit=limit,
                time_budget_seconds=time_budget_seconds,
                continue_on_error=True,
                respect_retry_after=True,
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
            )
            snapshot = self.publication_reconciliation_snapshot()
            attempted = int(snapshot.get("attempted", 0))
            time_exhausted = bool(snapshot.get("time_budget_exhausted", False))
            if attempted == 0 or (attempted < limit and not time_exhausted):
                break
        else:
            raise RuntimeError("result publication drain did not quiesce")
        return published

    def publication_reconciliation_snapshot(self) -> dict[str, Any]:
        return dict(self._last_publication_reconciliation)

    def complete(
        self,
        lease: WorkLease,
        owner: str,
        result: Mapping[str, Any],
    ) -> bool:
        lease.close_artifacts()
        lease_check_time = time.time()
        with self._connect() as lease_db:
            active_lease = lease_db.execute(
                "SELECT 1 FROM queries WHERE query_id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_token = ? AND lease_until > ?",
                (lease.query_id, owner, lease.token, lease_check_time),
            ).fetchone()
        if active_lease is None:
            return False
        normalized = self._validate_result(result)
        if "generator" in normalized:
            generator = dict(normalized["generator"])
            if generator["query_id"] not in {"", lease.query_id}:
                raise ValueError("generator query_id does not match lease")
            generator["query_id"] = lease.query_id
            with self._connect() as query_db:
                query_row = query_db.execute(
                    "SELECT query_id, prefix_hash, target_hash, smt2_hash, "
                    "prefix_smt2_hash, target_smt2_hash FROM queries "
                    "WHERE query_id = ?",
                    (lease.query_id,),
                ).fetchone()
            if query_row is None:
                raise ValueError("generator query does not exist")
            query_body = self._verified_query_body(query_row)
            converters = query_body.get("converter_chain", ())
            if not generator["converter_chain"] and isinstance(converters, list):
                generator["converter_chain"] = converters
            normalized_generator = normalize_generator(generator)
            normalized["generator"] = normalized_generator
            normalized["generator_hash"] = generator_hash(normalized_generator)
        if normalized.get("backend_kind") == "bitblast-cadical-qfbv":
            certificate = normalized.get("bitblast_certificate")
            if (
                not isinstance(certificate, Mapping)
                or certificate.get("query_id") != lease.query_id
            ):
                raise ValueError("bit-blast certificate query identity mismatch")
            loaded_cnf_query = self._load_query_ir(lease.query_id)
            if loaded_cnf_query is None:
                raise ValueError("cannot independently load bit-blast Query IR")
            try:
                cnf_plan = bitblast_qfbv_query(
                    lease.query_id,
                    loaded_cnf_query[0],
                    loaded_cnf_query[1],
                    normalized["backend_capabilities"],
                )
            except (QfbvBitBlastError, ValueError) as error:
                raise ValueError(
                    "cannot independently reconstruct bit-blast formula"
                ) from error
            if dict(cnf_plan.certificate) != certificate:
                raise ValueError("bit-blast certificate does not match Query IR")
            policy = str(normalized["backend_incremental_proof_policy_sha256"])
            with self._qfbv_incremental_proof_checker_lock:
                incremental_checker = self._qfbv_incremental_proof_checkers.get(policy)
            if incremental_checker is None or incremental_checker.store is None:
                raise ValueError(
                    "incremental proof checker policy is not registered locally"
                )
            import_elapsed_us = 0
            try:
                imported_ids = normalized["backend_incremental_import_record_sha256"]
                for record_sha256 in imported_ids:
                    imported_authorization = incremental_checker.verify_clause_record(
                        cnf_plan,
                        incremental_checker.store.load(record_sha256),
                    )
                    import_elapsed_us += imported_authorization.checker_elapsed_us
                realtime_elapsed_us = 0
                realtime_import_ids = normalized.get(
                    "backend_realtime_import_record_sha256", []
                )
                realtime_acks = normalized.get("backend_realtime_import_acks", [])
                for ack in realtime_acks:
                    realtime_authorization = verify_checked_import_ack(
                        cnf_plan, ack, checker=incremental_checker
                    )
                    event = incremental_checker.store.event_at(
                        int(ack["event_sequence"])
                    )
                    if event != (
                        realtime_authorization.formula_sha256,
                        realtime_authorization.record_sha256,
                    ):
                        raise RealtimeStreamError(
                            "checked-import ACK does not name its durable event"
                        )
                    realtime_elapsed_us += realtime_authorization.checker_elapsed_us
                if [
                    str(ack["record_sha256"]) for ack in realtime_acks
                ] != realtime_import_ids:
                    raise RealtimeStreamError(
                        "checked-import ACK order differs from worker telemetry"
                    )
                realtime_activity_receipts = normalized.get(
                    "backend_realtime_clause_activity_receipts", []
                )
                realtime_acks_by_digest = {
                    str(ack["ack_sha256"]): ack for ack in realtime_acks
                }
                for activity_receipt in realtime_activity_receipts:
                    activity_ack = realtime_acks_by_digest.get(
                        str(activity_receipt["ack_sha256"])
                    )
                    if activity_ack is None:
                        raise RealtimeStreamError(
                            "clause activity names an unavailable delivery ACK"
                        )
                    activity_authorization = verify_clause_activity_receipt(
                        cnf_plan,
                        activity_receipt,
                        ack=activity_ack,
                        checker=incremental_checker,
                    )
                    event = incremental_checker.store.event_at(
                        int(activity_ack["event_sequence"])
                    )
                    if event != (
                        activity_authorization.formula_sha256,
                        activity_authorization.record_sha256,
                    ):
                        raise RealtimeStreamError(
                            "clause activity differs from its durable event"
                        )
                    realtime_elapsed_us += activity_authorization.checker_elapsed_us
                adaptive_enqueued = normalized.get(
                    "backend_realtime_adaptive_enqueued_decisions", {}
                )
                adaptive_decisions = {
                    str(decision["decision_sha256"]): decision
                    for decision in normalized.get(
                        "backend_realtime_adaptive_decisions", []
                    )
                }
                for adaptive_record, decision_sha256 in adaptive_enqueued.items():
                    authorization = incremental_checker.verify_clause_record(
                        cnf_plan,
                        incremental_checker.store.load(adaptive_record),
                    )
                    decision = adaptive_decisions[str(decision_sha256)]
                    candidate = decision["candidate"]
                    event = incremental_checker.store.event_at(
                        int(candidate["event_sequence"])
                    )
                    if (
                        candidate["record_sha256"] != authorization.record_sha256
                        or candidate["formula_sha256"] != authorization.formula_sha256
                        or candidate["source_worker"] != authorization.source_worker
                        or int(candidate["clause_literals"])
                        != len(authorization.clause)
                        or int(candidate["proof_steps"]) != authorization.proof_steps
                        or int(candidate["propagation_count"])
                        != authorization.propagation_count
                        or event
                        != (
                            authorization.formula_sha256,
                            authorization.record_sha256,
                        )
                    ):
                        raise RealtimeStreamError(
                            "adaptive admission differs from its checked proof"
                        )
                    realtime_elapsed_us += authorization.checker_elapsed_us
                pairing_decisions = normalized.get(
                    "backend_realtime_pairing_decisions", []
                )
                pairing_family = formula_family_sha256(cnf_plan.certificate)
                pairing_consumer = normalized.get(
                    "backend_realtime_pairing_consumer_worker", ""
                )
                for pairing_decision in pairing_decisions:
                    candidate = pairing_decision["candidate"]
                    authorization = incremental_checker.verify_clause_record(
                        cnf_plan,
                        incremental_checker.store.load(str(candidate["record_sha256"])),
                    )
                    event_sequence = int(candidate["event_sequence"])
                    event = incremental_checker.store.event_at(event_sequence)
                    latest = incremental_checker.store.latest_event_sequence(
                        authorization.formula_sha256
                    )
                    if (
                        candidate["record_sha256"] != authorization.record_sha256
                        or candidate["publisher_worker"] != authorization.source_worker
                        or candidate["consumer_worker"] != pairing_consumer
                        or candidate["formula_family_sha256"] != pairing_family
                        or int(candidate["event_lag"]) > max(0, latest - event_sequence)
                        or event
                        != (
                            authorization.formula_sha256,
                            authorization.record_sha256,
                        )
                    ):
                        raise UtilityPairingError(
                            "utility pairing differs from its checked proof"
                        )
                    realtime_elapsed_us += authorization.checker_elapsed_us
                learned_ids = normalized.get(
                    "backend_realtime_learned_record_sha256", []
                )
                for learned_sha256 in learned_ids:
                    learned_authorization = incremental_checker.verify_clause_record(
                        cnf_plan,
                        incremental_checker.store.load(learned_sha256),
                    )
                    realtime_elapsed_us += learned_authorization.checker_elapsed_us
                if normalized["status"] == "sat" and not set(
                    realtime_import_ids
                ).issubset(imported_ids):
                    raise RealtimeStreamError(
                        "active realtime clauses are absent from SAT telemetry"
                    )
                if normalized["status"] == "unsat":
                    record_sha256 = str(
                        normalized["backend_incremental_proof_record_sha256"]
                    )
                    final_record = incremental_checker.store.load(record_sha256)
                    if [
                        str(item["receipt_sha256"]) for item in final_record["imports"]
                    ] != imported_ids:
                        raise IncrementalProofError(
                            "final proof imports differ from worker telemetry"
                        )
                    clause_authorization = incremental_checker.verify_clause_record(
                        cnf_plan, final_record
                    )
                    result_authorization = incremental_checker.verify_result_receipt(
                        cnf_plan,
                        normalized["backend_incremental_result_receipt"],
                    )
                    if (
                        clause_authorization.record_sha256 != record_sha256
                        or result_authorization.clause_receipt_sha256 != record_sha256
                    ):
                        raise IncrementalProofError(
                            "incremental proof authorization identity changed"
                        )
                    wire_protocol = normalized.get(
                        "backend_lidrup_wire_protocol", ""
                    )
                    if wire_protocol:
                        wire_policy = str(
                            normalized["backend_lidrup_checker_policy_sha256"]
                        )
                        with self._qfbv_proof_wire_lock:
                            wire_registration = self._qfbv_proof_wires.get(
                                wire_policy
                            )
                        if wire_registration is None:
                            raise ProofWireError(
                                "LIDRUP checker policy is not registered locally"
                            )
                        wire_checker, wire_store = wire_registration
                        wire_artifacts, wire_receipt = wire_store.load(
                            str(normalized["backend_lidrup_artifact_sha256"]),
                            str(normalized["backend_lidrup_receipt_sha256"]),
                        )
                        checked_wire_receipt = wire_checker.validate_receipt(
                            cnf_plan,
                            normalized["backend_incremental_result_receipt"],
                            incremental_checker.store,
                            wire_artifacts,
                            wire_receipt,
                        )
                        if (
                            checked_wire_receipt["checker_policy_sha256"]
                            != wire_policy
                            or wire_artifacts.learned_clause_count
                            != int(normalized["backend_lidrup_learned_clauses"])
                        ):
                            raise ProofWireError(
                                "LIDRUP store evidence differs from result telemetry"
                            )
                        normalized["store_lidrup_verified"] = True
                        normalized["store_lidrup_checker_elapsed_us"] = int(
                            checked_wire_receipt["checker_elapsed_us"]
                        )
                    normalized["store_incremental_proof_verified"] = True
                    normalized["store_incremental_proof_checker_elapsed_us"] = (
                        clause_authorization.checker_elapsed_us
                        + result_authorization.checker_elapsed_us
                    )
            except (
                FileNotFoundError,
                OSError,
                sqlite3.Error,
                IncrementalProofError,
                RealtimeStreamError,
                UtilityPairingError,
                ProofWireError,
            ) as error:
                raise ValueError(
                    "incremental QF_BV proof failed independent store verification"
                ) from error
            normalized["store_incremental_import_verified_count"] = len(imported_ids)
            normalized["store_incremental_import_checker_elapsed_us"] = (
                import_elapsed_us
            )
            if normalized.get("backend_realtime_stream_protocol"):
                normalized["store_realtime_stream_verified"] = True
                normalized["store_realtime_stream_checker_elapsed_us"] = (
                    realtime_elapsed_us
                )
                normalized["store_realtime_stream_verified_imports"] = len(
                    realtime_import_ids
                )
                normalized["store_realtime_stream_verified_learned"] = len(learned_ids)
                normalized["store_realtime_clause_activity_verified"] = bool(
                    normalized.get("backend_realtime_clause_activity_enabled", False)
                )
                normalized["store_realtime_clause_activity_verified_receipts"] = len(
                    realtime_activity_receipts
                )
                if normalized.get("backend_realtime_adaptive_protocol"):
                    if normalized["backend_realtime_adaptive_protocol"] != (
                        QFBV_ADAPTIVE_EXCHANGE_PROTOCOL
                    ):
                        raise ValueError("adaptive proof admission protocol changed")
                    normalized["store_realtime_adaptive_verified"] = True
                    normalized["store_realtime_adaptive_verified_decisions"] = len(
                        normalized["backend_realtime_adaptive_decisions"]
                    )
                    normalized["store_realtime_adaptive_verified_enqueues"] = len(
                        normalized["backend_realtime_adaptive_enqueued_decisions"]
                    )
                if normalized.get("backend_realtime_pairing_protocol"):
                    if normalized["backend_realtime_pairing_protocol"] != (
                        QFBV_UTILITY_PAIRING_PROTOCOL
                    ):
                        raise ValueError("utility pairing protocol changed")
                    normalized["store_realtime_pairing_verified"] = True
                    normalized["store_realtime_pairing_verified_decisions"] = len(
                        normalized["backend_realtime_pairing_decisions"]
                    )
                    normalized["store_realtime_pairing_verified_outcomes"] = len(
                        normalized["backend_realtime_pairing_outcomes"]
                    )
        if normalized.get("backend_kind") == "smtlib-qfbv":
            lowering = normalized.get("lowering_certificate")
            if (
                isinstance(lowering, Mapping)
                and lowering.get("query_id") != lease.query_id
            ):
                raise ValueError("QF_BV lowering certificate query id mismatch")
            if normalized.get("backend_context_protocol") == (
                QFBV_SHARED_CONTEXT_PROTOCOL
            ):
                with self._connect() as context_db:
                    context_row = context_db.execute(
                        "SELECT query_id, prefix_hash, target_hash, smt2_hash, "
                        "prefix_smt2_hash, target_smt2_hash FROM queries "
                        "WHERE query_id = ?",
                        (lease.query_id,),
                    ).fetchone()
                if context_row is None:
                    raise ValueError("shared context query does not exist")
                query_body = self._verified_query_body(context_row)
                loaded_context = self._load_query_ir(
                    lease.query_id,
                    query_body=query_body,
                )
                if loaded_context is None:
                    raise ValueError("cannot verify shared context Query IR")
                expected_lowering, expected_context = qfbv_prefix_context_identity(
                    lease.query_id,
                    loaded_context[0],
                    loaded_context[1],
                    normalized["backend_capabilities"],
                )
                if lowering != expected_lowering:
                    raise ValueError("shared context lowering does not match Query IR")
                if expected_context is None:
                    raise ValueError("shared context requires a non-empty prefix")
                if (
                    normalized["backend_shared_context_sha256"]
                    != expected_context["context_sha256"]
                    or normalized["backend_shared_parent_context_sha256"]
                    != expected_context["parent_context_sha256"]
                    or normalized["backend_shared_context_depth"]
                    != expected_context["depth"]
                ):
                    raise ValueError("shared context identity does not match Query IR")
            lemma_records = normalized.get("backend_verified_lemma_records")
            published_lemma_ids = normalized.get(
                "backend_published_lemma_record_sha256"
            )
            has_active_lemmas = isinstance(lemma_records, list) and bool(lemma_records)
            has_published_lemmas = isinstance(published_lemma_ids, list) and bool(
                published_lemma_ids
            )
            if has_active_lemmas or has_published_lemmas:
                if normalized.get("backend_context_protocol") != (
                    QFBV_SHARED_CONTEXT_PROTOCOL
                ):
                    raise ValueError("verified lemmas require a shared prefix context")
                lemma_policy = str(normalized["backend_lemma_exchange_policy_sha256"])
                with self._qfbv_lemma_exchange_lock:
                    lemma_exchange = self._qfbv_lemma_exchanges.get(lemma_policy)
                if lemma_exchange is None:
                    raise ValueError(
                        "QF_BV lemma exchange policy is not registered locally"
                    )
                deadline = time.monotonic() + max(
                    0.001,
                    min(int(lease.timeout_ms), 3_600_000) / 1000.0,
                )
                checker_elapsed = 0
                try:
                    assert isinstance(lemma_records, list)
                    for lemma_record in lemma_records:
                        remaining_ms = int((deadline - time.monotonic()) * 1000.0)
                        if remaining_ms <= 0:
                            raise LemmaExchangeError(
                                "commit-time lemma verification deadline expired"
                            )
                        authorization = lemma_exchange.verify_record_for_target(
                            lemma_record,
                            normalized["backend_shared_context_sha256"],
                            timeout_ms=remaining_ms,
                        )
                        checker_elapsed += authorization.checker_elapsed_us
                    if has_published_lemmas:
                        if loaded_context is None:
                            raise LemmaExchangeError(
                                "published lemmas lack verified Query IR"
                            )
                        full_lowering, expected_source = qfbv_full_context_identity(
                            lease.query_id,
                            loaded_context[0],
                            loaded_context[1],
                            normalized["backend_capabilities"],
                        )
                        if full_lowering != lowering or expected_source is None:
                            raise LemmaExchangeError(
                                "published lemma source lowering is inconsistent"
                            )
                        if (
                            normalized.get("backend_lemma_source_context_sha256")
                            != expected_source["context_sha256"]
                        ):
                            raise LemmaExchangeError(
                                "published lemma source does not match Query IR"
                            )
                        assert isinstance(published_lemma_ids, list)
                        for record_sha256 in published_lemma_ids:
                            remaining_ms = int((deadline - time.monotonic()) * 1000.0)
                            if remaining_ms <= 0:
                                raise LemmaExchangeError(
                                    "commit-time lemma verification deadline expired"
                                )
                            published_record = lemma_exchange.store.load(record_sha256)
                            if (
                                published_record["record_sha256"] != record_sha256
                                or published_record["exchange_policy_sha256"]
                                != lemma_policy
                                or published_record["source_context"]["context_sha256"]
                                != expected_source["context_sha256"]
                            ):
                                raise LemmaExchangeError(
                                    "published lemma record identity mismatch"
                                )
                            authorization = lemma_exchange.verify_record_for_target(
                                published_record,
                                expected_source["context_sha256"],
                                timeout_ms=remaining_ms,
                            )
                            checker_elapsed += authorization.checker_elapsed_us
                except (
                    FileNotFoundError,
                    OSError,
                    sqlite3.Error,
                    LemmaExchangeError,
                ) as error:
                    raise ValueError(
                        "active QF_BV lemma failed independent store verification"
                    ) from error
                normalized["store_verified_lemma_count"] = len(lemma_records)
                normalized["store_verified_published_lemma_count"] = (
                    len(published_lemma_ids)
                    if isinstance(published_lemma_ids, list)
                    else 0
                )
                normalized["store_lemma_checker_elapsed_us"] = checker_elapsed
            substitution_core_hit = (
                normalized.get("backend_substitution_core_hit") is True
            )
            published_core_sha256 = normalized.get(
                "backend_substitution_core_published_record_sha256"
            )
            if substitution_core_hit or isinstance(published_core_sha256, str):
                policy = str(normalized["backend_substitution_core_policy_sha256"])
                with self._qfbv_substitution_core_exchange_lock:
                    core_exchange = self._qfbv_substitution_core_exchanges.get(policy)
                if core_exchange is None:
                    raise ValueError(
                        "QF_BV substitution-core policy is not registered locally"
                    )
                loaded_core_query = self._load_query_ir(lease.query_id)
                if loaded_core_query is None:
                    raise ValueError(
                        "cannot independently load substitution-core target Query IR"
                    )
                try:
                    (
                        _core_target_smt2,
                        _core_target_proof_query,
                        _core_target_reference,
                        core_target_lowering,
                        _core_target_offsets,
                        _core_target_terms,
                        _core_target_context,
                    ) = lower_qfbv_proof_problem(
                        lease.query_id,
                        loaded_core_query[0],
                        loaded_core_query[1],
                        normalized["backend_capabilities"],
                    )
                    if core_target_lowering != lowering:
                        raise SubstitutionCoreError(
                            "substitution-core target lowering differs from Query IR"
                        )
                    checker_elapsed = 0
                    if substitution_core_hit:
                        record_sha256 = str(
                            normalized["backend_substitution_core_record_sha256"]
                        )
                        record = core_exchange.store.load(record_sha256)
                        if (
                            record["record_sha256"] != record_sha256
                            or record["source_query_id"]
                            != normalized["backend_substitution_core_source_query_id"]
                        ):
                            raise SubstitutionCoreError(
                                "substitution-core result identity mismatch"
                            )
                        expected_mapping = {
                            int(source): int(target)
                            for source, target in normalized[
                                "backend_substitution_core_mapping"
                            ]
                        }
                        authorization = core_exchange.verify_record_for_formula(
                            record,
                            loaded_core_query[0],
                            loaded_core_query[1],
                            normalized["backend_capabilities"],
                            timeout_ms=max(1, min(int(lease.timeout_ms), 3_600_000)),
                            expected_mapping=expected_mapping,
                            verification_domain="query-store",
                        )
                        checker_elapsed += authorization.checker_elapsed_us
                        normalized["store_substitution_core_verified"] = True
                        normalized["store_substitution_core_proof_reused"] = (
                            authorization.proof_reused
                        )
                    if isinstance(published_core_sha256, str):
                        published_record = core_exchange.store.load(
                            published_core_sha256
                        )
                        if (
                            published_record["source_query_id"] != lease.query_id
                            or published_record["source_clause_count"]
                            != normalized[
                                "backend_substitution_core_published_clause_count"
                            ]
                            or not set(published_record["source_roots"]).issubset(
                                set(loaded_core_query[0])
                            )
                        ):
                            raise SubstitutionCoreError(
                                "published substitution core is not from this query"
                            )
                        authorization = core_exchange.verify_record_for_formula(
                            published_record,
                            loaded_core_query[0],
                            loaded_core_query[1],
                            normalized["backend_capabilities"],
                            timeout_ms=max(1, min(int(lease.timeout_ms), 3_600_000)),
                            verification_domain="query-store",
                        )
                        checker_elapsed += authorization.checker_elapsed_us
                        normalized["store_published_substitution_core_verified"] = True
                        normalized["store_published_substitution_core_proof_reused"] = (
                            authorization.proof_reused
                        )
                except (
                    FileNotFoundError,
                    OSError,
                    sqlite3.Error,
                    TimeoutError,
                    SubstitutionCoreError,
                ) as error:
                    raise ValueError(
                        "QF_BV substitution core failed independent store verification"
                    ) from error
                normalized["store_substitution_core_checker_elapsed_us"] = (
                    checker_elapsed
                )
            proof_receipt = normalized.get("backend_unsat_proof_receipt")
            if isinstance(proof_receipt, Mapping):
                with self._connect() as proof_db:
                    proof_query_row = proof_db.execute(
                        "SELECT query_id, prefix_hash, target_hash, smt2_hash, "
                        "prefix_smt2_hash, target_smt2_hash FROM queries "
                        "WHERE query_id = ?",
                        (lease.query_id,),
                    ).fetchone()
                if proof_query_row is None:
                    raise ValueError("UNSAT proof query does not exist")
                proof_query_body = self._verified_query_body(proof_query_row)
                loaded_proof_query = self._load_query_ir(
                    lease.query_id,
                    query_body=proof_query_body,
                )
                if loaded_proof_query is None:
                    raise ValueError("cannot independently lower UNSAT proof query")
                try:
                    (
                        proof_smt2,
                        proof_query_smt2,
                        proof_reference_smt2,
                        proof_lowering,
                        proof_offsets,
                        _proof_terms,
                        proof_context,
                    ) = lower_qfbv_proof_problem(
                        lease.query_id,
                        loaded_proof_query[0],
                        loaded_proof_query[1],
                        normalized["backend_capabilities"],
                    )
                except (ValueError, ProofVerificationError) as error:
                    raise ValueError(
                        "cannot independently reconstruct UNSAT proof input"
                    ) from error
                if proof_lowering != normalized.get("lowering_certificate"):
                    raise ValueError("UNSAT proof lowering does not match Query IR")
                policy = str(proof_receipt["checker_policy_sha256"])
                with self._qfbv_proof_verifier_lock:
                    proof_verifier = self._qfbv_proof_verifiers.get(policy)
                if proof_verifier is None:
                    raise ValueError(
                        "UNSAT proof checker policy is not registered locally"
                    )
                try:
                    checker_elapsed = proof_verifier.verify_receipt(
                        proof_receipt,
                        query_id=lease.query_id,
                        smt2=proof_smt2.encode("ascii"),
                        proof_query_smt2=proof_query_smt2.encode("ascii"),
                        reference_smt2=proof_reference_smt2.encode("ascii"),
                        offsets=proof_offsets,
                        lowering_certificate_sha256=str(
                            proof_lowering["certificate_sha256"]
                        ),
                        capability_sha256=str(
                            normalized["backend_capabilities"]["capability_sha256"]
                        ),
                        context=proof_context,
                        timeout_ms=max(1, min(int(lease.timeout_ms), 3_600_000)),
                    )
                except (OSError, sqlite3.Error, ProofVerificationError) as error:
                    raise ValueError(
                        "UNSAT proof failed independent store verification"
                    ) from error
                normalized["store_unsat_proof_verified"] = True
                normalized["store_unsat_proof_checker_elapsed_us"] = checker_elapsed
        if normalized["status"] == "sat":
            with self._connect() as validation_db:
                query_row = validation_db.execute(
                    "SELECT query_id, prefix_hash, target_hash, smt2_hash, "
                    "prefix_smt2_hash, target_smt2_hash FROM queries "
                    "WHERE query_id = ?",
                    (lease.query_id,),
                ).fetchone()
                witnesses = validation_db.execute(
                    "SELECT input_hex FROM witnesses WHERE query_id = ? "
                    "ORDER BY witness_hash LIMIT ?",
                    (lease.query_id, _MAX_SAT_VALIDATION_WITNESSES),
                ).fetchall()
            if query_row is None:
                raise ValueError("cannot independently validate SAT result")
            query_body = self._verified_query_body(query_row)
            loaded = self._load_query_ir(lease.query_id, query_body=query_body)
            if loaded is None or not self._sat_result_has_verified_candidate(
                normalized,
                loaded,
                witnesses,
            ):
                raise ValueError("SAT result failed independent store validation")
            normalized["store_model_verified"] = True
        pairing_checkpoint: tuple[str, str, dict[str, Any]] | None = None
        if normalized.get("store_realtime_pairing_verified") is True:
            try:
                pairing_policy = UtilityPairingPolicy.from_sealed(
                    normalized["backend_realtime_pairing_policy"]
                )
                pairing_snapshot = verify_pairing_snapshot(
                    normalized["backend_realtime_pairing_controller_snapshot"],
                    policy=pairing_policy,
                )
            except (KeyError, UtilityPairingError) as error:
                raise ValueError(
                    "verified utility-pairing checkpoint is incomplete"
                ) from error
            pairing_consumer = str(
                normalized["backend_realtime_pairing_consumer_worker"]
            )
            if pairing_snapshot["pending_outcomes"] != 0 or any(
                state["consumer_worker"] != pairing_consumer
                for state in pairing_snapshot["pairs"].values()
            ):
                raise ValueError("verified utility-pairing checkpoint scope changed")
            pairing_checkpoint = (
                pairing_consumer,
                pairing_policy.sha256,
                pairing_snapshot,
            )
        now = time.time()
        result_json = ""
        subsumed: list[str] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE queries SET status = 'done', lease_owner = NULL, "
                "lease_until = NULL, updated = ? "
                "WHERE query_id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_token = ? AND lease_until > ?",
                (now, lease.query_id, owner, lease.token, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return False
            if pairing_checkpoint is not None:
                disposition = self._commit_qfbv_utility_pairing_snapshot(
                    db,
                    consumer_worker=pairing_checkpoint[0],
                    policy_sha256=pairing_checkpoint[1],
                    snapshot=pairing_checkpoint[2],
                    query_id=lease.query_id,
                    updated=now,
                )
                normalized["store_realtime_pairing_checkpoint_disposition"] = (
                    disposition
                )
                normalized["store_realtime_pairing_checkpoint_next_event"] = (
                    pairing_checkpoint[2]["next_controller_event_ordinal"]
                )
            result_json = _canonical_json(normalized).decode("ascii")
            db.execute(
                "INSERT OR REPLACE INTO results(query_id, result_json, completed) "
                "VALUES(?, ?, ?)",
                (lease.query_id, result_json, now),
            )
            db.execute(
                "INSERT INTO result_publications("
                "query_id, published, attempts, last_error, created, updated, "
                "next_retry, dead_letter"
                ") VALUES(?, 0, 0, '', ?, ?, 0, 0) "
                "ON CONFLICT(query_id) DO UPDATE SET "
                "published=0, attempts=0, last_error='', "
                "created=excluded.created, updated=excluded.updated, "
                "next_retry=0, dead_letter=0",
                (lease.query_id, now, now),
            )
            if "generator" in normalized:
                db.execute(
                    "INSERT OR REPLACE INTO generators("
                    "query_id, generator_hash, generator_json, created"
                    ") VALUES(?, ?, ?, ?)",
                    (
                        lease.query_id,
                        normalized["generator_hash"],
                        _canonical_json(normalized["generator"]).decode("ascii"),
                        now,
                    ),
                )
            self._store_partial_solutions(db, lease.query_id, normalized, now)
            if normalized["status"] == "unsat":
                clause_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM query_clauses WHERE query_id = ?",
                        (lease.query_id,),
                    ).fetchone()[0]
                )
                db.execute(
                    "INSERT OR REPLACE INTO unsat_sets(query_id, clause_count) "
                    "VALUES(?, ?)",
                    (lease.query_id, clause_count),
                )
                subsumed = [
                    str(row["query_id"])
                    for row in db.execute(
                        "SELECT candidate.query_id FROM queries candidate "
                        "WHERE candidate.status = 'pending' "
                        "AND candidate.query_id != ? AND NOT EXISTS ("
                        "SELECT 1 FROM query_clauses source "
                        "WHERE source.query_id = ? AND NOT EXISTS ("
                        "SELECT 1 FROM query_clauses member "
                        "WHERE member.query_id = candidate.query_id "
                        "AND member.clause_hash = source.clause_hash))",
                        (lease.query_id, lease.query_id),
                    )
                ]
                for query_id in subsumed:
                    cached = self._validate_result(
                        {
                            "status": "unsat",
                            "assignments": {},
                            "solver": "query-store-unsat-subset",
                            "elapsed_us": 0,
                            "reused_from": lease.query_id,
                        }
                    )
                    cached_json = _canonical_json(cached).decode("ascii")
                    db.execute(
                        "UPDATE queries SET status = 'done', updated = ? "
                        "WHERE query_id = ? AND status = 'pending'",
                        (now, query_id),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO results("
                        "query_id, result_json, completed) VALUES(?, ?, ?)",
                        (query_id, cached_json, now),
                    )
                    db.execute(
                        "INSERT INTO result_publications("
                        "query_id, published, attempts, last_error, created, updated, "
                        "next_retry, dead_letter"
                        ") VALUES(?, 0, 0, '', ?, ?, 0, 0) "
                        "ON CONFLICT(query_id) DO UPDATE SET "
                        "published=0, attempts=0, last_error='', "
                        "created=excluded.created, updated=excluded.updated, "
                        "next_retry=0, dead_letter=0",
                        (query_id, now, now),
                    )
            db.commit()
        # The SQLite commit above is authoritative.  Filesystem publication is
        # an outbox side effect: a transient failure must not turn a completed
        # query back into a solver error or consume another solve attempt.
        self.reconcile_result_publications(
            [lease.query_id, *subsumed],
            limit=_DEFAULT_COMPLETION_PUBLICATION_LIMIT,
            time_budget_seconds=_DEFAULT_COMPLETION_PUBLICATION_SECONDS,
            continue_on_error=True,
            respect_retry_after=True,
        )
        return True

    def fail(
        self,
        lease: WorkLease,
        owner: str,
        reason: str,
        *,
        max_attempts: int = 3,
    ) -> bool:
        lease.close_artifacts()
        attempt_limit = _bounded_int(
            max_attempts,
            "maximum query attempts",
            1,
            1_000_000,
        )
        terminal_result = self._validate_result(
            {
                "status": "error",
                "assignments": {},
                "reason": reason,
                "solver": "worker",
                "elapsed_us": 0,
            }
        )
        terminal_json = _canonical_json(terminal_result).decode("ascii")
        now = time.time()
        terminal = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT attempts FROM queries WHERE query_id = ? "
                "AND status = 'leased' AND lease_owner = ? AND lease_token = ? "
                "AND lease_until > ?",
                (lease.query_id, owner, lease.token, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            if int(row["attempts"]) < attempt_limit:
                updated = db.execute(
                    "UPDATE queries SET status = 'pending', lease_owner = NULL, "
                    "lease_until = NULL, updated = ? WHERE query_id = ? "
                    "AND status = 'leased' AND lease_owner = ? "
                    "AND lease_token = ? AND lease_until > ?",
                    (now, lease.query_id, owner, lease.token, now),
                )
                if updated.rowcount != 1:
                    db.rollback()
                    return False
                db.commit()
                return True
            updated = db.execute(
                "UPDATE queries SET status = 'done', lease_owner = NULL, "
                "lease_until = NULL, updated = ? WHERE query_id = ? "
                "AND status = 'leased' AND lease_owner = ? "
                "AND lease_token = ? AND lease_until > ?",
                (now, lease.query_id, owner, lease.token, now),
            )
            if updated.rowcount != 1:
                db.rollback()
                return False
            db.execute(
                "INSERT OR REPLACE INTO results(query_id, result_json, completed) "
                "VALUES(?, ?, ?)",
                (lease.query_id, terminal_json, now),
            )
            db.execute(
                "INSERT INTO result_publications("
                "query_id, published, attempts, last_error, created, updated, "
                "next_retry, dead_letter"
                ") VALUES(?, 0, 0, '', ?, ?, 0, 0) "
                "ON CONFLICT(query_id) DO UPDATE SET "
                "published=0, attempts=0, last_error='', "
                "created=excluded.created, updated=excluded.updated, "
                "next_retry=0, dead_letter=0",
                (lease.query_id, now, now),
            )
            db.commit()
            terminal = True
        if terminal:
            self.reconcile_result_publications(
                [lease.query_id],
                limit=_DEFAULT_COMPLETION_PUBLICATION_LIMIT,
                time_budget_seconds=_DEFAULT_COMPLETION_PUBLICATION_SECONDS,
                continue_on_error=True,
                respect_retry_after=True,
            )
        return terminal

    def materialize_candidates(
        self,
        query_id: str,
        result: Mapping[str, Any],
    ) -> list[Path]:
        normalized = self._validate_result(result)
        if normalized["status"] != "sat":
            return []
        primary_assignments = {
            int(index): value for index, value in normalized["assignments"].items()
        }
        assignment_sets = [primary_assignments]
        generator = normalized.get("generator")
        generator_hash_value = normalized.get("generator_hash", "")
        if isinstance(generator, dict):
            for model in generator.get("verified_models", ()):
                assignment_sets.append(
                    {int(index): value for index, value in model.items()}
                )
        with self._connect() as db:
            witnesses = db.execute(
                "SELECT witness_hash, input_hex, metadata_json FROM witnesses "
                "WHERE query_id = ? ORDER BY witness_hash",
                (query_id,),
            ).fetchall()
        loaded_query_ir = self._load_query_ir(query_id)
        if loaded_query_ir is None:
            return []
        query_input_offsets = (
            sorted(
                {
                    int(node["attrs"]["index"])
                    for node in loaded_query_ir[1].values()
                    if node.get("op") == "read"
                    and isinstance(node.get("attrs"), Mapping)
                    and isinstance(node["attrs"].get("index"), int)
                }
            )
            if loaded_query_ir is not None
            else []
        )

        replay_budget = 8
        try:
            replay_budget = max(
                0, min(64, int(os.environ.get("SYMCC_GENERATOR_REPLAY_SAMPLES", "8")))
            )
        except ValueError:
            replay_budget = 8
        query_ir = (
            loaded_query_ir if isinstance(generator, dict) and replay_budget else None
        )

        written: list[Path] = []
        seen_paths: set[Path] = set()
        for witness in witnesses:
            witness_bytes = bytearray.fromhex(str(witness["input_hex"]))
            for model_index, assignments in enumerate(assignment_sets):
                candidate = bytearray(witness_bytes)
                valid = True
                for index, value in assignments.items():
                    if index >= len(candidate):
                        valid = False
                        break
                    candidate[index] = value
                if not valid:
                    continue
                content = bytes(candidate)
                candidate_query_ir_verified = (
                    loaded_query_ir is not None
                    and self._candidate_satisfies_query(
                        loaded_query_ir[0],
                        loaded_query_ir[1],
                        content,
                    )
                )
                if not candidate_query_ir_verified:
                    continue
                candidate_hash = _digest(content)
                path = self.candidate_dir / candidate_hash[:2] / f"{candidate_hash}.bin"
                _atomic_write_exact(path, content)
                manifest = {
                    "schema": "symcc-query-candidate-v1",
                    "query_id": query_id,
                    "witness_hash": str(witness["witness_hash"]),
                    "candidate_sha256": candidate_hash,
                    "model_index": model_index,
                    "generator_hash": generator_hash_value,
                    "query_input_offsets": query_input_offsets,
                    "model_assignment_offsets": sorted(assignments),
                    "solver_verified": True,
                    "query_ir_verified": candidate_query_ir_verified,
                    "verified": False,
                }
                manifest_path = path.with_suffix(".json")
                _atomic_write_exact(
                    manifest_path, _canonical_json(manifest) + b"\n"
                )
                if path not in seen_paths:
                    written.append(path)
                    seen_paths.add(path)

                metadata = json.loads(str(witness["metadata_json"]))
                output_dir_raw = metadata.get("output_dir", "")
                if isinstance(output_dir_raw, str) and output_dir_raw:
                    output_dir = Path(output_dir_raw)
                    if output_dir.is_dir():
                        external = output_dir / (
                            f"async-{query_id[:16]}-{candidate_hash[:12]}"
                        )
                        _atomic_write_exact(external, content)
                        external_manifest = (
                            external.parent / f".{external.name}.query.json"
                        )
                        _atomic_write_exact(
                            external_manifest,
                            _canonical_json(manifest) + b"\n",
                        )
            if query_ir is None or not isinstance(generator, dict):
                continue
            roots, expressions = query_ir
            try:
                replayed, replay_metrics = sample_generator(
                    generator,
                    bytes(witness_bytes),
                    replay_budget,
                    lambda candidate: self._candidate_satisfies_query(
                        roots, expressions, candidate
                    ),
                )
            except ValueError:
                continue
            for replay_index, content in enumerate(replayed):
                candidate_hash = _digest(content)
                path = self.candidate_dir / candidate_hash[:2] / f"{candidate_hash}.bin"
                _atomic_write_exact(path, content)
                manifest_path = path.with_suffix(".json")
                replay_manifest = {
                    "schema": "symcc-query-candidate-v1",
                    "query_id": query_id,
                    "witness_hash": str(witness["witness_hash"]),
                    "candidate_sha256": candidate_hash,
                    "model_index": -1,
                    "generator_replay_index": replay_index,
                    "generator_hash": generator_hash_value,
                    "candidate_source": "generator-query-ir-replay",
                    "query_input_offsets": query_input_offsets,
                    "model_assignment_offsets": [],
                    "solver_verified": False,
                    "generator_replay_verified": True,
                    "query_ir_verified": True,
                    "generator_replay_metrics": replay_metrics,
                    "verified": False,
                }
                _atomic_write_exact(
                    manifest_path,
                    _canonical_json(replay_manifest) + b"\n",
                )
                if path not in seen_paths:
                    written.append(path)
                    seen_paths.add(path)

                metadata = json.loads(str(witness["metadata_json"]))
                output_dir_raw = metadata.get("output_dir", "")
                if isinstance(output_dir_raw, str) and output_dir_raw:
                    output_dir = Path(output_dir_raw)
                    if output_dir.is_dir():
                        external = output_dir / (
                            f"async-{query_id[:16]}-{candidate_hash[:12]}"
                        )
                        _atomic_write_exact(external, content)
                        external_manifest = (
                            external.parent / f".{external.name}.query.json"
                        )
                        _atomic_write_exact(
                            external_manifest,
                            _canonical_json(replay_manifest) + b"\n",
                        )
        return written

    def _load_query_ir(
        self,
        query_id: str,
        *,
        query_body: Mapping[str, Any] | None = None,
    ) -> tuple[list[str], dict[str, dict[str, Any]]] | None:
        if query_body is None:
            with self._connect() as db:
                query_row = db.execute(
                    "SELECT query_id, prefix_hash, target_hash, smt2_hash, "
                    "prefix_smt2_hash, target_smt2_hash FROM queries "
                    "WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
            if query_row is None:
                return None
            try:
                loaded_body = self._verified_query_body(query_row)
            except ValueError:
                return None
            query_body = loaded_body
        prefix = query_body.get("prefix", ())
        target = query_body.get("target")
        if not isinstance(prefix, list) or not isinstance(target, str) or not target:
            return None
        roots = [str(root) for root in prefix]
        roots.append(target)
        expressions: dict[str, dict[str, Any]] = {}
        pending = list(roots)
        with self._connect() as db:
            while pending:
                node_hash = pending.pop()
                if node_hash in expressions:
                    continue
                if len(expressions) >= _MAX_QUERY_IR_NODES:
                    return None
                if len(node_hash) != 64:
                    return None
                row = db.execute(
                    "SELECT body_json FROM expressions WHERE hash = ?",
                    (node_hash,),
                ).fetchone()
                if row is None:
                    return None
                try:
                    node = json.loads(
                        str(row["body_json"]),
                        object_pairs_hook=_object_without_duplicate_keys,
                        parse_constant=_reject_nonfinite_json,
                    )
                except (ValueError, TypeError, RecursionError):
                    return None
                if (
                    not isinstance(node, dict)
                    or node.get("schema") != NODE_SCHEMA
                    or _digest(_canonical_json(node)) != node_hash
                ):
                    return None
                children = node.get("children", ())
                if not isinstance(children, list):
                    return None
                expressions[node_hash] = node
                for child in children:
                    child_hash = str(child)
                    if child_hash not in expressions:
                        pending.append(child_hash)
        return roots, expressions

    def load_query_ir(
        self,
        query_id: str,
    ) -> tuple[list[str], dict[str, dict[str, Any]]] | None:
        """Return the validated reachable Query IR for solver adapters."""
        return self._load_query_ir(str(query_id))

    def query_input_offsets(self, query_id: str) -> tuple[int, ...]:
        loaded = self._load_query_ir(str(query_id))
        if loaded is None:
            return ()
        _roots, expressions = loaded
        offsets = {
            int(node["attrs"]["index"])
            for node in expressions.values()
            if node.get("op") == "read"
            and isinstance(node.get("attrs"), Mapping)
            and isinstance(node["attrs"].get("index"), int)
            and 0 <= int(node["attrs"]["index"]) <= (1 << 32) - 1
        }
        return tuple(sorted(offsets))

    def query_constraint_shape(self, query_id: str) -> dict[str, Any] | None:
        """Return the persisted target-shape selection record."""

        with self._connect() as db:
            row = db.execute(
                "SELECT shape_hash, context_hash, read_count, node_count, "
                "duplicate_rank, representative_query_id FROM query_shapes "
                "WHERE query_id = ?",
                (str(query_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "schema": CONSTRAINT_SHAPE_SCHEMA,
            "shape_hash": str(row["shape_hash"]),
            "context_hash": str(row["context_hash"]),
            "read_count": int(row["read_count"]),
            "node_count": int(row["node_count"]),
            "duplicate_rank": int(row["duplicate_rank"]),
            "representative_query_id": str(row["representative_query_id"]),
        }

    def validate_candidate(self, query_id: str, candidate: bytes) -> bool:
        loaded = self._load_query_ir(str(query_id))
        if loaded is None:
            return False
        roots, expressions = loaded
        return self._candidate_satisfies_query(roots, expressions, bytes(candidate))

    @staticmethod
    def _to_signed(value: int, bits: int) -> int:
        sign_bit = 1 << (bits - 1)
        return value - (1 << bits) if value & sign_bit else value

    @staticmethod
    def _evaluate_expression(
        node_hash: str,
        expressions: Mapping[str, Mapping[str, Any]],
        candidate: bytes,
        memo: dict[str, int],
        *,
        budget: list[int] | None = None,
    ) -> int | None:
        """Evaluate a Query IR DAG with an explicit post-order stack."""
        if node_hash in memo:
            return memo[node_hash]
        stack: list[tuple[str, bool]] = [(node_hash, False)]
        active: set[str] = set()
        while stack:
            current, expanded = stack.pop()
            if current in memo:
                active.discard(current)
                continue
            node = expressions.get(current)
            if node is None:
                return None
            children_raw = node.get("children", ())
            if not isinstance(children_raw, list):
                return None
            children = [str(child) for child in children_raw]
            if expanded:
                if any(child not in memo for child in children):
                    return None
                if budget is not None:
                    if not budget or budget[0] <= 0:
                        return None
                    budget[0] -= 1
                value = QueryStore._evaluate_expression_node(
                    current,
                    expressions,
                    candidate,
                    memo,
                )
                active.discard(current)
                if value is None:
                    return None
                continue
            if current in active:
                return None
            active.add(current)
            stack.append((current, True))
            for child in reversed(children):
                if child in active:
                    return None
                if child not in memo:
                    stack.append((child, False))
        return memo.get(node_hash)

    @staticmethod
    def _evaluate_expression_node(
        node_hash: str,
        expressions: Mapping[str, Mapping[str, Any]],
        candidate: bytes,
        memo: dict[str, int],
    ) -> int | None:
        """Evaluate one node after all of its children are memoized."""
        node = expressions.get(node_hash)
        if node is None:
            return None
        try:
            op = str(node.get("op", ""))
            bits = int(node.get("bits", 0))
        except (TypeError, ValueError):
            return None
        if bits < 1 or bits > _QUERY_EVAL_MAX_BITS:
            return None
        mask = (1 << bits) - 1
        children_raw = node.get("children", ())
        attrs = node.get("attrs", {})
        if not isinstance(children_raw, list) or not isinstance(attrs, Mapping):
            return None
        children = [str(child) for child in children_raw]

        def child(index: int) -> int | None:
            if index >= len(children):
                return None
            return memo.get(children[index])

        def child_bits(index: int) -> int | None:
            if index >= len(children):
                return None
            child_node = expressions.get(children[index])
            if child_node is None:
                return None
            try:
                value = int(child_node.get("bits", 0))
            except (TypeError, ValueError):
                return None
            return value if 1 <= value <= _QUERY_EVAL_MAX_BITS else None

        values = [child(index) for index in range(len(children))]
        if any(value is None for value in values):
            return None
        operands = [int(value) for value in values if value is not None]

        try:
            if op == "bool":
                result = 1 if bool(attrs.get("value", False)) else 0
            elif op == "constant":
                result = int(str(attrs.get("value_hex", "0")), 16)
            elif op == "read":
                if bits > 8:
                    return None
                offset = int(attrs.get("index", -1))
                if offset < 0 or offset >= len(candidate):
                    return None
                result = candidate[offset]
            elif op == "concat":
                if len(operands) != 2:
                    return None
                right_bits = child_bits(1)
                if right_bits is None:
                    return None
                result = (operands[0] << right_bits) | operands[1]
            elif op == "extract":
                if len(operands) != 1:
                    return None
                index = int(attrs.get("index", -1))
                if index < 0:
                    return None
                result = operands[0] >> index
            elif op in {"zext", "sext"}:
                if len(operands) != 1:
                    return None
                source_bits = child_bits(0)
                if source_bits is None or source_bits > bits:
                    return None
                result = operands[0]
                if op == "sext" and ((result >> (source_bits - 1)) & 1):
                    result |= mask ^ ((1 << source_bits) - 1)
            elif op == "add":
                if len(operands) != 2:
                    return None
                result = operands[0] + operands[1]
            elif op == "sub":
                if len(operands) != 2:
                    return None
                result = operands[0] - operands[1]
            elif op == "mul":
                if len(operands) != 2:
                    return None
                result = operands[0] * operands[1]
            elif op in {"udiv", "urem", "sdiv", "srem"}:
                if len(operands) != 2 or operands[1] == 0:
                    return None
                if op == "udiv":
                    result = operands[0] // operands[1]
                elif op == "urem":
                    result = operands[0] % operands[1]
                else:
                    width = child_bits(0)
                    if width is None:
                        return None
                    lhs = QueryStore._to_signed(operands[0], width)
                    rhs = QueryStore._to_signed(operands[1], width)
                    if rhs == 0:
                        return None
                    magnitude = abs(lhs) // abs(rhs)
                    if op == "sdiv":
                        result = -magnitude if (lhs < 0) ^ (rhs < 0) else magnitude
                    else:
                        remainder = abs(lhs) % abs(rhs)
                        result = -remainder if lhs < 0 else remainder
            elif op == "neg":
                if len(operands) != 1:
                    return None
                result = -operands[0]
            elif op == "not":
                if len(operands) != 1:
                    return None
                result = ~operands[0]
            elif op == "and":
                if not operands:
                    return None
                result = operands[0]
                for operand in operands[1:]:
                    result &= operand
            elif op == "or":
                if not operands:
                    return None
                result = operands[0]
                for operand in operands[1:]:
                    result |= operand
            elif op == "xor":
                if not operands:
                    return None
                result = operands[0]
                for operand in operands[1:]:
                    result ^= operand
            elif op in {"shl", "lshr", "ashr"}:
                if len(operands) != 2:
                    return None
                shift = operands[1]
                if shift >= bits:
                    if op == "ashr" and ((operands[0] >> (bits - 1)) & 1):
                        result = mask
                    else:
                        result = 0
                elif op == "shl":
                    result = operands[0] << shift
                elif op == "lshr":
                    result = operands[0] >> shift
                else:
                    result = QueryStore._to_signed(operands[0], bits) >> shift
            elif op == "equal":
                if len(operands) != 2:
                    return None
                result = int(operands[0] == operands[1])
            elif op == "distinct":
                if len(operands) != 2:
                    return None
                result = int(operands[0] != operands[1])
            elif op in {"ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge"}:
                if len(operands) != 2:
                    return None
                if op[0] == "s":
                    width = child_bits(0)
                    if width is None:
                        return None
                    lhs = QueryStore._to_signed(operands[0], width)
                    rhs = QueryStore._to_signed(operands[1], width)
                else:
                    lhs, rhs = operands[0], operands[1]
                relation = op[1:]
                result = int(
                    {
                        "lt": lhs < rhs,
                        "le": lhs <= rhs,
                        "gt": lhs > rhs,
                        "ge": lhs >= rhs,
                    }[relation]
                )
            elif op == "land":
                if not operands:
                    return None
                result = int(all(operand != 0 for operand in operands))
            elif op == "lor":
                if not operands:
                    return None
                result = int(any(operand != 0 for operand in operands))
            elif op == "lnot":
                if len(operands) != 1:
                    return None
                result = int(operands[0] == 0)
            elif op == "ite":
                if len(operands) != 3:
                    return None
                result = operands[1] if operands[0] != 0 else operands[2]
            elif op in {"rol", "ror"}:
                if len(operands) != 2:
                    return None
                shift = operands[1] % bits
                if shift == 0:
                    result = operands[0]
                elif op == "rol":
                    result = (operands[0] << shift) | (operands[0] >> (bits - shift))
                else:
                    result = (operands[0] >> shift) | (operands[0] << (bits - shift))
            else:
                return None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

        result &= mask
        memo[node_hash] = result
        return result

    @staticmethod
    def _candidate_satisfies_query(
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        candidate: bytes,
        *,
        node_budget: list[int] | None = None,
    ) -> bool:
        memo: dict[str, int] = {}
        for root in roots:
            value = QueryStore._evaluate_expression(
                root,
                expressions,
                candidate,
                memo,
                budget=node_budget,
            )
            if value is None or value == 0:
                return False
        return True

    def _load_query_context(
        self,
        query_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT query_id, status, prefix_hash, target_hash, smt2_hash, "
                "prefix_smt2_hash, target_smt2_hash, metadata_json "
                "FROM queries WHERE query_id = ?",
                (query_id,),
            ).fetchone()
            result_row = db.execute(
                "SELECT result_json FROM results WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            query_body = self._verified_query_body(row)
        except ValueError:
            return None
        prefix = query_body.get("prefix", ())
        target = query_body.get("target")
        if not isinstance(prefix, list) or not isinstance(target, str) or not target:
            return None
        loaded = self._load_query_ir(query_id, query_body=query_body)
        if loaded is None:
            return None
        roots, expressions = loaded
        if roots != [*(str(root) for root in prefix), target]:
            return None
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (ValueError, TypeError):
            metadata = {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        result = None
        if result_row is not None:
            try:
                result = self._validate_result(
                    json.loads(str(result_row["result_json"]))
                )
            except (ValueError, TypeError):
                result = None
        return {
            "query_id": query_id,
            "prefix": [str(root) for root in prefix],
            "target": target,
            "roots": roots,
            "expressions": expressions,
            "status": str(row["status"]),
            "prefix_hash": str(row["prefix_hash"]),
            "target_hash": str(row["target_hash"]),
            "metadata": dict(metadata),
            "result": result,
        }

    @staticmethod
    def _patched_candidate(
        input_hex: str,
        assignments: Mapping[str, Any],
    ) -> bytes | None:
        try:
            candidate = bytearray.fromhex(input_hex)
        except ValueError:
            return None
        for raw_offset, raw_value in assignments.items():
            try:
                offset = int(raw_offset)
                value = int(raw_value)
            except (TypeError, ValueError):
                return None
            if offset < 0 or offset >= len(candidate) or value < 0 or value > 255:
                return None
            candidate[offset] = value
        return bytes(candidate)

    @staticmethod
    def _normalize_schedule_artifact(
        artifact: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if artifact.get("schema") != SCHEDULE_CONSTRAINT_SCHEMA:
            return None
        replay_prefixes_raw = artifact.get("replay_prefixes", ())
        replay_prefixes: list[list[int]] = []
        if isinstance(replay_prefixes_raw, Sequence) and not isinstance(
            replay_prefixes_raw, (str, bytes)
        ):
            for raw_prefix in replay_prefixes_raw[:256]:
                prefix = _normalize_prefix(raw_prefix, max_len=256)
                if prefix:
                    replay_prefixes.append(prefix)
        query_ids_raw = artifact.get("query_ids", ())
        query_ids: list[str] = []
        if isinstance(artifact.get("query_id"), str):
            query_ids.append(str(artifact["query_id"]))
        if isinstance(query_ids_raw, Sequence) and not isinstance(
            query_ids_raw, (str, bytes)
        ):
            query_ids.extend(
                str(item) for item in query_ids_raw if isinstance(item, str)
            )
        query_ids = [
            query_id.lower()
            for query_id in dict.fromkeys(query_ids)
            if len(query_id) == 64
            and all(ch in "0123456789abcdef" for ch in query_id.lower())
        ][:256]
        return {
            "schema": SCHEDULE_CONSTRAINT_SCHEMA,
            "input_id": str(artifact.get("input_id", ""))[:256],
            "target_branch": _nonnegative_int(artifact.get("target_branch", 0)),
            "trace_digest": _hex_digest(artifact.get("trace_digest"))
            or _digest(_canonical_json(artifact)),
            "current_prefix": _normalize_prefix(
                artifact.get("current_prefix", ()), max_len=256
            ),
            "replay_prefixes": replay_prefixes,
            "conflict_count": _nonnegative_int(artifact.get("conflict_count", 0)),
            "sync_conflict_count": _nonnegative_int(
                artifact.get("sync_conflict_count", 0)
            ),
            "memory_conflict_count": _nonnegative_int(
                artifact.get("memory_conflict_count", 0)
            ),
            "provenance_counts": (
                _normalize_json(artifact.get("provenance_counts", {}))
                if isinstance(artifact.get("provenance_counts", {}), dict)
                else {}
            ),
            "event_count": _nonnegative_int(artifact.get("event_count", 0)),
            "schedulable_count": _nonnegative_int(artifact.get("schedulable_count", 0)),
            "query_ids": query_ids,
        }

    def _query_ids_for_schedule(
        self,
        schedule: Mapping[str, Any],
        *,
        max_queries: int,
        max_scan: int,
    ) -> list[tuple[str, str]]:
        explicit = [
            str(query_id).lower()
            for query_id in schedule.get("query_ids", ())
            if isinstance(query_id, str)
        ]
        if explicit:
            with self._connect() as db:
                rows = [
                    db.execute(
                        "SELECT query_id FROM queries WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                    for query_id in explicit[:max_queries]
                ]
            return [
                (str(row["query_id"]), "query_id") for row in rows if row is not None
            ]

        target_branch = _nonnegative_int(schedule.get("target_branch", 0))
        matches: list[tuple[str, str]] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT query_id, metadata_json FROM queries "
                "ORDER BY updated DESC, priority DESC, query_id LIMIT ?",
                (max(1, int(max_scan)),),
            ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"]))
            except (ValueError, TypeError):
                metadata = {}
            if not isinstance(metadata, Mapping):
                metadata = {}
            metadata_targets = (
                metadata.get("site"),
                metadata.get("target_branch"),
                metadata.get("target_site"),
                metadata.get("branch"),
            )
            matched_by = ""
            if target_branch > 0:
                if any(
                    _nonnegative_int(value, -1) == target_branch
                    for value in metadata_targets
                ):
                    matched_by = "target_branch"
            elif not metadata_targets:
                matched_by = "recent"
            if matched_by:
                matches.append((str(row["query_id"]), matched_by))
                if len(matches) >= max_queries:
                    break
        return matches

    def _joint_validation_for_query(
        self,
        schedule: Mapping[str, Any],
        query_id: str,
        matched_by: str,
        *,
        max_witnesses: int,
    ) -> dict[str, Any] | None:
        context = self._load_query_context(query_id)
        if context is None:
            return None
        prefix_roots = context["prefix"]
        roots = context["roots"]
        expressions = context["expressions"]
        result = context.get("result")
        prefix_observed = False
        target_satisfied = False
        witnesses_checked = 0
        models_checked = 0
        source = "query-ir-witness"
        reason = ""
        with self._connect() as db:
            witnesses = db.execute(
                "SELECT input_hex FROM witnesses WHERE query_id = ? "
                "ORDER BY witness_hash LIMIT ?",
                (query_id, max(0, int(max_witnesses))),
            ).fetchall()
        for witness in witnesses:
            input_hex = str(witness["input_hex"])
            try:
                candidate = bytes.fromhex(input_hex)
            except ValueError:
                continue
            witnesses_checked += 1
            if self._candidate_satisfies_query(prefix_roots, expressions, candidate):
                prefix_observed = True
            if self._candidate_satisfies_query(roots, expressions, candidate):
                target_satisfied = True
                break

        path_status = "unknown"
        if isinstance(result, Mapping):
            result_status = str(result.get("status", "unknown"))
            if result_status == "unsat":
                path_status = "unsat"
                source = "solver-result"
                reason = "solver returned UNSAT for prefix+target"
            elif result_status == "sat":
                source = "solver-result+query-ir"
                for _model_index, assignments in self._result_assignment_sets(result):
                    models_checked += 1
                    for witness in witnesses:
                        candidate = self._patched_candidate(
                            str(witness["input_hex"]), assignments
                        )
                        if candidate is None:
                            continue
                        if self._candidate_satisfies_query(
                            prefix_roots, expressions, candidate
                        ):
                            prefix_observed = True
                        if self._candidate_satisfies_query(
                            roots, expressions, candidate
                        ):
                            target_satisfied = True
                            break
                    if target_satisfied:
                        break
                if target_satisfied:
                    path_status = "sat"
                    reason = "solver assignment satisfies Query IR roots"
                else:
                    path_status = "unknown"
                    reason = "SAT result was not verified by bounded evaluator"
            else:
                reason = f"solver status is {result_status}"
        elif target_satisfied:
            path_status = "sat"
            reason = "witness already satisfies Query IR roots"
        elif prefix_observed:
            reason = "witness satisfies prefix roots but not target root"
        else:
            reason = "no checked witness satisfied prefix roots"

        replay_prefixes = schedule.get("replay_prefixes", ())
        if not replay_prefixes:
            joint_status = "schedule_no_replay"
        elif path_status == "sat":
            joint_status = "ready"
        elif path_status == "unsat":
            joint_status = "path_unsat"
        elif prefix_observed:
            joint_status = "path_target_unsolved"
        else:
            joint_status = "path_unknown"

        body = {
            "schema": JOINT_SCHEDULE_QUERY_SCHEMA,
            "schedule": {
                "schema": SCHEDULE_CONSTRAINT_SCHEMA,
                "trace_digest": str(schedule["trace_digest"]),
                "input_id": str(schedule.get("input_id", "")),
                "target_branch": _nonnegative_int(schedule.get("target_branch", 0)),
                "current_prefix": list(schedule.get("current_prefix", ())),
                "replay_prefixes": [
                    list(prefix)
                    for prefix in list(schedule.get("replay_prefixes", ()))[:256]
                ],
                "conflict_count": _nonnegative_int(schedule.get("conflict_count", 0)),
                "sync_conflict_count": _nonnegative_int(
                    schedule.get("sync_conflict_count", 0)
                ),
                "memory_conflict_count": _nonnegative_int(
                    schedule.get("memory_conflict_count", 0)
                ),
                "provenance_counts": dict(schedule.get("provenance_counts", {})),
            },
            "query": {
                "query_id": query_id,
                "status": str(context["status"]),
                "prefix_hash": str(context["prefix_hash"]),
                "target_hash": str(context["target_hash"]),
                "metadata": dict(context["metadata"]),
                "matched_by": matched_by,
            },
            "path_validation": {
                "status": path_status,
                "source": source,
                "prefix_observed": prefix_observed,
                "target_satisfied": target_satisfied,
                "witnesses_checked": witnesses_checked,
                "models_checked": models_checked,
                "reason": reason[:512],
            },
            "joint_status": joint_status,
        }
        validation_hash = _digest(_canonical_json(body))
        body["validation_hash"] = validation_hash
        return body

    def _store_joint_schedule_validation(
        self,
        record: Mapping[str, Any],
        *,
        output_path: str | os.PathLike[str] | None = None,
    ) -> bool:
        try:
            validation_hash = str(record["validation_hash"])
            schedule = record["schedule"]
            query = record["query"]
            path_validation = record["path_validation"]
        except (KeyError, TypeError):
            return False
        payload = _canonical_json(record).decode("ascii")
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO joint_schedule_validations("
                "validation_hash, schedule_trace_digest, query_id, "
                "target_branch, joint_status, path_status, artifact_json, "
                "created) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    validation_hash,
                    str(schedule.get("trace_digest", "")),
                    str(query.get("query_id", "")),
                    _nonnegative_int(schedule.get("target_branch", 0)),
                    str(record.get("joint_status", "unknown")),
                    str(path_validation.get("status", "unknown")),
                    payload,
                    now,
                ),
            )
        inserted = cursor.rowcount == 1
        path = self.joint_schedule_dir / validation_hash[:2] / f"{validation_hash}.json"
        if inserted and not path.exists():
            _atomic_write(path, payload.encode("ascii") + b"\n")
        if inserted and output_path:
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("a", encoding="ascii") as stream:
                stream.write(payload)
                stream.write("\n")
        return inserted

    def validate_schedule_artifact(
        self,
        artifact: Mapping[str, Any],
        *,
        output_path: str | os.PathLike[str] | None = None,
        max_queries: int = 64,
        max_scan: int = 1024,
        max_witnesses: int = 16,
    ) -> list[dict[str, Any]]:
        schedule = self._normalize_schedule_artifact(artifact)
        if schedule is None:
            return []
        query_ids = self._query_ids_for_schedule(
            schedule,
            max_queries=max(1, int(max_queries)),
            max_scan=max(1, int(max_scan)),
        )
        records: list[dict[str, Any]] = []
        for query_id, matched_by in query_ids:
            record = self._joint_validation_for_query(
                schedule,
                query_id,
                matched_by,
                max_witnesses=max_witnesses,
            )
            if record is None:
                continue
            self._store_joint_schedule_validation(
                record,
                output_path=output_path,
            )
            records.append(record)
        return records

    def validate_schedule_constraints_file(
        self,
        path: str | os.PathLike[str],
        *,
        output_path: str | os.PathLike[str] | None = None,
        max_artifacts: int = 4096,
        max_queries: int = 64,
        max_scan: int = 1024,
        max_witnesses: int = 16,
    ) -> int:
        source = Path(path)
        if not source.is_file():
            return 0
        written = 0
        try:
            with source.open("r", encoding="utf-8", errors="replace") as stream:
                for index, line in enumerate(stream):
                    if index >= max(0, int(max_artifacts)):
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        artifact = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(artifact, Mapping):
                        continue
                    before = self.stats().get("joint_schedule_validations", 0)
                    self.validate_schedule_artifact(
                        artifact,
                        output_path=output_path,
                        max_queries=max_queries,
                        max_scan=max_scan,
                        max_witnesses=max_witnesses,
                    )
                    after = self.stats().get("joint_schedule_validations", 0)
                    written += max(0, after - before)
        except OSError:
            return written
        return written

    def solve_joint_schedule_smt(
        self,
        schedule_artifact: Mapping[str, Any],
        query_id: str,
        *,
        query_index: int = 0,
        output_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Solve one Query IR path and one schedule/rf query jointly."""
        context = self._load_query_context(query_id)
        if context is None:
            raise KeyError(f"unknown Query IR query {query_id}")
        with self._connect() as db:
            row = db.execute(
                "SELECT smt2_hash FROM queries WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown Query IR query {query_id}")
        smt2_hash = str(row["smt2_hash"])
        with self._connect() as db:
            store, _path = self._artifact_store_and_path(db, smt2_hash)
            try:
                smt2_snapshot = store.snapshot(smt2_hash, retain_content=True)
            except (OSError, ValueError) as error:
                raise ValueError(
                    "stored Query IR SMT failed stable validation"
                ) from error
        if smt2_snapshot.content is None or smt2_snapshot.sha256 != smt2_hash:
            raise ValueError("stored Query IR SMT digest mismatch")
        try:
            query_smt2 = smt2_snapshot.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("stored Query IR SMT is not UTF-8") from error

        expressions = context["expressions"]
        byte_offsets = sorted(
            {
                int(node["attrs"]["index"])
                for node in expressions.values()
                if node.get("op") == "read" and isinstance(node.get("attrs"), Mapping)
            }
        )
        result = solve_joint_path_schedule_query(
            schedule_artifact,
            query_smt2,
            query_id=query_id,
            query_index=query_index,
            query_byte_offsets=byte_offsets,
        )
        if result.get("schema") != JOINT_PATH_SCHEDULE_SCHEMA:
            raise ValueError("joint solver returned an unexpected schema")
        if not verify_joint_path_schedule_result(
            schedule_artifact,
            query_smt2,
            result,
        ):
            raise ValueError("joint path/schedule result failed verification")

        query_ir_verified = False
        if result["status"] == "sat":
            input_bytes = result.get("model", {}).get("input_bytes", {})
            if isinstance(input_bytes, Mapping):
                size = max(byte_offsets, default=-1) + 1
                candidate = bytearray(size)
                complete = True
                for offset in byte_offsets:
                    raw_value = input_bytes.get(str(offset))
                    if raw_value is None:
                        complete = False
                        break
                    value = int(raw_value)
                    if value < 0 or value > 255:
                        complete = False
                        break
                    candidate[offset] = value
                query_ir_verified = complete and self._candidate_satisfies_query(
                    context["roots"],
                    expressions,
                    bytes(candidate),
                )
        result["query_ir_model_verified"] = query_ir_verified
        result["query_smt2_object_hash"] = smt2_hash
        result.pop("result_sha256", None)
        result_hash = _digest(_canonical_json(result))
        result["result_sha256"] = result_hash

        payload = _canonical_json(result).decode("ascii")
        with self._connect() as db:
            inserted = (
                db.execute(
                    "INSERT OR IGNORE INTO joint_smt_solves("
                    "result_hash, schedule_trace_digest, query_id, query_index, "
                    "status, query_ir_model_verified, artifact_json, created"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result_hash,
                        str(result.get("schedule_trace_digest", "")),
                        query_id,
                        int(query_index),
                        str(result.get("status", "unknown")),
                        int(query_ir_verified),
                        payload,
                        time.time(),
                    ),
                ).rowcount
                == 1
            )
        path = (
            self.joint_schedule_dir / result_hash[:2] / f"{result_hash}.joint-smt.json"
        )
        if inserted and not path.exists():
            _atomic_write(path, payload.encode("ascii") + b"\n")
        if inserted and output_path:
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("a", encoding="ascii") as stream:
                stream.write(payload)
                stream.write("\n")
        return result

    def materialize_partial_candidates(
        self,
        query_id: str,
        *,
        limit: int | None = None,
    ) -> list[Path]:
        if not _env_flag("SYMCC_PARTIAL_SOLUTION_CACHE", True):
            return []
        if limit is None:
            limit = _env_int("SYMCC_PARTIAL_SOLUTION_LIMIT", 8, 0, 256)
        if limit <= 0:
            return []
        loaded = self._load_query_ir(query_id)
        if loaded is None:
            return []
        roots, expressions = loaded
        scan_limit = _env_int("SYMCC_PARTIAL_SOLUTION_SCAN", 512, 1, 65536)
        with self._connect() as db:
            witnesses = db.execute(
                "SELECT witness_hash, input_hex, metadata_json FROM witnesses "
                "WHERE query_id = ? ORDER BY witness_hash",
                (query_id,),
            ).fetchall()
            partials = db.execute(
                "SELECT ps.solution_hash, ps.source_query_id, "
                "ps.assignment_json, ps.model_index, ps.generator_hash, "
                "ps.provenance, ps.proof_json, ps.source_verified, "
                "(SELECT COUNT(*) FROM partial_solution_literals psl "
                "JOIN query_literals ql "
                "ON ql.query_id = ? "
                "AND ql.literal_hash = psl.literal_hash "
                "WHERE psl.solution_hash = ps.solution_hash) "
                "AS literal_overlap, "
                "(SELECT COUNT(*) FROM partial_solution_clauses psc "
                "JOIN query_clauses qc "
                "ON qc.query_id = ? "
                "AND qc.clause_hash = psc.clause_hash "
                "WHERE psc.solution_hash = ps.solution_hash) "
                "AS overlap FROM partial_solutions ps "
                "WHERE ps.source_query_id != ? "
                "ORDER BY literal_overlap DESC, overlap DESC, "
                "ps.created DESC, ps.solution_hash "
                "LIMIT ?",
                (query_id, query_id, query_id, scan_limit),
            ).fetchall()

        written: list[Path] = []
        seen_paths: set[Path] = set()
        records: list[tuple[str, str, str, str, float]] = []
        now = time.time()
        for witness in witnesses:
            try:
                base = bytearray.fromhex(str(witness["input_hex"]))
                metadata = json.loads(str(witness["metadata_json"]))
            except (ValueError, TypeError):
                continue
            if not isinstance(metadata, Mapping):
                metadata = {}
            for partial in partials:
                if len(written) >= limit:
                    break
                try:
                    assignments = json.loads(str(partial["assignment_json"]))
                except (ValueError, TypeError):
                    continue
                if not isinstance(assignments, Mapping):
                    continue
                candidate = bytearray(base)
                valid = True
                for raw_offset, raw_value in assignments.items():
                    try:
                        offset = int(raw_offset)
                        value = int(raw_value)
                    except (TypeError, ValueError):
                        valid = False
                        break
                    if (
                        offset < 0
                        or offset >= len(candidate)
                        or value < 0
                        or value > 255
                    ):
                        valid = False
                        break
                    candidate[offset] = value
                if not valid:
                    continue
                content = bytes(candidate)
                if not self._candidate_satisfies_query(roots, expressions, content):
                    continue
                candidate_hash = _digest(content)
                path = self.candidate_dir / candidate_hash[:2] / f"{candidate_hash}.bin"
                _atomic_write_exact(path, content)
                solution_hash = str(partial["solution_hash"])
                manifest = {
                    "schema": "symcc-query-candidate-v1",
                    "query_id": query_id,
                    "witness_hash": str(witness["witness_hash"]),
                    "candidate_sha256": candidate_hash,
                    "model_index": -1,
                    "generator_hash": "",
                    "solver_verified": False,
                    "partial_solution_hash": solution_hash,
                    "partial_solution_source_query_id": str(partial["source_query_id"]),
                    "partial_solution_model_index": int(partial["model_index"]),
                    "partial_solution_generator_hash": str(partial["generator_hash"]),
                    "partial_solution_provenance": str(partial["provenance"]),
                    "partial_solution_source_verified": bool(
                        partial["source_verified"]
                    ),
                    "partial_solution_proof_sha256": _digest(
                        str(partial["proof_json"]).encode("ascii")
                    ),
                    "partial_solution_literal_overlap": int(partial["literal_overlap"]),
                    "partial_solution_clause_overlap": int(partial["overlap"]),
                    "partial_solution_verified": True,
                    "query_ir_verified": True,
                    "verified": False,
                }
                manifest_path = path.with_suffix(f".partial-{solution_hash[:12]}.json")
                _atomic_write_exact(
                    manifest_path, _canonical_json(manifest) + b"\n"
                )
                records.append(
                    (
                        query_id,
                        str(witness["witness_hash"]),
                        solution_hash,
                        candidate_hash,
                        now,
                    )
                )
                if path not in seen_paths:
                    written.append(path)
                    seen_paths.add(path)

                output_dir_raw = metadata.get("output_dir", "")
                if isinstance(output_dir_raw, str) and output_dir_raw:
                    output_dir = Path(output_dir_raw)
                    if output_dir.is_dir():
                        external = output_dir / (
                            f"async-partial-{query_id[:16]}-{candidate_hash[:12]}"
                        )
                        _atomic_write_exact(external, content)
            if len(written) >= limit:
                break
        if records:
            with self._connect() as db:
                db.executemany(
                    "INSERT OR IGNORE INTO partial_solution_candidates("
                    "query_id, witness_hash, solution_hash, candidate_hash, "
                    "created) VALUES(?, ?, ?, ?, ?)",
                    records,
                )
        return written

    def materialize_partial_candidates_for_pending(
        self,
        *,
        max_queries: int | None = None,
    ) -> list[Path]:
        if not _env_flag("SYMCC_PARTIAL_SOLUTION_CACHE", True):
            return []
        if max_queries is None:
            max_queries = _env_int(
                "SYMCC_PARTIAL_SOLUTION_PENDING_QUERIES", 64, 0, 4096
            )
        if max_queries <= 0:
            return []
        with self._connect() as db:
            query_ids = [
                str(row["query_id"])
                for row in db.execute(
                    "SELECT query_id FROM queries WHERE status = 'pending' "
                    "ORDER BY priority DESC, updated DESC, query_id LIMIT ?",
                    (max_queries,),
                )
            ]
        written: list[Path] = []
        for pending_query_id in query_ids:
            written.extend(self.materialize_partial_candidates(pending_query_id))
        return written

    def stats(self) -> dict[str, int]:
        with self._connect() as db:
            result = {
                "expressions": int(
                    db.execute("SELECT COUNT(*) FROM expressions").fetchone()[0]
                ),
                "prefix_nodes": int(
                    db.execute("SELECT COUNT(*) FROM prefix_nodes").fetchone()[0]
                ),
                "queries": int(
                    db.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
                ),
                "query_shape_classes": int(
                    db.execute(
                        "SELECT COUNT(*) FROM (SELECT 1 FROM query_shapes "
                        "GROUP BY context_hash, shape_hash)"
                    ).fetchone()[0]
                ),
                "query_shape_duplicates": int(
                    db.execute(
                        "SELECT COUNT(*) FROM query_shapes WHERE duplicate_rank > 1"
                    ).fetchone()[0]
                ),
                "query_shape_max_class": int(
                    db.execute(
                        "SELECT COALESCE(MAX(duplicate_rank), 0) FROM query_shapes"
                    ).fetchone()[0]
                ),
                "witnesses": int(
                    db.execute("SELECT COUNT(*) FROM witnesses").fetchone()[0]
                ),
                "pending": int(
                    db.execute(
                        "SELECT COUNT(*) FROM queries WHERE status = 'pending'"
                    ).fetchone()[0]
                ),
                "leased": int(
                    db.execute(
                        "SELECT COUNT(*) FROM queries WHERE status = 'leased'"
                    ).fetchone()[0]
                ),
                "done": int(
                    db.execute(
                        "SELECT COUNT(*) FROM queries WHERE status = 'done'"
                    ).fetchone()[0]
                ),
                "results": int(
                    db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
                ),
                "result_publications_pending": int(
                    db.execute(
                        "SELECT COUNT(*) FROM result_publications "
                        "WHERE published = 0"
                    ).fetchone()[0]
                ),
                "result_publications_active": int(
                    db.execute(
                        "SELECT COUNT(*) FROM result_publications "
                        "WHERE published = 0 AND dead_letter = 0"
                    ).fetchone()[0]
                ),
                "result_publications_deferred": int(
                    db.execute(
                        "SELECT COUNT(*) FROM result_publications "
                        "WHERE published = 0 AND dead_letter = 0 "
                        "AND next_retry > ?",
                        (time.time(),),
                    ).fetchone()[0]
                ),
                "result_publications_dead_lettered": int(
                    db.execute(
                        "SELECT COUNT(*) FROM result_publications "
                        "WHERE published = 0 AND dead_letter = 1"
                    ).fetchone()[0]
                ),
                "result_publication_oldest_age_seconds": max(
                    0.0,
                    time.time() - float(
                        db.execute(
                            "SELECT COALESCE(MIN(created), ?) "
                            "FROM result_publications "
                            "WHERE published = 0 AND dead_letter = 0",
                            (time.time(),),
                        ).fetchone()[0]
                    ),
                ),
                "result_publications_completed": int(
                    db.execute(
                        "SELECT COUNT(*) FROM result_publications "
                        "WHERE published = 1"
                    ).fetchone()[0]
                ),
                "result_publication_retries": int(
                    db.execute(
                        "SELECT COALESCE(SUM(attempts), 0) "
                        "FROM result_publications"
                    ).fetchone()[0]
                ),
                "generators": int(
                    db.execute("SELECT COUNT(*) FROM generators").fetchone()[0]
                ),
                "generator_models": sum(
                    len(json.loads(str(row[0])).get("verified_models", ()))
                    for row in db.execute("SELECT generator_json FROM generators")
                ),
                "portfolio_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE '%\"portfolio\"%'"
                    ).fetchone()[0]
                ),
                "portfolio_disagreements": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE '%\"disagreement\":true%'"
                    ).fetchone()[0]
                ),
                "portfolio_cancel_requests": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE '%\"cancel_requested\":true%'"
                    ).fetchone()[0]
                ),
                "portfolio_incomplete_consensus": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE '%\"consensus_complete\":false%'"
                    ).fetchone()[0]
                ),
                "portfolio_cancelled_attempts": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("portfolio", {})
                        .get("cancelled_attempts", 0)
                    )
                    for row in db.execute("SELECT result_json FROM results")
                ),
                "cross_worker_context_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_context_protocol":'
                        '"smtlib-cross-worker-cas-push-pop-v1"%\''
                    ).fetchone()[0]
                ),
                "cross_worker_context_exact_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_shared_context_exact_hit\":true%'"
                    ).fetchone()[0]
                ),
                "cross_worker_context_parent_reuses": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_parent_context_reused\":true%'"
                    ).fetchone()[0]
                ),
                "cross_worker_context_quota_timeouts": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_shared_context_materialization":'
                        '"quota-timeout"%\''
                    ).fetchone()[0]
                ),
                "proof_authorized_unsat_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_unsat_proof_verified\":true%'"
                    ).fetchone()[0]
                ),
                "proof_receipt_reuses": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_unsat_proof_reused\":true%'"
                    ).fetchone()[0]
                ),
                "incremental_sat_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_incremental_proof_protocol":'
                        '"symcc-qfbv-incremental-lrup-clause-v1"%\''
                    ).fetchone()[0]
                ),
                "incremental_sat_verified_unsat": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_incremental_proof_verified\":true%'"
                    ).fetchone()[0]
                ),
                "incremental_sat_proof_fragments_created": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_incremental_proof_created\":true%'"
                    ).fetchone()[0]
                ),
                "incremental_sat_import_candidates": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_incremental_import_candidates", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_incremental_proof_protocol\"%'"
                    )
                ),
                "incremental_sat_imported_clauses": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_incremental_imported_clauses", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_incremental_proof_protocol\"%'"
                    )
                ),
                "incremental_sat_checker_elapsed_us": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_incremental_proof_checker_elapsed_us", 0
                        )
                    )
                    + int(
                        json.loads(str(row[0])).get(
                            "store_incremental_import_checker_elapsed_us", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_incremental_proof_protocol\"%'"
                    )
                ),
                "partition_execution_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_partition_execution_protocol":'
                        '"symcc-qfbv-proof-aware-execution-v1"%\''
                    ).fetchone()[0]
                ),
                "partition_execution_sat": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_partition_execution_result\":\"sat\"%'"
                    ).fetchone()[0]
                ),
                "partition_execution_unsat": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_partition_execution_result\":\"unsat\"%'"
                    ).fetchone()[0]
                ),
                "partition_execution_cubes": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_partition_cube_count", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_partition_execution_protocol\"%'"
                    )
                ),
                "partition_execution_completed_cubes": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_partition_completed_cubes", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_partition_execution_protocol\"%'"
                    )
                ),
                "online_cubing_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_online_cubing_protocol":'
                        '"symcc-qfbv-online-cubing-v1"%\''
                    ).fetchone()[0]
                ),
                "online_cubing_static": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_decision\":%\"arm\":\"static\"%'"
                    ).fetchone()[0]
                ),
                "online_cubing_activity": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_decision\":%\"arm\":\"activity\"%'"
                    ).fetchone()[0]
                ),
                "online_cubing_cost": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_decision\":%\"arm\":\"cost\"%'"
                    ).fetchone()[0]
                ),
                "online_cubing_prerun_elapsed_us": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_online_cubing_outcome", {})
                        .get("prerun_elapsed_us", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_protocol\"%'"
                    )
                ),
                "online_cubing_configured_cpu_budget_ms": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_online_cubing_outcome", {})
                        .get("configured_cpu_budget_ms", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_protocol\"%'"
                    )
                ),
                "online_cubing_effective_cpu_budget_ms": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_online_cubing_outcome", {})
                        .get("effective_cpu_budget_ms", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_online_cubing_protocol\"%'"
                    )
                ),
                "lidrup_wire_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_lidrup_wire_protocol":'
                        '"symcc-qfbv-lidrup-wire-v1"%\''
                    ).fetchone()[0]
                ),
                "lidrup_wire_verified_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_lidrup_verified\":true%'"
                    ).fetchone()[0]
                ),
                "lidrup_wire_artifacts_created": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_lidrup_created\":true%'"
                    ).fetchone()[0]
                ),
                "lidrup_wire_learned_clauses": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_lidrup_learned_clauses", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_lidrup_wire_protocol\"%'"
                    )
                ),
                "lidrup_wire_checker_elapsed_us": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_lidrup_checker_elapsed_us", 0
                        )
                    )
                    + int(
                        json.loads(str(row[0])).get(
                            "store_lidrup_checker_elapsed_us", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_lidrup_wire_protocol\"%'"
                    )
                ),
                "incremental_sat_native_context_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_native_context_protocol":'
                        '"cadical-ipasir-assumptions-v1"%\''
                    ).fetchone()[0]
                ),
                "incremental_sat_native_context_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_native_context_cache_hit\":true%'"
                    ).fetchone()[0]
                ),
                "realtime_proof_stream_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_realtime_stream_protocol":'
                        '"symcc-qfbv-realtime-lidrup-stream-v1"%\''
                    ).fetchone()[0]
                ),
                "realtime_proof_stream_verified_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_realtime_stream_verified\":true%'"
                    ).fetchone()[0]
                ),
                "realtime_proof_stream_events": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_events_observed", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_stream_protocol\"%'"
                    )
                ),
                "realtime_proof_stream_imports_delivered": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_import_delivered", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_stream_protocol\"%'"
                    )
                ),
                "realtime_proof_stream_learned_published": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_learned_published", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_stream_protocol\"%'"
                    )
                ),
                "realtime_proof_stream_checker_elapsed_us": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_stream_checker_elapsed_us", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_stream_protocol\"%'"
                    )
                ),
                "realtime_clause_activity_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_clause_activity_enabled\":true%'"
                    ).fetchone()[0]
                ),
                "realtime_clause_activity_verified_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_realtime_clause_activity_verified\":true%'"
                    ).fetchone()[0]
                ),
                "realtime_clause_activity_receipts": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_clause_activity_verified_receipts", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_clause_activity_enabled\":true%'"
                    )
                ),
                "realtime_clause_activity_unit": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_clause_activity_unit", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_clause_activity_enabled\":true%'"
                    )
                ),
                "realtime_clause_activity_conflict": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_clause_activity_conflict", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_clause_activity_enabled\":true%'"
                    )
                ),
                "realtime_clause_activity_unactivated": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "backend_realtime_clause_activity_unactivated", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_clause_activity_enabled\":true%'"
                    )
                ),
                "adaptive_proof_admission_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_realtime_adaptive_protocol":'
                        '"symcc-qfbv-adaptive-proof-admission-v1"%\''
                    ).fetchone()[0]
                ),
                "adaptive_proof_admission_verified_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_realtime_adaptive_verified\":true%'"
                    ).fetchone()[0]
                ),
                "adaptive_proof_admission_decisions": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_adaptive_verified_decisions", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_adaptive_protocol\"%'"
                    )
                ),
                "adaptive_proof_admission_enqueues": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_adaptive_verified_enqueues", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_adaptive_protocol\"%'"
                    )
                ),
                "utility_pairing_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_realtime_pairing_protocol":'
                        '"symcc-qfbv-utility-aware-worker-pairing-v1"%\''
                    ).fetchone()[0]
                ),
                "utility_pairing_verified_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        "'%\"store_realtime_pairing_verified\":true%'"
                    ).fetchone()[0]
                ),
                "utility_pairing_persisted_workers": int(
                    db.execute(
                        "SELECT COUNT(*) FROM utility_pairing_snapshots"
                    ).fetchone()[0]
                ),
                "malleable_worker_pools": int(
                    db.execute(
                        "SELECT COUNT(*) FROM malleable_worker_snapshots"
                    ).fetchone()[0]
                ),
                "malleable_worker_allocation_generations": int(
                    db.execute(
                        "SELECT COALESCE(SUM(allocation_generation), 0) "
                        "FROM malleable_worker_snapshots"
                    ).fetchone()[0]
                ),
                "utility_pairing_decisions": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_pairing_verified_decisions", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_pairing_protocol\"%'"
                    )
                ),
                "utility_pairing_outcomes": sum(
                    int(
                        json.loads(str(row[0])).get(
                            "store_realtime_pairing_verified_outcomes", 0
                        )
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_pairing_protocol\"%'"
                    )
                ),
                "utility_pairing_admitted": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_realtime_pairing_action_counts", {})
                        .get("admit", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_pairing_protocol\"%'"
                    )
                ),
                "utility_pairing_suppressed": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_realtime_pairing_action_counts", {})
                        .get("suppress", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_pairing_protocol\"%'"
                    )
                ),
                "utility_pairing_activated": sum(
                    sum(int(counts.get(name, 0)) for name in ("unit", "conflict"))
                    for counts in (
                        json.loads(str(row[0])).get(
                            "backend_realtime_pairing_outcome_counts", {}
                        )
                        for row in db.execute(
                            "SELECT result_json FROM results WHERE result_json LIKE "
                            "'%\"backend_realtime_pairing_protocol\"%'"
                        )
                    )
                ),
                "utility_pairing_unactivated": sum(
                    int(
                        json.loads(str(row[0]))
                        .get("backend_realtime_pairing_outcome_counts", {})
                        .get("unactivated", 0)
                    )
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_realtime_pairing_protocol\"%'"
                    )
                ),
                "verified_lemma_results": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results WHERE result_json LIKE "
                        '\'%"backend_lemma_protocol":'
                        '"symcc-qfbv-verified-prefix-lemma-v1"%\''
                    ).fetchone()[0]
                ),
                "verified_lemmas_injected": sum(
                    int(json.loads(str(row[0])).get("backend_lemma_injected", 0))
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_lemma_protocol\"%'"
                    )
                ),
                "verified_lemmas_published": sum(
                    int(json.loads(str(row[0])).get("backend_lemma_published", 0))
                    for row in db.execute(
                        "SELECT result_json FROM results WHERE result_json LIKE "
                        "'%\"backend_lemma_protocol\"%'"
                    )
                ),
                "solver_pscache_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE '%\"solver_pscache_hit\":true%'"
                    ).fetchone()[0]
                ),
                "solver_pscache_conflict_solutions": sum(
                    len(
                        json.loads(str(row[0])).get(
                            "solver_pscache_conflict_solutions", ()
                        )
                    )
                    for row in db.execute("SELECT result_json FROM results")
                ),
                "solver_pscache_conflict_checks": sum(
                    int(
                        json.loads(str(row[0])).get("solver_pscache_conflict_checks", 0)
                    )
                    for row in db.execute("SELECT result_json FROM results")
                ),
                "selective_query_attempts": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        "'%\"selective_query_attempted\":true%'"
                    ).fetchone()[0]
                ),
                "selective_query_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        "'%\"selective_query_hit\":true%'"
                    ).fetchone()[0]
                ),
                "selective_query_graph_attempts": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        '\'%"selective_query_partition_mode":'
                        '"relation-graph-v1"%\' '
                        "AND result_json LIKE "
                        "'%\"selective_query_attempted\":true%'"
                    ).fetchone()[0]
                ),
                "selective_query_graph_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        '\'%"solver":"z3-selective-graph"%\''
                    ).fetchone()[0]
                ),
                "selective_query_partial_unknown": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        '\'%"selective_query_partial_status":"unknown"%\''
                    ).fetchone()[0]
                ),
                "selective_query_policy_skips": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        '\'%"selective_query_policy_decision":'
                        '"learned-skip"%\''
                    ).fetchone()[0]
                ),
                "selective_query_mixed_hits": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        "'%\"selective_query_hit_completion\":%' "
                        "AND result_json NOT LIKE "
                        "'%\"selective_query_hit_completion\":0%' "
                        "AND result_json NOT LIKE "
                        "'%\"selective_query_hit_completion\":1%'"
                    ).fetchone()[0]
                ),
                "unsat_sets": int(
                    db.execute("SELECT COUNT(*) FROM unsat_sets").fetchone()[0]
                ),
                "unsat_subsumed": int(
                    db.execute(
                        "SELECT COUNT(*) FROM results "
                        "WHERE result_json LIKE "
                        "'%query-store-unsat-subset%'"
                    ).fetchone()[0]
                ),
                "partial_solutions": int(
                    db.execute("SELECT COUNT(*) FROM partial_solutions").fetchone()[0]
                ),
                "partial_solution_candidates": int(
                    db.execute(
                        "SELECT COUNT(*) FROM partial_solution_candidates"
                    ).fetchone()[0]
                ),
                "partial_solution_clause_links": int(
                    db.execute(
                        "SELECT COUNT(*) FROM partial_solution_clauses"
                    ).fetchone()[0]
                ),
                "partial_solution_literal_links": int(
                    db.execute(
                        "SELECT COUNT(*) FROM partial_solution_literals"
                    ).fetchone()[0]
                ),
                "conflict_partial_solutions": int(
                    db.execute(
                        "SELECT COUNT(*) FROM partial_solutions "
                        "WHERE provenance = 'z3-assumption-conflict'"
                    ).fetchone()[0]
                ),
                "joint_schedule_validations": int(
                    db.execute(
                        "SELECT COUNT(*) FROM joint_schedule_validations"
                    ).fetchone()[0]
                ),
                "joint_schedule_ready": int(
                    db.execute(
                        "SELECT COUNT(*) FROM joint_schedule_validations "
                        "WHERE joint_status = 'ready'"
                    ).fetchone()[0]
                ),
                "joint_smt_solves": int(
                    db.execute("SELECT COUNT(*) FROM joint_smt_solves").fetchone()[0]
                ),
                "joint_smt_verified": int(
                    db.execute(
                        "SELECT COUNT(*) FROM joint_smt_solves "
                        "WHERE status = 'sat' "
                        "AND query_ir_model_verified = 1"
                    ).fetchone()[0]
                ),
            }
            return result

    def work_counts(self) -> dict[str, int]:
        """Return the constant-query scheduler view without parsing result JSON."""
        with self._connect() as db:
            rows = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    "SELECT status, COUNT(*) AS count FROM queries GROUP BY status"
                )
            }
        return {
            "pending": rows.get("pending", 0),
            "leased": rows.get("leased", 0),
            "done": rows.get("done", 0),
            "total": sum(rows.values()),
        }


class SubprocessSolver:
    """Invoke a backend-neutral helper that consumes one SMT-LIB file."""

    def __init__(self, command: Sequence[str]):
        if not command:
            raise ValueError("solver command must not be empty")
        self.command = tuple(command)
        self._active_lock = threading.Lock()
        self._active: dict[int, tuple[str, subprocess.Popen[str], threading.Event]] = {}
        self._running: dict[str, set[threading.Event]] = {}

    def __call__(self, lease: WorkLease) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        cancelled = threading.Event()
        query_id = str(lease.query_id)
        with self._active_lock:
            self._running.setdefault(query_id, set()).add(cancelled)
        process: subprocess.Popen[str] | None = None
        token = 0
        artifact_descriptors: tuple[int, ...] = ()
        try:
            artifact_descriptors = lease.duplicate_artifacts("full")
            if artifact_descriptors:
                query_path = f"/proc/self/fd/{artifact_descriptors[0]}"
                pass_fds = artifact_descriptors
            else:
                query_path = str(lease.smt2_path)
                pass_fds = ()
            process = subprocess.Popen(
                [*self.command, query_path, str(lease.timeout_ms)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                pass_fds=pass_fds,
            )
            token = id(process)
            with self._active_lock:
                self._active[token] = (query_id, process, cancelled)
            if cancelled.is_set():
                _interrupt_process(process)
            stdout, stderr = _communicate_solver_bounded(
                process,
                self.command,
                max(1.0, lease.timeout_ms / 1000.0 + 5.0),
            )
        except BaseException:
            if process is not None:
                _interrupt_process(process)
            raise
        finally:
            with self._active_lock:
                if token:
                    self._active.pop(token, None)
                running = self._running.get(query_id)
                if running is not None:
                    running.discard(cancelled)
                    if not running:
                        self._running.pop(query_id, None)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            for descriptor in artifact_descriptors:
                os.close(descriptor)
        assert process is not None
        elapsed_us = (time.monotonic_ns() - started) // 1000
        if cancelled.is_set():
            return {
                "status": "unknown",
                "assignments": {},
                "solver": (self.command[0] if self.command else "subprocess"),
                "elapsed_us": elapsed_us,
                "cancelled": True,
                "cancel_reason": "portfolio-sat-winner",
                "reason": "solver helper interrupted after portfolio SAT",
            }
        if process.returncode not in (0, 1, 2):
            raise RuntimeError(
                f"solver exited {process.returncode}: {stderr.strip()[:2048]}"
            )
        parsed = _parse_solver_response(stdout)
        parsed.setdefault("elapsed_us", elapsed_us)
        return parsed

    def cancel(self, lease: WorkLease) -> bool:
        query_id = str(lease.query_id)
        with self._active_lock:
            running = tuple(self._running.get(query_id, ()))
            if not running:
                return False
            for cancelled in running:
                cancelled.set()
            active = [
                (process, cancelled)
                for active_query, process, cancelled in self._active.values()
                if active_query == query_id and process.poll() is None
            ]
        for process, _ in active:
            _interrupt_process(process)
        return True


class PersistentSubprocessSolver:
    """Reuse prefix solver contexts through the helper's line protocol."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ):
        if not command:
            raise ValueError("solver command must not be empty")
        self.command = tuple(command)
        child_environment = os.environ.copy()
        if environment:
            child_environment.update(
                {str(key): str(value) for key, value in environment.items()}
            )
        self.environment = child_environment
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._active_query = ""
        self._active_cancelled: threading.Event | None = None
        self._active_deadline: float | None = None
        self._running: dict[str, set[threading.Event]] = {}
        self._fd_channel: socket.socket | None = None
        self._process_valid = False
        self._generation_query_ids: set[bytes] = set()
        self.process = self._spawn()
        self._process_valid = True

    def _spawn(self) -> subprocess.Popen[str]:
        parent_channel, child_channel = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        environment = dict(self.environment)
        environment["SYMCC_QUERY_FD_CHANNEL"] = str(child_channel.fileno())
        try:
            process = subprocess.Popen(
                [*self.command, "--server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
                start_new_session=True,
                pass_fds=(child_channel.fileno(),),
            )
        except BaseException:
            parent_channel.close()
            raise
        finally:
            child_channel.close()
        previous_channel = self._fd_channel
        self._fd_channel = parent_channel
        if previous_channel is not None:
            try:
                previous_channel.close()
            except OSError:
                pass
        self._generation_query_ids.clear()
        return process

    @staticmethod
    def _close_streams(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _ensure_process(self) -> subprocess.Popen[str]:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("persistent solver is closed")
            if not self._process_valid or self.process.poll() is not None:
                if self.process.poll() is None:
                    _interrupt_process(self.process)
                self._close_streams(self.process)
                self.process = self._spawn()
                self._process_valid = True
            return self.process

    def _invalidate_process(self, process: subprocess.Popen[str]) -> None:
        """Retire a helper generation whose dual-channel state is uncertain."""
        with self._state_lock:
            if self.process is not process:
                return
            self._process_valid = False
            channel = self._fd_channel
            self._fd_channel = None
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
        try:
            _interrupt_process(process)
        finally:
            self._close_streams(process)

    def _admit_generation_request(
        self,
        process: subprocess.Popen[str],
        query_id: str,
    ) -> subprocess.Popen[str]:
        """Admit one logical query ID at most once per helper generation."""
        query_fingerprint = hashlib.sha256(query_id.encode("utf-8")).digest()
        with self._state_lock:
            if self._closed:
                raise RuntimeError("persistent solver is closed")
            if self.process is not process or not self._process_valid:
                raise RuntimeError(
                    "persistent solver generation changed before admission"
                )
            rotate = (
                query_fingerprint in self._generation_query_ids
                or len(self._generation_query_ids)
                >= _MAX_PERSISTENT_GENERATION_REQUESTS
            )

        if rotate:
            self._invalidate_process(process)
            process = self._ensure_process()

        with self._state_lock:
            if self._closed:
                raise RuntimeError("persistent solver is closed")
            if (
                self.process is not process
                or not self._process_valid
                or process.poll() is not None
            ):
                raise RuntimeError(
                    "persistent solver generation changed during admission"
                )
            if query_fingerprint in self._generation_query_ids:
                raise RuntimeError("persistent solver request was already admitted")
            self._generation_query_ids.add(query_fingerprint)
        return process

    def _read_response(
        self,
        process: subprocess.Popen[str],
        timeout_seconds: float,
        *,
        deadline: float | None = None,
    ) -> str:
        stream = process.stdout
        if stream is None:
            raise RuntimeError("persistent solver output is unavailable")
        descriptor = stream.fileno()
        if deadline is None:
            deadline = time.monotonic() + max(0.001, timeout_seconds)
        response = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.command, timeout_seconds)
                try:
                    readable = selector.select(remaining)
                except InterruptedError:
                    continue
                if not readable:
                    raise subprocess.TimeoutExpired(self.command, timeout_seconds)
                try:
                    chunk = os.read(
                        descriptor,
                        min(65536, _MAX_SOLVER_RESPONSE_BYTES + 1 - len(response)),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    if not response:
                        return ""
                    raise RuntimeError(
                        "persistent solver returned an unterminated response"
                    )
                newline = chunk.find(b"\n")
                if newline >= 0:
                    response.extend(chunk[:newline])
                    if (
                        chunk[newline + 1 :]
                        or len(response) > _MAX_SOLVER_RESPONSE_BYTES
                    ):
                        raise RuntimeError(
                            "persistent solver returned multiple or oversized responses"
                        )
                    try:
                        return response.decode("utf-8", errors="strict")
                    except UnicodeError as error:
                        raise RuntimeError(
                            "persistent solver returned invalid UTF-8"
                        ) from error
                response.extend(chunk)
                if len(response) > _MAX_SOLVER_RESPONSE_BYTES:
                    raise RuntimeError("persistent solver response exceeds 16 MiB")

    @staticmethod
    def _request_timeout_seconds(timeout_ms: int) -> float:
        if type(timeout_ms) is not int or timeout_ms < 1 or timeout_ms > 3_600_000:
            raise ValueError("persistent solver timeout must be in [1, 3600000] ms")
        return timeout_ms / 1000.0 + _PERSISTENT_SOLVER_GRACE_SECONDS

    @staticmethod
    def _encode_request_frame(fields: Sequence[str]) -> bytes:
        try:
            encoded = "\t".join(fields).encode("utf-8", errors="strict") + b"\n"
        except UnicodeError as error:
            raise ValueError(
                "query solver protocol fields must be valid UTF-8"
            ) from error
        if len(encoded) > _MAX_SOLVER_REQUEST_BYTES:
            raise ValueError(
                f"persistent solver request exceeds {_MAX_SOLVER_REQUEST_BYTES} bytes"
            )
        return encoded

    def _send_descriptor_request(
        self,
        channel: socket.socket,
        request_id: bytes,
        descriptors: Sequence[int],
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> None:
        rights = array("i", descriptors)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]
        with selectors.DefaultSelector() as selector:
            selector.register(channel, selectors.EVENT_WRITE)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        self.command,
                        timeout_seconds,
                    )
                try:
                    sent = channel.sendmsg(
                        [request_id],
                        ancillary,
                        getattr(socket, "MSG_DONTWAIT", 0),
                    )
                except InterruptedError:
                    continue
                except BlockingIOError:
                    if not selector.select(remaining):
                        raise subprocess.TimeoutExpired(
                            self.command,
                            timeout_seconds,
                        )
                    continue
                if sent != len(request_id):
                    raise OSError(
                        errno.EIO,
                        "short query artifact descriptor transfer",
                    )
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(
                        self.command,
                        timeout_seconds,
                    )
                return

    def _write_request(
        self,
        process: subprocess.Popen[str],
        request: bytes,
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> None:
        stream = process.stdin
        if stream is None:
            raise RuntimeError("persistent solver input is unavailable")
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
        view = memoryview(request)
        offset = 0
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_WRITE)
            while offset < len(view):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        self.command,
                        timeout_seconds,
                    )
                try:
                    written = os.write(descriptor, view[offset:])
                except InterruptedError:
                    continue
                except BlockingIOError:
                    if not selector.select(remaining):
                        raise subprocess.TimeoutExpired(
                            self.command,
                            timeout_seconds,
                        )
                    continue
                if written <= 0:
                    raise OSError(errno.EPIPE, "persistent solver request write failed")
                offset += written
        if time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(
                self.command,
                timeout_seconds,
            )

    def _artifact_fields(self, lease: WorkLease) -> tuple[str, str]:
        descriptors = lease.duplicate_artifacts("prefix", "target")
        if not descriptors:
            return str(lease.prefix_smt2_path), str(lease.target_smt2_path)
        try:
            channel = self._fd_channel
            if channel is None:
                raise RuntimeError(
                    "persistent solver descriptor channel is unavailable"
                )
            request_id = str(lease.query_id).encode("ascii", errors="strict")
            timeout_seconds = self._request_timeout_seconds(lease.timeout_ms)
            deadline = self._active_deadline
            if deadline is None:
                deadline = time.monotonic() + timeout_seconds
            self._send_descriptor_request(
                channel,
                request_id,
                descriptors,
                deadline=deadline,
                timeout_seconds=timeout_seconds,
            )
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        return f"{_FD_PROTOCOL_PREFIX}prefix", f"{_FD_PROTOCOL_PREFIX}target"

    @staticmethod
    def _validate_request_fields(
        fields: Sequence[str],
        descriptor_transport: bool,
    ) -> None:
        if any(
            not isinstance(field, str)
            or not field
            or "\x00" in field
            or "\t" in field
            or "\n" in field
            or "\r" in field
            for field in fields
        ):
            raise ValueError("query solver protocol fields are empty or delimited")
        if not descriptor_transport:
            return
        try:
            descriptor_request_id = fields[0].encode("ascii", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("sealed query solver request id must be ASCII") from error
        if len(descriptor_request_id) > _MAX_DESCRIPTOR_REQUEST_ID_BYTES:
            raise ValueError("sealed query solver request id exceeds 256 bytes")

    def __call__(self, lease: WorkLease) -> Mapping[str, Any]:
        cancelled = threading.Event()
        query_id = str(lease.query_id)
        with self._state_lock:
            if self._closed:
                raise RuntimeError("persistent solver is closed")
            self._running.setdefault(query_id, set()).add(cancelled)
        try:
            return self._run_registered(lease, cancelled)
        finally:
            with self._state_lock:
                running = self._running.get(query_id)
                if running is not None:
                    running.discard(cancelled)
                    if not running:
                        self._running.pop(query_id, None)

    def _run_registered(
        self,
        lease: WorkLease,
        cancelled: threading.Event,
    ) -> Mapping[str, Any]:
        with self._io_lock:
            descriptor_transport = lease.has_sealed_artifacts
            artifact_field_preview = (
                (f"{_FD_PROTOCOL_PREFIX}prefix", f"{_FD_PROTOCOL_PREFIX}target")
                if descriptor_transport
                else (str(lease.prefix_smt2_path), str(lease.target_smt2_path))
            )
            request_field_preview = (
                str(lease.query_id),
                str(lease.timeout_ms),
                str(lease.prefix_key),
                *artifact_field_preview,
                lease.input_hex or "-",
            )
            timeout_seconds = self._request_timeout_seconds(lease.timeout_ms)
            self._validate_request_fields(
                request_field_preview,
                descriptor_transport,
            )
            request_frame = self._encode_request_frame(request_field_preview)
            deadline = time.monotonic() + timeout_seconds
            with self._state_lock:
                self._active_query = str(lease.query_id)
                self._active_cancelled = cancelled
                self._active_deadline = deadline
            process: subprocess.Popen[str] | None = None
            try:
                process = self._ensure_process()
                if cancelled.is_set():
                    self._invalidate_process(process)
                    return {
                        "status": "unknown",
                        "assignments": {},
                        "solver": (
                            self.command[0] if self.command else "persistent-subprocess"
                        ),
                        "cancelled": True,
                        "cancel_reason": "portfolio-sat-winner",
                        "reason": "persistent solver interrupted after portfolio SAT",
                    }
                process = self._admit_generation_request(
                    process,
                    request_field_preview[0],
                )
                if cancelled.is_set():
                    self._invalidate_process(process)
                    return {
                        "status": "unknown",
                        "assignments": {},
                        "solver": (
                            self.command[0] if self.command else "persistent-subprocess"
                        ),
                        "cancelled": True,
                        "cancel_reason": "portfolio-sat-winner",
                        "reason": "persistent solver interrupted after portfolio SAT",
                    }
                artifact_fields = self._artifact_fields(lease)
                if artifact_fields != artifact_field_preview:
                    raise RuntimeError(
                        "query artifact transport changed after validation"
                    )
                self._write_request(
                    process,
                    request_frame,
                    deadline=deadline,
                    timeout_seconds=timeout_seconds,
                )
                response = self._read_response(
                    process,
                    timeout_seconds,
                    deadline=deadline,
                )
                if cancelled.is_set():
                    self._invalidate_process(process)
                    return {
                        "status": "unknown",
                        "assignments": {},
                        "solver": (
                            self.command[0] if self.command else "persistent-subprocess"
                        ),
                        "cancelled": True,
                        "cancel_reason": "portfolio-sat-winner",
                        "reason": ("persistent solver interrupted after portfolio SAT"),
                    }
                if not response:
                    raise RuntimeError("persistent solver closed its output")
                parsed = _parse_solver_response(response)
                if parsed.pop("request_id", None) != request_field_preview[0]:
                    raise RuntimeError("persistent solver response id mismatch")
                return parsed
            except BaseException:
                if process is not None:
                    self._invalidate_process(process)
                raise
            finally:
                with self._state_lock:
                    if self._active_cancelled is cancelled:
                        self._active_query = ""
                        self._active_cancelled = None
                        self._active_deadline = None

    def cancel(self, lease: WorkLease) -> bool:
        query_id = str(lease.query_id)
        with self._state_lock:
            running = tuple(self._running.get(query_id, ()))
            if not running:
                return False
            for cancelled in running:
                cancelled.set()
            process = (
                self.process
                if self._active_query == query_id and self._active_cancelled in running
                else None
            )
        if process is not None and process.poll() is None:
            _interrupt_process(process)
        return True

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._process_valid = False
            for running in self._running.values():
                for cancelled in running:
                    cancelled.set()
            process = self.process
            channel = self._fd_channel
            self._fd_channel = None
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.close()
                process.wait(timeout=2.0)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                _interrupt_process(process)
        self._close_streams(process)
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass

    def __enter__(self) -> "PersistentSubprocessSolver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class PortfolioSolver:
    """Run a bounded exact-solver portfolio and keep attempt telemetry."""

    def __init__(
        self,
        solvers: Sequence[tuple[str, Callable[[WorkLease], Mapping[str, Any]]]],
        *,
        parallelism: int | None = None,
        cancel_grace_ms: int | None = None,
    ):
        if not solvers:
            raise ValueError("portfolio must contain at least one solver")
        self.solvers = tuple((str(name)[:128], solver) for name, solver in solvers)
        if parallelism is None:
            parallelism = _env_int(
                "SYMCC_QUERY_SOLVER_PORTFOLIO_PARALLELISM", len(self.solvers), 1, 64
            )
        self.parallelism = max(1, min(int(parallelism), len(self.solvers), 64))
        if cancel_grace_ms is None:
            cancel_grace_ms = _env_int(
                "SYMCC_QUERY_SOLVER_PORTFOLIO_CANCEL_GRACE_MS",
                -1,
                -1,
                60000,
            )
        self.cancel_grace_ms = max(-1, min(int(cancel_grace_ms), 60000))

    @staticmethod
    def _run_solver(
        index: int,
        name: str,
        solver: Callable[[WorkLease], Mapping[str, Any]],
        lease: WorkLease,
    ) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
        started = time.monotonic_ns()
        try:
            result = dict(solver(lease))
        except Exception as exc:
            elapsed_us = (time.monotonic_ns() - started) // 1000
            return (
                index,
                {
                    "name": name,
                    "status": "error",
                    "solver": name,
                    "elapsed_us": elapsed_us,
                    "prefix_cache_hit": False,
                    "cancelled": False,
                    "cancel_reason": "",
                    "reason": str(exc)[:512],
                },
                None,
            )

        elapsed_us = _bounded_int(
            result.get(
                "elapsed_us",
                (time.monotonic_ns() - started) // 1000,
            ),
            "portfolio solver elapsed_us",
            0,
            (1 << 63) - 1,
        )
        status = str(result.get("status", "unknown"))
        if status not in _RESULT_STATUSES:
            status = "unknown"
        attempt = {
            "name": name,
            "status": status,
            "solver": str(result.get("solver", name))[:128],
            "elapsed_us": elapsed_us,
            "prefix_cache_hit": bool(result.get("prefix_cache_hit", False)),
            "backend_kind": str(result.get("backend_kind", ""))[:64],
            "capability_status": str(result.get("capability_status", "supported"))[:32],
            "backend_model_verified": bool(result.get("backend_model_verified", False)),
            "backend_unsat_authorized": bool(
                result.get("backend_unsat_authorized", False)
            ),
            "backend_context_protocol": str(result.get("backend_context_protocol", ""))[
                :64
            ],
            "capability_sha256": str(
                result.get("backend_capabilities", {}).get("capability_sha256", "")
                if isinstance(result.get("backend_capabilities"), Mapping)
                else ""
            )[:64],
            "lowering_certificate_sha256": str(
                result.get("lowering_certificate", {}).get("certificate_sha256", "")
                if isinstance(result.get("lowering_certificate"), Mapping)
                else ""
            )[:64],
            "cancelled": bool(result.get("cancelled", False)),
            "cancel_reason": str(result.get("cancel_reason", ""))[:128],
            "reason": str(result.get("reason", ""))[:512],
        }
        if status in {"sat", "unsat"}:
            result["_portfolio_name"] = name
            result["_portfolio_index"] = index
            return index, attempt, result
        return index, attempt, None

    @staticmethod
    def _cancelled_attempt(
        index: int,
        name: str,
        *,
        reason: str,
    ) -> tuple[int, dict[str, Any], None]:
        return (
            index,
            {
                "name": name,
                "status": "unknown",
                "solver": name,
                "elapsed_us": 0,
                "prefix_cache_hit": False,
                "backend_kind": "",
                "capability_status": "supported",
                "backend_model_verified": False,
                "backend_unsat_authorized": False,
                "backend_context_protocol": "",
                "capability_sha256": "",
                "lowering_certificate_sha256": "",
                "cancelled": True,
                "cancel_reason": reason[:128],
                "reason": "portfolio attempt cancelled before execution",
            },
            None,
        )

    def __call__(self, lease: WorkLease) -> Mapping[str, Any]:
        started_total = time.monotonic_ns()
        attempts_by_index: list[dict[str, Any] | None] = [None for _ in self.solvers]
        terminal: list[dict[str, Any]] = []
        cancellation_enabled = self.parallelism > 1 and self.cancel_grace_ms >= 0
        cancel_requested = False
        if self.parallelism <= 1:
            for index, (name, solver) in enumerate(self.solvers):
                _, attempt, result = self._run_solver(index, name, solver, lease)
                attempts_by_index[index] = attempt
                if result is not None:
                    terminal.append(result)
        else:
            executor = ThreadPoolExecutor(max_workers=self.parallelism)
            future_metadata: dict[
                Future[tuple[int, dict[str, Any], dict[str, Any] | None]],
                tuple[int, str, Callable[[WorkLease], Mapping[str, Any]]],
            ] = {}
            for index, (name, solver) in enumerate(self.solvers):
                future = executor.submit(self._run_solver, index, name, solver, lease)
                future_metadata[future] = (index, name, solver)
            pending = set(future_metadata)
            cancellation_deadline: float | None = None
            try:
                while pending:
                    timeout = None
                    if cancellation_deadline is not None:
                        timeout = max(0.0, cancellation_deadline - time.monotonic())
                        if timeout <= 0.0:
                            break
                    done, _ = wait(
                        pending,
                        timeout=timeout,
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        break
                    for future in sorted(
                        done,
                        key=lambda item: future_metadata[item][0],
                    ):
                        pending.remove(future)
                        index, name, _ = future_metadata[future]
                        try:
                            result_index, attempt, result = future.result()
                        except CancelledError:
                            result_index, attempt, result = self._cancelled_attempt(
                                index,
                                name,
                                reason="portfolio-sat-winner",
                            )
                        attempts_by_index[result_index] = attempt
                        if result is not None:
                            terminal.append(result)
                            if (
                                cancellation_enabled
                                and result.get("status") == "sat"
                                and cancellation_deadline is None
                            ):
                                cancellation_deadline = (
                                    time.monotonic() + self.cancel_grace_ms / 1000.0
                                )
                    observed_statuses = {
                        str(result.get("status")) for result in terminal
                    }
                    if "sat" in observed_statuses and "unsat" in observed_statuses:
                        break

                if (
                    pending
                    and cancellation_enabled
                    and any(result.get("status") == "sat" for result in terminal)
                ):
                    cancel_requested = True
                    for future in sorted(
                        pending,
                        key=lambda item: future_metadata[item][0],
                    ):
                        index, name, solver = future_metadata[future]
                        if future.cancel():
                            attempts_by_index[index] = self._cancelled_attempt(
                                index,
                                name,
                                reason="portfolio-sat-winner",
                            )[1]
                            continue
                        cancel_method = getattr(solver, "cancel", None)
                        if callable(cancel_method):
                            try:
                                cancel_method(lease)
                            except Exception:
                                pass

                for future in sorted(
                    pending,
                    key=lambda item: future_metadata[item][0],
                ):
                    index, name, _ = future_metadata[future]
                    try:
                        result_index, attempt, result = future.result()
                    except CancelledError:
                        result_index, attempt, result = self._cancelled_attempt(
                            index,
                            name,
                            reason="portfolio-sat-winner",
                        )
                    attempts_by_index[result_index] = attempt
                    if result is not None:
                        terminal.append(result)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

        attempts = [attempt for attempt in attempts_by_index if attempt is not None]
        terminal.sort(key=lambda result: int(result.get("_portfolio_index", 0)))

        statuses = {str(result.get("status")) for result in terminal}
        elapsed_total = (time.monotonic_ns() - started_total) // 1000
        disagreement = "sat" in statuses and "unsat" in statuses
        cancelled_attempts = sum(
            int(bool(attempt.get("cancelled", False))) for attempt in attempts
        )
        portfolio = {
            "schema": PORTFOLIO_SCHEMA,
            "winner": "",
            "disagreement": disagreement,
            "mode": "parallel" if self.parallelism > 1 else "sequential",
            "parallelism": self.parallelism,
            "cancellation_enabled": cancellation_enabled,
            "cancel_grace_ms": (self.cancel_grace_ms if cancellation_enabled else 0),
            "cancel_requested": cancel_requested,
            "cancelled_attempts": cancelled_attempts,
            "consensus_complete": cancelled_attempts == 0,
            "elapsed_us": elapsed_total,
            "attempts": attempts,
        }
        if disagreement:
            return {
                "schema": RESULT_SCHEMA,
                "status": "unknown",
                "assignments": {},
                "solver": "portfolio",
                "elapsed_us": elapsed_total,
                "reason": "solver portfolio SAT/UNSAT disagreement",
                "portfolio": portfolio,
            }

        winner = next(
            (result for result in terminal if str(result.get("status")) == "sat"),
            None,
        )
        if winner is None:
            winner = next(
                (result for result in terminal if str(result.get("status")) == "unsat"),
                None,
            )
        if winner is None:
            return {
                "schema": RESULT_SCHEMA,
                "status": "unknown",
                "assignments": {},
                "solver": "portfolio",
                "elapsed_us": elapsed_total,
                "reason": "no portfolio solver returned SAT or UNSAT",
                "portfolio": portfolio,
            }

        output = dict(winner)
        winner_name = str(output.pop("_portfolio_name", ""))
        output.pop("_portfolio_index", None)
        output["solver"] = f"portfolio:{output.get('solver', winner_name)}"
        output["elapsed_us"] = elapsed_total
        portfolio["winner"] = winner_name
        output["portfolio"] = portfolio
        return output


SolverCallable = Callable[[WorkLease], Mapping[str, Any]]


class QueryLeaseHeartbeatError(RuntimeError):
    """Raised when a worker can no longer prove ownership of a query lease."""


class QueryLeaseHeartbeat:
    """Renew a fenced query lease while solving and validating its result."""

    def __init__(
        self,
        store: QueryStore,
        lease: WorkLease,
        owner: str,
        *,
        lease_seconds: float,
        interval_seconds: float | None = None,
    ):
        try:
            duration = float(lease_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("query heartbeat lease_seconds must be finite") from error
        if not math.isfinite(duration) or not 0.1 <= duration <= 86_400.0:
            raise ValueError(
                "query heartbeat lease_seconds must be in [0.1, 86400]"
            )
        default_interval = max(0.01, min(30.0, duration / 3.0))
        try:
            interval = float(
                default_interval if interval_seconds is None else interval_seconds
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("query heartbeat interval_seconds must be finite") from error
        if (
            not math.isfinite(interval)
            or interval < 0.01
            or interval > duration * 0.8
        ):
            raise ValueError(
                "query heartbeat interval_seconds must be within the lease window"
            )
        self.store = store
        self.lease = lease
        self.owner = owner
        self.lease_seconds = duration
        self.interval_seconds = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"query-lease-heartbeat-{lease.query_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.store.renew(
                    self.lease,
                    self.owner,
                    self.lease_seconds,
                )
                if not renewed:
                    raise QueryLeaseHeartbeatError(
                        "query lease expired or was superseded"
                    )
            except BaseException as error:
                with self._lock:
                    self._error = error
                self._stop.set()
                return

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            if isinstance(error, QueryLeaseHeartbeatError):
                raise error
            raise QueryLeaseHeartbeatError(
                f"query lease renewal failed: {error}"
            ) from error

    def close(self, *, check: bool = True) -> None:
        self._stop.set()
        # QueryStore connections have a 30 second busy timeout.  Waiting just
        # beyond it prevents a renewal thread from outliving its solver call.
        self._thread.join(timeout=31.0)
        if self._thread.is_alive():
            raise QueryLeaseHeartbeatError("query lease heartbeat did not stop")
        if check:
            self.check()

def solve_claimed(
    store: QueryStore,
    owner: str,
    solver: SolverCallable,
    lease: WorkLease,
    *,
    lease_seconds: float = 60.0,
    max_attempts: int = 3,
) -> tuple[str, Mapping[str, Any] | None]:
    """Solve one already-claimed lease while continuously preserving ownership."""
    heartbeat = QueryLeaseHeartbeat(
        store,
        lease,
        owner,
        lease_seconds=lease_seconds,
    )
    heartbeat_closed = False
    try:
        result = solver(lease)
        heartbeat.check()
        if not store.complete(lease, owner, result):
            return "stale", None
        return str(result.get("status", "unknown")), result
    except Exception as exc:
        # Stop renewal before requeueing so a failed attempt cannot keep a
        # pending query artificially live.
        try:
            heartbeat.close(check=False)
        finally:
            heartbeat_closed = True
        store.fail(lease, owner, str(exc), max_attempts=max_attempts)
        return "error", None
    finally:
        if not heartbeat_closed:
            heartbeat.close(check=False)


def solve_one(
    store: QueryStore,
    owner: str,
    solver: SolverCallable,
    *,
    lease_seconds: float = 60.0,
    max_attempts: int = 3,
    traversal: str = "dfs",
    shard_index: int = 0,
    shard_count: int = 1,
) -> str | None:
    effective_lease_seconds = max(0.1, float(lease_seconds))
    lease = store.claim(
        owner,
        effective_lease_seconds,
        traversal=traversal,
        shard_index=shard_index,
        shard_count=shard_count,
    )
    if lease is None:
        return None
    status, _result = solve_claimed(
        store,
        owner,
        solver,
        lease,
        lease_seconds=effective_lease_seconds,
        max_attempts=max_attempts,
    )
    return status
