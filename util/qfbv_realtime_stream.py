#!/usr/bin/env python3
"""Proof-checked QF_BV clause streaming into a running CaDiCaL solve."""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from qfbv_incremental_proof import (
    CLAUSE_PROTOCOL,
    EVENT_STREAM_PROTOCOL,
    ClauseAuthorization,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import BitBlastPlan
from qfbv_adaptive_exchange import (
    ADAPTIVE_EXCHANGE_PROTOCOL,
    AdaptiveExchangeError,
    AdaptiveProofCandidate,
    AdaptiveProofController,
    AdaptiveProofObservation,
)
from qfbv_utility_pairing import (
    UTILITY_PAIRING_PROTOCOL,
    UtilityPairingCandidate,
    UtilityPairingController,
    UtilityPairingError,
    formula_family_sha256,
)


REALTIME_STREAM_PROTOCOL = "symcc-qfbv-realtime-lidrup-stream-v1"
CHECKED_IMPORT_ACK_SCHEMA = "symcc-qfbv-checked-import-ack-v1"
CLAUSE_ACTIVITY_PROTOCOL = "symcc-qfbv-native-clause-activity-v1"
CLAUSE_ACTIVITY_SCHEMA = "symcc-qfbv-clause-activity-receipt-v1"
CLAUSE_COMPRESSION_PROTOCOL = "symcc-qfbv-native-clause-compression-v1"
MAX_STREAM_IMPORTS = 4096
MAX_STREAM_EVENTS = 1_000_000
MAX_STREAM_ACKS = 4096
MAX_LEARNED_CLAUSES = 65_536
_HEX64 = re.compile(r"[0-9a-f]{64}")


class RealtimeStreamError(ValueError):
    """A native stream, checked import, or acknowledgement failed closed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise RealtimeStreamError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise RealtimeStreamError(f"{name} must be in [{lower}, {upper}]")
    return value


def _hex_digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise RealtimeStreamError(f"{name} must be a lowercase SHA-256 digest")
    return value


class NativeRealtimeCadical:
    """Strict ctypes adapter for the CaDiCaL C++ IPASIR-UP shim."""

    def __init__(
        self,
        library_path: str | Path,
        *,
        require_clause_compression: bool = False,
    ) -> None:
        if type(require_clause_compression) is not bool:
            raise RealtimeStreamError("compression requirement must be boolean")
        path = Path(library_path).resolve(strict=True)
        if not path.is_file():
            raise RealtimeStreamError("realtime CaDiCaL shim is not a regular file")
        self.path = path
        self.library = ctypes.CDLL(str(path))
        self._configure_api()
        protocol_raw = self.library.symcc_qfbv_realtime_protocol()
        signature_raw = self.library.symcc_qfbv_realtime_signature()
        if protocol_raw is None or signature_raw is None:
            raise RealtimeStreamError("realtime CaDiCaL shim lacks an identity")
        self.protocol = protocol_raw.decode("ascii", "strict")
        self.signature = signature_raw.decode("ascii", "strict")
        activity_raw = (
            self.library.symcc_qfbv_realtime_activity_protocol()
            if hasattr(self.library, "symcc_qfbv_realtime_activity_protocol")
            else None
        )
        self.activity_protocol = (
            activity_raw.decode("ascii", "strict")
            if activity_raw is not None
            else ""
        )
        compression_raw = (
            self.library.symcc_qfbv_realtime_compression_protocol()
            if hasattr(self.library, "symcc_qfbv_realtime_compression_protocol")
            else None
        )
        self.compression_protocol = (
            compression_raw.decode("ascii", "strict")
            if compression_raw is not None
            else ""
        )
        if self.protocol != REALTIME_STREAM_PROTOCOL:
            raise RealtimeStreamError("realtime CaDiCaL protocol is unsupported")
        if not self.signature.startswith(
            "symcc-qfbv-realtime-v1|cadical-3.0."
        ):
            raise RealtimeStreamError("realtime shim requires CaDiCaL 3.0.x")
        if self.activity_protocol not in {"", CLAUSE_ACTIVITY_PROTOCOL}:
            raise RealtimeStreamError("native clause-activity protocol is unsupported")
        if self.compression_protocol not in {"", CLAUSE_COMPRESSION_PROTOCOL}:
            raise RealtimeStreamError("native clause-compression protocol is unsupported")
        if require_clause_compression and not self.compression_protocol:
            raise RealtimeStreamError("native clause compression is required")

    def _configure_api(self) -> None:
        api = self.library
        api.symcc_qfbv_realtime_protocol.argtypes = []
        api.symcc_qfbv_realtime_protocol.restype = ctypes.c_char_p
        api.symcc_qfbv_realtime_signature.argtypes = []
        api.symcc_qfbv_realtime_signature.restype = ctypes.c_char_p
        api.symcc_qfbv_realtime_init.argtypes = [
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_uint64,
        ]
        api.symcc_qfbv_realtime_init.restype = ctypes.c_void_p
        api.symcc_qfbv_realtime_release.argtypes = [ctypes.c_void_p]
        api.symcc_qfbv_realtime_release.restype = None
        api.symcc_qfbv_realtime_add.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_add.restype = ctypes.c_int
        api.symcc_qfbv_realtime_assume.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_assume.restype = ctypes.c_int
        api.symcc_qfbv_realtime_observe.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_observe.restype = ctypes.c_int
        api.symcc_qfbv_realtime_solve.argtypes = [ctypes.c_void_p]
        api.symcc_qfbv_realtime_solve.restype = ctypes.c_int
        api.symcc_qfbv_realtime_val.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_val.restype = ctypes.c_int
        api.symcc_qfbv_realtime_failed.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_failed.restype = ctypes.c_int
        api.symcc_qfbv_realtime_terminate.argtypes = [ctypes.c_void_p]
        api.symcc_qfbv_realtime_terminate.restype = ctypes.c_int
        api.symcc_qfbv_realtime_clear_termination.argtypes = [ctypes.c_void_p]
        api.symcc_qfbv_realtime_clear_termination.restype = ctypes.c_int
        api.symcc_qfbv_realtime_enqueue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
        ]
        api.symcc_qfbv_realtime_enqueue.restype = ctypes.c_int
        api.symcc_qfbv_realtime_dequeue_ack.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        api.symcc_qfbv_realtime_dequeue_ack.restype = ctypes.c_int
        api.symcc_qfbv_realtime_dequeue_learned.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        api.symcc_qfbv_realtime_dequeue_learned.restype = ctypes.c_int
        api.symcc_qfbv_realtime_stat.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.symcc_qfbv_realtime_stat.restype = ctypes.c_uint64
        api.symcc_qfbv_realtime_reset_queues.argtypes = [ctypes.c_void_p]
        api.symcc_qfbv_realtime_reset_queues.restype = ctypes.c_int
        activity_symbols = (
            "symcc_qfbv_realtime_activity_protocol",
            "symcc_qfbv_realtime_dequeue_activity",
            "symcc_qfbv_realtime_enable_activity",
        )
        activity_presence = tuple(hasattr(api, name) for name in activity_symbols)
        if any(activity_presence) and not all(activity_presence):
            raise RealtimeStreamError(
                "native clause-activity ABI is incomplete"
            )
        if all(activity_presence):
            api.symcc_qfbv_realtime_activity_protocol.argtypes = []
            api.symcc_qfbv_realtime_activity_protocol.restype = ctypes.c_char_p
            api.symcc_qfbv_realtime_dequeue_activity.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
            ]
            api.symcc_qfbv_realtime_dequeue_activity.restype = ctypes.c_int
            api.symcc_qfbv_realtime_enable_activity.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            api.symcc_qfbv_realtime_enable_activity.restype = ctypes.c_int
        if hasattr(api, "symcc_qfbv_realtime_compression_protocol"):
            api.symcc_qfbv_realtime_compression_protocol.argtypes = []
            api.symcc_qfbv_realtime_compression_protocol.restype = ctypes.c_char_p

    def new_context(
        self,
        *,
        max_learned_length: int,
        max_imports: int,
        max_import_literals: int,
        max_learned: int,
    ) -> "NativeRealtimeContext":
        pointer = self.library.symcc_qfbv_realtime_init(
            _bounded_int(
                max_learned_length, "maximum learned length", 0, 65_536
            ),
            _bounded_int(max_imports, "native import capacity", 0, 4096),
            _bounded_int(
                max_import_literals,
                "native import literal capacity",
                1,
                1 << 24,
            ),
            _bounded_int(max_learned, "native learned capacity", 0, 65_536),
        )
        if not pointer:
            raise RealtimeStreamError("realtime CaDiCaL context allocation failed")
        return NativeRealtimeContext(self, int(pointer))


class NativeRealtimeContext:
    """One native solver pointer; queue methods are safe during ``solve``."""

    _STAT_NAMES = (
        "imports_enqueued",
        "imports_delivered",
        "imports_rejected",
        "learned_exported",
        "learned_dropped",
        "imports_queued",
        "learned_queued",
        "acks_queued",
        "solve_generation",
        "solving",
        "imports_activated_unit",
        "imports_activated_conflict",
        "activity_queued",
        "imports_tracked",
        "import_uncompressed_bytes",
        "import_compressed_bytes",
        "import_inline_clauses",
        "import_heap_clauses",
        "queued_import_encoded_bytes",
        "compressed_literals_decoded",
        "compression_failures",
    )

    def __init__(self, owner: NativeRealtimeCadical, pointer: int) -> None:
        self.owner = owner
        self.pointer = pointer
        self._closed = False
        self._activity_buffer: Any = None
        self._activity_buffer_capacity = 0

    @property
    def _opaque(self) -> ctypes.c_void_p:
        if self._closed:
            raise RealtimeStreamError("realtime CaDiCaL context is closed")
        return ctypes.c_void_p(self.pointer)

    def _expect_zero(self, result: int, operation: str) -> None:
        if result != 0:
            raise RealtimeStreamError(f"native realtime {operation} failed: {result}")

    def add(self, literal: int) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_add(
                self._opaque, int(literal)
            )),
            "clause addition",
        )

    def assume(self, literal: int) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_assume(
                self._opaque, int(literal)
            )),
            "assumption",
        )

    def observe(self, maximum_variable: int) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_observe(
                self._opaque, int(maximum_variable)
            )),
            "variable observation",
        )

    def solve(self) -> int:
        result = int(self.owner.library.symcc_qfbv_realtime_solve(self._opaque))
        if result not in {0, 10, 20}:
            raise RealtimeStreamError(f"native realtime solve returned {result}")
        return result

    def val(self, literal: int) -> int:
        return int(self.owner.library.symcc_qfbv_realtime_val(
            self._opaque, int(literal)
        ))

    def failed(self, literal: int) -> bool:
        return bool(self.owner.library.symcc_qfbv_realtime_failed(
            self._opaque, int(literal)
        ))

    def terminate(self) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_terminate(self._opaque)),
            "termination",
        )

    def clear_termination(self) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_clear_termination(
                self._opaque
            )),
            "termination reset",
        )

    def enqueue(self, token: int, clause: Sequence[int]) -> bool:
        normalized = tuple(int(literal) for literal in clause)
        array_type = ctypes.c_int * max(1, len(normalized))
        values = array_type(*(normalized or (0,)))
        result = int(self.owner.library.symcc_qfbv_realtime_enqueue(
            self._opaque,
            _bounded_int(token, "native import token", 1, (1 << 64) - 1),
            values,
            len(normalized),
        ))
        if result < 0:
            raise RealtimeStreamError(f"native import rejected its contract: {result}")
        return result == 1

    def dequeue_ack(self) -> tuple[int, int, int] | None:
        token = ctypes.c_uint64()
        generation = ctypes.c_uint64()
        ordinal = ctypes.c_uint64()
        result = int(self.owner.library.symcc_qfbv_realtime_dequeue_ack(
            self._opaque,
            ctypes.byref(token),
            ctypes.byref(generation),
            ctypes.byref(ordinal),
        ))
        if result < 0:
            raise RealtimeStreamError("native acknowledgement queue failed")
        if result == 0:
            return None
        return int(token.value), int(generation.value), int(ordinal.value)

    def dequeue_learned(self, maximum_length: int) -> tuple[int, ...] | None:
        capacity = _bounded_int(
            maximum_length, "maximum learned length", 0, 65_536
        )
        array_type = ctypes.c_int * max(1, capacity)
        values = array_type()
        size = ctypes.c_int()
        result = int(self.owner.library.symcc_qfbv_realtime_dequeue_learned(
            self._opaque, values, capacity, ctypes.byref(size)
        ))
        if result == -2:
            raise RealtimeStreamError("native learned clause exceeds its contract")
        if result < 0:
            raise RealtimeStreamError("native learned-clause queue failed")
        if result == 0:
            return None
        length = _bounded_int(size.value, "native learned length", 0, capacity)
        return tuple(int(values[index]) for index in range(length))

    def enable_activity(self, enabled: bool) -> None:
        if self.owner.activity_protocol != CLAUSE_ACTIVITY_PROTOCOL:
            if enabled:
                raise RealtimeStreamError(
                    "native clause-activity tracking is unavailable"
                )
            return
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_enable_activity(
                self._opaque, int(enabled)
            )),
            "clause-activity configuration",
        )

    def dequeue_activity(
        self, maximum_clause_literals: int
    ) -> tuple[int, int, int, int, str, int, tuple[int, ...]] | None:
        if self.owner.activity_protocol != CLAUSE_ACTIVITY_PROTOCOL:
            raise RealtimeStreamError("native clause-activity tracking is unavailable")
        capacity = _bounded_int(
            maximum_clause_literals,
            "maximum activity witness length",
            1,
            65_536,
        )
        if self._activity_buffer_capacity < capacity:
            self._activity_buffer = (ctypes.c_int * capacity)()
            self._activity_buffer_capacity = capacity
        values = self._activity_buffer
        token = ctypes.c_uint64()
        generation = ctypes.c_uint64()
        ordinal = ctypes.c_uint64()
        level = ctypes.c_uint64()
        kind = ctypes.c_int()
        unit_literal = ctypes.c_int()
        size = ctypes.c_int()
        result = int(self.owner.library.symcc_qfbv_realtime_dequeue_activity(
            self._opaque,
            ctypes.byref(token),
            ctypes.byref(generation),
            ctypes.byref(ordinal),
            ctypes.byref(level),
            ctypes.byref(kind),
            ctypes.byref(unit_literal),
            values,
            capacity,
            ctypes.byref(size),
        ))
        if result == -2:
            raise RealtimeStreamError("native clause-activity witness is oversized")
        if result < 0:
            raise RealtimeStreamError("native clause-activity queue failed")
        if result == 0:
            return None
        activity_kind = {1: "unit", 2: "conflict"}.get(int(kind.value))
        if activity_kind is None:
            raise RealtimeStreamError("native clause-activity kind is invalid")
        length = _bounded_int(
            size.value, "native activity witness length", 0, capacity
        )
        return (
            int(token.value),
            int(generation.value),
            int(ordinal.value),
            int(level.value),
            activity_kind,
            int(unit_literal.value),
            tuple(int(values[index]) for index in range(length)),
        )

    def stats(self) -> dict[str, int]:
        return {
            name: int(self.owner.library.symcc_qfbv_realtime_stat(
                self._opaque, index
            ))
            for index, name in enumerate(self._STAT_NAMES)
        }

    def reset_queues(self) -> None:
        self._expect_zero(
            int(self.owner.library.symcc_qfbv_realtime_reset_queues(self._opaque)),
            "queue reset",
        )

    def close(self) -> None:
        if not self._closed:
            self.owner.library.symcc_qfbv_realtime_release(self._opaque)
            self._closed = True
            self.pointer = 0


@dataclass(frozen=True)
class _PendingImport:
    authorization: ClauseAuthorization
    event_sequence: int
    authorized_monotonic_ns: int
    adaptive_decision_sha256: str = ""


@dataclass(frozen=True)
class _DeferredImport:
    authorization: ClauseAuthorization
    event_sequence: int
    retry_count: int
    next_poll: int


def make_checked_import_ack(
    plan: BitBlastPlan,
    authorization: ClauseAuthorization,
    *,
    stream_id: str,
    token: int,
    event_sequence: int,
    solve_generation: int,
    delivery_ordinal: int,
    authorized_monotonic_ns: int,
    native_signature: str,
    checker_policy_sha256: str,
) -> dict[str, Any]:
    clause_sha256 = _digest(_canonical_json(list(authorization.clause)))
    body: dict[str, Any] = {
        "schema": CHECKED_IMPORT_ACK_SCHEMA,
        "protocol": REALTIME_STREAM_PROTOCOL,
        "event_stream_protocol": EVENT_STREAM_PROTOCOL,
        "clause_protocol": CLAUSE_PROTOCOL,
        "stream_id": _hex_digest(stream_id, "stream identity"),
        "token": _bounded_int(token, "stream token", 1, (1 << 64) - 1),
        "event_sequence": _bounded_int(
            event_sequence, "proof event sequence", 1, (1 << 63) - 1
        ),
        "solve_generation": _bounded_int(
            solve_generation, "native solve generation", 1, (1 << 63) - 1
        ),
        "delivery_ordinal": _bounded_int(
            delivery_ordinal, "native delivery ordinal", 1, MAX_STREAM_ACKS
        ),
        "formula_sha256": authorization.formula_sha256,
        "cnf_sha256": str(plan.certificate["cnf_sha256"]),
        "record_sha256": authorization.record_sha256,
        "clause_sha256": clause_sha256,
        "checker_policy_sha256": _hex_digest(
            checker_policy_sha256, "checker policy"
        ),
        "authorized_monotonic_ns": _bounded_int(
            authorized_monotonic_ns,
            "authorization monotonic time",
            1,
            (1 << 63) - 1,
        ),
        "native_signature": str(native_signature)[:256],
    }
    body["ack_sha256"] = _digest(_canonical_json(body))
    return body


def verify_checked_import_ack(
    plan: BitBlastPlan,
    raw: Mapping[str, Any],
    *,
    checker: IncrementalProofChecker,
) -> ClauseAuthorization:
    if raw.get("schema") != CHECKED_IMPORT_ACK_SCHEMA:
        raise RealtimeStreamError("unsupported checked-import ACK schema")
    if raw.get("protocol") != REALTIME_STREAM_PROTOCOL:
        raise RealtimeStreamError("unsupported realtime stream protocol")
    if raw.get("event_stream_protocol") != EVENT_STREAM_PROTOCOL:
        raise RealtimeStreamError("unsupported proof event stream protocol")
    if raw.get("clause_protocol") != CLAUSE_PROTOCOL:
        raise RealtimeStreamError("checked-import clause protocol changed")
    if checker.store is None:
        raise RealtimeStreamError("checked-import ACK requires a proof store")
    body = dict(raw)
    ack_sha256 = _hex_digest(body.pop("ack_sha256", ""), "ACK identity")
    if _digest(_canonical_json(body)) != ack_sha256:
        raise RealtimeStreamError("checked-import ACK identity changed")
    _hex_digest(body.get("stream_id"), "stream identity")
    _bounded_int(body.get("token"), "stream token", 1, (1 << 64) - 1)
    _bounded_int(
        body.get("event_sequence"), "proof event sequence", 1, (1 << 63) - 1
    )
    _bounded_int(
        body.get("solve_generation"),
        "native solve generation",
        1,
        (1 << 63) - 1,
    )
    _bounded_int(
        body.get("delivery_ordinal"),
        "native delivery ordinal",
        1,
        MAX_STREAM_ACKS,
    )
    _bounded_int(
        body.get("authorized_monotonic_ns"),
        "authorization monotonic time",
        1,
        (1 << 63) - 1,
    )
    if body.get("cnf_sha256") != plan.certificate["cnf_sha256"]:
        raise RealtimeStreamError("checked-import ACK CNF identity changed")
    policy = _hex_digest(body.get("checker_policy_sha256"), "checker policy")
    if policy != checker.policy_sha256:
        raise RealtimeStreamError("checked-import ACK checker policy changed")
    signature = str(body.get("native_signature", ""))
    if not signature.startswith("symcc-qfbv-realtime-v1|cadical-3.0."):
        raise RealtimeStreamError("checked-import ACK native signature is invalid")
    record_sha256 = _hex_digest(body.get("record_sha256"), "proof record")
    try:
        authorization = checker.verify_clause_record(
            plan, checker.store.load(record_sha256)
        )
    except (OSError, sqlite3.Error, IncrementalProofError) as error:
        raise RealtimeStreamError(
            "checked-import ACK proof cannot be independently replayed"
        ) from error
    if authorization.formula_sha256 != body.get("formula_sha256"):
        raise RealtimeStreamError("checked-import ACK formula identity changed")
    if _digest(_canonical_json(list(authorization.clause))) != body.get(
        "clause_sha256"
    ):
        raise RealtimeStreamError("checked-import ACK clause identity changed")
    return authorization


def _expected_activity_witness(
    clause: Sequence[int], kind: str, unit_literal: int
) -> tuple[int, ...]:
    normalized = tuple(clause)
    if kind == "unit":
        if unit_literal == 0 or normalized.count(unit_literal) != 1:
            raise RealtimeStreamError("unit activity has an invalid open literal")
        return tuple(-literal for literal in normalized if literal != unit_literal)
    if kind == "conflict":
        if unit_literal != 0:
            raise RealtimeStreamError("conflict activity carries an open literal")
        return tuple(-literal for literal in normalized)
    raise RealtimeStreamError("clause-activity kind is invalid")


def make_clause_activity_receipt(
    plan: BitBlastPlan,
    authorization: ClauseAuthorization,
    ack: Mapping[str, Any],
    *,
    token: int,
    solve_generation: int,
    activity_ordinal: int,
    decision_level: int,
    kind: str,
    unit_literal: int,
    falsifying_assignments: Sequence[int],
    native_signature: str,
) -> dict[str, Any]:
    checked_unit_literal = _bounded_int(
        unit_literal,
        "activity unit literal",
        -plan.max_variable,
        plan.max_variable,
    )
    if type(kind) is not str:
        raise RealtimeStreamError("clause-activity kind is invalid")
    expected = _expected_activity_witness(
        authorization.clause, kind, checked_unit_literal
    )
    witness = tuple(
        _bounded_int(
            literal,
            "activity witness literal",
            -plan.max_variable,
            plan.max_variable,
        )
        for literal in falsifying_assignments
    )
    if witness != expected:
        raise RealtimeStreamError("native clause-activity witness is inconsistent")
    body: dict[str, Any] = {
        "schema": CLAUSE_ACTIVITY_SCHEMA,
        "protocol": CLAUSE_ACTIVITY_PROTOCOL,
        "realtime_protocol": REALTIME_STREAM_PROTOCOL,
        "stream_id": _hex_digest(ack.get("stream_id"), "activity stream"),
        "token": _bounded_int(token, "activity token", 1, (1 << 64) - 1),
        "solve_generation": _bounded_int(
            solve_generation, "activity solve generation", 1, (1 << 63) - 1
        ),
        "activity_ordinal": _bounded_int(
            activity_ordinal, "native activity ordinal", 1, MAX_STREAM_ACKS
        ),
        "decision_level": _bounded_int(
            decision_level, "native activity decision level", 0, (1 << 63) - 1
        ),
        "kind": kind,
        "unit_literal": checked_unit_literal,
        "falsifying_assignments": list(witness),
        "formula_sha256": authorization.formula_sha256,
        "cnf_sha256": str(plan.certificate["cnf_sha256"]),
        "record_sha256": authorization.record_sha256,
        "clause_sha256": _digest(_canonical_json(list(authorization.clause))),
        "source_worker": authorization.source_worker,
        "ack_sha256": _hex_digest(ack.get("ack_sha256"), "activity ACK"),
        "native_signature": str(native_signature)[:256],
    }
    if (
        body["token"] != ack.get("token")
        or body["solve_generation"] != ack.get("solve_generation")
        or body["record_sha256"] != ack.get("record_sha256")
        or body["native_signature"] != ack.get("native_signature")
    ):
        raise RealtimeStreamError("clause activity and delivery ACK disagree")
    body["activity_sha256"] = _digest(_canonical_json(body))
    return body


def verify_clause_activity_receipt(
    plan: BitBlastPlan,
    raw: Mapping[str, Any],
    *,
    ack: Mapping[str, Any],
    checker: IncrementalProofChecker,
) -> ClauseAuthorization:
    if not isinstance(raw, Mapping):
        raise RealtimeStreamError("clause-activity receipt must be an object")
    body = dict(raw)
    supplied = _hex_digest(body.pop("activity_sha256", ""), "activity identity")
    if _digest(_canonical_json(body)) != supplied:
        raise RealtimeStreamError("clause-activity receipt identity changed")
    expected_fields = {
        "schema", "protocol", "realtime_protocol", "stream_id", "token",
        "solve_generation", "activity_ordinal", "decision_level", "kind",
        "unit_literal", "falsifying_assignments", "formula_sha256",
        "cnf_sha256", "record_sha256", "clause_sha256", "source_worker",
        "ack_sha256", "native_signature",
    }
    if set(body) != expected_fields:
        raise RealtimeStreamError("clause-activity receipt shape changed")
    if (
        body.get("schema") != CLAUSE_ACTIVITY_SCHEMA
        or body.get("protocol") != CLAUSE_ACTIVITY_PROTOCOL
        or body.get("realtime_protocol") != REALTIME_STREAM_PROTOCOL
    ):
        raise RealtimeStreamError("clause-activity receipt scope changed")
    authorization = verify_checked_import_ack(plan, ack, checker=checker)
    if body.get("ack_sha256") != ack.get("ack_sha256"):
        raise RealtimeStreamError("clause activity names another ACK")
    token = _bounded_int(body.get("token"), "activity token", 1, (1 << 64) - 1)
    generation = _bounded_int(
        body.get("solve_generation"),
        "activity solve generation",
        1,
        (1 << 63) - 1,
    )
    _bounded_int(
        body.get("activity_ordinal"),
        "native activity ordinal",
        1,
        MAX_STREAM_ACKS,
    )
    _bounded_int(
        body.get("decision_level"),
        "native activity decision level",
        0,
        (1 << 63) - 1,
    )
    if token != ack.get("token") or generation != ack.get("solve_generation"):
        raise RealtimeStreamError("clause activity and ACK generation disagree")
    if (
        body.get("stream_id") != ack.get("stream_id")
        or body.get("formula_sha256") != authorization.formula_sha256
        or body.get("cnf_sha256") != plan.certificate["cnf_sha256"]
        or body.get("record_sha256") != authorization.record_sha256
        or body.get("source_worker") != authorization.source_worker
        or body.get("native_signature") != ack.get("native_signature")
        or body.get("clause_sha256")
        != _digest(_canonical_json(list(authorization.clause)))
    ):
        raise RealtimeStreamError("clause-activity proof identity changed")
    witness_raw = body.get("falsifying_assignments")
    if not isinstance(witness_raw, list):
        raise RealtimeStreamError("clause-activity witness must be a list")
    witness = tuple(
        _bounded_int(
            literal,
            "activity witness literal",
            -plan.max_variable,
            plan.max_variable,
        )
        for literal in witness_raw
    )
    kind = body.get("kind")
    if type(kind) is not str:
        raise RealtimeStreamError("clause-activity kind is invalid")
    expected = _expected_activity_witness(
        authorization.clause,
        kind,
        _bounded_int(
            body.get("unit_literal"),
            "activity unit literal",
            -plan.max_variable,
            plan.max_variable,
        ),
    )
    if witness != expected:
        raise RealtimeStreamError("clause-activity witness does not match the clause")
    return authorization


class RealtimeClauseExchangeSession:
    """Poll a durable proof stream while one native CDCL solve is active."""

    def __init__(
        self,
        plan: BitBlastPlan,
        native: NativeRealtimeContext,
        proof_store: IncrementalProofStore,
        proof_checker: IncrementalProofChecker,
        *,
        native_signature: str,
        source_worker: str,
        worker_epoch: int,
        next_sequence: Callable[[], int],
        stream_ordinal: int,
        seen_records: Sequence[str] = (),
        seen_clauses: Sequence[Sequence[int]] = (),
        max_imports: int = 64,
        max_events: int = 4096,
        max_learned: int = 64,
        max_learned_length: int = 32,
        poll_interval_ms: int = 2,
        checker_budget_ms: int = 100,
        adaptive_controller: AdaptiveProofController | None = None,
        pairing_controller: UtilityPairingController | None = None,
        solve_budget_ms: int = 0,
        track_clause_activity: bool = False,
    ) -> None:
        if proof_checker.store is None or proof_checker.store.root != proof_store.root:
            raise RealtimeStreamError("stream checker and proof store must agree")
        self.plan = plan
        self.native = native
        self.proof_store = proof_store
        self.proof_checker = proof_checker
        self.native_signature = str(native_signature)
        self.source_worker = str(source_worker)[:256]
        self.worker_epoch = _bounded_int(
            worker_epoch, "stream worker epoch", 0, (1 << 63) - 1
        )
        self.next_sequence = next_sequence
        self.max_imports = _bounded_int(
            max_imports, "stream maximum imports", 0, MAX_STREAM_IMPORTS
        )
        self.max_events = _bounded_int(
            max_events, "stream maximum events", 1, MAX_STREAM_EVENTS
        )
        self.max_learned = _bounded_int(
            max_learned, "stream maximum learned clauses", 0, MAX_LEARNED_CLAUSES
        )
        self.max_learned_length = _bounded_int(
            max_learned_length, "stream maximum learned length", 0, 65_536
        )
        self.poll_interval = _bounded_int(
            poll_interval_ms, "stream poll interval", 1, 1000
        ) / 1000.0
        self.checker_budget_ns = _bounded_int(
            checker_budget_ms, "stream checker budget", 1, 60_000
        ) * 1_000_000
        self.adaptive_controller = adaptive_controller
        self.pairing_controller = pairing_controller
        self.solve_budget_ms = _bounded_int(
            solve_budget_ms, "stream solve budget", 0, 3_600_000
        )
        if not isinstance(track_clause_activity, bool):
            raise RealtimeStreamError("clause-activity option must be boolean")
        if pairing_controller is not None and not track_clause_activity:
            raise RealtimeStreamError(
                "utility pairing requires clause-activity tracking"
            )
        if (
            track_clause_activity
            and getattr(
                getattr(native, "owner", None), "activity_protocol", ""
            ) != CLAUSE_ACTIVITY_PROTOCOL
        ):
            raise RealtimeStreamError(
                "clause-activity tracking requires a capable native shim"
            )
        self.track_clause_activity = track_clause_activity
        self.started_monotonic_ns = time.monotonic_ns()
        stream_body = {
            "protocol": REALTIME_STREAM_PROTOCOL,
            "formula_sha256": plan.formula_sha256,
            "cnf_sha256": plan.certificate["cnf_sha256"],
            "source_worker": self.source_worker,
            "worker_epoch": self.worker_epoch,
            "stream_ordinal": _bounded_int(
                stream_ordinal, "stream ordinal", 0, (1 << 63) - 1
            ),
            "native_signature": self.native_signature,
            "started_monotonic_ns": self.started_monotonic_ns,
        }
        self.stream_id = _digest(_canonical_json(stream_body))
        if self.adaptive_controller is not None:
            self.adaptive_controller.begin_stream(self.stream_id)
        self._pairing_formula_family = (
            formula_family_sha256(plan.certificate)
            if self.pairing_controller is not None
            else ""
        )
        if self.pairing_controller is not None:
            self.pairing_controller.begin_stream(self.stream_id)
        self._formula_digests = tuple(dict.fromkeys(
            increment.formula_sha256 for increment in reversed(plan.increments)
        ))
        self._cursors = {
            digest: max(
                0,
                self.proof_store.latest_event_sequence(digest)
                - self.max_events,
            )
            for digest in self._formula_digests
        }
        self._seen = set(map(str, seen_records))
        self._seen_clauses = {
            tuple(int(literal) for literal in clause)
            for clause in seen_clauses
        }
        self._pending: dict[int, _PendingImport] = {}
        self._deferred: dict[str, _DeferredImport] = {}
        self._acks: list[dict[str, Any]] = []
        self._acks_by_token: dict[int, dict[str, Any]] = {}
        self._delivered_by_token: dict[int, _PendingImport] = {}
        self._delivered: list[str] = []
        self._activity_receipts: list[dict[str, Any]] = []
        self._activity_records: set[str] = set()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"qfbv-proof-stream-{self.stream_id[:12]}",
            daemon=True,
        )
        self._token = 0
        self._fatal_error = ""
        self._baseline = native.stats()
        self._polls = 0
        self._events = 0
        self._candidates = 0
        self._settled_candidates = 0
        self._duplicate_clauses = 0
        self._authorized = 0
        self._rejected = 0
        self._backpressure = 0
        self._checker_elapsed_us = 0
        self._learned_candidates = 0
        self._learned_published = 0
        self._learned_rejected = 0
        self._learned_created = 0
        self._learned_records: list[str] = []
        self._solve_generation = 0
        self._solve_started_monotonic_ns = 0
        self._adaptive_decisions: list[dict[str, Any]] = []
        self._adaptive_enqueued_decisions: dict[str, str] = {}
        self._adaptive_rejected = 0
        self._adaptive_retried = 0
        self._adaptive_budget_exhausted = False
        self._adaptive_budget_dropped = 0
        self._pairing_decisions: list[dict[str, Any]] = []
        self._pairing_outcomes: list[dict[str, Any]] = []
        self._pairing_pending: dict[str, str] = {}
        self._pairing_suppressed = 0

    def start(self) -> None:
        self.native.reset_queues()
        if self.track_clause_activity:
            self.native.enable_activity(True)
        self._thread.start()
        if not self._started.wait(timeout=2.0):
            self._stop.set()
            self._thread.join(timeout=2.0)
            raise RealtimeStreamError("realtime proof stream did not start")

    def abort(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            raise RealtimeStreamError("realtime proof stream did not abort")
        self._settle_pairing_pending()
        self._expire_adaptive_pending()
        self.native.reset_queues()

    def progress(self) -> dict[str, Any]:
        """Return a bounded live snapshot for readiness and experiment gates."""
        actions = {
            action: sum(
                decision["action"] == action
                for decision in self._adaptive_decisions
            )
            for action in ("admit", "defer", "reject")
        }
        return {
            "events": self._events,
            "candidates": self._candidates,
            "settled_candidates": self._settled_candidates,
            "duplicate_clauses": self._duplicate_clauses,
            "authorized": self._authorized,
            "backpressure": self._backpressure,
            "pending": len(self._pending),
            "deferred": len(self._deferred),
            "adaptive_actions": actions,
            "decision_budget_exhausted": self._adaptive_budget_exhausted,
            "fatal_error": self._fatal_error,
            "activity_receipts": len(self._activity_receipts),
            "activity_unit": sum(
                receipt["kind"] == "unit"
                for receipt in self._activity_receipts
            ),
            "activity_conflict": sum(
                receipt["kind"] == "conflict"
                for receipt in self._activity_receipts
            ),
            "pairing_decisions": len(self._pairing_decisions),
            "pairing_suppressed": self._pairing_suppressed,
        }

    def _next_token(self) -> int:
        self._token += 1
        if self._token > (1 << 64) - 1:
            raise RealtimeStreamError("realtime import token space is exhausted")
        return self._token

    def _drain_acks(self) -> None:
        while True:
            raw = self.native.dequeue_ack()
            if raw is None:
                return
            token, generation, ordinal = raw
            pending = self._pending.pop(token, None)
            if pending is None:
                raise RealtimeStreamError("native returned an unknown import ACK")
            ack = make_checked_import_ack(
                self.plan,
                pending.authorization,
                stream_id=self.stream_id,
                token=token,
                event_sequence=pending.event_sequence,
                solve_generation=generation,
                delivery_ordinal=ordinal,
                authorized_monotonic_ns=pending.authorized_monotonic_ns,
                native_signature=self.native_signature,
                checker_policy_sha256=self.proof_checker.policy_sha256,
            )
            self._acks.append(ack)
            self._acks_by_token[token] = ack
            self._delivered_by_token[token] = pending
            self._delivered.append(pending.authorization.record_sha256)
            if self.adaptive_controller is not None:
                if not pending.adaptive_decision_sha256:
                    raise RealtimeStreamError(
                        "adaptive native ACK lacks its admission decision"
                    )
                latency_us = max(
                    0,
                    (time.monotonic_ns() - pending.authorized_monotonic_ns)
                    // 1000,
                )
                self.adaptive_controller.observe_delivery(
                    pending.adaptive_decision_sha256,
                    latency_us=latency_us,
                )

    def _drain_activity(self) -> None:
        if not self.track_clause_activity:
            return
        while True:
            raw = self.native.dequeue_activity(65_536)
            if raw is None:
                return
            (
                token,
                generation,
                ordinal,
                level,
                kind,
                unit_literal,
                witness,
            ) = raw
            pending = self._delivered_by_token.get(token)
            ack = self._acks_by_token.get(token)
            if pending is None or ack is None:
                raise RealtimeStreamError(
                    "native activity has no delivered import ACK"
                )
            record = pending.authorization.record_sha256
            if record in self._activity_records:
                raise RealtimeStreamError(
                    "native emitted duplicate activity for one import"
                )
            receipt = make_clause_activity_receipt(
                self.plan,
                pending.authorization,
                ack,
                token=token,
                solve_generation=generation,
                activity_ordinal=ordinal,
                decision_level=level,
                kind=kind,
                unit_literal=unit_literal,
                falsifying_assignments=witness,
                native_signature=self.native_signature,
            )
            self._activity_receipts.append(receipt)
            self._activity_records.add(record)

    def _expire_adaptive_pending(self) -> None:
        if self.adaptive_controller is None:
            return
        for pending in tuple(self._pending.values()):
            if pending.adaptive_decision_sha256:
                self.adaptive_controller.observe_expired(
                    pending.adaptive_decision_sha256
                )
        self._pending.clear()

    def _pairing_candidate(
        self,
        authorization: ClauseAuthorization,
        event_sequence: int,
        event_lag: int,
    ) -> UtilityPairingCandidate:
        return UtilityPairingCandidate(
            record_sha256=authorization.record_sha256,
            stream_id=self.stream_id,
            publisher_worker=authorization.source_worker,
            consumer_worker=self.source_worker,
            formula_family_sha256=self._pairing_formula_family,
            event_sequence=event_sequence,
            event_lag=event_lag,
            checker_elapsed_us=authorization.checker_elapsed_us,
        )

    def _pairing_consider(
        self,
        authorization: ClauseAuthorization,
        event_sequence: int,
        event_lag: int,
    ) -> bool:
        controller = self.pairing_controller
        if controller is None:
            return True
        decision = controller.consider(self._pairing_candidate(
            authorization, event_sequence, event_lag
        ))
        self._pairing_decisions.append(decision)
        if decision["action"] == "suppress":
            self._pairing_suppressed += 1
            return False
        record = authorization.record_sha256
        if record in self._pairing_pending:
            raise RealtimeStreamError("pairing record has duplicate admission")
        self._pairing_pending[record] = str(decision["decision_sha256"])
        return True

    def _pairing_observe(self, record: str, outcome: str) -> None:
        if self.pairing_controller is None:
            return
        decision = self._pairing_pending.pop(record, None)
        if decision is None:
            raise RealtimeStreamError("pairing outcome lacks an admission")
        self._pairing_outcomes.append(
            self.pairing_controller.observe(decision, outcome=outcome)
        )

    def _settle_pairing_pending(self) -> None:
        if self.pairing_controller is None:
            return
        activity = {
            str(receipt["record_sha256"]): str(receipt["kind"])
            for receipt in self._activity_receipts
        }
        delivered = set(self._delivered)
        for record in tuple(self._pairing_pending):
            if record in activity:
                outcome = activity[record]
            elif record in delivered:
                outcome = "unactivated"
            else:
                outcome = "expired"
            self._pairing_observe(record, outcome)

    def _adaptive_observation(self) -> AdaptiveProofObservation:
        stats = self.native.stats()
        now_ns = time.monotonic_ns()
        solving = bool(stats["solving"])
        if solving and not self._solve_started_monotonic_ns:
            self._solve_started_monotonic_ns = now_ns
        elapsed_ms = (
            max(
                0,
                (now_ns - self._solve_started_monotonic_ns) // 1_000_000,
            )
            if self._solve_started_monotonic_ns
            else 0
        )
        remaining_ms = (
            max(0, self.solve_budget_ms - elapsed_ms)
            if self.solve_budget_ms
            else 3_600_000
        )
        return AdaptiveProofObservation(
            solve_generation=int(stats["solve_generation"]),
            solving=solving,
            solve_age_ms=min(elapsed_ms, 3_600_000),
            remaining_ms=remaining_ms,
            queue_depth=min(len(self._pending), 4096),
            ack_depth=min(int(stats["acks_queued"]), 4096),
            deferred_depth=min(len(self._deferred), 4096),
        )

    def _adaptive_candidate(
        self,
        authorization: ClauseAuthorization,
        event_sequence: int,
        retry_count: int,
    ) -> AdaptiveProofCandidate:
        return AdaptiveProofCandidate(
            record_sha256=authorization.record_sha256,
            formula_sha256=authorization.formula_sha256,
            stream_id=self.stream_id,
            source_worker=authorization.source_worker,
            event_sequence=event_sequence,
            clause_literals=len(authorization.clause),
            proof_steps=authorization.proof_steps,
            propagation_count=authorization.propagation_count,
            checker_elapsed_us=authorization.checker_elapsed_us,
            retry_count=retry_count,
        )

    def _schedule_deferred(
        self,
        authorization: ClauseAuthorization,
        event_sequence: int,
        retry_count: int,
    ) -> None:
        assert self.adaptive_controller is not None
        delay = 1 << min(retry_count, 6)
        self._deferred[authorization.record_sha256] = _DeferredImport(
            authorization=authorization,
            event_sequence=event_sequence,
            retry_count=retry_count,
            next_poll=self._polls + delay,
        )

    def _adaptive_attempt(
        self,
        authorization: ClauseAuthorization,
        event_sequence: int,
        retry_count: int,
    ) -> None:
        controller = self.adaptive_controller
        assert controller is not None
        if not controller.can_decide:
            self._adaptive_budget_exhausted = True
            self._adaptive_budget_dropped += 1
            self._adaptive_rejected += 1
            return
        candidate = self._adaptive_candidate(
            authorization, event_sequence, retry_count
        )
        decision = controller.consider(candidate, self._adaptive_observation())
        self._adaptive_decisions.append(decision)
        action = str(decision["action"])
        if action == "reject":
            self._adaptive_rejected += 1
            return
        if action == "defer":
            self._schedule_deferred(
                authorization, event_sequence, retry_count + 1
            )
            return
        token = self._next_token()
        authorized_ns = time.monotonic_ns()
        try:
            enqueued = self.native.enqueue(token, authorization.clause)
        except RealtimeStreamError:
            self.native.terminate()
            raise
        decision_sha256 = str(decision["decision_sha256"])
        if not enqueued:
            controller.observe_backpressure(decision_sha256)
            self._backpressure += 1
            self._schedule_deferred(
                authorization, event_sequence, retry_count + 1
            )
            return
        self._seen_clauses.add(authorization.clause)
        self._pending[token] = _PendingImport(
            authorization=authorization,
            event_sequence=event_sequence,
            authorized_monotonic_ns=authorized_ns,
            adaptive_decision_sha256=decision_sha256,
        )
        self._adaptive_enqueued_decisions[
            authorization.record_sha256
        ] = decision_sha256
        self._authorized += 1

    def _retry_deferred(self) -> None:
        if (
            self.adaptive_controller is None
            or not self._deferred
            or self._authorized >= self.max_imports
        ):
            return
        if not self.adaptive_controller.can_decide:
            self._adaptive_budget_exhausted = True
            return
        available = max(
            0,
            self.adaptive_controller.policy.queue_capacity - len(self._pending),
        )
        if available == 0:
            return
        eligible = sorted(
            (
                item for item in self._deferred.values()
                if item.next_poll <= self._polls
            ),
            key=lambda item: (
                item.retry_count,
                item.event_sequence,
                item.authorization.record_sha256,
            ),
        )[:available]
        for item in eligible:
            self._deferred.pop(item.authorization.record_sha256, None)
            decisions_before = len(self._adaptive_decisions)
            self._adaptive_attempt(
                item.authorization, item.event_sequence, item.retry_count
            )
            self._adaptive_retried += int(
                len(self._adaptive_decisions) > decisions_before
            )

    def _publish_learned(self, clause: tuple[int, ...]) -> None:
        self._learned_candidates += 1
        if self._learned_candidates > self.max_learned:
            self._learned_rejected += 1
            return
        deadline_ns = time.monotonic_ns() + self.checker_budget_ns
        try:
            record = make_rup_clause_record(
                self.plan,
                clause,
                source_worker=self.source_worker,
                worker_epoch=self.worker_epoch,
                sequence=self.next_sequence(),
                deadline_ns=deadline_ns,
            )
            authorization = self.proof_checker.verify_clause_record(
                self.plan, record
            )
            digest, created = self.proof_store.publish(record)
            if digest != authorization.record_sha256:
                raise RealtimeStreamError("published learned proof identity changed")
            self._seen.add(digest)
            self._seen_clauses.add(authorization.clause)
            self._learned_published += 1
            self._learned_created += int(created)
            self._learned_records.append(digest)
            self._checker_elapsed_us += authorization.checker_elapsed_us
        except (
            OSError,
            sqlite3.Error,
            IncrementalProofError,
            RealtimeStreamError,
        ):
            self._learned_rejected += 1

    def _drain_learned(self) -> None:
        while self._learned_candidates < self.max_learned:
            clause = self.native.dequeue_learned(self.max_learned_length)
            if clause is None:
                return
            self._publish_learned(clause)

    def _scan_events(self) -> None:
        self._retry_deferred()
        if (
            self.adaptive_controller is not None
            and not self.adaptive_controller.can_decide
        ):
            self._adaptive_budget_exhausted = True
            return
        if self._events >= self.max_events or self._authorized >= self.max_imports:
            return
        for formula in self._formula_digests:
            if self._events >= self.max_events or self._authorized >= self.max_imports:
                break
            batch_limit = min(64, self.max_events - self._events)
            events = self.proof_store.events_after(
                formula,
                after_sequence=self._cursors[formula],
                limit=max(1, batch_limit),
            )
            latest_sequence = self.proof_store.latest_event_sequence(formula)
            for event_sequence, digest in events:
                if (
                    self._events >= self.max_events
                    or self._authorized >= self.max_imports
                ):
                    break
                self._cursors[formula] = event_sequence
                self._events += 1
                if digest in self._seen:
                    continue
                self._seen.add(digest)
                self._candidates += 1
                try:
                    authorization = self.proof_checker.verify_clause_record(
                        self.plan, self.proof_store.load(digest)
                    )
                except (OSError, sqlite3.Error, IncrementalProofError):
                    self._rejected += 1
                    self._settled_candidates += 1
                    continue
                self._checker_elapsed_us += authorization.checker_elapsed_us
                if authorization.clause in self._seen_clauses:
                    self._duplicate_clauses += 1
                    self._settled_candidates += 1
                    continue
                if not self._pairing_consider(
                    authorization,
                    event_sequence,
                    max(0, latest_sequence - event_sequence),
                ):
                    self._settled_candidates += 1
                    continue
                if self.adaptive_controller is not None:
                    self._adaptive_attempt(authorization, event_sequence, 0)
                    self._settled_candidates += 1
                    continue
                token = self._next_token()
                authorized_ns = time.monotonic_ns()
                try:
                    enqueued = self.native.enqueue(token, authorization.clause)
                except RealtimeStreamError:
                    self.native.terminate()
                    raise
                if not enqueued:
                    self._backpressure += 1
                    self._pairing_observe(
                        authorization.record_sha256, "backpressure"
                    )
                    self._settled_candidates += 1
                    continue
                self._seen_clauses.add(authorization.clause)
                self._pending[token] = _PendingImport(
                    authorization=authorization,
                    event_sequence=event_sequence,
                    authorized_monotonic_ns=authorized_ns,
                )
                self._authorized += 1
                self._settled_candidates += 1

    def _run(self) -> None:
        self._started.set()
        try:
            while not self._stop.is_set():
                self._polls += 1
                self._drain_acks()
                self._drain_activity()
                self._drain_learned()
                self._scan_events()
                self._stop.wait(self.poll_interval)
            self._drain_acks()
            self._drain_activity()
            self._drain_learned()
            self._drain_acks()
            self._drain_activity()
        except (
            AdaptiveExchangeError,
            UtilityPairingError,
            OSError,
            sqlite3.Error,
            IncrementalProofError,
            RealtimeStreamError,
        ) as error:
            self._fatal_error = str(error)[:512]
            try:
                self.native.terminate()
            except RealtimeStreamError:
                pass

    def finish(self, solve_generation: int) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.checker_budget_ns / 1e9 + 1.0))
        if self._thread.is_alive():
            raise RealtimeStreamError("realtime proof stream did not quiesce")
        self._solve_generation = _bounded_int(
            solve_generation, "stream solve generation", 1, (1 << 63) - 1
        )
        delivered_ordinals = [int(ack["delivery_ordinal"]) for ack in self._acks]
        activity_ordinals = [
            int(receipt["activity_ordinal"])
            for receipt in self._activity_receipts
        ]
        if any(
            int(ack["solve_generation"]) != self._solve_generation
            for ack in self._acks
        ):
            raise RealtimeStreamError("native ACK belongs to another solve generation")
        if delivered_ordinals != list(range(1, len(delivered_ordinals) + 1)):
            raise RealtimeStreamError("native ACK delivery ordinals are not contiguous")
        if activity_ordinals != list(range(1, len(activity_ordinals) + 1)):
            raise RealtimeStreamError(
                "native clause-activity ordinals are not contiguous"
            )
        final_stats = self.native.stats()
        if final_stats["solving"] != 0:
            raise RealtimeStreamError(
                "realtime proof stream cannot finish during an active solve"
            )
        counter_names = (
            "imports_enqueued",
            "imports_delivered",
            "imports_rejected",
            "learned_exported",
            "learned_dropped",
            "imports_activated_unit",
            "imports_activated_conflict",
        )
        compression_counter_names = (
            "import_uncompressed_bytes",
            "import_compressed_bytes",
            "import_inline_clauses",
            "import_heap_clauses",
            "compressed_literals_decoded",
            "compression_failures",
        ) if self.native.owner.compression_protocol else ()
        counter_names += compression_counter_names
        if any(
            final_stats[key] < self._baseline[key] for key in counter_names
        ):
            raise RealtimeStreamError("native realtime counters moved backwards")
        native_delta = {
            key: final_stats[key] - self._baseline[key]
            for key in counter_names
        }
        if native_delta["imports_delivered"] != len(self._acks):
            raise RealtimeStreamError("native delivery count disagrees with ACKs")
        if native_delta["imports_enqueued"] != self._authorized:
            raise RealtimeStreamError("native enqueue count disagrees with checker")
        if compression_counter_names and (
            native_delta["compression_failures"] != 0
            or native_delta["import_inline_clauses"]
            + native_delta["import_heap_clauses"]
            != native_delta["imports_enqueued"]
            or final_stats["queued_import_encoded_bytes"]
            > native_delta["import_compressed_bytes"]
        ):
            raise RealtimeStreamError(
                "native clause-compression accounting is inconsistent"
            )
        unit_activities = sum(
            receipt["kind"] == "unit" for receipt in self._activity_receipts
        )
        conflict_activities = sum(
            receipt["kind"] == "conflict"
            for receipt in self._activity_receipts
        )
        if (
            native_delta["imports_activated_unit"] != unit_activities
            or native_delta["imports_activated_conflict"]
            != conflict_activities
            or len(self._activity_receipts) > len(self._acks)
        ):
            raise RealtimeStreamError(
                "native clause-activity accounting disagrees with receipts"
            )
        if self.track_clause_activity and (
            final_stats["activity_queued"] != 0
            or final_stats["imports_tracked"] != len(self._acks)
        ):
            raise RealtimeStreamError(
                "native clause-activity queue or tracked imports are not quiescent"
            )
        pending = len(self._pending)
        self._settle_pairing_pending()
        self._expire_adaptive_pending()
        result = {
            "backend_realtime_stream_protocol": REALTIME_STREAM_PROTOCOL,
            "backend_realtime_event_stream_protocol": EVENT_STREAM_PROTOCOL,
            "backend_realtime_stream_id": self.stream_id,
            "backend_realtime_native_signature": self.native_signature,
            "backend_realtime_solve_generation": self._solve_generation,
            "backend_realtime_polls": self._polls,
            "backend_realtime_events_observed": self._events,
            "backend_realtime_import_candidates": self._candidates,
            "backend_realtime_import_settled_candidates": (
                self._settled_candidates
            ),
            "backend_realtime_import_duplicate_clauses": (
                self._duplicate_clauses
            ),
            "backend_realtime_import_authorized": self._authorized,
            "backend_realtime_import_delivered": len(self._acks),
            "backend_realtime_import_pending": pending,
            "backend_realtime_import_rejected": self._rejected,
            "backend_realtime_import_backpressure": self._backpressure,
            "backend_realtime_import_checker_elapsed_us": (
                self._checker_elapsed_us
            ),
            "backend_realtime_import_record_sha256": list(self._delivered),
            "backend_realtime_import_acks": list(self._acks),
            "backend_realtime_clause_activity_enabled": (
                self.track_clause_activity
            ),
            "backend_realtime_clause_activity_protocol": (
                CLAUSE_ACTIVITY_PROTOCOL if self.track_clause_activity else ""
            ),
            "backend_realtime_clause_activity_unit": unit_activities,
            "backend_realtime_clause_activity_conflict": conflict_activities,
            "backend_realtime_clause_activity_unactivated": (
                len(self._acks) - len(self._activity_receipts)
            ),
            "backend_realtime_clause_activity_receipts": list(
                self._activity_receipts
            ),
            "backend_realtime_learned_candidates": self._learned_candidates,
            "backend_realtime_learned_published": self._learned_published,
            "backend_realtime_learned_created": self._learned_created,
            "backend_realtime_learned_rejected": self._learned_rejected,
            "backend_realtime_learned_record_sha256": list(
                self._learned_records
            ),
            "backend_realtime_native_imports_enqueued": native_delta[
                "imports_enqueued"
            ],
            "backend_realtime_native_imports_delivered": native_delta[
                "imports_delivered"
            ],
            "backend_realtime_native_imports_rejected": native_delta[
                "imports_rejected"
            ],
            "backend_realtime_native_learned_exported": native_delta[
                "learned_exported"
            ],
            "backend_realtime_native_learned_dropped": native_delta[
                "learned_dropped"
            ],
            "backend_realtime_native_imports_activated_unit": native_delta[
                "imports_activated_unit"
            ],
            "backend_realtime_native_imports_activated_conflict": native_delta[
                "imports_activated_conflict"
            ],
            "backend_realtime_stream_error": self._fatal_error,
        }
        if compression_counter_names:
            result.update({
                "backend_realtime_clause_compression_protocol": (
                    CLAUSE_COMPRESSION_PROTOCOL
                ),
                "backend_realtime_clause_uncompressed_bytes": native_delta[
                    "import_uncompressed_bytes"
                ],
                "backend_realtime_clause_compressed_bytes": native_delta[
                    "import_compressed_bytes"
                ],
                "backend_realtime_clause_inline_clauses": native_delta[
                    "import_inline_clauses"
                ],
                "backend_realtime_clause_heap_clauses": native_delta[
                    "import_heap_clauses"
                ],
                "backend_realtime_clause_queued_encoded_bytes": final_stats[
                    "queued_import_encoded_bytes"
                ],
                "backend_realtime_clause_decoded_literals": native_delta[
                    "compressed_literals_decoded"
                ],
                "backend_realtime_clause_compression_failures": native_delta[
                    "compression_failures"
                ],
            })
        if self.track_clause_activity:
            self.native.enable_activity(False)
        if self.adaptive_controller is not None:
            actions = {
                action: sum(
                    decision["action"] == action
                    for decision in self._adaptive_decisions
                )
                for action in ("admit", "defer", "reject")
            }
            result.update({
                "backend_realtime_adaptive_protocol": (
                    ADAPTIVE_EXCHANGE_PROTOCOL
                ),
                "backend_realtime_adaptive_policy": (
                    self.adaptive_controller.policy.as_dict()
                ),
                "backend_realtime_adaptive_policy_sha256": (
                    self.adaptive_controller.policy.sha256
                ),
                "backend_realtime_adaptive_decisions": list(
                    self._adaptive_decisions
                ),
                "backend_realtime_adaptive_action_counts": actions,
                "backend_realtime_adaptive_retried": self._adaptive_retried,
                "backend_realtime_adaptive_rejected": self._adaptive_rejected,
                "backend_realtime_adaptive_deferred_final": len(
                    self._deferred
                ),
                "backend_realtime_adaptive_decision_budget_exhausted": (
                    self._adaptive_budget_exhausted
                ),
                "backend_realtime_adaptive_decision_budget_dropped": (
                    self._adaptive_budget_dropped
                ),
                "backend_realtime_adaptive_enqueued_decisions": dict(
                    sorted(self._adaptive_enqueued_decisions.items())
                ),
                "backend_realtime_adaptive_controller_snapshot": (
                    self.adaptive_controller.snapshot()
                ),
            })
        if self.pairing_controller is not None:
            actions = {
                action: sum(
                    decision["action"] == action
                    for decision in self._pairing_decisions
                )
                for action in ("admit", "suppress")
            }
            phases = {
                phase: sum(
                    decision["phase"] == phase
                    for decision in self._pairing_decisions
                )
                for phase in ("explore", "exploit")
            }
            outcomes = {
                outcome: sum(
                    item["outcome"] == outcome
                    for item in self._pairing_outcomes
                )
                for outcome in (
                    "unit",
                    "conflict",
                    "unactivated",
                    "backpressure",
                    "expired",
                )
            }
            result.update({
                "backend_realtime_pairing_protocol": UTILITY_PAIRING_PROTOCOL,
                "backend_realtime_pairing_policy": (
                    self.pairing_controller.policy.as_dict()
                ),
                "backend_realtime_pairing_policy_sha256": (
                    self.pairing_controller.policy.sha256
                ),
                "backend_realtime_pairing_formula_family_sha256": (
                    self._pairing_formula_family
                ),
                "backend_realtime_pairing_consumer_worker": self.source_worker,
                "backend_realtime_pairing_decisions": list(
                    self._pairing_decisions
                ),
                "backend_realtime_pairing_outcomes": list(
                    self._pairing_outcomes
                ),
                "backend_realtime_pairing_action_counts": actions,
                "backend_realtime_pairing_phase_counts": phases,
                "backend_realtime_pairing_outcome_counts": outcomes,
                "backend_realtime_pairing_controller_snapshot": (
                    self.pairing_controller.snapshot()
                ),
            })
        self.native.reset_queues()
        return result
