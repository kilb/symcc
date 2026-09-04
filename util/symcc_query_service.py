#!/usr/bin/env python3
"""Ingest SymCC query spools and solve them with independent workers."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import stat
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

from query_store import (
    PersistentSubprocessSolver,
    PortfolioSolver,
    QueryAdmissionError,
    QueryLeaseHeartbeatError,
    QueryStore,
    SubprocessSolver,
    solve_claimed,
    solve_one,
)
from qf_bv_backend import (
    PersistentSmtLibQfbvSolver,
    SmtLibQfbvSolver,
    normalize_qfbv_capabilities,
)
from cross_worker_context import CrossWorkerContextStore
from qfbv_proof_receipt import (
    MAX_PROOF_BYTES,
    ProofVerificationError,
    QfbvProofStore,
    QfbvProofVerifier,
    normalize_qfbv_proof_config,
)
from qfbv_lemma_exchange import (
    MAX_RECORD_BYTES,
    QfbvLemmaExchange,
    QfbvLemmaStore,
)
from qfbv_substitution_core import (
    MAX_RECORD_BYTES as MAX_SUBSTITUTION_CORE_RECORD_BYTES,
    QfbvSubstitutionCoreExchange,
    QfbvSubstitutionCoreStore,
)
from qfbv_incremental_proof import (
    DEFAULT_REPLAY_CACHE_BYTES,
    DEFAULT_REPLAY_CACHE_ENTRIES,
    MAX_REPLAY_CACHE_BYTES,
    MAX_REPLAY_CACHE_ENTRIES,
    IncrementalProofChecker,
    IncrementalProofStore,
)
from qfbv_proof_wire import (
    LidrupExternalChecker,
    LidrupWireStore,
)
from qfbv_adaptive_exchange import (
    AdaptiveExchangeError,
    AdaptiveProofPolicy,
)
from qfbv_utility_pairing import (
    UtilityPairingError,
    UtilityPairingPolicy,
)
from qfbv_malleable_workers import (
    MalleableJobSignal,
    MalleableWorkerController,
    MalleableWorkerPolicy,
)
from qfbv_proof_prefix_partition import ProofPrefixPartitionStore
from qfbv_partition_execution import (
    PartitionExecutionPolicy,
    PartitionExecutionStore,
    PartitioningQfbvBackend,
)
from qfbv_online_cubing import (
    OnlineCubingError,
    OnlineCubingPolicy,
    OnlineCubingPolicyStore,
)
from cadical_qfbv_backend import (
    CadicalQfbvSolver,
    PersistentCadicalQfbvSolver,
)
from qfbv_artifact_lifecycle import (
    ArtifactCollection,
    ArtifactJobLease,
    ArtifactLeaseHeartbeat,
    ArtifactLifecycleRegistry,
)


_MALLEABLE_QUERY_JOB = "query-store"
_MALLEABLE_QUERY_FAMILY = hashlib.sha256(
    b"symcc-query-store-global-formula-family-v1"
).hexdigest()


def _durable_proof_artifacts(result: Mapping[str, Any]) -> list[str]:
    """Extract bounded proof/certificate identities after result commit."""
    found: set[str] = set()
    pending: list[tuple[str, Any]] = [("", result)]
    observed = 0
    while pending and observed < 65_536:
        name, value = pending.pop()
        observed += 1
        if isinstance(value, Mapping):
            pending.extend((str(key), item) for key, item in value.items())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            pending.extend((name, item) for item in value[:65_536])
        elif (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            and name.endswith("sha256")
            and any(marker in name for marker in ("proof", "certificate", "receipt"))
        ):
            found.add(value)
    return sorted(found)[:4096]


class _MalleableQueryWorkerPool:
    """Thread-safe QueryStore adapter for the generic F438 controller."""

    def __init__(
        self,
        store: QueryStore,
        *,
        pool_id: str,
        slots: int,
        backlog_per_slot: int,
        hysteresis_slots: int,
    ) -> None:
        self.store = store
        self.worker_ids = tuple(f"slot-{slot}" for slot in range(slots))
        self.policy = MalleableWorkerPolicy(
            total_slots=slots,
            backlog_per_slot=backlog_per_slot,
            rebalance_hysteresis_slots=hysteresis_slots,
        )
        self.pool_id = pool_id or (
            "query-service-"
            + hashlib.sha256(str(store.root).encode("utf-8")).hexdigest()[:24]
        )
        self._lock_descriptor = self._acquire_pool_lock()
        try:
            restored = store.load_qfbv_malleable_worker_snapshot(
                self.pool_id, self.policy
            )
            if restored is None:
                self.controller = MalleableWorkerController(
                    self.pool_id, self.worker_ids, self.policy
                )
                self._persist()
            else:
                if set(restored["assignments"]) != set(self.worker_ids):
                    raise ValueError("stored malleable-worker inventory changed")
                self.controller = MalleableWorkerController.from_snapshot(
                    self.policy, restored
                )
        except BaseException:
            os.close(self._lock_descriptor)
            self._lock_descriptor = -1
            raise
        self._condition = threading.Condition()
        # QueryStore expiry authorizes another claim, but it does not prove that
        # this process's solver call has returned.  Keep that liveness fact
        # process-local so a drain cannot recycle the physical slot early.
        self._live_leases: set[str] = set()

    def _acquire_pool_lock(self) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError(
                errno.EOPNOTSUPP,
                "O_NOFOLLOW is required for malleable pool ownership",
            )
        lock_name = hashlib.sha256(self.pool_id.encode("utf-8")).hexdigest()
        path = self.store.root / f".malleable-pool-{lock_name}.lock"
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise OSError(errno.ESTALE, "malleable pool lock identity changed")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise RuntimeError(
                        f"malleable pool {self.pool_id!r} already has a coordinator"
                    ) from error
                raise
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        descriptor = self._lock_descriptor
        if descriptor < 0:
            return
        self._lock_descriptor = -1
        os.close(descriptor)

    def __del__(self) -> None:
        descriptor = getattr(self, "_lock_descriptor", -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._lock_descriptor = -1

    def _persist(self) -> None:
        self.store.commit_qfbv_malleable_worker_snapshot(
            self.pool_id, self.policy, self.controller.snapshot()
        )

    def _advance_pending(self) -> bool:
        snapshot = self.controller.snapshot()
        pending = snapshot["pending_transition"]
        if pending is None:
            return False
        transition = pending["transition"]
        changed = False
        for worker in transition["drain_workers"]:
            permission = self.controller.permission(worker)
            for lease_id in tuple(permission["leases"]):
                if lease_id in self._live_leases:
                    continue
                if not self.store.query_lease_is_active(lease_id):
                    self.controller.finish_lease(
                        worker,
                        permission["assignment_generation"],
                        permission["assignment_token"],
                        lease_id,
                    )
                    changed = True
            snapshot = self.controller.snapshot()
            current_pending = snapshot["pending_transition"]
            assert current_pending is not None
            if worker in current_pending["receipts"]:
                continue
            permission = self.controller.permission(worker)
            expected = transition["expected_leases"][worker]
            retired = current_pending["retired_leases"][worker]
            if not permission["leases"] and retired == expected:
                self.controller.acknowledge_drain(
                    worker,
                    permission["assignment_generation"],
                    permission["assignment_token"],
                    returned_leases=expected,
                    durable_proofs=permission["durable_proofs"],
                    proof_cursor=permission["proof_cursor"],
                )
                changed = True
        if changed:
            self._persist()
        pending = self.controller.snapshot()["pending_transition"]
        assert pending is not None
        if set(pending["receipts"]) == set(pending["transition"]["drain_workers"]):
            self.controller.commit()
            self._persist()
            self._condition.notify_all()
            return True
        return changed

    def rebalance(self) -> dict[str, Any]:
        with self._condition:
            self._advance_pending()
            if not self.controller.pending:
                stats = self.store.work_counts()
                backlog = int(stats["pending"]) + int(stats["leased"])
                transition = self.controller.prepare(
                    [
                        MalleableJobSignal(
                            job_id=_MALLEABLE_QUERY_JOB,
                            formula_family_sha256=_MALLEABLE_QUERY_FAMILY,
                            backlog=backlog,
                        )
                    ]
                )
                if transition is not None:
                    self._persist()
                    self._advance_pending()
            self._condition.notify_all()
            return self.controller.snapshot()

    def claim(
        self,
        slot: int,
        store: QueryStore,
        owner: str,
        *,
        lease_seconds: float,
        traversal: str,
    ) -> tuple[dict[str, Any], Any, str] | None:
        worker = self.worker_ids[slot]
        with self._condition:
            permission = self.controller.permission(worker)
            if permission["state"] != "active":
                return None
            active_workers = [
                candidate
                for candidate in self.worker_ids
                if self.controller.permission(candidate)["state"] == "active"
            ]
            logical_shard = active_workers.index(worker)
            lease = store.claim(
                owner,
                lease_seconds,
                traversal=traversal,
                shard_index=logical_shard,
                shard_count=len(active_workers),
            )
            if lease is None:
                return None
            lease_id = f"{lease.query_id}:{lease.token}"
            try:
                self.controller.attach_lease(
                    worker,
                    permission["assignment_generation"],
                    permission["assignment_token"],
                    lease_id,
                )
                self._live_leases.add(lease_id)
            except BaseException:
                store.fail(
                    lease,
                    owner,
                    "malleable assignment rejected after claim",
                    max_attempts=(1 << 31) - 1,
                )
                raise
            return permission, lease, lease_id

    def finish(
        self,
        slot: int,
        permission: Mapping[str, Any],
        lease_id: str,
        *,
        durable_proofs: Sequence[str] = (),
    ) -> None:
        worker = self.worker_ids[slot]
        with self._condition:
            if lease_id not in self._live_leases:
                raise RuntimeError("malleable live lease is not owned by this process")
            current = self.controller.permission(worker)
            drain_proofs = durable_proofs if current["state"] == "draining" else ()
            self.controller.finish_lease(
                worker,
                int(permission["assignment_generation"]),
                str(permission["assignment_token"]),
                lease_id,
                durable_proofs=drain_proofs,
            )
            self._live_leases.remove(lease_id)
            if self.controller.pending:
                self._persist()
                self._advance_pending()
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return self.controller.snapshot()


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw, 10)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error


def _environment_boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _comma_separated_integers(raw: str, name: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip(), 10) for item in raw.split(","))
    except ValueError as error:
        raise ValueError(f"{name} must be comma-separated integers") from error
    if not values or any(not item.strip() for item in raw.split(",")):
        raise ValueError(f"{name} must be comma-separated integers")
    return values


def _comma_separated_strings(raw: str, name: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(","))
    if not values or any(not item for item in values):
        raise ValueError(f"{name} must be a comma-separated list")
    return values


def _move(source: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / (
            f"{source.stem}-{time.time_ns()}{source.suffix}"
        )
    os.replace(source, destination)


def _try_spool_ingest_lock(spool: Path) -> int | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "O_NOFOLLOW is required for query spool locking",
        )
    lock_path = spool / ".ingest.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "query spool lock identity is not stable",
                lock_path,
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                os.close(descriptor)
                return None
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ingest_spool(store: QueryStore, spool: Path) -> tuple[int, int]:
    incoming = spool / "incoming"
    accepted = spool / "accepted"
    rejected = spool / "rejected"
    incoming.mkdir(parents=True, exist_ok=True)
    lock_descriptor = _try_spool_ingest_lock(spool)
    if lock_descriptor is None:
        return 0, 0
    imported = 0
    failed = 0
    try:
        for path in sorted(incoming.glob("*.json")):
            try:
                store.ingest_file(path)
            except QueryAdmissionError as exc:
                error = rejected / f"{path.name}.error"
                error.parent.mkdir(parents=True, exist_ok=True)
                error.write_text(
                    f"ValueError: {exc}\n",
                    encoding="utf-8",
                )
                _move(path, rejected)
                failed += 1
                continue

            # Publication failure is retryable, not an invalid Query IR.
            _move(path, accepted)
            imported += 1
        return imported, failed
    finally:
        os.close(lock_descriptor)


def _default_solver() -> list[str]:
    configured = os.environ.get("SYMCC_QUERY_SOLVER", "")
    if configured:
        return shlex.split(configured)
    discovered = shutil.which("symcc-query-solver")
    if discovered:
        return [discovered]
    root = Path(__file__).resolve().parents[1]
    candidates = sorted(
        root.glob(
            "build/SymCCRuntime-prefix/src/SymCCRuntime-build/"
            "src/backends/qsym/symcc-query-solver"
        )
    )
    if candidates:
        return [str(candidates[-1])]
    raise RuntimeError("symcc-query-solver was not found; set SYMCC_QUERY_SOLVER")


def _load_portfolio(raw: str) -> list[dict[str, object]]:
    if not raw:
        return []
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as stream:
                data = json.load(stream)
        else:
            data = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("invalid solver portfolio JSON") from exc
    if isinstance(data, dict):
        data = data.get("solvers", ())
    if not isinstance(data, list):
        raise RuntimeError("solver portfolio must be a list")
    solvers: list[dict[str, object]] = []
    for index, raw_solver in enumerate(data[:32]):
        if not isinstance(raw_solver, dict):
            continue
        command = raw_solver.get("command")
        if isinstance(command, str):
            command_value = shlex.split(command)
        elif isinstance(command, list) and all(
            isinstance(item, str) for item in command
        ):
            command_value = list(command)
        else:
            continue
        if not command_value:
            continue
        name = str(raw_solver.get("name", f"solver{index}"))[:128]
        kind = str(raw_solver.get("kind", "symcc-json"))
        if kind not in {
            "symcc-json",
            "smtlib-qfbv",
            "bitblast-cadical-qfbv",
        }:
            raise RuntimeError(f"unsupported solver portfolio kind {kind!r}")
        solver = {
            "name": name,
            "kind": kind,
            "command": command_value,
            "persistent": bool(raw_solver.get("persistent", kind == "symcc-json")),
        }
        if kind in {"smtlib-qfbv", "bitblast-cadical-qfbv"}:
            try:
                solver["capabilities"] = normalize_qfbv_capabilities(
                    raw_solver.get("capabilities")
                )
            except ValueError as error:
                raise RuntimeError(
                    f"invalid capabilities for solver {name!r}"
                ) from error
            if solver["persistent"] and not solver["capabilities"]["incremental"]:
                raise RuntimeError(
                    f"persistent solver {name!r} must advertise incremental"
                )
            if kind == "bitblast-cadical-qfbv":
                if not solver["capabilities"]["incremental"]:
                    raise RuntimeError(
                        f"CaDiCaL solver {name!r} must advertise incremental"
                    )
                try:
                    maximum_imports = int(raw_solver.get("max_imported_clauses", 64))
                    worker_epoch = int(raw_solver.get("worker_epoch", 0))
                    context_cache = int(raw_solver.get("context_cache", 8))
                except (TypeError, ValueError, OverflowError) as error:
                    raise RuntimeError(
                        f"invalid incremental proof bounds for solver {name!r}"
                    ) from error
                if (
                    not 0 <= maximum_imports <= 4096
                    or not 0 <= worker_epoch < (1 << 63)
                    or not 1 <= context_cache <= 64
                ):
                    raise RuntimeError(
                        f"invalid incremental proof bounds for solver {name!r}"
                    )
                solver["max_imported_clauses"] = maximum_imports
                solver["worker_epoch"] = worker_epoch
                solver["context_cache"] = context_cache
                native_library = raw_solver.get("native_library")
                if solver["persistent"]:
                    if not isinstance(native_library, str) or not native_library:
                        raise RuntimeError(
                            f"persistent CaDiCaL solver {name!r} requires "
                            "native_library"
                        )
                    solver["native_library"] = native_library
                elif native_library is not None:
                    raise RuntimeError(
                        "native_library requires persistent CaDiCaL mode"
                    )
                realtime = raw_solver.get("realtime_stream")
                if realtime is not None:
                    if not solver["persistent"] or not isinstance(realtime, dict):
                        raise RuntimeError(
                            "realtime_stream requires persistent CaDiCaL mode"
                        )
                    allowed = {
                        "library",
                        "max_imports",
                        "max_events",
                        "max_learned",
                        "max_learned_length",
                        "poll_interval_ms",
                        "checker_budget_ms",
                        "native_queue_literals",
                        "require_clause_compression",
                        "adaptive",
                        "track_clause_activity",
                        "pairing",
                    }
                    if set(realtime) - allowed:
                        raise RuntimeError(
                            f"unknown realtime_stream option for solver {name!r}"
                        )
                    realtime_library = realtime.get("library")
                    if not isinstance(realtime_library, str) or not realtime_library:
                        raise RuntimeError(
                            f"realtime_stream for solver {name!r} requires library"
                        )
                    numeric_options = allowed - {
                        "library",
                        "adaptive",
                        "track_clause_activity",
                        "require_clause_compression",
                        "pairing",
                    }
                    if any(
                        isinstance(realtime.get(option), bool)
                        for option in numeric_options
                        if option in realtime
                    ):
                        raise RuntimeError(
                            f"invalid realtime_stream bounds for solver {name!r}"
                        )
                    try:
                        realtime_config = {
                            "library": realtime_library,
                            "max_imports": int(realtime.get("max_imports", 64)),
                            "max_events": int(realtime.get("max_events", 4096)),
                            "max_learned": int(realtime.get("max_learned", 64)),
                            "max_learned_length": int(
                                realtime.get("max_learned_length", 32)
                            ),
                            "poll_interval_ms": int(
                                realtime.get("poll_interval_ms", 2)
                            ),
                            "checker_budget_ms": int(
                                realtime.get("checker_budget_ms", 100)
                            ),
                            "native_queue_literals": int(
                                realtime.get("native_queue_literals", 65_536)
                            ),
                        }
                    except (TypeError, ValueError, OverflowError) as error:
                        raise RuntimeError(
                            f"invalid realtime_stream bounds for solver {name!r}"
                        ) from error
                    track_activity = realtime.get("track_clause_activity", False)
                    if not isinstance(track_activity, bool):
                        raise RuntimeError(
                            f"invalid clause-activity option for solver {name!r}"
                        )
                    realtime_config["track_clause_activity"] = track_activity
                    require_compression = realtime.get(
                        "require_clause_compression", False
                    )
                    if not isinstance(require_compression, bool):
                        raise RuntimeError(
                            f"invalid clause-compression option for solver {name!r}"
                        )
                    realtime_config["require_clause_compression"] = (
                        require_compression
                    )
                    if not (
                        0 <= realtime_config["max_imports"] <= 4096
                        and 1 <= realtime_config["max_events"] <= 1_000_000
                        and 0 <= realtime_config["max_learned"] <= 65_536
                        and 0 <= realtime_config["max_learned_length"] <= 65_536
                        and 1 <= realtime_config["poll_interval_ms"] <= 1000
                        and 1 <= realtime_config["checker_budget_ms"] <= 60_000
                        and 1 <= realtime_config["native_queue_literals"] <= 1 << 24
                    ):
                        raise RuntimeError(
                            f"invalid realtime_stream bounds for solver {name!r}"
                        )
                    adaptive = realtime.get("adaptive")
                    if adaptive is not None:
                        if not isinstance(adaptive, dict):
                            raise RuntimeError(
                                f"invalid adaptive proof policy for solver {name!r}"
                            )
                        try:
                            realtime_config["adaptive"] = (
                                AdaptiveProofPolicy.from_mapping(
                                    adaptive,
                                    queue_capacity=realtime_config["max_imports"],
                                ).as_config()
                            )
                        except AdaptiveExchangeError as error:
                            raise RuntimeError(
                                f"invalid adaptive proof policy for solver {name!r}"
                            ) from error
                    pairing = realtime.get("pairing")
                    if pairing is not None:
                        if not isinstance(pairing, dict):
                            raise RuntimeError(
                                f"invalid utility pairing policy for solver {name!r}"
                            )
                        if not track_activity:
                            raise RuntimeError(
                                f"utility pairing requires clause activity for solver {name!r}"
                            )
                        try:
                            realtime_config["pairing"] = (
                                UtilityPairingPolicy.from_mapping(pairing).as_config()
                            )
                        except UtilityPairingError as error:
                            raise RuntimeError(
                                f"invalid utility pairing policy for solver {name!r}: {error}"
                            ) from error
                    solver["realtime_stream"] = realtime_config
                if any(
                    raw_solver.get(field) is not None
                    for field in (
                        "unsat_proof",
                        "learned_lemmas",
                        "substitution_cores",
                        "native_state_fork",
                    )
                ):
                    raise RuntimeError(
                        "CaDiCaL incremental proof mode does not combine with "
                        "SMT proof/lemma options in one portfolio entry"
                    )
                solvers.append(solver)
                continue
            native_state_fork = raw_solver.get("native_state_fork", False)
            if not isinstance(native_state_fork, bool):
                raise RuntimeError(
                    f"native state fork for solver {name!r} must be Boolean"
                )
            if native_state_fork and not solver["persistent"]:
                raise RuntimeError(
                    f"native state fork for solver {name!r} requires persistence"
                )
            solver["native_state_fork"] = native_state_fork
            try:
                prefix_cache = int(raw_solver.get("prefix_cache", 4))
            except (TypeError, ValueError, OverflowError) as error:
                raise RuntimeError(
                    f"invalid prefix cache for solver {name!r}"
                ) from error
            if prefix_cache < 1 or prefix_cache > 64:
                raise RuntimeError(
                    f"prefix cache for solver {name!r} must be in [1, 64]"
                )
            solver["prefix_cache"] = prefix_cache
            proof_raw = raw_solver.get("unsat_proof")
            if proof_raw is not None:
                if not isinstance(proof_raw, dict):
                    raise RuntimeError(
                        f"invalid UNSAT proof configuration for solver {name!r}"
                    )
                try:
                    solver["unsat_proof"] = normalize_qfbv_proof_config(proof_raw)
                except ProofVerificationError as error:
                    raise RuntimeError(
                        f"invalid UNSAT proof configuration for solver {name!r}"
                    ) from error
            substitution_core_raw = raw_solver.get("substitution_cores")
            if substitution_core_raw is not None:
                if not isinstance(substitution_core_raw, dict):
                    raise RuntimeError(
                        f"invalid substitution-core configuration for solver {name!r}"
                    )
                if "unsat_proof" not in solver:
                    raise RuntimeError(
                        "substitution-core reuse requires an unsat_proof policy"
                    )
                extractor_raw = substitution_core_raw.get("extractor_command")
                if isinstance(extractor_raw, str):
                    extractor_command = shlex.split(extractor_raw)
                elif isinstance(extractor_raw, list) and all(
                    isinstance(item, str) and item for item in extractor_raw
                ):
                    extractor_command = list(extractor_raw)
                else:
                    raise RuntimeError(
                        f"substitution-core extractor for solver {name!r} is invalid"
                    )
                try:
                    max_candidates = int(
                        substitution_core_raw.get("max_candidates", 64)
                    )
                    max_candidate_scan = int(
                        substitution_core_raw.get("max_candidate_scan", 4096)
                    )
                    max_join_states = int(
                        substitution_core_raw.get("max_join_states", 65_536)
                    )
                    max_unification_pairs = int(
                        substitution_core_raw.get("max_unification_pairs", 1_000_000)
                    )
                    verified_core_cache_entries = int(
                        substitution_core_raw.get("verified_core_cache_entries", 1024)
                    )
                    lookup_timeout_ms = int(
                        substitution_core_raw.get("lookup_timeout_ms", 5_000)
                    )
                    publish_timeout_ms = int(
                        substitution_core_raw.get("publish_timeout_ms", 30_000)
                    )
                except (TypeError, ValueError, OverflowError) as error:
                    raise RuntimeError(
                        f"substitution-core bounds for solver {name!r} are invalid"
                    ) from error
                if (
                    not 1 <= max_candidates <= 4096
                    or not max_candidates <= max_candidate_scan <= 1_000_000
                    or not 1 <= max_join_states <= 100_000_000
                    or not 1 <= max_unification_pairs <= 100_000_000
                    or not 1 <= verified_core_cache_entries <= 65_536
                    or not 1 <= lookup_timeout_ms <= 3_600_000
                    or not 1 <= publish_timeout_ms <= 3_600_000
                ):
                    raise RuntimeError(
                        f"substitution-core bounds for solver {name!r} are invalid"
                    )
                solver["substitution_cores"] = {
                    "extractor_command": extractor_command,
                    "max_candidates": max_candidates,
                    "max_candidate_scan": max_candidate_scan,
                    "max_join_states": max_join_states,
                    "max_unification_pairs": max_unification_pairs,
                    "verified_core_cache_entries": (verified_core_cache_entries),
                    "lookup_timeout_ms": lookup_timeout_ms,
                    "publish_timeout_ms": publish_timeout_ms,
                }
            lemma_raw = raw_solver.get("learned_lemmas")
            if lemma_raw is not None:
                if not isinstance(lemma_raw, dict):
                    raise RuntimeError(
                        f"invalid learned lemma configuration for solver {name!r}"
                    )
                if not solver["persistent"] or "unsat_proof" not in solver:
                    raise RuntimeError(
                        "learned lemma exchange requires a persistent QF_BV "
                        "solver with unsat_proof"
                    )
                if native_state_fork:
                    raise RuntimeError(
                        "native state fork cannot publish cvc5 learned literals"
                    )
                literal_type = str(lemma_raw.get("type", "preprocess"))
                if literal_type not in {
                    "preprocess",
                    "input",
                    "solvable",
                    "internal",
                }:
                    raise RuntimeError(
                        f"invalid learned literal type for solver {name!r}"
                    )
                try:
                    maximum = int(lemma_raw.get("max_per_query", 8))
                    timeout = int(lemma_raw.get("timeout_ms", 30_000))
                except (TypeError, ValueError, OverflowError) as error:
                    raise RuntimeError(
                        f"invalid learned lemma bounds for solver {name!r}"
                    ) from error
                if not 1 <= maximum <= 64 or not 1 <= timeout <= 3_600_000:
                    raise RuntimeError(
                        f"learned lemma bounds for solver {name!r} are invalid"
                    )
                solver["learned_lemmas"] = {
                    "type": literal_type,
                    "max_per_query": maximum,
                    "timeout_ms": timeout,
                }
        elif raw_solver.get("unsat_proof") is not None:
            raise RuntimeError("UNSAT proof receipts require smtlib-qfbv kind")
        elif raw_solver.get("learned_lemmas") is not None:
            raise RuntimeError("learned lemma exchange requires smtlib-qfbv kind")
        elif raw_solver.get("substitution_cores") is not None:
            raise RuntimeError("substitution-core reuse requires smtlib-qfbv kind")
        elif raw_solver.get("native_state_fork") is not None:
            raise RuntimeError("native state fork requires smtlib-qfbv kind")
        solvers.append(solver)
    if not solvers:
        raise RuntimeError("solver portfolio does not contain valid solvers")
    return solvers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--spool", type=Path)
    parser.add_argument("--solver", help="solver helper command")
    parser.add_argument(
        "--portfolio",
        help="JSON or path: {'solvers':[{'name':'z3','command':'...'}]}",
    )
    parser.add_argument(
        "--portfolio-parallelism",
        type=int,
        default=0,
        help="max concurrent solvers per portfolio query; 0 uses environment/auto",
    )
    parser.add_argument(
        "--portfolio-cancel-grace-ms",
        type=int,
        default=None,
        help=(
            "cancel remaining attempts this many ms after a SAT result; "
            "unset uses the environment and -1 disables cancellation"
        ),
    )
    parser.add_argument(
        "--qfbv-context-store",
        type=Path,
        help=(
            "shared content-addressed QF_BV prefix context directory; "
            "defaults to SYMCC_QFBV_CONTEXT_STORE"
        ),
    )
    parser.add_argument(
        "--qfbv-context-max-objects",
        type=int,
        default=None,
        help="maximum number of shared QF_BV context objects",
    )
    parser.add_argument(
        "--qfbv-context-max-active",
        type=int,
        default=None,
        help="cluster-wide active shared-context materialization quota",
    )
    parser.add_argument(
        "--qfbv-context-lease-seconds",
        type=float,
        default=None,
        help="fenced shared-context materialization lease duration",
    )
    parser.add_argument(
        "--qfbv-proof-store",
        type=Path,
        help=(
            "shared content-addressed QF_BV UNSAT proof directory; "
            "defaults to SYMCC_QFBV_PROOF_STORE or a store-local directory"
        ),
    )
    parser.add_argument(
        "--qfbv-proof-max-objects",
        type=int,
        default=None,
        help="maximum combined QF_BV proof and receipt objects",
    )
    parser.add_argument(
        "--qfbv-proof-max-bytes",
        type=int,
        default=None,
        help="maximum bytes in one raw CPC proof body",
    )
    parser.add_argument(
        "--qfbv-lemma-store",
        type=Path,
        help=(
            "shared content-addressed verified QF_BV lemma directory; "
            "defaults to SYMCC_QFBV_LEMMA_STORE or a store-local directory"
        ),
    )
    parser.add_argument(
        "--qfbv-lemma-max-records",
        type=int,
        default=None,
        help="maximum verified QF_BV lemma records",
    )
    parser.add_argument(
        "--qfbv-lemma-max-bytes",
        type=int,
        default=None,
        help="maximum total encoded bytes in the verified lemma store",
    )
    parser.add_argument(
        "--qfbv-substitution-core-store",
        type=Path,
        help=(
            "shared content-addressed variable-substitution UNSAT-core directory; "
            "defaults to SYMCC_QFBV_SUBSTITUTION_CORE_STORE"
        ),
    )
    parser.add_argument(
        "--qfbv-substitution-core-max-records",
        type=int,
        default=None,
        help="maximum verified variable-substitution UNSAT-core records",
    )
    parser.add_argument(
        "--qfbv-substitution-core-max-bytes",
        type=int,
        default=None,
        help="maximum total encoded bytes in the substitution-core store",
    )
    parser.add_argument(
        "--qfbv-incremental-proof-store",
        type=Path,
        help=(
            "shared content-addressed project LRUP DAG directory; "
            "defaults to SYMCC_QFBV_INCREMENTAL_PROOF_STORE"
        ),
    )
    parser.add_argument(
        "--qfbv-incremental-proof-max-records",
        type=int,
        default=None,
        help="maximum checked incremental proof fragments",
    )
    parser.add_argument(
        "--qfbv-incremental-proof-max-bytes",
        type=int,
        default=None,
        help="maximum total encoded bytes in the incremental proof store",
    )
    parser.add_argument(
        "--qfbv-proof-replay-cache-entries",
        type=int,
        default=None,
        help="bounded plan-scoped proof authorization cache entries; 0 disables",
    )
    parser.add_argument(
        "--qfbv-proof-replay-cache-bytes",
        type=int,
        default=None,
        help="accounted bytes for proof replay cache entries; 0 disables",
    )
    parser.add_argument(
        "--qfbv-partition-store",
        type=Path,
        help=(
            "shared content-addressed proof-prefix partition directory; "
            "defaults to SYMCC_QFBV_PARTITION_STORE"
        ),
    )
    parser.add_argument(
        "--qfbv-partition-cubes",
        type=int,
        default=None,
        help="execute each QF_BV query as this many certified cubes; 0 disables",
    )
    parser.add_argument(
        "--qfbv-partition-parallelism",
        type=int,
        default=None,
        help="cluster-local cube worker budget for proof-aware execution",
    )
    parser.add_argument(
        "--qfbv-partition-max-attempts",
        type=int,
        default=None,
        help="maximum fenced attempts per certified cube",
    )
    parser.add_argument(
        "--qfbv-partition-cube-timeout-ms",
        type=int,
        default=None,
        help="deadline for each cube solver invocation",
    )
    parser.add_argument(
        "--qfbv-partition-task-lease-ms",
        type=int,
        default=None,
        help="renewable fenced lease duration for each cube task",
    )
    parser.add_argument(
        "--qfbv-partition-execution-store",
        type=Path,
        help="persistent SQLite cube-task ledger; defaults below the QueryStore",
    )
    parser.add_argument(
        "--qfbv-online-cubing",
        action="store_true",
        help="enable persistent activity/cost-guided online cube selection",
    )
    parser.add_argument(
        "--qfbv-online-cubing-store",
        type=Path,
        help="persistent online-cubing SQLite ledger; defaults below QueryStore",
    )
    parser.add_argument(
        "--qfbv-online-cubing-candidates",
        help="canonical comma-separated cube-count candidates",
    )
    parser.add_argument(
        "--qfbv-online-cubing-strategies",
        help="canonical strategy list: static,activity,cost",
    )
    parser.add_argument(
        "--qfbv-online-cubing-prerun-ms",
        type=int,
        default=None,
        help="bounded solver prerun used to collect checked activity",
    )
    parser.add_argument(
        "--qfbv-online-cubing-min-exploration",
        type=int,
        default=None,
        help="minimum observed outcomes per online-cubing strategy",
    )
    parser.add_argument(
        "--qfbv-online-cubing-cost-min-exploration",
        type=int,
        default=None,
        help="minimum observed outcomes per cost-guided cube candidate",
    )
    parser.add_argument(
        "--qfbv-online-cubing-exploration-permille",
        type=int,
        default=None,
        help="post-warmup exploration probability in permille",
    )
    parser.add_argument(
        "--qfbv-online-cubing-cost-reference-us",
        type=int,
        default=None,
        help="fixed-point elapsed-cost normalization reference",
    )
    parser.add_argument(
        "--qfbv-lidrup-checker",
        type=Path,
        help="pinned lidrup-check executable for strict external replay",
    )
    parser.add_argument(
        "--qfbv-lidrup-checker-sha256",
        help="required SHA-256 identity of --qfbv-lidrup-checker",
    )
    parser.add_argument(
        "--qfbv-lidrup-wire-store",
        type=Path,
        help="immutable ICNF/LIDRUP sidecar store",
    )
    parser.add_argument(
        "--qfbv-lidrup-wire-max-records",
        type=int,
        default=None,
        help="maximum combined LIDRUP artifacts and checker receipts",
    )
    parser.add_argument(
        "--qfbv-lidrup-wire-max-bytes",
        type=int,
        default=None,
        help="maximum encoded bytes in the LIDRUP sidecar store",
    )
    parser.add_argument(
        "--qfbv-lidrup-timeout-ms",
        type=int,
        default=None,
        help="strict external LIDRUP checker deadline",
    )
    parser.add_argument(
        "--qfbv-artifact-lifecycle-store",
        type=Path,
        help=(
            "shared fenced job-root and dependency graph directory; "
            "defaults to SYMCC_QFBV_ARTIFACT_LIFECYCLE_STORE"
        ),
    )
    parser.add_argument(
        "--qfbv-artifact-max-jobs",
        type=int,
        default=None,
        help=(
            "maximum distinct fenced artifact job IDs retained by one lifecycle store"
        ),
    )
    parser.add_argument(
        "--qfbv-artifact-job-id",
        help="stable campaign/job identity for fenced artifact roots",
    )
    parser.add_argument(
        "--qfbv-artifact-job-lease-seconds",
        type=float,
        default=None,
        help="lifetime of one renewable artifact-root lease",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-on-start",
        action="store_true",
        help="run one bounded artifact collection before solving",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-only",
        action="store_true",
        help="run one bounded artifact collection and exit",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-grace-seconds",
        type=float,
        default=None,
        help="retain artifacts accessed within this wall-clock grace period",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-max-objects",
        type=int,
        default=None,
        help="hard object-deletion budget for one collection",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-max-bytes",
        type=int,
        default=None,
        help="hard byte-deletion budget for one collection",
    )
    parser.add_argument(
        "--qfbv-artifact-gc-time-ms",
        type=int,
        default=None,
        help=(
            "selection/lock budget for one collection; an admitted atomic "
            "deletion is allowed to finish"
        ),
    )
    parser.add_argument(
        "--qfbv-artifact-gc-scan-max-objects",
        type=int,
        default=None,
        help="hard pre-GC cross-store index synchronization budget",
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument(
        "--malleable-workers",
        action="store_true",
        help="enable generation-fenced logical grow/drain/migrate/shrink",
    )
    parser.add_argument(
        "--malleable-pool-id",
        default="",
        help="stable persistence identity for this prelaunched worker pool",
    )
    parser.add_argument(
        "--malleable-backlog-per-slot",
        type=int,
        default=1,
        help="pending or leased queries required per active logical slot",
    )
    parser.add_argument(
        "--malleable-hysteresis-slots",
        type=int,
        default=1,
        help="minimum assignment changes required after initial allocation",
    )
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument(
        "--publication-reconcile-interval",
        type=float,
        default=1.0,
        help="seconds between bounded result-publication janitor passes; 0 disables",
    )
    parser.add_argument(
        "--publication-reconcile-limit",
        type=int,
        default=64,
        help="maximum outbox rows attempted by one janitor pass",
    )
    parser.add_argument(
        "--publication-reconcile-time-ms",
        type=int,
        default=100,
        help="wall-time budget for one result-publication janitor pass",
    )
    parser.add_argument(
        "--publication-max-attempts",
        type=int,
        default=8,
        help="failed publication attempts before a row enters the dead letter set",
    )
    parser.add_argument(
        "--publication-retry-base-seconds",
        type=float,
        default=1.0,
        help="initial publication retry delay",
    )
    parser.add_argument(
        "--publication-retry-max-seconds",
        type=float,
        default=300.0,
        help="maximum publication retry delay",
    )
    parser.add_argument("--lease-seconds", type=float, default=60.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--traversal",
        choices=("dfs", "bfs", "priority", "structural"),
        default="structural",
        help="persistent query scheduling policy",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain currently available work and exit",
    )
    parser.add_argument(
        "--schedule-constraints",
        type=Path,
        help="consume symcc-schedule-constraint-v1 JSONL for joint validation",
    )
    parser.add_argument(
        "--schedule-validation-out",
        type=Path,
        help="append symcc-joint-schedule-query-v1 validation JSONL",
    )
    parser.add_argument(
        "--schedule-validation-max-artifacts",
        type=int,
        default=4096,
        help="maximum schedule constraint rows scanned per validation pass",
    )
    parser.add_argument(
        "--schedule-validation-only",
        action="store_true",
        help="validate schedule artifacts against the query store and exit",
    )
    parser.add_argument(
        "--artifact-audit-only",
        action="store_true",
        help="report QueryStore SQLite-to-CAS reachability without deleting data",
    )
    parser.add_argument(
        "--artifact-audit-max-entries",
        type=int,
        default=100_000,
        help="maximum root and shard directory entries observed by an audit",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print store statistics without running workers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0.0 <= args.publication_reconcile_interval <= 3600.0:
        raise ValueError("publication reconcile interval must be in [0, 3600]")
    if not 1 <= args.publication_reconcile_limit <= 1_000_000:
        raise ValueError("publication reconcile limit must be in [1, 1000000]")
    if not 1 <= args.publication_reconcile_time_ms <= 3_600_000:
        raise ValueError("publication reconcile time must be in [1, 3600000] ms")
    if not 1 <= args.publication_max_attempts <= 1_000_000:
        raise ValueError("publication max attempts must be in [1, 1000000]")
    if not (
        0.0
        <= args.publication_retry_base_seconds
        <= args.publication_retry_max_seconds
        <= 7 * 24 * 60 * 60
    ):
        raise ValueError("publication retry delays are invalid")
    store = QueryStore(args.store)

    def reconcile_publications(*, respect_retry_after: bool) -> int:
        return store.reconcile_result_publications(
            limit=args.publication_reconcile_limit,
            time_budget_seconds=args.publication_reconcile_time_ms / 1000.0,
            continue_on_error=True,
            respect_retry_after=respect_retry_after,
            max_attempts=args.publication_max_attempts,
            retry_base_seconds=args.publication_retry_base_seconds,
            retry_max_seconds=args.publication_retry_max_seconds,
        )
    portfolio_specs = _load_portfolio(
        args.portfolio or os.environ.get("SYMCC_QUERY_SOLVER_PORTFOLIO", "")
    )
    lifecycle_raw = (
        str(args.qfbv_artifact_lifecycle_store)
        if args.qfbv_artifact_lifecycle_store is not None
        else os.environ.get("SYMCC_QFBV_ARTIFACT_LIFECYCLE_STORE", "")
    )
    artifact_max_jobs = (
        args.qfbv_artifact_max_jobs
        if args.qfbv_artifact_max_jobs is not None
        else _environment_integer("SYMCC_QFBV_ARTIFACT_MAX_JOBS", 1_000_000)
    )
    if not 1 <= artifact_max_jobs <= 10_000_000:
        raise ValueError("--qfbv-artifact-max-jobs must be in [1, 10000000]")
    artifact_lifecycle = (
        ArtifactLifecycleRegistry(lifecycle_raw, max_jobs=artifact_max_jobs)
        if lifecycle_raw
        else None
    )
    artifact_job_lease_seconds = (
        args.qfbv_artifact_job_lease_seconds
        if args.qfbv_artifact_job_lease_seconds is not None
        else _environment_float("SYMCC_QFBV_ARTIFACT_JOB_LEASE_SECONDS", 60.0)
    )
    if not 0.1 <= artifact_job_lease_seconds <= 86_400.0:
        raise ValueError("--qfbv-artifact-job-lease-seconds must be in [0.1, 86400]")
    artifact_gc_grace_seconds = (
        args.qfbv_artifact_gc_grace_seconds
        if args.qfbv_artifact_gc_grace_seconds is not None
        else _environment_float("SYMCC_QFBV_ARTIFACT_GC_GRACE_SECONDS", 86_400.0)
    )
    artifact_gc_max_objects = (
        args.qfbv_artifact_gc_max_objects
        if args.qfbv_artifact_gc_max_objects is not None
        else _environment_integer("SYMCC_QFBV_ARTIFACT_GC_MAX_OBJECTS", 1024)
    )
    artifact_gc_max_bytes = (
        args.qfbv_artifact_gc_max_bytes
        if args.qfbv_artifact_gc_max_bytes is not None
        else _environment_integer("SYMCC_QFBV_ARTIFACT_GC_MAX_BYTES", 256 * 1024 * 1024)
    )
    artifact_gc_time_ms = (
        args.qfbv_artifact_gc_time_ms
        if args.qfbv_artifact_gc_time_ms is not None
        else _environment_integer("SYMCC_QFBV_ARTIFACT_GC_TIME_MS", 30_000)
    )
    artifact_gc_scan_max_objects = (
        args.qfbv_artifact_gc_scan_max_objects
        if args.qfbv_artifact_gc_scan_max_objects is not None
        else _environment_integer("SYMCC_QFBV_ARTIFACT_GC_SCAN_MAX_OBJECTS", 100_000)
    )
    if not 0.0 <= artifact_gc_grace_seconds <= 365 * 86_400.0:
        raise ValueError("--qfbv-artifact-gc-grace-seconds must be in [0, 31536000]")
    if not 1 <= artifact_gc_max_objects <= 1_000_000:
        raise ValueError("--qfbv-artifact-gc-max-objects must be in [1, 1000000]")
    if not 1 <= artifact_gc_max_bytes <= 1 << 40:
        raise ValueError(f"--qfbv-artifact-gc-max-bytes must be in [1, {1 << 40}]")
    if not 1 <= artifact_gc_time_ms <= 3_600_000:
        raise ValueError("--qfbv-artifact-gc-time-ms must be in [1, 3600000]")
    if not 1 <= artifact_gc_scan_max_objects <= 3_000_000:
        raise ValueError("--qfbv-artifact-gc-scan-max-objects must be in [1, 3000000]")
    if (args.qfbv_artifact_gc_on_start or args.qfbv_artifact_gc_only) and (
        artifact_lifecycle is None
    ):
        raise ValueError("artifact GC requires --qfbv-artifact-lifecycle-store")
    worker_mode = not (
        args.schedule_validation_only
        or args.artifact_audit_only
        or args.stats
        or args.qfbv_artifact_gc_only
    )
    artifact_job_lease: ArtifactJobLease | None = None
    if artifact_lifecycle is not None and worker_mode:
        artifact_job_id = (
            args.qfbv_artifact_job_id
            or os.environ.get("SYMCC_QFBV_ARTIFACT_JOB_ID", "")
            or (f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}")
        )
        artifact_job_lease = artifact_lifecycle.start_job(
            artifact_job_id,
            f"{socket.gethostname()}:{os.getpid()}",
            lease_seconds=artifact_job_lease_seconds,
        )
    context_max_objects = (
        args.qfbv_context_max_objects
        if args.qfbv_context_max_objects is not None
        else _environment_integer("SYMCC_QFBV_CONTEXT_MAX_OBJECTS", 1_000_000)
    )
    context_max_active = (
        args.qfbv_context_max_active
        if args.qfbv_context_max_active is not None
        else _environment_integer("SYMCC_QFBV_CONTEXT_MAX_ACTIVE", 64)
    )
    context_lease_seconds = (
        args.qfbv_context_lease_seconds
        if args.qfbv_context_lease_seconds is not None
        else _environment_float("SYMCC_QFBV_CONTEXT_LEASE_SECONDS", 30.0)
    )
    if not 1 <= context_max_objects <= 10_000_000:
        raise ValueError("--qfbv-context-max-objects must be in [1, 10000000]")
    if not 1 <= context_max_active <= 4096:
        raise ValueError("--qfbv-context-max-active must be in [1, 4096]")
    if not 0.1 <= context_lease_seconds <= 3600.0:
        raise ValueError("--qfbv-context-lease-seconds must be in [0.1, 3600]")
    context_store_raw = (
        str(args.qfbv_context_store)
        if args.qfbv_context_store is not None
        else os.environ.get("SYMCC_QFBV_CONTEXT_STORE", "")
    )
    shared_context_store = (
        CrossWorkerContextStore(
            context_store_raw,
            max_contexts=context_max_objects,
            max_active_materializations=context_max_active,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if context_store_raw
        else None
    )
    proof_max_objects = (
        args.qfbv_proof_max_objects
        if args.qfbv_proof_max_objects is not None
        else _environment_integer("SYMCC_QFBV_PROOF_MAX_OBJECTS", 1_000_000)
    )
    proof_max_bytes = (
        args.qfbv_proof_max_bytes
        if args.qfbv_proof_max_bytes is not None
        else _environment_integer("SYMCC_QFBV_PROOF_MAX_BYTES", MAX_PROOF_BYTES)
    )
    if not 1 <= proof_max_objects <= 10_000_000:
        raise ValueError("--qfbv-proof-max-objects must be in [1, 10000000]")
    if not 1024 <= proof_max_bytes <= MAX_PROOF_BYTES:
        raise ValueError(f"--qfbv-proof-max-bytes must be in [1024, {MAX_PROOF_BYTES}]")
    proof_configured = any(
        isinstance(spec.get("unsat_proof"), dict) for spec in portfolio_specs
    )
    proof_store_raw = (
        str(args.qfbv_proof_store)
        if args.qfbv_proof_store is not None
        else os.environ.get("SYMCC_QFBV_PROOF_STORE", "")
    )
    if proof_configured and not proof_store_raw:
        proof_store_raw = str(Path(args.store) / "qfbv-unsat-proofs")
    proof_store = (
        QfbvProofStore(
            proof_store_raw,
            max_objects=proof_max_objects,
            max_proof_bytes=proof_max_bytes,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if proof_store_raw
        else None
    )
    lemma_configured = any(
        isinstance(spec.get("learned_lemmas"), dict) for spec in portfolio_specs
    )
    lemma_max_records = (
        args.qfbv_lemma_max_records
        if args.qfbv_lemma_max_records is not None
        else _environment_integer("SYMCC_QFBV_LEMMA_MAX_RECORDS", 1_000_000)
    )
    lemma_max_bytes = (
        args.qfbv_lemma_max_bytes
        if args.qfbv_lemma_max_bytes is not None
        else _environment_integer("SYMCC_QFBV_LEMMA_MAX_BYTES", 256 * 1024 * 1024)
    )
    if not 1 <= lemma_max_records <= 10_000_000:
        raise ValueError("--qfbv-lemma-max-records must be in [1, 10000000]")
    if not MAX_RECORD_BYTES <= lemma_max_bytes <= 1 << 40:
        raise ValueError(
            f"--qfbv-lemma-max-bytes must be in [{MAX_RECORD_BYTES}, {1 << 40}]"
        )
    lemma_store_raw = (
        str(args.qfbv_lemma_store)
        if args.qfbv_lemma_store is not None
        else os.environ.get("SYMCC_QFBV_LEMMA_STORE", "")
    )
    if lemma_configured and not lemma_store_raw:
        lemma_store_raw = str(Path(args.store) / "qfbv-verified-lemmas")
    if lemma_configured and (shared_context_store is None or proof_store is None):
        raise ValueError(
            "learned lemma exchange requires QF_BV context and proof stores"
        )
    lemma_store = (
        QfbvLemmaStore(
            lemma_store_raw,
            max_records=lemma_max_records,
            max_bytes=lemma_max_bytes,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if lemma_store_raw
        else None
    )
    substitution_core_configured = any(
        isinstance(spec.get("substitution_cores"), dict) for spec in portfolio_specs
    )
    substitution_core_max_records = (
        args.qfbv_substitution_core_max_records
        if args.qfbv_substitution_core_max_records is not None
        else _environment_integer("SYMCC_QFBV_SUBSTITUTION_CORE_MAX_RECORDS", 100_000)
    )
    substitution_core_max_bytes = (
        args.qfbv_substitution_core_max_bytes
        if args.qfbv_substitution_core_max_bytes is not None
        else _environment_integer("SYMCC_QFBV_SUBSTITUTION_CORE_MAX_BYTES", 1 << 30)
    )
    if not 1 <= substitution_core_max_records <= 10_000_000:
        raise ValueError(
            "--qfbv-substitution-core-max-records must be in [1, 10000000]"
        )
    if not (
        MAX_SUBSTITUTION_CORE_RECORD_BYTES <= substitution_core_max_bytes <= 1 << 40
    ):
        raise ValueError(
            "--qfbv-substitution-core-max-bytes must be in "
            f"[{MAX_SUBSTITUTION_CORE_RECORD_BYTES}, {1 << 40}]"
        )
    substitution_core_store_raw = (
        str(args.qfbv_substitution_core_store)
        if args.qfbv_substitution_core_store is not None
        else os.environ.get("SYMCC_QFBV_SUBSTITUTION_CORE_STORE", "")
    )
    if substitution_core_configured and not substitution_core_store_raw:
        substitution_core_store_raw = str(Path(args.store) / "qfbv-substitution-cores")
    if substitution_core_configured and proof_store is None:
        raise ValueError("substitution-core reuse requires the QF_BV proof store")
    substitution_core_store = (
        QfbvSubstitutionCoreStore(
            substitution_core_store_raw,
            max_records=substitution_core_max_records,
            max_bytes=substitution_core_max_bytes,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if substitution_core_store_raw
        else None
    )
    incremental_proof_configured = any(
        spec.get("kind") == "bitblast-cadical-qfbv" for spec in portfolio_specs
    )
    incremental_proof_max_records = (
        args.qfbv_incremental_proof_max_records
        if args.qfbv_incremental_proof_max_records is not None
        else _environment_integer("SYMCC_QFBV_INCREMENTAL_PROOF_MAX_RECORDS", 1_000_000)
    )
    incremental_proof_max_bytes = (
        args.qfbv_incremental_proof_max_bytes
        if args.qfbv_incremental_proof_max_bytes is not None
        else _environment_integer(
            "SYMCC_QFBV_INCREMENTAL_PROOF_MAX_BYTES", 64 * 1024 * 1024 * 1024
        )
    )
    if not 1 <= incremental_proof_max_records <= 10_000_000:
        raise ValueError(
            "--qfbv-incremental-proof-max-records must be in [1, 10000000]"
        )
    if not 1 <= incremental_proof_max_bytes <= 1 << 44:
        raise ValueError(
            "--qfbv-incremental-proof-max-bytes must be in [1, 17592186044416]"
        )
    proof_replay_cache_entries = (
        args.qfbv_proof_replay_cache_entries
        if args.qfbv_proof_replay_cache_entries is not None
        else _environment_integer(
            "SYMCC_QFBV_PROOF_REPLAY_CACHE_ENTRIES",
            DEFAULT_REPLAY_CACHE_ENTRIES,
        )
    )
    proof_replay_cache_bytes = (
        args.qfbv_proof_replay_cache_bytes
        if args.qfbv_proof_replay_cache_bytes is not None
        else _environment_integer(
            "SYMCC_QFBV_PROOF_REPLAY_CACHE_BYTES",
            DEFAULT_REPLAY_CACHE_BYTES,
        )
    )
    if not 0 <= proof_replay_cache_entries <= MAX_REPLAY_CACHE_ENTRIES:
        raise ValueError(
            "--qfbv-proof-replay-cache-entries must be in "
            f"[0, {MAX_REPLAY_CACHE_ENTRIES}]"
        )
    if not 0 <= proof_replay_cache_bytes <= MAX_REPLAY_CACHE_BYTES:
        raise ValueError(
            f"--qfbv-proof-replay-cache-bytes must be in [0, {MAX_REPLAY_CACHE_BYTES}]"
        )
    if (proof_replay_cache_entries == 0) != (proof_replay_cache_bytes == 0):
        raise ValueError(
            "--qfbv-proof-replay-cache-entries and bytes must be disabled together"
        )
    incremental_proof_store_raw = (
        str(args.qfbv_incremental_proof_store)
        if args.qfbv_incremental_proof_store is not None
        else os.environ.get("SYMCC_QFBV_INCREMENTAL_PROOF_STORE", "")
    )
    if incremental_proof_configured and not incremental_proof_store_raw:
        incremental_proof_store_raw = str(Path(args.store) / "qfbv-incremental-proofs")
    incremental_proof_store = (
        IncrementalProofStore(
            incremental_proof_store_raw,
            max_records=incremental_proof_max_records,
            max_bytes=incremental_proof_max_bytes,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if incremental_proof_store_raw
        else None
    )
    incremental_proof_checker = (
        IncrementalProofChecker(
            incremental_proof_store,
            replay_cache_entries=proof_replay_cache_entries,
            replay_cache_bytes=proof_replay_cache_bytes,
        )
        if incremental_proof_store is not None
        else None
    )
    partition_cube_count = (
        args.qfbv_partition_cubes
        if args.qfbv_partition_cubes is not None
        else _environment_integer("SYMCC_QFBV_PARTITION_CUBES", 0)
    )
    partition_parallelism = (
        args.qfbv_partition_parallelism
        if args.qfbv_partition_parallelism is not None
        else _environment_integer("SYMCC_QFBV_PARTITION_PARALLELISM", 4)
    )
    partition_max_attempts = (
        args.qfbv_partition_max_attempts
        if args.qfbv_partition_max_attempts is not None
        else _environment_integer("SYMCC_QFBV_PARTITION_MAX_ATTEMPTS", 3)
    )
    partition_cube_timeout_ms = (
        args.qfbv_partition_cube_timeout_ms
        if args.qfbv_partition_cube_timeout_ms is not None
        else _environment_integer("SYMCC_QFBV_PARTITION_CUBE_TIMEOUT_MS", 30_000)
    )
    partition_task_lease_ms = (
        args.qfbv_partition_task_lease_ms
        if args.qfbv_partition_task_lease_ms is not None
        else _environment_integer("SYMCC_QFBV_PARTITION_TASK_LEASE_MS", 60_000)
    )
    partition_execution_enabled = partition_cube_count != 0
    if partition_execution_enabled and not 2 <= partition_cube_count <= 4096:
        raise ValueError("--qfbv-partition-cubes must be 0 or in [2, 4096]")
    if not 1 <= partition_parallelism <= 256:
        raise ValueError("--qfbv-partition-parallelism must be in [1, 256]")
    if not 1 <= partition_max_attempts <= 32:
        raise ValueError("--qfbv-partition-max-attempts must be in [1, 32]")
    partition_execution_policy = PartitionExecutionPolicy(
        parallelism=partition_parallelism,
        max_attempts=partition_max_attempts,
        cube_timeout_ms=partition_cube_timeout_ms,
        task_lease_ms=partition_task_lease_ms,
    )
    partition_store_raw = (
        str(args.qfbv_partition_store)
        if args.qfbv_partition_store is not None
        else os.environ.get("SYMCC_QFBV_PARTITION_STORE", "")
    )
    if partition_execution_enabled and not partition_store_raw:
        partition_store_raw = str(Path(args.store) / "qfbv-partitions")
    partition_store = (
        ProofPrefixPartitionStore(
            partition_store_raw,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if partition_store_raw
        else None
    )
    partition_execution_store_raw = (
        str(args.qfbv_partition_execution_store)
        if args.qfbv_partition_execution_store is not None
        else os.environ.get("SYMCC_QFBV_PARTITION_EXECUTION_STORE", "")
    )
    if partition_execution_enabled and not partition_execution_store_raw:
        partition_execution_store_raw = str(
            Path(args.store) / "qfbv-partition-executions"
        )
    partition_execution_store = (
        PartitionExecutionStore(
            partition_execution_store_raw,
            lifecycle=artifact_lifecycle,
            lifecycle_lease=artifact_job_lease,
        )
        if partition_execution_store_raw
        else None
    )
    online_cubing_enabled = bool(args.qfbv_online_cubing) or _environment_boolean(
        "SYMCC_QFBV_ONLINE_CUBING", False
    )
    online_cubing_store: OnlineCubingPolicyStore | None = None
    if online_cubing_enabled:
        if not partition_execution_enabled:
            raise ValueError("online cubing requires proof-aware partition execution")
        raw_candidates = (
            str(args.qfbv_online_cubing_candidates)
            if args.qfbv_online_cubing_candidates is not None
            else os.environ.get(
                "SYMCC_QFBV_ONLINE_CUBING_CANDIDATES", "2,4,8,16"
            )
        )
        candidates = tuple(
            sorted(
                {
                    *_comma_separated_integers(
                        raw_candidates, "online cubing candidates"
                    ),
                    partition_cube_count,
                }
            )
        )
        raw_strategies = (
            str(args.qfbv_online_cubing_strategies)
            if args.qfbv_online_cubing_strategies is not None
            else os.environ.get(
                "SYMCC_QFBV_ONLINE_CUBING_STRATEGIES",
                "static,activity,cost",
            )
        )
        try:
            online_policy = OnlineCubingPolicy(
                base_cube_count=partition_cube_count,
                cube_candidates=candidates,
                strategies=_comma_separated_strings(
                    raw_strategies, "online cubing strategies"
                ),
                prerun_budget_ms=(
                    args.qfbv_online_cubing_prerun_ms
                    if args.qfbv_online_cubing_prerun_ms is not None
                    else _environment_integer(
                        "SYMCC_QFBV_ONLINE_CUBING_PRERUN_MS", 250
                    )
                ),
                base_cube_timeout_ms=partition_cube_timeout_ms,
                max_cube_attempts=partition_max_attempts,
                min_exploration_samples=(
                    args.qfbv_online_cubing_min_exploration
                    if args.qfbv_online_cubing_min_exploration is not None
                    else _environment_integer(
                        "SYMCC_QFBV_ONLINE_CUBING_MIN_EXPLORATION", 2
                    )
                ),
                cost_min_exploration_samples=(
                    args.qfbv_online_cubing_cost_min_exploration
                    if args.qfbv_online_cubing_cost_min_exploration is not None
                    else _environment_integer(
                        "SYMCC_QFBV_ONLINE_CUBING_COST_MIN_EXPLORATION", 1
                    )
                ),
                exploration_permille=(
                    args.qfbv_online_cubing_exploration_permille
                    if args.qfbv_online_cubing_exploration_permille is not None
                    else _environment_integer(
                        "SYMCC_QFBV_ONLINE_CUBING_EXPLORATION_PERMILLE", 100
                    )
                ),
                cost_reference_us=(
                    args.qfbv_online_cubing_cost_reference_us
                    if args.qfbv_online_cubing_cost_reference_us is not None
                    else _environment_integer(
                        "SYMCC_QFBV_ONLINE_CUBING_COST_REFERENCE_US", 1_000_000
                    )
                ),
            )
        except OnlineCubingError as error:
            raise ValueError(f"invalid online-cubing configuration: {error}") from error
        online_store_raw = (
            str(args.qfbv_online_cubing_store)
            if args.qfbv_online_cubing_store is not None
            else os.environ.get("SYMCC_QFBV_ONLINE_CUBING_STORE", "")
        )
        if not online_store_raw:
            online_store_raw = str(Path(args.store) / "qfbv-online-cubing.sqlite3")
        try:
            online_cubing_store = OnlineCubingPolicyStore(
                online_store_raw, online_policy
            )
        except OnlineCubingError as error:
            raise ValueError(f"invalid online-cubing configuration: {error}") from error
    if partition_execution_enabled:
        if len(portfolio_specs) != 1 or portfolio_specs[0].get("kind") != (
            "bitblast-cadical-qfbv"
        ):
            raise ValueError(
                "proof-aware partition execution requires exactly one CaDiCaL "
                "QF_BV portfolio backend"
            )
        if (
            incremental_proof_store is None
            or incremental_proof_checker is None
            or partition_store is None
            or partition_execution_store is None
        ):
            raise ValueError(
                "proof-aware partition execution requires proof, partition, and "
                "execution stores"
            )
    lidrup_checker_raw = (
        str(args.qfbv_lidrup_checker)
        if args.qfbv_lidrup_checker is not None
        else os.environ.get("SYMCC_QFBV_LIDRUP_CHECKER", "")
    )
    lidrup_checker_sha256 = str(
        args.qfbv_lidrup_checker_sha256 or ""
    ) or os.environ.get("SYMCC_QFBV_LIDRUP_CHECKER_SHA256", "")
    lidrup_wire_store_raw = (
        str(args.qfbv_lidrup_wire_store)
        if args.qfbv_lidrup_wire_store is not None
        else os.environ.get("SYMCC_QFBV_LIDRUP_WIRE_STORE", "")
    )
    if bool(lidrup_checker_raw) != bool(lidrup_checker_sha256):
        raise ValueError(
            "LIDRUP checker path and SHA-256 identity must be configured together"
        )
    if lidrup_wire_store_raw and not lidrup_checker_raw:
        raise ValueError("LIDRUP wire store requires a pinned checker")
    if lidrup_checker_raw and incremental_proof_store is None:
        raise ValueError("LIDRUP wire checking requires the incremental proof store")
    if lidrup_checker_raw and not lidrup_wire_store_raw:
        lidrup_wire_store_raw = str(Path(args.store) / "qfbv-lidrup-wire")
    lidrup_wire_max_records = (
        args.qfbv_lidrup_wire_max_records
        if args.qfbv_lidrup_wire_max_records is not None
        else _environment_integer("SYMCC_QFBV_LIDRUP_WIRE_MAX_RECORDS", 100_000)
    )
    lidrup_wire_max_bytes = (
        args.qfbv_lidrup_wire_max_bytes
        if args.qfbv_lidrup_wire_max_bytes is not None
        else _environment_integer(
            "SYMCC_QFBV_LIDRUP_WIRE_MAX_BYTES", 4 * 1024 * 1024 * 1024
        )
    )
    lidrup_timeout_ms = (
        args.qfbv_lidrup_timeout_ms
        if args.qfbv_lidrup_timeout_ms is not None
        else _environment_integer("SYMCC_QFBV_LIDRUP_TIMEOUT_MS", 30_000)
    )
    lidrup_checker = (
        LidrupExternalChecker(
            lidrup_checker_raw,
            checker_sha256=lidrup_checker_sha256,
            timeout_ms=lidrup_timeout_ms,
        )
        if lidrup_checker_raw
        else None
    )
    lidrup_wire_store = (
        LidrupWireStore(
            lidrup_wire_store_raw,
            max_records=lidrup_wire_max_records,
            max_bytes=lidrup_wire_max_bytes,
        )
        if lidrup_wire_store_raw
        else None
    )
    artifact_gc_result: ArtifactCollection | None = None
    artifact_gc_inventory: dict[str, dict[str, int | bool]] = {}

    def artifact_gc_payload(result: ArtifactCollection) -> dict[str, object]:
        return {
            "deleted": [
                {"kind": item.kind, "digest": item.digest} for item in result.deleted
            ],
            "deleted_objects": len(result.deleted),
            "deleted_bytes": result.deleted_bytes,
            "examined": result.examined,
            "protected": result.protected,
            "expired_jobs": result.expired_jobs,
            "stop_reason": result.stop_reason,
        }

    def delete_artifact(kind: str, digest: str, size: int) -> int:
        if kind == "context" and shared_context_store is not None:
            return shared_context_store.delete_lifecycle_artifact(kind, digest, size)
        if kind in {"proof", "receipt"} and proof_store is not None:
            return proof_store.delete_lifecycle_artifact(kind, digest, size)
        if kind == "lemma" and lemma_store is not None:
            return lemma_store.delete_lifecycle_artifact(kind, digest, size)
        if kind == "core" and substitution_core_store is not None:
            return substitution_core_store.delete_lifecycle_artifact(kind, digest, size)
        if kind == "sat-proof" and incremental_proof_store is not None:
            return incremental_proof_store.delete_lifecycle_artifact(kind, digest, size)
        if kind == "partition" and partition_store is not None:
            return partition_store.delete_lifecycle_artifact(kind, digest, size)
        if kind == "partition-execution" and partition_execution_store is not None:
            return partition_execution_store.delete_lifecycle_artifact(
                kind, digest, size
            )
        raise RuntimeError(f"artifact GC store for {kind!r} is unavailable")

    if args.qfbv_artifact_gc_on_start or args.qfbv_artifact_gc_only:
        assert artifact_lifecycle is not None
        context_count = (
            shared_context_store.stats()["contexts"]
            if shared_context_store is not None
            else 0
        )
        proof_stats = proof_store.stats() if proof_store is not None else {}
        proof_count = int(proof_stats.get("proofs", 0)) + int(
            proof_stats.get("receipts", 0)
        )
        lemma_count = lemma_store.stats()["records"] if lemma_store is not None else 0
        core_count = (
            substitution_core_store.stats()["records"]
            if substitution_core_store is not None
            else 0
        )
        incremental_count = (
            incremental_proof_store.stats()["records"]
            if incremental_proof_store is not None
            else 0
        )
        partition_count = (
            partition_store.stats(max_entries=artifact_gc_scan_max_objects)[
                "partitions"
            ]
            if partition_store is not None
            else 0
        )
        partition_execution_count = (
            partition_execution_store.stats()["terminal_executions"]
            if partition_execution_store is not None
            else 0
        )
        indexed_total = (
            context_count
            + proof_count
            + lemma_count
            + core_count
            + incremental_count
            + int(partition_count)
            + int(partition_execution_count)
        )
        if indexed_total > artifact_gc_scan_max_objects:
            raise RuntimeError(
                "artifact GC index synchronization budget is insufficient: "
                f"{indexed_total} > {artifact_gc_scan_max_objects}"
            )
        if shared_context_store is not None:
            artifact_gc_inventory["context"] = (
                shared_context_store.synchronize_lifecycle(
                    max_entries=artifact_gc_scan_max_objects
                )
            )
        if proof_store is not None:
            artifact_gc_inventory["proof"] = proof_store.synchronize_lifecycle(
                max_entries=artifact_gc_scan_max_objects
            )
        if lemma_store is not None:
            artifact_gc_inventory["lemma"] = lemma_store.synchronize_lifecycle(
                max_entries=artifact_gc_scan_max_objects
            )
        if substitution_core_store is not None:
            artifact_gc_inventory["core"] = (
                substitution_core_store.synchronize_lifecycle(
                    max_entries=artifact_gc_scan_max_objects
                )
            )
        if incremental_proof_store is not None:
            artifact_gc_inventory["sat-proof"] = (
                incremental_proof_store.synchronize_lifecycle(
                    max_entries=artifact_gc_scan_max_objects
                )
            )
        if partition_store is not None:
            artifact_gc_inventory["partition"] = partition_store.synchronize_lifecycle(
                max_entries=artifact_gc_scan_max_objects
            )
        if partition_execution_store is not None:
            artifact_gc_inventory["partition-execution"] = (
                partition_execution_store.synchronize_lifecycle(
                    max_entries=artifact_gc_scan_max_objects
                )
            )
        if not all(
            bool(result["complete"]) for result in artifact_gc_inventory.values()
        ):
            raise RuntimeError("artifact GC index synchronization is incomplete")
        artifact_gc_result = artifact_lifecycle.collect(
            delete_artifact,
            grace_seconds=artifact_gc_grace_seconds,
            max_objects=artifact_gc_max_objects,
            max_bytes=artifact_gc_max_bytes,
            time_budget_ms=artifact_gc_time_ms,
        )
    if args.qfbv_artifact_gc_only:
        assert artifact_lifecycle is not None
        assert artifact_gc_result is not None
        print(
            json.dumps(
                {
                    "qfbv_artifact_gc": artifact_gc_payload(artifact_gc_result),
                    "qfbv_artifact_gc_inventory": artifact_gc_inventory,
                    "qfbv_artifact_lifecycle": artifact_lifecycle.stats(),
                },
                sort_keys=True,
            )
        )
        return 0

    def validate_schedule_constraints() -> int:
        if not args.schedule_constraints:
            return 0
        return store.validate_schedule_constraints_file(
            args.schedule_constraints,
            output_path=args.schedule_validation_out,
            max_artifacts=max(args.schedule_validation_max_artifacts, 0),
        )

    if args.schedule_validation_only:
        written = validate_schedule_constraints()
        print(
            json.dumps(
                {
                    "schedule_validations": written,
                    "store": store.stats(),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.artifact_audit_only:
        audit = store.audit_artifacts(
            max_entries=args.artifact_audit_max_entries,
        )
        print(json.dumps(audit, sort_keys=True))
        return 0 if bool(audit["scan"]["complete"]) else 2
    if args.stats:
        validate_schedule_constraints()
        stats: dict[str, object] = store.stats()
        if shared_context_store is not None:
            stats = {
                "store": stats,
                "qfbv_context_store": shared_context_store.stats(),
            }
        if proof_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_proof_store"] = proof_store.stats()
        if lemma_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_lemma_store"] = lemma_store.stats()
        if substitution_core_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_substitution_core_store"] = substitution_core_store.stats()
        if incremental_proof_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_incremental_proof_store"] = incremental_proof_store.stats()
        if incremental_proof_checker is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_proof_replay_cache"] = (
                incremental_proof_checker.replay_cache_stats()
            )
        if partition_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_partition_store"] = partition_store.stats()
        if partition_execution_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_partition_execution_store"] = partition_execution_store.stats()
        if online_cubing_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_online_cubing_store"] = online_cubing_store.snapshot()
        if lidrup_wire_store is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_lidrup_wire_store"] = lidrup_wire_store.stats()
        if artifact_lifecycle is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_artifact_lifecycle"] = artifact_lifecycle.stats()
        if artifact_gc_result is not None:
            if "store" not in stats:
                stats = {"store": stats}
            stats["qfbv_artifact_gc"] = artifact_gc_payload(artifact_gc_result)
            stats["qfbv_artifact_gc_inventory"] = artifact_gc_inventory
        print(json.dumps(stats, sort_keys=True))
        return 0
    solver_command = (
        []
        if portfolio_specs
        else (shlex.split(args.solver) if args.solver else _default_solver())
    )
    jobs = max(1, min(256, args.jobs))
    if partition_execution_enabled:
        if jobs > partition_parallelism:
            raise ValueError(
                "--jobs must not exceed --qfbv-partition-parallelism when "
                "certified cube execution is enabled"
            )
        if not 0.1 <= args.lease_seconds <= 86_400.0:
            raise ValueError(
                "--lease-seconds must be in [0.1, 86400] for partition execution"
            )
    if not 1 <= args.malleable_backlog_per_slot <= 1_000_000:
        raise ValueError("--malleable-backlog-per-slot must be in [1, 1000000]")
    if not 0 <= args.malleable_hysteresis_slots <= jobs:
        raise ValueError("--malleable-hysteresis-slots must be within --jobs")
    malleable_pool = (
        _MalleableQueryWorkerPool(
            store,
            pool_id=args.malleable_pool_id,
            slots=jobs,
            backlog_per_slot=args.malleable_backlog_per_slot,
            hysteresis_slots=args.malleable_hysteresis_slots,
        )
        if args.malleable_workers
        else None
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    def worker(slot: int, one_pass: bool) -> dict[str, int]:
        local_store = QueryStore(args.store)
        if lidrup_checker is not None and lidrup_wire_store is not None:
            local_store.register_qfbv_proof_wire(lidrup_checker, lidrup_wire_store)
        owner = f"{socket.gethostname()}:{os.getpid()}:{slot}"
        counts = {"sat": 0, "unsat": 0, "unknown": 0, "error": 0, "stale": 0}
        stack = ExitStack()

        def persistent_backend(
            command: list[str],
            backend_index: int,
        ) -> PersistentSubprocessSolver:
            environment: dict[str, str] = {}
            if "SYMCC_SELECTIVE_QUERY_POLICY_STATE" not in os.environ:
                policy_dir = Path(args.store) / "policies"
                policy_dir.mkdir(parents=True, exist_ok=True)
                environment["SYMCC_SELECTIVE_QUERY_POLICY_STATE"] = str(
                    policy_dir
                    / f"selective-worker-{slot}-backend-{backend_index}.state"
                )
            return PersistentSubprocessSolver(command, environment=environment)

        if portfolio_specs:
            portfolio_backends = []
            for backend_index, spec in enumerate(portfolio_specs):
                command = spec["command"]
                assert isinstance(command, list)
                kind = str(spec.get("kind", "symcc-json"))
                if kind == "smtlib-qfbv":
                    capabilities = spec.get("capabilities")
                    assert isinstance(capabilities, dict)
                    proof_verifier = None
                    proof_spec = spec.get("unsat_proof")
                    if isinstance(proof_spec, dict):
                        if proof_store is None:
                            raise RuntimeError(
                                "QF_BV proof verifier requires a proof store"
                            )
                        proof_verifier = QfbvProofVerifier(
                            proof_store,
                            generator_command=proof_spec["generator_command"],
                            checker_command=proof_spec["checker_command"],
                            signature_root=str(proof_spec["signature_root"]),
                            generator_trusted_files=proof_spec[
                                "generator_trusted_files"
                            ],
                            checker_trusted_files=proof_spec["checker_trusted_files"],
                            timeout_ms=int(proof_spec["timeout_ms"]),
                        )
                        local_store.register_qfbv_proof_verifier(proof_verifier)
                    lemma_exchange = None
                    lemma_spec = spec.get("learned_lemmas")
                    if isinstance(lemma_spec, dict):
                        if (
                            lemma_store is None
                            or shared_context_store is None
                            or proof_verifier is None
                        ):
                            raise RuntimeError(
                                "learned lemma exchange stores are unavailable"
                            )
                        lemma_exchange = QfbvLemmaExchange(
                            lemma_store,
                            shared_context_store,
                            proof_verifier,
                            timeout_ms=int(lemma_spec["timeout_ms"]),
                        )
                        local_store.register_qfbv_lemma_exchange(lemma_exchange)
                    substitution_core_exchange = None
                    substitution_core_spec = spec.get("substitution_cores")
                    if isinstance(substitution_core_spec, dict):
                        if substitution_core_store is None or proof_verifier is None:
                            raise RuntimeError(
                                "substitution-core exchange stores are unavailable"
                            )
                        substitution_core_exchange = QfbvSubstitutionCoreExchange(
                            substitution_core_store,
                            proof_verifier,
                            substitution_core_spec["extractor_command"],
                            max_candidates=int(
                                substitution_core_spec["max_candidates"]
                            ),
                            max_candidate_scan=int(
                                substitution_core_spec["max_candidate_scan"]
                            ),
                            max_join_states=int(
                                substitution_core_spec["max_join_states"]
                            ),
                            max_unification_pairs=int(
                                substitution_core_spec["max_unification_pairs"]
                            ),
                            verified_core_cache_entries=int(
                                substitution_core_spec["verified_core_cache_entries"]
                            ),
                            lookup_timeout_ms=int(
                                substitution_core_spec["lookup_timeout_ms"]
                            ),
                            publish_timeout_ms=int(
                                substitution_core_spec["publish_timeout_ms"]
                            ),
                        )
                        local_store.register_qfbv_substitution_core_exchange(
                            substitution_core_exchange
                        )
                    if bool(spec.get("persistent", False)):
                        backend = stack.enter_context(
                            PersistentSmtLibQfbvSolver(
                                local_store,
                                command,
                                name=str(spec["name"]),
                                capabilities=capabilities,
                                prefix_cache_entries=int(spec.get("prefix_cache", 4)),
                                shared_context_store=shared_context_store,
                                context_owner=(
                                    f"{owner}:backend-{backend_index}:{spec['name']}"
                                ),
                                materialization_lease_seconds=(context_lease_seconds),
                                materialization_max_active=(context_max_active),
                                proof_verifier=proof_verifier,
                                substitution_core_exchange=(substitution_core_exchange),
                                lemma_exchange=lemma_exchange,
                                learned_literal_type=str(
                                    lemma_spec.get("type", "preprocess")
                                )
                                if isinstance(lemma_spec, dict)
                                else "preprocess",
                                max_learned_lemmas=int(
                                    lemma_spec.get("max_per_query", 8)
                                )
                                if isinstance(lemma_spec, dict)
                                else 8,
                                lemma_timeout_ms=int(
                                    lemma_spec.get("timeout_ms", 30_000)
                                )
                                if isinstance(lemma_spec, dict)
                                else 30_000,
                                native_state_fork=bool(
                                    spec.get("native_state_fork", False)
                                ),
                            )
                        )
                    else:
                        backend = SmtLibQfbvSolver(
                            local_store,
                            command,
                            name=str(spec["name"]),
                            capabilities=capabilities,
                            proof_verifier=proof_verifier,
                            substitution_core_exchange=(substitution_core_exchange),
                        )
                elif kind == "bitblast-cadical-qfbv":
                    capabilities = spec.get("capabilities")
                    assert isinstance(capabilities, dict)
                    if (
                        incremental_proof_store is None
                        or incremental_proof_checker is None
                    ):
                        raise RuntimeError(
                            "CaDiCaL QF_BV incremental proof store is unavailable"
                        )
                    local_store.register_qfbv_incremental_proof_checker(
                        incremental_proof_checker
                    )
                    common = {
                        "name": str(spec["name"]),
                        "proof_store": incremental_proof_store,
                        "proof_checker": incremental_proof_checker,
                        "capabilities": capabilities,
                        "source_worker": (
                            f"{owner}:backend-{backend_index}:{spec['name']}"
                        ),
                        "worker_epoch": int(spec.get("worker_epoch", 0)) + slot,
                        "max_imported_clauses": int(
                            spec.get("max_imported_clauses", 64)
                        ),
                        "proof_wire_checker": lidrup_checker,
                        "proof_wire_store": lidrup_wire_store,
                    }
                    realtime_kwargs = {}
                    if bool(spec.get("persistent", False)):
                        realtime_spec = spec.get("realtime_stream")
                        if isinstance(realtime_spec, dict):
                            realtime_kwargs = {
                                "realtime_library_path": str(realtime_spec["library"]),
                                "realtime_max_imports": int(
                                    realtime_spec["max_imports"]
                                ),
                                "realtime_max_events": int(realtime_spec["max_events"]),
                                "realtime_max_learned": int(
                                    realtime_spec["max_learned"]
                                ),
                                "realtime_max_learned_length": int(
                                    realtime_spec["max_learned_length"]
                                ),
                                "realtime_poll_interval_ms": int(
                                    realtime_spec["poll_interval_ms"]
                                ),
                                "realtime_checker_budget_ms": int(
                                    realtime_spec["checker_budget_ms"]
                                ),
                                "realtime_native_queue_literals": int(
                                    realtime_spec["native_queue_literals"]
                                ),
                                "realtime_require_clause_compression": bool(
                                    realtime_spec.get(
                                        "require_clause_compression", False
                                    )
                                ),
                                "realtime_adaptive_policy": (
                                    realtime_spec.get("adaptive")
                                ),
                                "realtime_track_clause_activity": bool(
                                    realtime_spec.get("track_clause_activity", False)
                                ),
                                "realtime_pairing_policy": (
                                    realtime_spec.get("pairing")
                                ),
                            }

                    def build_cadical_cube_backend(instance: int) -> Any:
                        instance_common = {
                            **common,
                            "source_worker": (
                                f"{owner}:backend-{backend_index}:"
                                f"{spec['name']}:cube-{instance}"
                            ),
                        }
                        if bool(spec.get("persistent", False)):
                            return PersistentCadicalQfbvSolver(
                                local_store,
                                str(spec["native_library"]),
                                command,
                                context_cache_entries=int(spec.get("context_cache", 8)),
                                **realtime_kwargs,
                                **instance_common,
                            )
                        return CadicalQfbvSolver(
                            local_store,
                            command,
                            **instance_common,
                        )

                    if partition_execution_enabled:
                        assert partition_store is not None
                        assert partition_execution_store is not None
                        per_query_parallelism = max(1, partition_parallelism // jobs)
                        worker_partition_policy = PartitionExecutionPolicy(
                            parallelism=per_query_parallelism,
                            max_attempts=partition_execution_policy.max_attempts,
                            cube_timeout_ms=(
                                partition_execution_policy.cube_timeout_ms
                            ),
                            task_lease_ms=partition_execution_policy.task_lease_ms,
                        )
                        backend = stack.enter_context(PartitioningQfbvBackend(
                            local_store,
                            partition_store,
                            partition_execution_store,
                            incremental_proof_store,
                            incremental_proof_checker,
                            build_cadical_cube_backend,
                            capabilities=capabilities,
                            cube_count=partition_cube_count,
                            policy=worker_partition_policy,
                            source_worker=owner,
                            parent_lease_seconds=max(args.lease_seconds, 0.1),
                            online_policy_store=online_cubing_store,
                            online_activity_available=bool(
                                spec.get("persistent", False)
                                and isinstance(spec.get("realtime_stream"), dict)
                                and spec["realtime_stream"].get(
                                    "track_clause_activity", False
                                )
                            ),
                            prerun_backend_factory=(
                                build_cadical_cube_backend
                                if online_cubing_store is not None
                                else None
                            ),
                        ))
                    elif bool(spec.get("persistent", False)):
                        backend = stack.enter_context(build_cadical_cube_backend(0))
                    else:
                        backend = build_cadical_cube_backend(0)
                elif bool(spec.get("persistent", True)):
                    backend = stack.enter_context(
                        persistent_backend(command, backend_index)
                    )
                else:
                    backend = SubprocessSolver(command)
                portfolio_backends.append((str(spec["name"]), backend))
            backend = PortfolioSolver(
                portfolio_backends,
                parallelism=args.portfolio_parallelism or None,
                cancel_grace_ms=args.portfolio_cancel_grace_ms,
            )
        else:
            backend = stack.enter_context(persistent_backend(solver_command, 0))
        with stack:
            while not stop.is_set():
                if malleable_pool is None:
                    status = solve_one(
                        local_store,
                        owner,
                        backend,
                        lease_seconds=max(args.lease_seconds, 0.1),
                        max_attempts=max(args.max_attempts, 1),
                        traversal=args.traversal,
                        shard_index=slot,
                        shard_count=jobs,
                    )
                else:
                    claim = malleable_pool.claim(
                        slot,
                        local_store,
                        owner,
                        lease_seconds=max(args.lease_seconds, 0.1),
                        traversal=args.traversal,
                    )
                    if claim is None:
                        status = None
                    else:
                        permission, lease, lease_id = claim
                        durable_proofs: list[str] = []
                        try:
                            status, result = solve_claimed(
                                local_store,
                                owner,
                                backend,
                                lease,
                                lease_seconds=max(args.lease_seconds, 0.1),
                                max_attempts=max(args.max_attempts, 1),
                            )
                            if result is not None:
                                durable_proofs = _durable_proof_artifacts(result)
                        except QueryLeaseHeartbeatError:
                            status = "error"
                        finally:
                            malleable_pool.finish(
                                slot,
                                permission,
                                lease_id,
                                durable_proofs=durable_proofs,
                            )
                if status is None:
                    if one_pass:
                        break
                    stop.wait(max(args.poll, 0.01))
                    continue
                counts[status] = counts.get(status, 0) + 1
        return counts

    if args.spool:
        ingest_spool(store, args.spool)
    validate_schedule_constraints()
    if malleable_pool is not None:
        malleable_pool.rebalance()
    artifact_heartbeat = (
        ArtifactLeaseHeartbeat(
            artifact_lifecycle,
            artifact_job_lease,
            lease_seconds=artifact_job_lease_seconds,
        )
        if artifact_lifecycle is not None and artifact_job_lease is not None
        else None
    )
    if args.once:
        try:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                results = list(
                    executor.map(lambda slot: worker(slot, True), range(jobs))
                )
        finally:
            if artifact_heartbeat is not None:
                artifact_heartbeat.close()
            if malleable_pool is not None:
                malleable_pool.close()
        if args.spool:
            ingest_spool(store, args.spool)
        store.drain_result_publications(
            limit=args.publication_reconcile_limit,
            time_budget_seconds=args.publication_reconcile_time_ms / 1000.0,
            max_attempts=args.publication_max_attempts,
            retry_base_seconds=args.publication_retry_base_seconds,
            retry_max_seconds=args.publication_retry_max_seconds,
        )
        validate_schedule_constraints()
        aggregate: dict[str, int] = {}
        for result in results:
            for key, value in result.items():
                aggregate[key] = aggregate.get(key, 0) + value
        summary: dict[str, object] = {
            "worker": aggregate,
            "store": store.stats(),
        }
        if malleable_pool is not None:
            summary["malleable_worker_pool"] = malleable_pool.snapshot()
        if shared_context_store is not None:
            summary["qfbv_context_store"] = shared_context_store.stats()
        if proof_store is not None:
            summary["qfbv_proof_store"] = proof_store.stats()
        if lemma_store is not None:
            summary["qfbv_lemma_store"] = lemma_store.stats()
        if substitution_core_store is not None:
            summary["qfbv_substitution_core_store"] = substitution_core_store.stats()
        if incremental_proof_store is not None:
            summary["qfbv_incremental_proof_store"] = incremental_proof_store.stats()
        if incremental_proof_checker is not None:
            summary["qfbv_proof_replay_cache"] = (
                incremental_proof_checker.replay_cache_stats()
            )
        if partition_store is not None:
            summary["qfbv_partition_store"] = partition_store.stats()
        if partition_execution_store is not None:
            summary["qfbv_partition_execution_store"] = (
                partition_execution_store.stats()
            )
        if online_cubing_store is not None:
            summary["qfbv_online_cubing_store"] = online_cubing_store.snapshot()
        if lidrup_wire_store is not None:
            summary["qfbv_lidrup_wire_store"] = lidrup_wire_store.stats()
        if artifact_lifecycle is not None:
            summary["qfbv_artifact_lifecycle"] = artifact_lifecycle.stats()
        if artifact_gc_result is not None:
            summary["qfbv_artifact_gc"] = artifact_gc_payload(artifact_gc_result)
            summary["qfbv_artifact_gc_inventory"] = artifact_gc_inventory
        print(json.dumps(summary, sort_keys=True))
        return 0

    try:
        last_publication_reconcile = time.monotonic()
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [executor.submit(worker, slot, False) for slot in range(jobs)]
            try:
                while not stop.wait(max(args.poll, 0.01)):
                    if artifact_heartbeat is not None:
                        artifact_heartbeat.check()
                    if args.spool:
                        ingest_spool(store, args.spool)
                    now = time.monotonic()
                    if (
                        args.publication_reconcile_interval > 0.0
                        and now - last_publication_reconcile
                        >= args.publication_reconcile_interval
                    ):
                        reconcile_publications(respect_retry_after=True)
                        last_publication_reconcile = now
                    validate_schedule_constraints()
                    if malleable_pool is not None:
                        malleable_pool.rebalance()
            finally:
                stop.set()
                for future in futures:
                    future.result()
    finally:
        if artifact_heartbeat is not None:
            artifact_heartbeat.close()
        if malleable_pool is not None:
            malleable_pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
