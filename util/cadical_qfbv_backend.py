#!/usr/bin/env python3
"""CaDiCaL 3 QF_BV backend with checked incremental clause exchange."""

from __future__ import annotations

import os
import ctypes
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from qf_bv_backend import normalize_qfbv_capabilities
from qfbv_incremental_proof import (
    CLAUSE_PROTOCOL,
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    lift_ascii_lrat_proof,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import (
    QfbvBitBlastError,
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
    parse_dimacs_assignment,
)
from qfbv_proof_wire import (
    LIDRUP_WIRE_PROTOCOL,
    LidrupExternalChecker,
    LidrupWireStore,
    ProofWireError,
)
from qfbv_realtime_stream import (
    NativeRealtimeCadical,
    NativeRealtimeContext,
    RealtimeClauseExchangeSession,
    RealtimeStreamError,
)
from qfbv_adaptive_exchange import (
    AdaptiveProofController,
    AdaptiveProofPolicy,
)
from qfbv_utility_pairing import (
    UtilityPairingController,
    UtilityPairingPolicy,
    validate_pairing_worker_identity,
)


MAX_SOLVER_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_LRAT_BYTES = 64 * 1024 * 1024


def _interrupt(process: subprocess.Popen[str], grace: float = 0.2) -> None:
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
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _decode_bounded_output(output: Any, maximum: int, name: str) -> str:
    output.flush()
    size = os.fstat(output.fileno()).st_size
    if size > maximum:
        raise QfbvBitBlastError(f"{name} exceeds its byte contract")
    output.seek(0)
    encoded = output.read(maximum + 1)
    if len(encoded) > maximum:
        raise QfbvBitBlastError(f"{name} exceeds its byte contract")
    try:
        return encoded.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise QfbvBitBlastError(f"{name} is not valid UTF-8") from error


def _read_bounded_regular(path: Path, maximum: int, name: str) -> str:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for solver artifacts")
    descriptor = os.open(
        path, os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise IncrementalProofError(
                f"{name} is not a bounded regular file"
            )
        encoded = bytearray()
        while len(encoded) <= maximum:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, maximum + 1 - len(encoded)),
            )
            if not chunk:
                break
            encoded.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(encoded) > maximum
            or (metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise IncrementalProofError(f"{name} changed during stable read")
        return bytes(encoded).decode("ascii", "strict")
    finally:
        os.close(descriptor)


class CadicalQfbvSolver:
    """One-shot CaDiCaL adapter whose UNSAT path always replays LRUP."""

    def __init__(
        self,
        store: Any,
        command: Sequence[str],
        *,
        name: str,
        proof_store: IncrementalProofStore,
        proof_checker: IncrementalProofChecker,
        capabilities: Mapping[str, Any] | None = None,
        source_worker: str = "cadical-worker",
        worker_epoch: int = 0,
        max_imported_clauses: int = 64,
        proof_wire_checker: LidrupExternalChecker | None = None,
        proof_wire_store: LidrupWireStore | None = None,
    ) -> None:
        if not command or any(not str(item) for item in command):
            raise ValueError("CaDiCaL QF_BV command must not be empty")
        normalized = tuple(str(item) for item in command)
        if (
            not any("{cnf}" in item for item in normalized)
            or not any("{proof}" in item for item in normalized)
            or "--lrat" not in normalized
            or "--no-binary" not in normalized
            or "--plain" not in normalized
        ):
            raise ValueError(
                "CaDiCaL command requires {cnf}, {proof}, --plain, --lrat, "
                "and --no-binary"
            )
        if proof_checker.store is None or proof_checker.store.root != proof_store.root:
            raise ValueError("CaDiCaL proof checker and store must share one CAS")
        if (proof_wire_checker is None) != (proof_wire_store is None):
            raise ValueError("LIDRUP checker and wire store must be configured together")
        self.store = store
        self.command = normalized
        self.name = str(name)[:128] or "cadical-qfbv"
        self.capabilities = normalize_qfbv_capabilities({
            **dict(capabilities or {}),
            "accept_unsat": True,
            "incremental": True,
        })
        self.proof_store = proof_store
        self.proof_checker = proof_checker
        self.proof_wire_checker = proof_wire_checker
        self.proof_wire_store = proof_wire_store
        self.source_worker = str(source_worker)[:256] or "cadical-worker"
        self.worker_epoch = max(0, int(worker_epoch))
        self.max_imported_clauses = max(0, min(int(max_imported_clauses), 4096))
        self._lock = threading.Lock()
        self._active: dict[int, tuple[str, subprocess.Popen[str], threading.Event]] = {}
        self._running: set[str] = set()
        self._pending: set[str] = set()
        self._sequence = 0

    def _next_sequence(self) -> int:
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
            return sequence

    def _base(self) -> dict[str, Any]:
        return {
            "assignments": {},
            "solver": self.name,
            "backend_kind": "bitblast-cadical-qfbv",
            "backend_capabilities": self.capabilities,
            "backend_model_verified": False,
            "backend_unsat_authorized": False,
            "capability_status": "supported",
            "backend_incremental_proof_protocol": CLAUSE_PROTOCOL,
            "backend_incremental_proof_policy_sha256": (
                self.proof_checker.policy_sha256
            ),
            "backend_incremental_import_candidates": 0,
            "backend_incremental_imported_clauses": 0,
            "backend_incremental_import_checker_elapsed_us": 0,
        }

    def cancel(self, lease: Any) -> bool:
        query_id = str(lease.query_id)
        with self._lock:
            if query_id not in self._running:
                return False
            self._pending.add(query_id)
            active = [
                (process, event)
                for current, process, event in self._active.values()
                if current == query_id and process.poll() is None
            ]
            for _, event in active:
                event.set()
        for process, _ in active:
            _interrupt(process)
        return True

    def _register(
        self, query_id: str, process: subprocess.Popen[str]
    ) -> tuple[int, threading.Event]:
        token = id(process)
        event = threading.Event()
        with self._lock:
            self._active[token] = (query_id, process, event)
            immediate = query_id in self._pending
            if immediate:
                event.set()
        if immediate:
            _interrupt(process)
        return token, event

    def _unregister(self, token: int) -> None:
        with self._lock:
            self._active.pop(token, None)

    def _applicable_imports(self, plan: Any) -> tuple[list[Any], int, int]:
        candidates: list[str] = []
        for increment in reversed(plan.increments):
            candidates.extend(self.proof_store.records_for_formula(
                increment.formula_sha256,
                limit=max(1, self.max_imported_clauses),
            ))
            if len(candidates) >= self.max_imported_clauses:
                break
        unique = tuple(dict.fromkeys(candidates))[:self.max_imported_clauses]
        accepted: list[Any] = []
        checker_elapsed = 0
        clauses: set[tuple[int, ...]] = set()
        for digest in unique:
            try:
                authorization = self.proof_checker.verify_clause_record(
                    plan, self.proof_store.load(digest)
                )
            except (OSError, sqlite3.Error, IncrementalProofError):
                continue
            if authorization.clause in clauses:
                continue
            clauses.add(authorization.clause)
            accepted.append(authorization)
            checker_elapsed += authorization.checker_elapsed_us
        return accepted, len(unique), checker_elapsed

    @staticmethod
    def _dimacs_with_imports(plan: Any, imports: Sequence[Any]) -> str:
        clause_count = len(plan.clauses) + len(imports) + len(plan.assumptions)
        lines = [f"p cnf {plan.max_variable} {clause_count}"]
        lines.extend(" ".join(map(str, clause)) + " 0" for clause in plan.clauses)
        lines.extend(
            " ".join(map(str, authorization.clause)) + " 0"
            for authorization in imports
        )
        lines.extend(f"{literal} 0" for literal in plan.assumptions)
        return "\n".join(lines) + "\n"

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        return self.solve_with_assumptions(lease, ())

    def solve_with_assumptions(
        self,
        lease: Any,
        additional_assumptions: Sequence[int],
    ) -> Mapping[str, Any]:
        query_id = str(lease.query_id)
        with self._lock:
            self._running.add(query_id)
        try:
            return self._solve(lease, additional_assumptions)
        finally:
            with self._lock:
                self._running.discard(query_id)
                self._pending.discard(query_id)

    def _solve(
        self,
        lease: Any,
        additional_assumptions: Sequence[int] = (),
    ) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        base = self._base()
        loaded = self.store.load_query_ir(str(lease.query_id))
        if loaded is None:
            return {**base, "status": "error", "elapsed_us": 0,
                    "reason": "Query IR is unavailable"}
        roots, expressions = loaded
        try:
            plan = bitblast_qfbv_query(
                str(lease.query_id), roots, expressions, self.capabilities
            )
            plan = extend_bitblast_assumptions(plan, additional_assumptions)
        except (QfbvBitBlastError, ValueError) as error:
            return {
                "assignments": {},
                "solver": self.name,
                "status": "unknown",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": f"unsupported QF_BV bit-blast: {error}"[:512],
            }
        base["bitblast_certificate"] = dict(plan.certificate)
        imports, candidates, import_checker_us = self._applicable_imports(plan)
        base.update({
            "backend_incremental_import_candidates": candidates,
            "backend_incremental_imported_clauses": len(imports),
            "backend_incremental_import_checker_elapsed_us": import_checker_us,
            "backend_incremental_import_record_sha256": [
                item.record_sha256 for item in imports
            ],
        })
        timeout_ms = max(1, min(int(lease.timeout_ms), 3_600_000))
        with tempfile.TemporaryDirectory(prefix="symcc-cadical-qfbv-") as directory:
            cnf_path = Path(directory) / "query.cnf"
            proof_path = Path(directory) / "proof.lrat"
            stdout_path = Path(directory) / "solver.stdout"
            stderr_path = Path(directory) / "solver.stderr"
            cnf_path.write_text(
                self._dimacs_with_imports(plan, imports), encoding="ascii"
            )
            command = [
                item.replace("{cnf}", str(cnf_path))
                .replace("{proof}", str(proof_path))
                .replace("{timeout_ms}", str(timeout_ms))
                .replace("{timeout_s}", str(max(1, (timeout_ms + 999) // 1000)))
                for item in self.command
            ]
            try:
                with stdout_path.open("w+b") as stdout_file, stderr_path.open(
                    "w+b"
                ) as stderr_file:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                    )
                    token, cancelled = self._register(
                        str(lease.query_id), process
                    )
                    try:
                        process.wait(
                            timeout=max(1.0, timeout_ms / 1000.0 + 1.0)
                        )
                    except subprocess.TimeoutExpired:
                        _interrupt(process)
                        process.wait()
                        return {
                            **base,
                            "status": "unknown",
                            "elapsed_us": (
                                time.monotonic_ns() - started
                            ) // 1000,
                            "reason": "CaDiCaL QF_BV process timeout",
                        }
                    finally:
                        self._unregister(token)
                    stdout = _decode_bounded_output(
                        stdout_file, MAX_SOLVER_OUTPUT_BYTES, "CaDiCaL stdout"
                    )
                    stderr = _decode_bounded_output(
                        stderr_file, MAX_SOLVER_OUTPUT_BYTES, "CaDiCaL stderr"
                    )
            except QfbvBitBlastError as error:
                return {
                    **base,
                    "status": "error",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": str(error)[:512],
                }
            except OSError as error:
                return {
                    **base,
                    "status": "error",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": str(error)[:512],
                }
            if cancelled.is_set():
                return {
                    **base,
                    "status": "unknown",
                    "cancelled": True,
                    "cancel_reason": "portfolio-sat-winner",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": "CaDiCaL QF_BV process was cancelled",
                }
            try:
                status, model_assignment = parse_dimacs_assignment(stdout)
            except QfbvBitBlastError as error:
                diagnostic = (stderr + "\n" + stdout).strip()
                return {**base, "status": "error",
                        "elapsed_us": (time.monotonic_ns() - started) // 1000,
                        "reason": f"{error}: {diagnostic[-256:]}"}
            if process.returncode not in {0, 10, 20}:
                return {**base, "status": "error",
                        "elapsed_us": (time.monotonic_ns() - started) // 1000,
                        "reason": f"CaDiCaL exited {process.returncode}: {stderr[-384:]}"}
            if (status == "sat" and process.returncode != 10) or (
                status == "unsat" and process.returncode != 20
            ):
                return {
                    **base,
                    "status": "error",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": (
                        "CaDiCaL status disagrees with its process exit code"
                    ),
                }
            if status == "unknown":
                return {**base, "status": "unknown",
                        "elapsed_us": (time.monotonic_ns() - started) // 1000,
                        "reason": "CaDiCaL returned unknown"}
            if status == "sat":
                if any(
                    model_assignment.get(abs(literal)) != (literal > 0)
                    for literal in plan.assumptions
                ):
                    return {
                        **base,
                        "status": "unknown",
                        "elapsed_us": (time.monotonic_ns() - started) // 1000,
                        "reason": "CNF model does not satisfy the active assumptions",
                    }
                model = {
                    variable
                    for variable, value in model_assignment.items()
                    if value
                }
                assignments = plan.input_bytes_from_model(model)
                try:
                    candidate = bytearray.fromhex(str(lease.input_hex))
                except ValueError:
                    candidate = bytearray()
                for offset, value in assignments.items():
                    if offset >= len(candidate):
                        return {**base, "status": "unknown",
                                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                                "reason": "CNF model exceeds the concrete witness"}
                    candidate[offset] = value
                if not self.store.validate_candidate(
                    str(lease.query_id), bytes(candidate)
                ):
                    return {**base, "status": "unknown",
                            "elapsed_us": (time.monotonic_ns() - started) // 1000,
                            "reason": "CNF model failed Query IR validation"}
                return {
                    **base,
                    "status": "sat",
                    "assignments": assignments,
                    "backend_model_verified": True,
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                }
            try:
                proof = _read_bounded_regular(
                    proof_path, MAX_LRAT_BYTES, "LRAT proof"
                )
                record = lift_ascii_lrat_proof(
                    plan,
                    proof,
                    imported_clauses=imports,
                    source_worker=self.source_worker,
                    worker_epoch=self.worker_epoch,
                    sequence=self._next_sequence(),
                )
                authorization = self.proof_checker.verify_clause_record(plan, record)
                digest, created = self.proof_store.publish(record)
                if digest != authorization.record_sha256:
                    raise IncrementalProofError("published proof identity changed")
                receipt = make_unsat_result_receipt(
                    plan, digest, plan.assumptions
                )
                result_authorization = self.proof_checker.verify_result_receipt(
                    plan, receipt
                )
                wire_fields: dict[str, Any] = {}
                if (
                    self.proof_wire_checker is not None
                    and self.proof_wire_store is not None
                ):
                    artifacts, wire_receipt = self.proof_wire_checker.verify(
                        plan, receipt, self.proof_store
                    )
                    artifact_digest, wire_receipt_digest, wire_created = (
                        self.proof_wire_store.publish(artifacts, wire_receipt)
                    )
                    if (
                        artifact_digest != artifacts.metadata()["artifact_sha256"]
                        or wire_receipt_digest != wire_receipt["receipt_sha256"]
                    ):
                        raise ProofWireError("published LIDRUP identity changed")
                    wire_fields = {
                        "backend_lidrup_wire_protocol": LIDRUP_WIRE_PROTOCOL,
                        "backend_lidrup_checker_policy_sha256": (
                            self.proof_wire_checker.policy_sha256
                        ),
                        "backend_lidrup_artifact_sha256": artifact_digest,
                        "backend_lidrup_receipt_sha256": wire_receipt_digest,
                        "backend_lidrup_verified": True,
                        "backend_lidrup_created": wire_created,
                        "backend_lidrup_learned_clauses": (
                            artifacts.learned_clause_count
                        ),
                        "backend_lidrup_checker_elapsed_us": int(
                            wire_receipt["checker_elapsed_us"]
                        ),
                    }
            except (
                FileNotFoundError,
                OSError,
                UnicodeError,
                sqlite3.Error,
                IncrementalProofError,
                ProofWireError,
            ) as error:
                return {
                    **base,
                    "status": "unknown",
                    "backend_status": "unsat",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": f"incremental UNSAT proof verification failed: {error}"[:512],
                }
            return {
                **base,
                "status": "unsat",
                "backend_status": "unsat",
                "backend_unsat_authorized": True,
                "backend_incremental_proof_verified": True,
                "backend_incremental_proof_created": created,
                "backend_incremental_proof_record_sha256": digest,
                "backend_incremental_proof_steps": authorization.proof_steps,
                "backend_incremental_proof_propagations": (
                    authorization.propagation_count
                ),
                "backend_incremental_proof_checker_elapsed_us": (
                    authorization.checker_elapsed_us
                    + result_authorization.checker_elapsed_us
                ),
                "backend_incremental_result_receipt": receipt,
                **wire_fields,
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
            }


@dataclass
class _NativeContext:
    pointer: int
    cnf_sha256: str
    imported_records: set[str] = field(default_factory=set)
    imported_clauses: set[tuple[int, ...]] = field(default_factory=set)
    solve_count: int = 0
    deadline: float = 0.0
    cancelled: threading.Event = field(default_factory=threading.Event)
    timed_out: threading.Event = field(default_factory=threading.Event)
    terminator: Any = None
    realtime: NativeRealtimeContext | None = None


class PersistentCadicalQfbvSolver:
    """Native IPASIR context cache with an independently proved UNSAT path."""

    def __init__(
        self,
        store: Any,
        library_path: str | os.PathLike[str],
        proof_command: Sequence[str],
        *,
        name: str,
        proof_store: IncrementalProofStore,
        proof_checker: IncrementalProofChecker,
        capabilities: Mapping[str, Any] | None = None,
        source_worker: str = "cadical-native-worker",
        worker_epoch: int = 0,
        max_imported_clauses: int = 64,
        context_cache_entries: int = 8,
        realtime_library_path: str | os.PathLike[str] | None = None,
        realtime_max_imports: int = 64,
        realtime_max_events: int = 4096,
        realtime_max_learned: int = 64,
        realtime_max_learned_length: int = 32,
        realtime_poll_interval_ms: int = 2,
        realtime_checker_budget_ms: int = 100,
        realtime_native_queue_literals: int = 65_536,
        realtime_require_clause_compression: bool = False,
        realtime_adaptive_policy: Mapping[str, Any] | None = None,
        realtime_track_clause_activity: bool = False,
        realtime_pairing_policy: Mapping[str, Any] | None = None,
        proof_wire_checker: LidrupExternalChecker | None = None,
        proof_wire_store: LidrupWireStore | None = None,
    ) -> None:
        self.store = store
        self.name = str(name)[:128] or "cadical-native-qfbv"
        self.library_path = Path(library_path).resolve(strict=True)
        if not self.library_path.is_file():
            raise ValueError("CaDiCaL native library is not a regular file")
        self.library = ctypes.CDLL(str(self.library_path))
        self._configure_api()
        signature_raw = self.library.ccadical_signature()
        self.signature = signature_raw.decode("ascii", "strict")
        if not self.signature.startswith("cadical-3.0."):
            raise ValueError("native backend requires CaDiCaL 3.0.x")
        if not isinstance(realtime_require_clause_compression, bool):
            raise ValueError("realtime compression requirement must be boolean")
        if realtime_require_clause_compression and realtime_library_path is None:
            raise ValueError("realtime clause compression requires realtime mode")
        self.realtime = (
            NativeRealtimeCadical(
                realtime_library_path,
                require_clause_compression=realtime_require_clause_compression,
            )
            if realtime_library_path is not None
            else None
        )
        if (
            self.realtime is not None
            and self.realtime.signature
            != f"symcc-qfbv-realtime-v1|{self.signature}"
        ):
            raise ValueError(
                "realtime shim and CaDiCaL C library identities disagree"
            )
        self.proof_backend = CadicalQfbvSolver(
            store,
            proof_command,
            name=self.name,
            proof_store=proof_store,
            proof_checker=proof_checker,
            capabilities=capabilities,
            source_worker=source_worker,
            worker_epoch=worker_epoch,
            max_imported_clauses=max_imported_clauses,
            proof_wire_checker=proof_wire_checker,
            proof_wire_store=proof_wire_store,
        )
        self.capabilities = self.proof_backend.capabilities
        self.context_cache_entries = max(1, min(int(context_cache_entries), 64))
        self.realtime_max_imports = max(
            0, min(int(realtime_max_imports), 4096)
        )
        self.realtime_max_events = max(
            1, min(int(realtime_max_events), 1_000_000)
        )
        self.realtime_max_learned = max(
            0, min(int(realtime_max_learned), 65_536)
        )
        self.realtime_max_learned_length = max(
            0, min(int(realtime_max_learned_length), 65_536)
        )
        self.realtime_poll_interval_ms = max(
            1, min(int(realtime_poll_interval_ms), 1000)
        )
        self.realtime_checker_budget_ms = max(
            1, min(int(realtime_checker_budget_ms), 60_000)
        )
        self.realtime_native_queue_literals = max(
            1, min(int(realtime_native_queue_literals), 1 << 24)
        )
        if realtime_adaptive_policy is not None and self.realtime is None:
            raise ValueError("adaptive proof admission requires realtime mode")
        if not isinstance(realtime_track_clause_activity, bool):
            raise ValueError("realtime clause-activity option must be boolean")
        if realtime_track_clause_activity and self.realtime is None:
            raise ValueError("clause-activity tracking requires realtime mode")
        if realtime_pairing_policy is not None and self.realtime is None:
            raise ValueError("utility pairing requires realtime mode")
        if realtime_pairing_policy is not None and not realtime_track_clause_activity:
            raise ValueError("utility pairing requires clause-activity tracking")
        if (
            realtime_track_clause_activity
            and not self.realtime.activity_protocol
        ):
            raise ValueError(
                "clause-activity tracking requires a capable realtime shim"
            )
        self.realtime_track_clause_activity = realtime_track_clause_activity
        self.realtime_adaptive_controller = (
            AdaptiveProofController(AdaptiveProofPolicy.from_mapping(
                realtime_adaptive_policy,
                queue_capacity=self.realtime_max_imports,
            ))
            if realtime_adaptive_policy is not None
            else None
        )
        pairing_policy = (
            UtilityPairingPolicy.from_mapping(realtime_pairing_policy)
            if realtime_pairing_policy is not None
            else None
        )
        self.realtime_pairing_controller = None
        if pairing_policy is not None:
            pairing_worker = validate_pairing_worker_identity(
                self.proof_backend.source_worker, "pairing consumer worker"
            )
            snapshot = None
            snapshot_loader = getattr(
                store, "load_qfbv_utility_pairing_snapshot", None
            )
            if callable(snapshot_loader):
                snapshot = snapshot_loader(pairing_worker, pairing_policy)
            self.realtime_pairing_controller = (
                UtilityPairingController.from_snapshot(pairing_policy, snapshot)
                if snapshot is not None
                else UtilityPairingController(pairing_policy)
            )
        self._contexts: OrderedDict[str, _NativeContext] = OrderedDict()
        self._lock = threading.RLock()
        self._solve_lock = threading.Lock()
        self._active_query = ""
        self._active_context: _NativeContext | None = None

    def _configure_api(self) -> None:
        api = self.library
        api.ccadical_signature.argtypes = []
        api.ccadical_signature.restype = ctypes.c_char_p
        api.ccadical_init.argtypes = []
        api.ccadical_init.restype = ctypes.c_void_p
        api.ccadical_release.argtypes = [ctypes.c_void_p]
        api.ccadical_add.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.ccadical_assume.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.ccadical_solve.argtypes = [ctypes.c_void_p]
        api.ccadical_solve.restype = ctypes.c_int
        api.ccadical_val.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.ccadical_val.restype = ctypes.c_int
        api.ccadical_failed.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.ccadical_failed.restype = ctypes.c_int
        api.ccadical_terminate.argtypes = [ctypes.c_void_p]
        self._terminator_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
        api.ccadical_set_terminate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            self._terminator_type,
        ]

    def _new_context(self, plan: Any) -> _NativeContext:
        if self.realtime is not None:
            native = self.realtime.new_context(
                max_learned_length=self.realtime_max_learned_length,
                max_imports=self.realtime_max_imports,
                max_import_literals=self.realtime_native_queue_literals,
                max_learned=self.realtime_max_learned,
            )
            try:
                context = _NativeContext(
                    pointer=native.pointer,
                    cnf_sha256=str(plan.certificate["cnf_sha256"]),
                    realtime=native,
                )
                for clause in plan.clauses:
                    for literal in clause:
                        native.add(int(literal))
                    native.add(0)
                native.observe(int(plan.max_variable))
                return context
            except Exception:
                native.close()
                raise
        pointer = int(self.library.ccadical_init())
        if pointer == 0:
            raise OSError("CaDiCaL failed to allocate a native context")
        context = _NativeContext(pointer=pointer, cnf_sha256=str(
            plan.certificate["cnf_sha256"]
        ))

        def terminate(_state: Any) -> int:
            return int(
                context.cancelled.is_set()
                or (context.deadline > 0.0 and time.monotonic() >= context.deadline)
            )

        context.terminator = self._terminator_type(terminate)
        self.library.ccadical_set_terminate(
            ctypes.c_void_p(pointer), None, context.terminator
        )
        for clause in plan.clauses:
            for literal in clause:
                self.library.ccadical_add(ctypes.c_void_p(pointer), int(literal))
            self.library.ccadical_add(ctypes.c_void_p(pointer), 0)
        return context

    def _release_context(self, context: _NativeContext) -> None:
        if context.realtime is not None:
            context.realtime.close()
        else:
            self.library.ccadical_release(ctypes.c_void_p(context.pointer))

    def _terminate_context(self, context: _NativeContext) -> None:
        if context.realtime is not None:
            context.realtime.terminate()
        else:
            self.library.ccadical_terminate(ctypes.c_void_p(context.pointer))

    def _context(self, plan: Any) -> tuple[_NativeContext, bool]:
        key = plan.formula_sha256
        context = self._contexts.get(key)
        hit = context is not None
        if context is not None and context.cnf_sha256 != plan.certificate["cnf_sha256"]:
            raise IncrementalProofError("native context formula identity changed")
        if context is None:
            context = self._new_context(plan)
            self._contexts[key] = context
            while len(self._contexts) > self.context_cache_entries:
                _, evicted = self._contexts.popitem(last=False)
                self._release_context(evicted)
        else:
            self._contexts.move_to_end(key)
        return context, hit

    def cancel(self, lease: Any) -> bool:
        query_id = str(lease.query_id)
        with self._lock:
            context = (
                self._active_context
                if self._active_query == query_id
                else None
            )
            if context is not None:
                context.cancelled.set()
                self._terminate_context(context)
        proof_cancelled = self.proof_backend.cancel(lease)
        return context is not None or proof_cancelled

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        return self.solve_with_assumptions(lease, ())

    def solve_with_assumptions(
        self,
        lease: Any,
        additional_assumptions: Sequence[int],
    ) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        loaded = self.store.load_query_ir(str(lease.query_id))
        if loaded is None:
            return {
                "assignments": {}, "solver": self.name, "status": "error",
                "elapsed_us": 0, "reason": "Query IR is unavailable",
            }
        try:
            plan = bitblast_qfbv_query(
                str(lease.query_id), loaded[0], loaded[1], self.capabilities
            )
            plan = extend_bitblast_assumptions(plan, additional_assumptions)
        except (QfbvBitBlastError, ValueError) as error:
            return {
                "assignments": {}, "solver": self.name, "status": "unknown",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": f"unsupported QF_BV bit-blast: {error}"[:512],
            }
        imports, candidates, import_checker_us = (
            self.proof_backend._applicable_imports(plan)
        )
        timeout_ms = max(1, min(int(lease.timeout_ms), 3_600_000))
        stream_fields: dict[str, Any] = {}
        context: _NativeContext | None = None
        timer: threading.Timer | None = None
        session: RealtimeClauseExchangeSession | None = None
        try:
            with self._solve_lock:
                with self._lock:
                    context, cache_hit = self._context(plan)
                    import_limit = self.proof_backend.max_imported_clauses
                    for authorization in imports:
                        if authorization.record_sha256 in context.imported_records:
                            continue
                        if len(context.imported_records) >= import_limit:
                            break
                        if context.realtime is not None:
                            for literal in authorization.clause:
                                context.realtime.add(int(literal))
                            context.realtime.add(0)
                        else:
                            for literal in authorization.clause:
                                self.library.ccadical_add(
                                    ctypes.c_void_p(context.pointer), int(literal)
                                )
                            self.library.ccadical_add(
                                ctypes.c_void_p(context.pointer), 0
                            )
                        context.imported_records.add(
                            authorization.record_sha256
                        )
                        context.imported_clauses.add(authorization.clause)
                    context.cancelled.clear()
                    context.timed_out.clear()
                    context.deadline = time.monotonic() + timeout_ms / 1000.0
                    if context.realtime is not None:
                        context.realtime.clear_termination()
                    self._active_query = str(lease.query_id)
                    self._active_context = context
                    for literal in plan.assumptions:
                        if context.realtime is not None:
                            context.realtime.assume(int(literal))
                        else:
                            self.library.ccadical_assume(
                                ctypes.c_void_p(context.pointer), int(literal)
                            )
                    if context.realtime is not None:
                        remaining = max(
                            0, import_limit - len(context.imported_records)
                        )
                        session = RealtimeClauseExchangeSession(
                            plan,
                            context.realtime,
                            self.proof_backend.proof_store,
                            self.proof_backend.proof_checker,
                            native_signature=self.realtime.signature,
                            source_worker=self.proof_backend.source_worker,
                            worker_epoch=self.proof_backend.worker_epoch,
                            next_sequence=self.proof_backend._next_sequence,
                            stream_ordinal=context.solve_count + 1,
                            seen_records=tuple(context.imported_records),
                            seen_clauses=tuple(context.imported_clauses),
                            max_imports=min(
                                self.realtime_max_imports, remaining
                            ),
                            max_events=self.realtime_max_events,
                            max_learned=self.realtime_max_learned,
                            max_learned_length=(
                                self.realtime_max_learned_length
                            ),
                            poll_interval_ms=self.realtime_poll_interval_ms,
                            checker_budget_ms=(
                                self.realtime_checker_budget_ms
                            ),
                            adaptive_controller=(
                                self.realtime_adaptive_controller
                            ),
                            pairing_controller=(
                                self.realtime_pairing_controller
                            ),
                            solve_budget_ms=timeout_ms,
                            track_clause_activity=(
                                self.realtime_track_clause_activity
                            ),
                        )
                        session.start()

                        def reach_deadline() -> None:
                            context.timed_out.set()
                            try:
                                self._terminate_context(context)
                            except RealtimeStreamError:
                                pass

                        timer = threading.Timer(
                            timeout_ms / 1000.0, reach_deadline
                        )
                        timer.daemon = True
                        timer.start()
                try:
                    if context.realtime is not None:
                        result_code = context.realtime.solve()
                    else:
                        result_code = int(self.library.ccadical_solve(
                            ctypes.c_void_p(context.pointer)
                        ))
                finally:
                    if timer is not None:
                        timer.cancel()
                        timer.join()
                    if session is not None:
                        generation = context.realtime.stats()[
                            "solve_generation"
                        ]
                        stream_fields = session.finish(generation)
                with self._lock:
                    for digest in stream_fields.get(
                        "backend_realtime_import_record_sha256", []
                    ):
                        authorization = self.proof_backend.proof_checker.verify_clause_record(
                            plan, self.proof_backend.proof_store.load(digest)
                        )
                        context.imported_records.add(digest)
                        context.imported_clauses.add(authorization.clause)
                    context.deadline = 0.0
                    context.solve_count += 1
                    solve_count = context.solve_count
                    cancelled = context.cancelled.is_set()
                    timed_out = context.timed_out.is_set()
                    self._active_query = ""
                    self._active_context = None
                    if result_code == 10:
                        assumption_values = {
                            abs(literal): (
                                context.realtime.val(abs(literal))
                                if context.realtime is not None
                                else self.library.ccadical_val(
                                    ctypes.c_void_p(context.pointer),
                                    abs(literal),
                                )
                            )
                            for literal in plan.assumptions
                        }
                        true_variables = {
                            abs(literal)
                            for _offset, literals in plan.input_literals
                            for literal in literals
                            if (
                                context.realtime.val(abs(literal))
                                if context.realtime is not None
                                else self.library.ccadical_val(
                                    ctypes.c_void_p(context.pointer),
                                    abs(literal),
                                )
                            ) > 0
                        }
                        assignments = plan.input_bytes_from_model(true_variables)
                    else:
                        assumption_values = {}
                        assignments = {}
                    active_import_records = sorted(context.imported_records)
        except (
            OSError,
            sqlite3.Error,
            IncrementalProofError,
            RealtimeStreamError,
        ) as error:
            if session is not None:
                try:
                    session.abort()
                except RealtimeStreamError:
                    pass
            discarded: _NativeContext | None = None
            with self._lock:
                if (
                    context is not None
                    and self._contexts.get(plan.formula_sha256) is context
                ):
                    discarded = self._contexts.pop(plan.formula_sha256)
                self._active_query = ""
                self._active_context = None
            if discarded is not None:
                self._release_context(discarded)
            return {
                "assignments": {}, "solver": self.name, "status": "error",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": f"native CaDiCaL stream failed: {error}"[:512],
            }
        native_fields = {
            "backend_native_context_protocol": (
                "cadical-ipasir-up-realtime-v1"
                if self.realtime is not None
                else "cadical-ipasir-assumptions-v1"
            ),
            "backend_native_context_cache_hit": cache_hit,
            "backend_native_context_solve_count": solve_count,
            "backend_native_context_entries": len(self._contexts),
            "backend_native_signature": self.signature,
            "backend_native_result": result_code,
            **stream_fields,
        }
        if cancelled:
            return {
                "assignments": {}, "solver": self.name, "status": "unknown",
                "cancelled": True,
                "cancel_reason": "portfolio-sat-winner",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": "native CaDiCaL solve was cancelled",
            }
        if stream_fields.get("backend_realtime_stream_error"):
            return {
                "assignments": {}, "solver": self.name, "status": "unknown",
                **native_fields,
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": "realtime proof stream terminated the native solve",
            }
        if result_code == 0:
            return {
                "assignments": {}, "solver": self.name, "status": "unknown",
                **native_fields,
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": (
                    "native CaDiCaL reached its solve deadline"
                    if timed_out or self.realtime is None
                    else "native CaDiCaL returned unknown"
                ),
            }
        if result_code == 20:
            proved = dict(
                self.proof_backend.solve_with_assumptions(
                    lease, additional_assumptions
                )
            )
            if proved.get("status") != "unsat":
                return {
                    **proved,
                    "status": "unknown",
                    "backend_status": "unsat",
                    "backend_unsat_authorized": False,
                    "reason": (
                        "native UNSAT was not confirmed by the independent LRUP path"
                    ),
                    **native_fields,
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                }
            proved.update(native_fields)
            proved["elapsed_us"] = (time.monotonic_ns() - started) // 1000
            return proved
        if result_code != 10:
            return {
                "assignments": {}, "solver": self.name, "status": "error",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": f"native CaDiCaL returned invalid code {result_code}",
            }
        base = self.proof_backend._base()
        base.update({
            "bitblast_certificate": dict(plan.certificate),
            "backend_incremental_import_candidates": max(
                candidates, len(active_import_records)
            ),
            "backend_incremental_imported_clauses": len(active_import_records),
            "backend_incremental_import_checker_elapsed_us": import_checker_us,
            "backend_incremental_import_record_sha256": active_import_records,
            **native_fields,
        })
        try:
            if any(
                value == 0 or (value > 0) != (literal > 0)
                for literal in plan.assumptions
                for value in (assumption_values.get(abs(literal), 0),)
            ):
                return {
                    **base,
                    "status": "unknown",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": "native CNF model does not satisfy active assumptions",
                }
            candidate = bytearray.fromhex(str(lease.input_hex))
        except ValueError:
            candidate = bytearray()
        for offset, value in assignments.items():
            if offset >= len(candidate):
                return {
                    **base, "status": "unknown",
                    "elapsed_us": (time.monotonic_ns() - started) // 1000,
                    "reason": "native CNF model exceeds the concrete witness",
                }
            candidate[offset] = value
        if not self.store.validate_candidate(
            str(lease.query_id), bytes(candidate)
        ):
            return {
                **base, "status": "unknown",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": "native CNF model failed Query IR validation",
            }
        return {
            **base,
            "status": "sat",
            "assignments": assignments,
            "backend_model_verified": True,
            "elapsed_us": (time.monotonic_ns() - started) // 1000,
        }

    def close(self) -> None:
        with self._solve_lock, self._lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
            self._active_context = None
            self._active_query = ""
        for context in contexts:
            self._release_context(context)

    def __enter__(self) -> "PersistentCadicalQfbvSolver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
