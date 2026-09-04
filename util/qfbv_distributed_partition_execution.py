#!/usr/bin/env python3
"""Generation-fenced cross-node execution for certified QF_BV cubes.

F451 composes the F449 cube-task ledger with the F441/F447 durable ULFM
controller.  A worker result is current only when both the communicator work
fence and the exact cube lease token are current.  Communicator repair moves
every ambiguous in-flight cube to a new endpoint/generation without consuming
another semantic solve attempt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
import fcntl
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mpi_ulfm_recovery import (
    DurableUlfmCoordinator,
    UlfmRecoveryError,
    build_work_fence,
    verify_work_fence,
)
from qfbv_incremental_proof import IncrementalProofChecker, IncrementalProofStore
from qfbv_incremental_sat import BitBlastPlan
from qfbv_partition_execution import (
    CubeExecutionLease,
    PartitionExecutionPolicy,
    PartitionExecutionStore,
)


DISTRIBUTED_PARTITION_PROTOCOL = (
    "symcc-qfbv-generation-fenced-partition-execution-v1"
)
DISTRIBUTED_PARTITION_LEASE_SCHEMA = (
    "symcc-qfbv-generation-fenced-cube-lease-v1"
)
DISTRIBUTED_PARTITION_BINDING_SCHEMA = (
    "symcc-qfbv-distributed-cube-binding-store-v1"
)
MAX_BINDING_BYTES = 64 * 1024
_HEX = frozenset("0123456789abcdef")


class DistributedPartitionError(ValueError):
    """A distributed cube binding, transition, or recovery is invalid."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise DistributedPartitionError(
            "distributed cube value is not canonical JSON"
        ) from error


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex_digest(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise DistributedPartitionError(f"{name} must be a lowercase SHA-256")
    return value


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise DistributedPartitionError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise DistributedPartitionError(f"{name} is invalid") from error
    if (
        not encoded
        or len(encoded) > 256
        or any(character < 32 or character == 127 for character in encoded)
    ):
        raise DistributedPartitionError(f"{name} is invalid")
    return value


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise DistributedPartitionError(f"{name} must be in [{lower}, {upper}]")
    return value


def _timestamp(value: float | None, name: str) -> float:
    try:
        parsed = time.time() if value is None else float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise DistributedPartitionError(f"{name} must be finite") from error
    if not math.isfinite(parsed):
        raise DistributedPartitionError(f"{name} must be finite")
    return parsed


def distributed_run_id(execution_sha256: str) -> str:
    return f"qfbv-cubes:{_hex_digest(execution_sha256, 'execution')}"


def cube_work_id(execution_sha256: str, ordinal: int) -> str:
    return (
        f"cube:{_hex_digest(execution_sha256, 'execution')}:"
        f"{_integer(ordinal, 'cube ordinal', 0, 4095)}"
    )


def _parse_cube_work_id(value: Any) -> tuple[str, int]:
    work_id = _identity(value, "distributed cube work")
    fields = work_id.split(":")
    if len(fields) != 3 or fields[0] != "cube":
        raise DistributedPartitionError("distributed cube work identity changed")
    execution = _hex_digest(fields[1], "distributed cube execution")
    try:
        ordinal = int(fields[2])
    except (TypeError, ValueError, OverflowError) as error:
        raise DistributedPartitionError(
            "distributed cube work ordinal changed"
        ) from error
    _integer(ordinal, "distributed cube work ordinal", 0, 4095)
    if cube_work_id(execution, ordinal) != work_id:
        raise DistributedPartitionError("distributed cube work is not canonical")
    return execution, ordinal


def _cube_owner(fence: Mapping[str, Any]) -> str:
    owner_scope = {
        "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
        "run_id": fence["run_id"],
        "generation": fence["generation"],
        "generation_token": fence["generation_token"],
        "endpoint_id": fence["endpoint_id"],
        "shard_id": fence["shard_id"],
        "shard_token": fence["shard_token"],
    }
    return f"dist-cube:{int(fence['generation'])}:{_digest(_canonical_json(owner_scope))}"


def _cube_lease_dict(lease: CubeExecutionLease) -> dict[str, Any]:
    return {
        "execution_sha256": lease.execution_sha256,
        "partition_sha256": lease.partition_sha256,
        "query_id": lease.query_id,
        "ordinal": lease.ordinal,
        "cube_sha256": lease.cube_sha256,
        "literals": list(lease.literals),
        "assumptions": list(lease.assumptions),
        "token": lease.token,
        "owner": lease.owner,
        "timeout_ms": lease.timeout_ms,
    }


def build_distributed_cube_lease(
    cube_lease: CubeExecutionLease,
    ulfm_lease: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one exact F449 lease to one exact durable ULFM work lease."""
    if not isinstance(cube_lease, CubeExecutionLease):
        raise DistributedPartitionError("invalid local cube lease")
    try:
        fence = build_work_fence(ulfm_lease)
    except UlfmRecoveryError as error:
        raise DistributedPartitionError("invalid ULFM cube lease") from error
    if fence["run_id"] != distributed_run_id(cube_lease.execution_sha256):
        raise DistributedPartitionError("ULFM run is not bound to the execution")
    if fence["work_id"] != cube_work_id(
        cube_lease.execution_sha256, cube_lease.ordinal
    ):
        raise DistributedPartitionError("ULFM work is not bound to the cube")
    if cube_lease.owner != _cube_owner(fence):
        raise DistributedPartitionError("cube owner is not bound to the ULFM fence")
    body = {
        "schema": DISTRIBUTED_PARTITION_LEASE_SCHEMA,
        "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
        "cube": _cube_lease_dict(cube_lease),
        "ulfm_fence": fence,
    }
    body["lease_sha256"] = _digest(_canonical_json(body))
    return body


def verify_distributed_cube_lease(
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], CubeExecutionLease, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise DistributedPartitionError("distributed cube lease must be an object")
    body = dict(raw)
    supplied = _hex_digest(
        body.pop("lease_sha256", ""), "distributed cube lease"
    )
    if set(body) != {"schema", "protocol", "cube", "ulfm_fence"}:
        raise DistributedPartitionError("distributed cube lease shape changed")
    if (
        body["schema"] != DISTRIBUTED_PARTITION_LEASE_SCHEMA
        or body["protocol"] != DISTRIBUTED_PARTITION_PROTOCOL
        or not isinstance(body["cube"], Mapping)
    ):
        raise DistributedPartitionError("distributed cube lease scope changed")
    cube = dict(body["cube"])
    fields = {
        "execution_sha256",
        "partition_sha256",
        "query_id",
        "ordinal",
        "cube_sha256",
        "literals",
        "assumptions",
        "token",
        "owner",
        "timeout_ms",
    }
    if set(cube) != fields:
        raise DistributedPartitionError("distributed cube fields changed")
    if not isinstance(cube["literals"], list) or not isinstance(
        cube["assumptions"], list
    ):
        raise DistributedPartitionError("distributed cube assumptions changed")
    if any(type(value) is not int or value == 0 for value in cube["literals"]):
        raise DistributedPartitionError("distributed cube literals are invalid")
    if any(type(value) is not int or value == 0 for value in cube["assumptions"]):
        raise DistributedPartitionError("distributed cube assumptions are invalid")
    lease = CubeExecutionLease(
        execution_sha256=_hex_digest(cube["execution_sha256"], "execution"),
        partition_sha256=_hex_digest(cube["partition_sha256"], "partition"),
        query_id=_identity(cube["query_id"], "cube query"),
        ordinal=_integer(cube["ordinal"], "cube ordinal", 0, 4095),
        cube_sha256=_hex_digest(cube["cube_sha256"], "cube"),
        literals=tuple(cube["literals"]),
        assumptions=tuple(cube["assumptions"]),
        token=_integer(cube["token"], "cube token", 1, (1 << 63) - 1),
        owner=_identity(cube["owner"], "cube owner"),
        timeout_ms=_integer(cube["timeout_ms"], "cube timeout", 1, 3_600_000),
    )
    try:
        fence = verify_work_fence(body["ulfm_fence"])
    except UlfmRecoveryError as error:
        raise DistributedPartitionError("distributed ULFM fence changed") from error
    normalized = build_distributed_cube_lease(
        lease,
        {
            **fence,
            "owner_endpoint": fence["endpoint_id"],
        },
    )
    if normalized["lease_sha256"] != supplied or normalized != dict(raw):
        raise DistributedPartitionError("distributed cube lease identity changed")
    return normalized, lease, fence


class DistributedCubeBindingStore:
    """Crash-replayable association between cube and communicator leases."""

    _STATES = frozenset(
        {"active", "completed", "retry", "exhausted", "cancelled", "recovered", "stale"}
    )

    def __init__(self, root: str | os.PathLike[str]) -> None:
        root_path = Path(root)
        if root_path.is_symlink():
            raise DistributedPartitionError("distributed binding root is a symlink")
        root_path.mkdir(parents=True, exist_ok=True)
        self.root = root_path.resolve()
        if not stat.S_ISDIR(os.stat(self.root, follow_symlinks=False).st_mode):
            raise OSError("distributed binding root is not a directory")
        self.database = self.root / "distributed-cube-bindings.sqlite3"
        self.initialize_lock = self.root / ".initialize.lock"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.database.is_symlink():
            raise DistributedPartitionError("distributed binding database is a symlink")
        db = sqlite3.connect(self.database, timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _initialize(self) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for binding initialization")
        descriptor = os.open(
            self.initialize_lock,
            os.O_RDWR
            | os.O_CREAT
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            current = os.stat(self.initialize_lock, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise DistributedPartitionError(
                    "distributed binding initialization lock changed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = os.stat(self.initialize_lock, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (locked.st_dev, locked.st_ino):
                raise DistributedPartitionError(
                    "distributed binding initialization lock was replaced"
                )
            self._initialize_locked()
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _initialize_locked(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS distributed_binding_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS distributed_cube_bindings(
                    lease_sha256 TEXT PRIMARY KEY,
                    execution_sha256 TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    cube_token INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    shard_id TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    ulfm_lease_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_sha256 TEXT,
                    superseded_by TEXT,
                    lease_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    UNIQUE(execution_sha256, ordinal, cube_token),
                    UNIQUE(run_id, generation, shard_id, ulfm_lease_token)
                );
                CREATE INDEX IF NOT EXISTS distributed_binding_work_idx
                ON distributed_cube_bindings(run_id, work_id, updated);
                CREATE INDEX IF NOT EXISTS distributed_binding_state_idx
                ON distributed_cube_bindings(run_id, state, updated);
                """
            )
            expected = {
                "schema": DISTRIBUTED_PARTITION_BINDING_SCHEMA,
                "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
                "root": str(self.root),
            }
            for key, value in expected.items():
                row = db.execute(
                    "SELECT value FROM distributed_binding_metadata WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT OR IGNORE INTO distributed_binding_metadata(key,value) "
                        "VALUES(?,?)",
                        (key, value),
                    )
                    row = db.execute(
                        "SELECT value FROM distributed_binding_metadata WHERE key=?",
                        (key,),
                    ).fetchone()
                if row is None or row["value"] != value:
                    raise DistributedPartitionError(
                        f"distributed binding metadata mismatch for {key}"
                    )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw = str(row["lease_json"])
            if len(raw.encode("ascii")) > MAX_BINDING_BYTES:
                raise DistributedPartitionError("stored distributed lease is oversized")
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DistributedPartitionError("stored distributed lease is invalid") from error
        normalized, _, _ = verify_distributed_cube_lease(value)
        if _canonical_json(normalized).decode("ascii") != raw:
            raise DistributedPartitionError("stored distributed lease is not canonical")
        return normalized

    def record(
        self,
        lease: Mapping[str, Any],
        *,
        supersedes: str | None = None,
        now: float | None = None,
    ) -> str:
        normalized, cube, fence = verify_distributed_cube_lease(lease)
        encoded = _canonical_json(normalized)
        if len(encoded) > MAX_BINDING_BYTES:
            raise DistributedPartitionError("distributed cube lease is oversized")
        timestamp = _timestamp(now, "distributed binding timestamp")
        previous = (
            None
            if supersedes is None
            else _hex_digest(supersedes, "superseded distributed lease")
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM distributed_cube_bindings WHERE lease_sha256=?",
                (normalized["lease_sha256"],),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO distributed_cube_bindings("
                    "lease_sha256,execution_sha256,ordinal,cube_token,run_id,"
                    "generation,endpoint_id,shard_id,work_id,ulfm_lease_token,"
                    "state,lease_json,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        normalized["lease_sha256"],
                        cube.execution_sha256,
                        cube.ordinal,
                        cube.token,
                        fence["run_id"],
                        fence["generation"],
                        fence["endpoint_id"],
                        fence["shard_id"],
                        fence["work_id"],
                        fence["lease_token"],
                        "active",
                        encoded.decode("ascii"),
                        timestamp,
                        timestamp,
                    ),
                )
            elif self._decode(existing) != normalized:
                raise DistributedPartitionError("distributed lease identity collision")
            if previous is not None:
                changed = db.execute(
                    "UPDATE distributed_cube_bindings SET state='recovered',"
                    "superseded_by=?,updated=? WHERE lease_sha256=? "
                    "AND state IN ('active','stale') AND superseded_by IS NULL",
                    (normalized["lease_sha256"], timestamp, previous),
                ).rowcount
                if changed != 1:
                    row = db.execute(
                        "SELECT state,superseded_by FROM distributed_cube_bindings "
                        "WHERE lease_sha256=?",
                        (previous,),
                    ).fetchone()
                    if row is None or (
                        row["state"], row["superseded_by"]
                    ) != ("recovered", normalized["lease_sha256"]):
                        raise DistributedPartitionError(
                            "distributed recovery binding lost its predecessor"
                        )
        return str(normalized["lease_sha256"])

    def transition(
        self,
        lease_sha256: str,
        target: str,
        *,
        result_sha256: str | None = None,
        now: float | None = None,
    ) -> bool:
        identity = _hex_digest(lease_sha256, "distributed lease")
        if target not in self._STATES - {"active"}:
            raise DistributedPartitionError("distributed binding target is invalid")
        result = (
            None
            if result_sha256 is None
            else _hex_digest(result_sha256, "distributed result")
        )
        timestamp = _timestamp(now, "distributed transition timestamp")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE distributed_cube_bindings SET state=?,result_sha256=?,"
                "updated=? WHERE lease_sha256=? AND state='active'",
                (target, result, timestamp, identity),
            ).rowcount
            if changed == 1:
                return True
            row = db.execute(
                "SELECT state,result_sha256 FROM distributed_cube_bindings "
                "WHERE lease_sha256=?",
                (identity,),
            ).fetchone()
            return row is not None and (
                row["state"], row["result_sha256"]
            ) == (target, result)

    def load(self, lease_sha256: str) -> dict[str, Any] | None:
        identity = _hex_digest(lease_sha256, "distributed lease")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM distributed_cube_bindings WHERE lease_sha256=?",
                (identity,),
            ).fetchone()
        if row is None:
            return None
        return {
            "lease": self._decode(row),
            "state": str(row["state"]),
            "result_sha256": row["result_sha256"],
            "superseded_by": row["superseded_by"],
        }

    def active(self, run_id: str) -> list[dict[str, Any]]:
        run = _identity(run_id, "distributed run")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM distributed_cube_bindings WHERE run_id=? "
                "AND state='active' ORDER BY generation,shard_id,ordinal",
                (run,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def latest_for_work(self, run_id: str, work_id: str) -> dict[str, Any] | None:
        run = _identity(run_id, "distributed run")
        work = _identity(work_id, "distributed work")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM distributed_cube_bindings WHERE run_id=? "
                "AND work_id=? ORDER BY generation DESC,updated DESC LIMIT 1",
                (run, work),
            ).fetchone()
        return None if row is None else self._decode(row)

    def stats(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state,COUNT(*) AS count FROM distributed_cube_bindings "
                "GROUP BY state"
            ).fetchall()
        states = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "schema": DISTRIBUTED_PARTITION_BINDING_SCHEMA,
            "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
            "bindings": sum(states.values()),
            "states": dict(sorted(states.items())),
        }


class DistributedPartitionCoordinator:
    """Coordinate distributed cube claims, results, cancellation and repair."""

    def __init__(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        policy: PartitionExecutionPolicy,
        partition_store: PartitionExecutionStore,
        binding_store: DistributedCubeBindingStore,
        durable_ulfm: DurableUlfmCoordinator,
        proof_store: IncrementalProofStore,
        checker: IncrementalProofChecker,
        *,
        candidate_validator: Callable[[bytes], bool] | None = None,
        input_hex: str = "",
    ) -> None:
        if checker.store is None or checker.store.root != proof_store.root:
            raise DistributedPartitionError("distributed proof stores do not match")
        self.plan = plan
        self.certificate = dict(certificate)
        self.policy = policy
        self.partition_store = partition_store
        self.binding_store = binding_store
        self.durable = durable_ulfm
        self.proof_store = proof_store
        self.checker = checker
        self.candidate_validator = candidate_validator
        self.input_hex = input_hex
        self.execution_sha256 = partition_store.create(
            plan, certificate, policy, checker=checker
        )
        if self.durable.controller.run_id != distributed_run_id(
            self.execution_sha256
        ):
            raise DistributedPartitionError(
                "ULFM controller run is not the partition execution"
            )
        self._lock = threading.RLock()

    @staticmethod
    def _owner_for_permission(permission: Mapping[str, Any]) -> str:
        fence_scope = {
            "run_id": permission["run_id"],
            "generation": permission["generation"],
            "generation_token": permission["generation_token"],
            "endpoint_id": permission["owner_endpoint"],
            "shard_id": permission["shard_id"],
            "shard_token": permission["shard_token"],
        }
        return _cube_owner(fence_scope)

    def _free_shard(self, endpoint_id: str) -> str | None:
        endpoint = _identity(endpoint_id, "distributed endpoint")
        snapshot = self.durable.controller.snapshot()
        if snapshot["pending_recovery"] is not None:
            return None
        if endpoint not in snapshot["members"]:
            raise DistributedPartitionError("endpoint is outside the membership")
        queued = {item["shard_id"] for item in snapshot["recovery_queue"]}
        for shard_id, shard in sorted(snapshot["shards"].items()):
            if (
                shard["owner_endpoint"] == endpoint
                and not shard["active_work_id"]
                and shard_id not in queued
            ):
                return str(shard_id)
        return None

    def claim(
        self, endpoint_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Claim one cube only after reserving a current endpoint-owned shard."""
        timestamp = _timestamp(now, "distributed claim timestamp")
        with self._lock:
            shard_id = self._free_shard(endpoint_id)
            if shard_id is None:
                return None
            permission = self.durable.controller.shard_permission(shard_id)
            owner = self._owner_for_permission(permission)
            cube = self.partition_store.claim(
                self.execution_sha256, owner, now=timestamp
            )
            if cube is None:
                return None
            ulfm_lease: dict[str, Any] | None = None
            try:
                ulfm_lease = self.durable.dispatch(
                    shard_id, cube_work_id(cube.execution_sha256, cube.ordinal)
                )
                lease = build_distributed_cube_lease(cube, ulfm_lease)
                self.binding_store.record(lease, now=timestamp)
                return lease
            except Exception:
                if ulfm_lease is not None:
                    try:
                        self.durable.cancel(
                            build_work_fence(ulfm_lease), "cube-binding-failed"
                        )
                    except Exception:
                        pass
                try:
                    self.partition_store.complete(
                        self.plan,
                        self.certificate,
                        cube,
                        {
                            "status": "error",
                            "assignments": {},
                            "reason": "distributed cube dispatch failed",
                        },
                        checker=self.checker,
                        input_hex=self.input_hex,
                        now=timestamp,
                    )
                except Exception:
                    pass
                raise

    def heartbeat(
        self, raw_lease: Mapping[str, Any], *, now: float | None = None
    ) -> bool:
        timestamp = _timestamp(now, "distributed heartbeat timestamp")
        normalized, cube, fence = verify_distributed_cube_lease(raw_lease)
        with self._lock:
            binding = self.binding_store.load(normalized["lease_sha256"])
            if binding is None or binding["state"] != "active":
                return False
            if self.durable.classify_result(fence) != "current":
                self.binding_store.transition(
                    normalized["lease_sha256"], "stale", now=timestamp
                )
                return False
            return self.partition_store.heartbeat(cube, now=timestamp)

    @staticmethod
    def _result_identity(
        lease_sha256: str, result: Mapping[str, Any]
    ) -> str:
        return _digest(
            _canonical_json(
                {
                    "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
                    "lease_sha256": lease_sha256,
                    "result": dict(result),
                }
            )
        )

    def _cancel_active_peers(self, winner_sha256: str) -> None:
        for peer in self.binding_store.active(self.durable.controller.run_id):
            if peer["lease_sha256"] == winner_sha256:
                continue
            _, _, fence = verify_distributed_cube_lease(peer)
            if self.durable.classify_result(fence) == "current":
                self.durable.cancel(fence, "partition-sat-winner")
            self.binding_store.transition(peer["lease_sha256"], "cancelled")

    def complete(
        self,
        raw_lease: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        """Accept a result only through both the ULFM and F449 fences."""
        timestamp = _timestamp(now, "distributed completion timestamp")
        normalized, cube, fence = verify_distributed_cube_lease(raw_lease)
        with self._lock:
            binding = self.binding_store.load(normalized["lease_sha256"])
            if binding is None or binding["state"] != "active":
                return False
            if self.durable.classify_result(fence) != "current":
                self.binding_store.transition(
                    normalized["lease_sha256"], "stale", now=timestamp
                )
                return False
            accepted = self.partition_store.complete(
                self.plan,
                self.certificate,
                cube,
                result,
                checker=self.checker,
                candidate_validator=self.candidate_validator,
                input_hex=self.input_hex,
                now=timestamp,
            )
            if not accepted:
                self.durable.cancel(fence, "stale-cube-result")
                self.binding_store.transition(
                    normalized["lease_sha256"], "stale", now=timestamp
                )
                return False
            result_identity = self._result_identity(
                normalized["lease_sha256"], result
            )
            status = result.get("status")
            if status in {"sat", "unsat"}:
                self.durable.finish(fence, result_identity)
                self.binding_store.transition(
                    normalized["lease_sha256"],
                    "completed",
                    result_sha256=result_identity,
                    now=timestamp,
                )
            else:
                self.durable.cancel(fence, "cube-result-retry")
                task = self.partition_store.task_snapshot(
                    cube.execution_sha256, cube.ordinal
                )
                target = "exhausted" if task["status"] == "exhausted" else "retry"
                self.binding_store.transition(
                    normalized["lease_sha256"],
                    target,
                    result_sha256=result_identity,
                    now=timestamp,
                )
            if status == "sat":
                self._cancel_active_peers(normalized["lease_sha256"])
            return True

    def _cube_from_task(self, task: Mapping[str, Any]) -> CubeExecutionLease:
        owner = task.get("lease_owner")
        if type(owner) is not str:
            raise DistributedPartitionError("recovered cube has no lease owner")
        return CubeExecutionLease(
            execution_sha256=str(task["execution_sha256"]),
            partition_sha256=str(task["partition_sha256"]),
            query_id=str(task["query_id"]),
            ordinal=int(task["ordinal"]),
            cube_sha256=str(task["cube_sha256"]),
            literals=tuple(task["literals"]),
            assumptions=tuple(task["assumptions"]),
            token=int(task["lease_token"]),
            owner=owner,
            timeout_ms=self.policy.cube_timeout_ms,
        )

    def _reconstruct_orphan_bindings(
        self, timestamp: float
    ) -> list[dict[str, Any]]:
        """Rebuild leases after a crash between ULFM dispatch and binding insert."""
        reconstructed: list[dict[str, Any]] = []
        snapshot = self.durable.controller.snapshot()
        for shard_id, shard in sorted(snapshot["shards"].items()):
            if not shard["active_work_id"]:
                continue
            fence = build_work_fence(
                {
                    "run_id": snapshot["run_id"],
                    "generation": snapshot["generation"],
                    "generation_token": snapshot["generation_token"],
                    "shard_id": shard_id,
                    "owner_endpoint": shard["owner_endpoint"],
                    "shard_token": shard["shard_token"],
                    "work_id": shard["active_work_id"],
                    "lease_token": shard["active_lease_token"],
                }
            )
            previous = self.binding_store.latest_for_work(
                snapshot["run_id"], fence["work_id"]
            )
            if previous is not None:
                loaded = self.binding_store.load(previous["lease_sha256"])
                if (
                    loaded is not None
                    and loaded["state"] == "active"
                    and previous["ulfm_fence"] == fence
                ):
                    continue
            execution, ordinal = _parse_cube_work_id(fence["work_id"])
            if execution != self.execution_sha256:
                raise DistributedPartitionError(
                    "active ULFM shard contains another execution"
                )
            task = self.partition_store.task_snapshot(execution, ordinal)
            if task["status"] != "leased":
                if previous is None:
                    raise DistributedPartitionError(
                        "orphan ULFM work has no reconstructable cube lease"
                    )
                continue
            cube = self._cube_from_task(task)
            expected_owner = _cube_owner(fence)
            if cube.owner != expected_owner:
                if previous is None:
                    raise DistributedPartitionError(
                        "orphan ULFM work and cube owner disagree"
                    )
                prior_cube = verify_distributed_cube_lease(previous)[1]
                if (
                    prior_cube.token != task["lease_token"]
                    or prior_cube.owner != task["lease_owner"]
                ):
                    raise DistributedPartitionError(
                        "orphan cube was independently reassigned"
                    )
                moved = self.partition_store.recover_lease(
                    prior_cube, expected_owner, now=timestamp
                )
                if moved is None:
                    raise DistributedPartitionError(
                        "orphan cube recovery lost its inner fence"
                    )
                cube = moved
            lease = build_distributed_cube_lease(
                cube,
                {**fence, "owner_endpoint": fence["endpoint_id"]},
            )
            supersedes = None
            if previous is not None:
                loaded = self.binding_store.load(previous["lease_sha256"])
                if loaded is not None and loaded["state"] in {"active", "stale"}:
                    supersedes = previous["lease_sha256"]
            self.binding_store.record(
                lease, supersedes=supersedes, now=timestamp
            )
            reconstructed.append(lease)
        return reconstructed

    def reconcile(self, *, now: float | None = None) -> dict[str, int]:
        """Close crash windows between the inner and outer durable commits."""
        timestamp = _timestamp(now, "distributed reconciliation timestamp")
        counts = {
            "kept": 0,
            "completed": 0,
            "cancelled": 0,
            "stale": 0,
            "reconstructed": 0,
        }
        with self._lock:
            counts["reconstructed"] = len(
                self._reconstruct_orphan_bindings(timestamp)
            )
            for raw in self.binding_store.active(self.durable.controller.run_id):
                normalized, cube, fence = verify_distributed_cube_lease(raw)
                classification = self.durable.classify_result(fence)
                if classification != "current":
                    task = self.partition_store.task_snapshot(
                        cube.execution_sha256, cube.ordinal
                    )
                    if task["status"] in {"sat", "unsat"}:
                        result_sha256 = _hex_digest(
                            task["result_sha256"], "reconciled cube result"
                        )
                        self.binding_store.transition(
                            normalized["lease_sha256"],
                            "completed",
                            result_sha256=result_sha256,
                            now=timestamp,
                        )
                        counts["completed"] += 1
                    else:
                        self.binding_store.transition(
                            normalized["lease_sha256"], "stale", now=timestamp
                        )
                        counts["stale"] += 1
                    continue
                task = self.partition_store.task_snapshot(
                    cube.execution_sha256, cube.ordinal
                )
                if (
                    task["status"] == "leased"
                    and task["lease_token"] == cube.token
                    and task["lease_owner"] == cube.owner
                ):
                    counts["kept"] += 1
                    continue
                if task["status"] in {"sat", "unsat"}:
                    result_sha256 = _hex_digest(
                        task["result_sha256"], "reconciled cube result"
                    )
                    self.durable.finish(fence, result_sha256)
                    self.binding_store.transition(
                        normalized["lease_sha256"],
                        "completed",
                        result_sha256=result_sha256,
                        now=timestamp,
                    )
                    counts["completed"] += 1
                    continue
                self.durable.cancel(fence, f"cube-state-{task['status']}")
                target = (
                    "exhausted" if task["status"] == "exhausted" else "cancelled"
                )
                self.binding_store.transition(
                    normalized["lease_sha256"], target, now=timestamp
                )
                counts["cancelled"] += 1
        return counts

    def _settle_requeued(
        self, queued: Mapping[str, Any], timestamp: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        execution, ordinal = _parse_cube_work_id(queued["work_id"])
        if execution != self.execution_sha256:
            raise DistributedPartitionError(
                "ULFM recovery queue contains another execution"
            )
        previous = self.binding_store.latest_for_work(
            self.durable.controller.run_id, queued["work_id"]
        )
        outer = self.durable.dispatch(queued["shard_id"], queued["work_id"])
        fence = build_work_fence(outer)
        task = self.partition_store.task_snapshot(execution, ordinal)
        if task["status"] == "leased":
            old_cube = (
                verify_distributed_cube_lease(previous)[1]
                if previous is not None
                else self._cube_from_task(task)
            )
            if (
                old_cube.token != task["lease_token"]
                or old_cube.owner != task["lease_owner"]
            ):
                self.durable.cancel(fence, "cube-lease-already-moved")
                return None, {"work_id": queued["work_id"], "state": "stale"}
            replacement = self.partition_store.recover_lease(
                old_cube, _cube_owner(fence), now=timestamp
            )
            if replacement is None:
                self.durable.cancel(fence, "cube-recovery-rejected")
                return None, {"work_id": queued["work_id"], "state": "stale"}
            new_lease = build_distributed_cube_lease(replacement, outer)
            self.binding_store.record(
                new_lease,
                supersedes=(
                    None if previous is None else previous["lease_sha256"]
                ),
                now=timestamp,
            )
            return new_lease, None
        if task["status"] in {"sat", "unsat"}:
            result_sha256 = _hex_digest(
                task["result_sha256"], "recovered cube result"
            )
            self.durable.finish(fence, result_sha256)
            if previous is not None:
                self.binding_store.transition(
                    previous["lease_sha256"],
                    "completed",
                    result_sha256=result_sha256,
                    now=timestamp,
                )
            return None, {"work_id": queued["work_id"], "state": "completed"}
        self.durable.cancel(fence, f"cube-state-{task['status']}")
        if previous is not None:
            target = "exhausted" if task["status"] == "exhausted" else "cancelled"
            self.binding_store.transition(
                previous["lease_sha256"], target, now=timestamp
            )
        return None, {"work_id": queued["work_id"], "state": task["status"]}

    def resume_recovery_queue(
        self, *, now: float | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Drain a previously committed recovery queue without a new generation."""
        timestamp = _timestamp(now, "distributed recovery-resume timestamp")
        recovered: list[dict[str, Any]] = []
        settled: list[dict[str, Any]] = []
        with self._lock:
            while True:
                snapshot = self.durable.controller.snapshot()
                if snapshot["pending_recovery"] is not None:
                    raise DistributedPartitionError(
                        "cannot resume before the recovery receipt commits"
                    )
                if not snapshot["recovery_queue"]:
                    break
                lease, terminal = self._settle_requeued(
                    snapshot["recovery_queue"][0], timestamp
                )
                if lease is not None:
                    recovered.append(lease)
                if terminal is not None:
                    settled.append(terminal)
        return {"recovered_leases": recovered, "settled_work": settled}

    def recover(
        self,
        failed_endpoints: Sequence[str],
        attestations: Sequence[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Advance the communicator generation and immediately replay cubes."""
        timestamp = _timestamp(now, "distributed recovery timestamp")
        with self._lock:
            plan = self.durable.prepare_recovery(failed_endpoints)
            receipt = self.durable.commit_recovery(
                plan["plan_sha256"], attestations
            )
            resumed = self.resume_recovery_queue(now=timestamp)
            return {
                "schema": "symcc-qfbv-distributed-recovery-result-v1",
                "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
                "execution_sha256": self.execution_sha256,
                "recovery_receipt": receipt,
                **resumed,
            }

    def finalize_if_ready(self) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self.partition_store.snapshot(self.execution_sha256)
            if snapshot["state"] == "active" and snapshot["task_counts"] == {
                "unsat": snapshot["cube_count"]
            }:
                return self.partition_store.finalize_unsat(
                    self.plan,
                    self.certificate,
                    self.execution_sha256,
                    checker=self.checker,
                    proof_store=self.proof_store,
                )
            if snapshot["state"] in {"sat", "unsat", "incomplete"}:
                return self.partition_store.result(
                    self.plan, self.execution_sha256, checker=self.checker
                )
            return None

    def stats(self) -> dict[str, Any]:
        snapshot = self.durable.controller.snapshot()
        return {
            "schema": "symcc-qfbv-distributed-partition-stats-v1",
            "protocol": DISTRIBUTED_PARTITION_PROTOCOL,
            "execution": self.partition_store.snapshot(self.execution_sha256),
            "bindings": self.binding_store.stats(),
            "ulfm_generation": snapshot["generation"],
            "ulfm_generation_token": snapshot["generation_token"],
            "ulfm_members": len(snapshot["members"]),
            "ulfm_recovery_queue": len(snapshot["recovery_queue"]),
            "ulfm_active_work": sum(
                bool(shard["active_work_id"])
                for shard in snapshot["shards"].values()
            ),
        }
