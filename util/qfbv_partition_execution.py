#!/usr/bin/env python3
"""Proof-aware execution for certified QF_BV cube partitions.

The scheduler persists one fenced task per verified F448 cube.  A SAT task is
accepted only with a cube-satisfying Query-IR witness.  An UNSAT task is
accepted only after its assumption-scoped LRUP receipt is replayed.  When all
cubes are UNSAT, their checked clauses are resolved along the certified split
tree into a normal base-query result receipt.
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from qfbv_incremental_proof import (
    IncrementalProofChecker,
    IncrementalProofError,
    IncrementalProofStore,
    make_imported_lrup_clause_record,
    make_unsat_result_receipt,
    normalize_clause,
)
from qfbv_incremental_sat import (
    BitBlastPlan,
    QfbvBitBlastError,
    bitblast_qfbv_query,
    extend_bitblast_assumptions,
)
from qfbv_artifact_lifecycle import (
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from qfbv_proof_prefix_partition import (
    ProofPrefixPartitionError,
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
    verify_proof_prefix_partition,
)
from qfbv_online_cubing import (
    ONLINE_CUBING_PROTOCOL,
    OnlineCubingError,
    OnlineCubingPolicyStore,
    online_cubing_budget,
)
from qfbv_utility_pairing import UtilityPairingError, formula_family_sha256


PARTITION_EXECUTION_PROTOCOL = "symcc-qfbv-proof-aware-execution-v1"
PARTITION_EXECUTION_POLICY_SCHEMA = (
    "symcc-qfbv-proof-aware-execution-policy-v1"
)
PARTITION_EXECUTION_SCHEMA = "symcc-qfbv-proof-aware-execution-v1"
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_WORKERS = 256
MAX_ATTEMPTS = 32
_HEX = frozenset("0123456789abcdef")


class PartitionExecutionError(ValueError):
    """An execution policy, lease, cube result, or aggregate is invalid."""


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
        raise PartitionExecutionError("execution value is not canonical JSON") from error


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PartitionExecutionError("stored JSON contains duplicate members")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PartitionExecutionError(f"stored JSON contains {value}")


def _load_canonical_json(raw: Any, name: str) -> Any:
    if type(raw) is not str:
        raise PartitionExecutionError(f"stored {name} is not text")
    try:
        encoded = raw.encode("ascii")
    except UnicodeError as error:
        raise PartitionExecutionError(f"stored {name} is not ASCII JSON") from error
    if len(encoded) > MAX_RESULT_BYTES:
        raise PartitionExecutionError(f"stored {name} exceeds its byte contract")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, PartitionExecutionError):
            raise
        raise PartitionExecutionError(f"stored {name} is invalid JSON") from error
    if _canonical_json(value) != encoded:
        raise PartitionExecutionError(f"stored {name} is not canonical JSON")
    return value


def _hex_digest(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise PartitionExecutionError(f"{name} must be a lowercase SHA-256")
    return value


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise PartitionExecutionError(f"{name} must be in [{lower}, {upper}]")
    return value


def _timestamp(value: float | None, name: str) -> float:
    try:
        parsed = time.time() if value is None else float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise PartitionExecutionError(f"{name} must be finite") from error
    if not math.isfinite(parsed):
        raise PartitionExecutionError(f"{name} must be finite")
    return parsed


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise PartitionExecutionError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise PartitionExecutionError(f"{name} is invalid") from error
    if (
        not encoded
        or len(encoded) > 256
        or any(character < 32 or character == 127 for character in encoded)
    ):
        raise PartitionExecutionError(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class PartitionExecutionPolicy:
    parallelism: int = 4
    max_attempts: int = 3
    cube_timeout_ms: int = 30_000
    task_lease_ms: int = 60_000

    def __post_init__(self) -> None:
        _integer(self.parallelism, "partition parallelism", 1, MAX_WORKERS)
        _integer(self.max_attempts, "partition attempts", 1, MAX_ATTEMPTS)
        _integer(self.cube_timeout_ms, "cube timeout", 1, 3_600_000)
        _integer(self.task_lease_ms, "cube task lease", 100, 86_400_000)
        if self.task_lease_ms < self.cube_timeout_ms:
            raise PartitionExecutionError(
                "cube task lease must cover the cube solve timeout"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PARTITION_EXECUTION_POLICY_SCHEMA,
            "protocol": PARTITION_EXECUTION_PROTOCOL,
            "parallelism": self.parallelism,
            "max_attempts": self.max_attempts,
            "cube_timeout_ms": self.cube_timeout_ms,
            "task_lease_ms": self.task_lease_ms,
        }

    @property
    def sha256(self) -> str:
        return _digest(_canonical_json(self.as_dict()))


@dataclass(frozen=True)
class CubeExecutionLease:
    execution_sha256: str
    partition_sha256: str
    query_id: str
    ordinal: int
    cube_sha256: str
    literals: tuple[int, ...]
    assumptions: tuple[int, ...]
    token: int
    owner: str
    timeout_ms: int


@dataclass(frozen=True)
class _CubeSolverRequest:
    query_id: str
    token: int
    timeout_ms: int
    input_hex: str


@dataclass(frozen=True)
class _PrerunSolverRequest:
    query_id: str
    token: int
    timeout_ms: int
    input_hex: str


def execution_identity(
    plan: BitBlastPlan,
    certificate: Mapping[str, Any],
    policy: PartitionExecutionPolicy,
    *,
    checker: IncrementalProofChecker | None = None,
) -> tuple[str, dict[str, Any]]:
    verified = verify_proof_prefix_partition(plan, certificate, checker=checker)
    body = {
        "schema": PARTITION_EXECUTION_SCHEMA,
        "protocol": PARTITION_EXECUTION_PROTOCOL,
        "query_id": plan.query_id,
        "formula_sha256": plan.formula_sha256,
        "base_assumption_sha256": plan.assumption_sha256,
        "bitblast_certificate_sha256": plan.certificate["certificate_sha256"],
        "partition_sha256": verified["partition_sha256"],
        "policy_sha256": policy.sha256,
    }
    return _digest(_canonical_json(body)), verified


def _cube_for_ordinal(
    verified: Mapping[str, Any], ordinal: int
) -> Mapping[str, Any]:
    cubes = verified.get("cubes")
    if not isinstance(cubes, list) or not 0 <= ordinal < len(cubes):
        raise PartitionExecutionError("cube ordinal is outside the partition")
    cube = cubes[ordinal]
    if not isinstance(cube, Mapping) or cube.get("ordinal") != ordinal:
        raise PartitionExecutionError("cube order changed")
    return cube


def _derived_plan(plan: BitBlastPlan, cube: Mapping[str, Any]) -> BitBlastPlan:
    literals_raw = cube.get("literals")
    if not isinstance(literals_raw, list) or any(
        type(value) is not int for value in literals_raw
    ):
        raise PartitionExecutionError("cube literals are invalid")
    try:
        derived = extend_bitblast_assumptions(plan, literals_raw)
    except QfbvBitBlastError as error:
        raise PartitionExecutionError("cube assumptions cannot extend the plan") from error
    if list(derived.assumptions) != cube.get("assumptions"):
        raise PartitionExecutionError("derived cube assumptions changed")
    return derived


def _result_bytes(result: Mapping[str, Any]) -> bytes:
    encoded = _canonical_json(dict(result))
    if len(encoded) > MAX_RESULT_BYTES:
        raise PartitionExecutionError("cube result exceeds its byte contract")
    return encoded


def _assignment_satisfies_cube(
    plan: BitBlastPlan,
    literals: Sequence[int],
    assignments: Mapping[str | int, Any],
) -> bool:
    normalized: dict[int, int] = {}
    for raw_offset, raw_value in assignments.items():
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            isinstance(raw_offset, str)
            and str(offset) != raw_offset
            or isinstance(raw_offset, bool)
            or type(raw_value) is not int
            or not 0 <= offset <= 65_535
            or not 0 <= raw_value <= 255
            or offset in normalized
        ):
            return False
        normalized[offset] = raw_value
    variable_values: dict[int, bool] = {}
    for offset, input_literals in plan.input_literals:
        if offset not in normalized:
            return False
        value = normalized[offset]
        for bit, input_literal in enumerate(input_literals):
            semantic = bool(value & (1 << bit))
            variable_values[abs(input_literal)] = (
                semantic if input_literal > 0 else not semantic
            )
    return all(
        variable_values.get(abs(literal)) == (literal > 0)
        for literal in literals
    )


def verify_cube_result(
    plan: BitBlastPlan,
    certificate: Mapping[str, Any],
    ordinal: int,
    result: Mapping[str, Any],
    *,
    checker: IncrementalProofChecker,
    candidate_validator: Callable[[bytes], bool] | None = None,
    input_hex: str = "",
) -> tuple[str, dict[str, Any]]:
    """Verify a terminal or retryable result against one exact cube."""
    if not isinstance(result, Mapping):
        raise PartitionExecutionError("cube result must be an object")
    verified = verify_proof_prefix_partition(plan, certificate, checker=checker)
    cube = _cube_for_ordinal(verified, ordinal)
    derived = _derived_plan(plan, cube)
    status = result.get("status")
    if status not in {"sat", "unsat", "unknown", "error"}:
        raise PartitionExecutionError("cube result has an invalid status")
    normalized = json.loads(_result_bytes(result).decode("ascii"))
    if status in {"sat", "unsat"} and normalized.get(
        "bitblast_certificate"
    ) != dict(derived.certificate):
        raise PartitionExecutionError("cube result has another assumption scope")
    if status == "sat":
        assignments = normalized.get("assignments")
        if (
            normalized.get("backend_model_verified") is not True
            or not isinstance(assignments, Mapping)
            or not _assignment_satisfies_cube(
                plan, cube["literals"], assignments
            )
        ):
            raise PartitionExecutionError("SAT witness does not satisfy its cube")
        if candidate_validator is not None:
            try:
                candidate = bytearray.fromhex(input_hex)
            except ValueError as error:
                raise PartitionExecutionError("parent input is not hexadecimal") from error
            for raw_offset, value in assignments.items():
                offset = int(raw_offset)
                if offset >= len(candidate):
                    raise PartitionExecutionError("SAT witness exceeds parent input")
                candidate[offset] = int(value)
            if not candidate_validator(bytes(candidate)):
                raise PartitionExecutionError("SAT witness failed Query IR replay")
    elif status == "unsat":
        if (
            normalized.get("backend_unsat_authorized") is not True
            or normalized.get("backend_incremental_proof_verified") is not True
        ):
            raise PartitionExecutionError("UNSAT cube lacks proof authorization")
        receipt = normalized.get("backend_incremental_result_receipt")
        record_digest = _hex_digest(
            normalized.get("backend_incremental_proof_record_sha256"),
            "cube proof record",
        )
        try:
            authorization = checker.verify_result_receipt(derived, receipt)
        except (IncrementalProofError, OSError, sqlite3.Error) as error:
            raise PartitionExecutionError("cube proof receipt failed replay") from error
        if (
            authorization.clause_receipt_sha256 != record_digest
            or authorization.failed_assumptions
            != tuple(sorted(derived.assumptions))
        ):
            raise PartitionExecutionError("cube proof does not bind every assumption")
    return str(status), normalized


class PartitionExecutionStore:
    """SQLite-backed cube task ledger with expiring, token-fenced leases."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ) -> None:
        if lifecycle_lease is not None and lifecycle is None:
            raise PartitionExecutionError(
                "partition execution lifecycle lease requires a registry"
            )
        root_path = Path(root)
        if root_path.is_symlink():
            raise PartitionExecutionError(
                "partition execution root must not be a symlink"
            )
        root_path.mkdir(parents=True, exist_ok=True)
        self.root = root_path.resolve()
        metadata = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("partition execution root is not a directory")
        self.database = self.root / "partition-execution.sqlite3"
        self.initialize_lock = self.root / ".initialize.lock"
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.database.is_symlink():
            raise PartitionExecutionError(
                "partition execution database must not be a symlink"
            )
        connection = sqlite3.connect(self.database, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for execution initialization")
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
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise PartitionExecutionError(
                    "partition execution initialization lock changed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = os.stat(self.initialize_lock, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (locked.st_dev, locked.st_ino):
                raise PartitionExecutionError(
                    "partition execution initialization lock was replaced"
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
                CREATE TABLE IF NOT EXISTS partition_execution_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS partition_executions(
                    execution_sha256 TEXT PRIMARY KEY,
                    query_id TEXT NOT NULL,
                    partition_sha256 TEXT NOT NULL,
                    formula_sha256 TEXT NOT NULL,
                    base_assumption_sha256 TEXT NOT NULL,
                    bitblast_certificate_sha256 TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    winner_ordinal INTEGER,
                    aggregate_record_sha256 TEXT,
                    aggregate_receipt_json TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS partition_cube_tasks(
                    execution_sha256 TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    cube_sha256 TEXT NOT NULL,
                    assumption_sha256 TEXT NOT NULL,
                    literals_json TEXT NOT NULL,
                    assumptions_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    lease_token INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL,
                    result_sha256 TEXT,
                    result_json TEXT,
                    updated REAL NOT NULL,
                    PRIMARY KEY(execution_sha256, ordinal),
                    FOREIGN KEY(execution_sha256) REFERENCES partition_executions(
                        execution_sha256
                    ) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS partition_cube_claim_idx
                ON partition_cube_tasks(execution_sha256, status, ordinal);
                """
            )
            expected = {
                "schema": PARTITION_EXECUTION_SCHEMA,
                "protocol": PARTITION_EXECUTION_PROTOCOL,
                "root": str(self.root),
                "lifecycle_identity_sha256": (
                    self.lifecycle.identity_sha256
                    if self.lifecycle is not None
                    else ""
                ),
            }
            for key, value in expected.items():
                row = db.execute(
                    "SELECT value FROM partition_execution_metadata WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO partition_execution_metadata(key,value) "
                        "VALUES(?,?)",
                        (key, value),
                    )
                elif str(row["value"]) != value:
                    raise PartitionExecutionError(
                        f"partition execution metadata mismatch for {key}"
                    )

    def create(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        policy: PartitionExecutionPolicy,
        *,
        checker: IncrementalProofChecker | None = None,
        now: float | None = None,
    ) -> str:
        execution, verified = execution_identity(
            plan, certificate, policy, checker=checker
        )
        timestamp = _timestamp(now, "execution timestamp")
        cubes = verified["cubes"]
        assert isinstance(cubes, list)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO partition_executions("
                "execution_sha256,query_id,partition_sha256,formula_sha256,"
                "base_assumption_sha256,bitblast_certificate_sha256,policy_json,"
                "policy_sha256,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution,
                    plan.query_id,
                    verified["partition_sha256"],
                    plan.formula_sha256,
                    plan.assumption_sha256,
                    plan.certificate["certificate_sha256"],
                    _canonical_json(policy.as_dict()).decode("ascii"),
                    policy.sha256,
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            if row is None or any(
                (
                    row[column] != expected
                    for column, expected in (
                        ("query_id", plan.query_id),
                        ("partition_sha256", verified["partition_sha256"]),
                        ("formula_sha256", plan.formula_sha256),
                        ("base_assumption_sha256", plan.assumption_sha256),
                        (
                            "bitblast_certificate_sha256",
                            plan.certificate["certificate_sha256"],
                        ),
                        ("policy_sha256", policy.sha256),
                    )
                )
            ):
                raise PartitionExecutionError("execution identity collision")
            for cube in cubes:
                db.execute(
                    "INSERT OR IGNORE INTO partition_cube_tasks("
                    "execution_sha256,ordinal,cube_sha256,assumption_sha256,"
                    "literals_json,assumptions_json,status,attempts,lease_token,updated"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        execution,
                        cube["ordinal"],
                        cube["cube_sha256"],
                        cube["assumption_sha256"],
                        _canonical_json(cube["literals"]).decode("ascii"),
                        _canonical_json(cube["assumptions"]).decode("ascii"),
                        "pending",
                        0,
                        0,
                        timestamp,
                    ),
                )
            task_count = db.execute(
                "SELECT COUNT(*) FROM partition_cube_tasks WHERE execution_sha256=?",
                (execution,),
            ).fetchone()[0]
            if int(task_count) != len(cubes):
                raise PartitionExecutionError("execution cube inventory changed")
            task_rows = db.execute(
                "SELECT ordinal,cube_sha256,assumption_sha256,literals_json,"
                "assumptions_json FROM partition_cube_tasks "
                "WHERE execution_sha256=? ORDER BY ordinal",
                (execution,),
            ).fetchall()
            expected_tasks = [
                (
                    cube["ordinal"],
                    cube["cube_sha256"],
                    cube["assumption_sha256"],
                    _canonical_json(cube["literals"]).decode("ascii"),
                    _canonical_json(cube["assumptions"]).decode("ascii"),
                )
                for cube in cubes
            ]
            observed_tasks = [
                (
                    int(task["ordinal"]),
                    str(task["cube_sha256"]),
                    str(task["assumption_sha256"]),
                    str(task["literals_json"]),
                    str(task["assumptions_json"]),
                )
                for task in task_rows
            ]
            if observed_tasks != expected_tasks:
                raise PartitionExecutionError("execution cube inventory changed")
        self._record_lifecycle(execution)
        return execution

    @staticmethod
    def _policy(row: sqlite3.Row) -> PartitionExecutionPolicy:
        try:
            raw = _load_canonical_json(row["policy_json"], "execution policy")
            policy = PartitionExecutionPolicy(
                parallelism=raw["parallelism"],
                max_attempts=raw["max_attempts"],
                cube_timeout_ms=raw["cube_timeout_ms"],
                task_lease_ms=raw["task_lease_ms"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PartitionExecutionError("stored execution policy is invalid") from error
        if policy.as_dict() != raw or policy.sha256 != row["policy_sha256"]:
            raise PartitionExecutionError("stored execution policy changed")
        return policy

    def claim(
        self,
        execution_sha256: str,
        owner: str,
        *,
        now: float | None = None,
    ) -> CubeExecutionLease | None:
        execution = _hex_digest(execution_sha256, "execution")
        worker = _identity(owner, "cube worker")
        timestamp = _timestamp(now, "claim timestamp")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            if parent is None:
                raise PartitionExecutionError("partition execution does not exist")
            policy = self._policy(parent)
            if parent["state"] != "active":
                return None
            db.execute(
                "UPDATE partition_cube_tasks SET status=CASE "
                "WHEN attempts < ? THEN 'pending' ELSE 'exhausted' END,"
                "lease_owner=NULL,lease_until=NULL,updated=? "
                "WHERE execution_sha256=? AND status='leased' AND lease_until < ?",
                (policy.max_attempts, timestamp, execution, timestamp),
            )
            task = db.execute(
                "SELECT * FROM partition_cube_tasks WHERE execution_sha256=? "
                "AND status='pending' ORDER BY json_array_length(literals_json),"
                "ordinal LIMIT 1",
                (execution,),
            ).fetchone()
            if task is None:
                live = db.execute(
                    "SELECT COUNT(*) FROM partition_cube_tasks "
                    "WHERE execution_sha256=? AND status IN ('pending','leased')",
                    (execution,),
                ).fetchone()[0]
                if int(live) == 0:
                    exhausted = db.execute(
                        "SELECT COUNT(*) FROM partition_cube_tasks "
                        "WHERE execution_sha256=? AND status='exhausted'",
                        (execution,),
                    ).fetchone()[0]
                    if int(exhausted):
                        db.execute(
                            "UPDATE partition_executions SET state='incomplete',"
                            "updated=? WHERE execution_sha256=? AND state='active'",
                            (timestamp, execution),
                        )
                return None
            token = int(task["lease_token"]) + 1
            lease_until = timestamp + policy.task_lease_ms / 1000.0
            changed = db.execute(
                "UPDATE partition_cube_tasks SET status='leased',attempts=attempts+1,"
                "lease_token=?,lease_owner=?,lease_until=?,updated=? "
                "WHERE execution_sha256=? AND ordinal=? AND status='pending'",
                (
                    token,
                    worker,
                    lease_until,
                    timestamp,
                    execution,
                    task["ordinal"],
                ),
            ).rowcount
            if changed != 1:
                raise PartitionExecutionError("cube claim lost its transaction")
            return CubeExecutionLease(
                execution_sha256=execution,
                partition_sha256=str(parent["partition_sha256"]),
                query_id=str(parent["query_id"]),
                ordinal=int(task["ordinal"]),
                cube_sha256=str(task["cube_sha256"]),
                literals=tuple(_load_canonical_json(
                    task["literals_json"], "cube literals"
                )),
                assumptions=tuple(_load_canonical_json(
                    task["assumptions_json"], "cube assumptions"
                )),
                token=token,
                owner=worker,
                timeout_ms=policy.cube_timeout_ms,
            )

    def heartbeat(
        self,
        lease: CubeExecutionLease,
        *,
        now: float | None = None,
    ) -> bool:
        timestamp = _timestamp(now, "heartbeat timestamp")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (lease.execution_sha256,),
            ).fetchone()
            if parent is None or parent["state"] != "active":
                return False
            policy = self._policy(parent)
            return db.execute(
                "UPDATE partition_cube_tasks SET lease_until=?,updated=? "
                "WHERE execution_sha256=? AND ordinal=? AND status='leased' "
                "AND lease_token=? AND lease_owner=? AND lease_until>=?",
                (
                    timestamp + policy.task_lease_ms / 1000.0,
                    timestamp,
                    lease.execution_sha256,
                    lease.ordinal,
                    lease.token,
                    lease.owner,
                    timestamp,
                ),
            ).rowcount == 1

    def recover_lease(
        self,
        lease: CubeExecutionLease,
        owner: str,
        *,
        now: float | None = None,
    ) -> CubeExecutionLease | None:
        """Move one exact in-flight cube to a new generation-fenced owner.

        Infrastructure recovery does not consume another semantic solver
        attempt: the old attempt is still the same ambiguous computation.  A
        new cube token nevertheless fences every heartbeat and completion
        emitted by the failed communicator generation.
        """
        worker = _identity(owner, "recovered cube worker")
        timestamp = _timestamp(now, "cube recovery timestamp")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (lease.execution_sha256,),
            ).fetchone()
            if parent is None or parent["state"] != "active":
                return None
            policy = self._policy(parent)
            task = db.execute(
                "SELECT * FROM partition_cube_tasks WHERE execution_sha256=? "
                "AND ordinal=?",
                (lease.execution_sha256, lease.ordinal),
            ).fetchone()
            if (
                task is None
                or task["status"] != "leased"
                or int(task["lease_token"]) != lease.token
                or task["lease_owner"] != lease.owner
                or str(parent["partition_sha256"]) != lease.partition_sha256
                or str(parent["query_id"]) != lease.query_id
                or str(task["cube_sha256"]) != lease.cube_sha256
                or lease.timeout_ms != policy.cube_timeout_ms
                or tuple(
                    _load_canonical_json(task["literals_json"], "cube literals")
                )
                != lease.literals
                or tuple(
                    _load_canonical_json(
                        task["assumptions_json"], "cube assumptions"
                    )
                )
                != lease.assumptions
            ):
                return None
            token = int(task["lease_token"]) + 1
            lease_until = timestamp + policy.task_lease_ms / 1000.0
            changed = db.execute(
                "UPDATE partition_cube_tasks SET lease_token=?,lease_owner=?,"
                "lease_until=?,updated=? WHERE execution_sha256=? AND ordinal=? "
                "AND status='leased' AND lease_token=? AND lease_owner=?",
                (
                    token,
                    worker,
                    lease_until,
                    timestamp,
                    lease.execution_sha256,
                    lease.ordinal,
                    lease.token,
                    lease.owner,
                ),
            ).rowcount
            if changed != 1:
                raise PartitionExecutionError("cube recovery lost its transaction")
        return CubeExecutionLease(
            execution_sha256=lease.execution_sha256,
            partition_sha256=lease.partition_sha256,
            query_id=lease.query_id,
            ordinal=lease.ordinal,
            cube_sha256=lease.cube_sha256,
            literals=lease.literals,
            assumptions=lease.assumptions,
            token=token,
            owner=worker,
            timeout_ms=lease.timeout_ms,
        )

    def task_snapshot(
        self, execution_sha256: str, ordinal: int
    ) -> dict[str, Any]:
        """Return the exact durable state needed for cross-node reconciliation."""
        execution = _hex_digest(execution_sha256, "execution")
        cube_ordinal = _integer(ordinal, "cube ordinal", 0, 4095)
        with self._connect() as db:
            parent = db.execute(
                "SELECT state,partition_sha256,query_id FROM partition_executions "
                "WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            task = db.execute(
                "SELECT * FROM partition_cube_tasks WHERE execution_sha256=? "
                "AND ordinal=?",
                (execution, cube_ordinal),
            ).fetchone()
        if parent is None or task is None:
            raise PartitionExecutionError("partition cube task does not exist")
        result_json = task["result_json"]
        result = (
            None
            if result_json is None
            else _load_canonical_json(result_json, "cube task result")
        )
        return {
            "schema": "symcc-qfbv-partition-cube-task-snapshot-v1",
            "protocol": PARTITION_EXECUTION_PROTOCOL,
            "execution_sha256": execution,
            "partition_sha256": str(parent["partition_sha256"]),
            "query_id": str(parent["query_id"]),
            "execution_state": str(parent["state"]),
            "ordinal": cube_ordinal,
            "cube_sha256": str(task["cube_sha256"]),
            "literals": _load_canonical_json(task["literals_json"], "cube literals"),
            "assumptions": _load_canonical_json(
                task["assumptions_json"], "cube assumptions"
            ),
            "status": str(task["status"]),
            "attempts": int(task["attempts"]),
            "lease_token": int(task["lease_token"]),
            "lease_owner": task["lease_owner"],
            "lease_until": task["lease_until"],
            "result_sha256": task["result_sha256"],
            "result": result,
        }

    def complete(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        lease: CubeExecutionLease,
        result: Mapping[str, Any],
        *,
        checker: IncrementalProofChecker,
        candidate_validator: Callable[[bytes], bool] | None = None,
        input_hex: str = "",
        now: float | None = None,
    ) -> bool:
        timestamp = _timestamp(now, "completion timestamp")
        execution, verified = execution_identity(
            plan,
            certificate,
            self.policy(lease.execution_sha256),
            checker=checker,
        )
        cube = _cube_for_ordinal(verified, lease.ordinal)
        if (
            execution != lease.execution_sha256
            or verified["partition_sha256"] != lease.partition_sha256
            or cube["cube_sha256"] != lease.cube_sha256
            or tuple(cube["literals"]) != lease.literals
            or tuple(cube["assumptions"]) != lease.assumptions
        ):
            raise PartitionExecutionError("cube lease scope changed")
        status, normalized = verify_cube_result(
            plan,
            verified,
            lease.ordinal,
            result,
            checker=checker,
            candidate_validator=candidate_validator,
            input_hex=input_hex,
        )
        encoded = _result_bytes(normalized)
        result_sha256 = _digest(
            _canonical_json(
                {
                    "protocol": PARTITION_EXECUTION_PROTOCOL,
                    "execution_sha256": execution,
                    "cube_sha256": lease.cube_sha256,
                    "result": normalized,
                }
            )
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute(
                "SELECT state FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            if parent is None or parent["state"] != "active":
                return False
            task = db.execute(
                "SELECT status,lease_token,lease_owner,lease_until,attempts "
                "FROM partition_cube_tasks WHERE execution_sha256=? AND ordinal=?",
                (execution, lease.ordinal),
            ).fetchone()
            if (
                task is None
                or task["status"] != "leased"
                or int(task["lease_token"]) != lease.token
                or task["lease_owner"] != lease.owner
                or task["lease_until"] is None
                or float(task["lease_until"]) < timestamp
            ):
                return False
            if status in {"sat", "unsat"}:
                next_status = status
            elif int(task["attempts"]) < self.policy(execution).max_attempts:
                next_status = "pending"
            else:
                next_status = "exhausted"
            db.execute(
                "UPDATE partition_cube_tasks SET status=?,lease_owner=NULL,"
                "lease_until=NULL,result_sha256=?,result_json=?,updated=? "
                "WHERE execution_sha256=? AND ordinal=?",
                (
                    next_status,
                    result_sha256,
                    encoded.decode("ascii"),
                    timestamp,
                    execution,
                    lease.ordinal,
                ),
            )
            if status == "sat":
                db.execute(
                    "UPDATE partition_executions SET state='sat',winner_ordinal=?,"
                    "updated=? WHERE execution_sha256=? AND state='active'",
                    (lease.ordinal, timestamp, execution),
                )
                db.execute(
                    "UPDATE partition_cube_tasks SET status='cancelled',"
                    "lease_owner=NULL,lease_until=NULL,updated=? "
                    "WHERE execution_sha256=? AND ordinal<>? "
                    "AND status IN ('pending','leased')",
                    (timestamp, execution, lease.ordinal),
                )
            elif next_status == "exhausted":
                live = db.execute(
                    "SELECT COUNT(*) FROM partition_cube_tasks "
                    "WHERE execution_sha256=? AND status IN ('pending','leased')",
                    (execution,),
                ).fetchone()[0]
                if int(live) == 0:
                    db.execute(
                        "UPDATE partition_executions SET state='incomplete',"
                        "updated=? WHERE execution_sha256=? AND state='active'",
                        (timestamp, execution),
                    )
        if status == "sat" or next_status == "exhausted":
            self._record_lifecycle(execution)
        return True

    def policy(self, execution_sha256: str) -> PartitionExecutionPolicy:
        execution = _hex_digest(execution_sha256, "execution")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
        if row is None:
            raise PartitionExecutionError("partition execution does not exist")
        return self._policy(row)

    def snapshot(self, execution_sha256: str) -> dict[str, Any]:
        execution = _hex_digest(execution_sha256, "execution")
        with self._connect() as db:
            parent = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            if parent is None:
                raise PartitionExecutionError("partition execution does not exist")
            rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM partition_cube_tasks "
                "WHERE execution_sha256=? GROUP BY status",
                (execution,),
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "schema": PARTITION_EXECUTION_SCHEMA,
            "protocol": PARTITION_EXECUTION_PROTOCOL,
            "execution_sha256": execution,
            "partition_sha256": str(parent["partition_sha256"]),
            "query_id": str(parent["query_id"]),
            "state": str(parent["state"]),
            "winner_ordinal": parent["winner_ordinal"],
            "aggregate_record_sha256": parent["aggregate_record_sha256"],
            "task_counts": dict(sorted(counts.items())),
            "cube_count": sum(counts.values()),
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as db:
            execution_rows = db.execute(
                "SELECT state,COUNT(*) AS count FROM partition_executions "
                "GROUP BY state"
            ).fetchall()
            task_rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM partition_cube_tasks "
                "GROUP BY status"
            ).fetchall()
            totals = db.execute(
                "SELECT COUNT(*) AS tasks,COALESCE(SUM(attempts),0) AS attempts "
                "FROM partition_cube_tasks"
            ).fetchone()
        executions = {
            str(row["state"]): int(row["count"]) for row in execution_rows
        }
        tasks = {str(row["status"]): int(row["count"]) for row in task_rows}
        return {
            "schema": PARTITION_EXECUTION_SCHEMA,
            "protocol": PARTITION_EXECUTION_PROTOCOL,
            "executions": sum(executions.values()),
            "terminal_executions": sum(
                executions.get(state, 0)
                for state in ("sat", "unsat", "incomplete")
            ),
            "execution_states": dict(sorted(executions.items())),
            "tasks": int(totals["tasks"]),
            "task_states": dict(sorted(tasks.items())),
            "attempts": int(totals["attempts"]),
        }

    def _terminal_manifest(
        self, execution_sha256: str
    ) -> tuple[dict[str, Any], tuple[ArtifactRef, ...]] | None:
        parent, rows = self._result_rows(execution_sha256)
        state = str(parent["state"])
        if state not in {"sat", "unsat", "incomplete"}:
            return None
        dependencies: list[ArtifactRef] = [
            ArtifactRef("partition", str(parent["partition_sha256"]))
        ]
        if state == "sat":
            winner = parent["winner_ordinal"]
            if type(winner) is not int or not 0 <= winner < len(rows):
                raise PartitionExecutionError("terminal SAT winner is invalid")
            raw_result = _load_canonical_json(
                rows[winner]["result_json"], "SAT cube result"
            )
            imports = raw_result.get(
                "backend_incremental_import_record_sha256", []
            )
            if not isinstance(imports, list) or len(imports) > 4096:
                raise PartitionExecutionError("terminal SAT imports are invalid")
            dependencies.extend(
                ArtifactRef("sat-proof", _hex_digest(item, "SAT proof import"))
                for item in imports
            )
        elif state == "unsat":
            dependencies.append(
                ArtifactRef(
                    "sat-proof",
                    _hex_digest(
                        parent["aggregate_record_sha256"], "aggregate proof"
                    ),
                )
            )
        unique_dependencies = tuple(dict.fromkeys(dependencies))
        manifest = {
            "schema": "symcc-qfbv-partition-execution-lifecycle-v1",
            "protocol": PARTITION_EXECUTION_PROTOCOL,
            "execution_sha256": execution_sha256,
            "query_id": str(parent["query_id"]),
            "partition_sha256": str(parent["partition_sha256"]),
            "policy_sha256": str(parent["policy_sha256"]),
            "state": state,
            "winner_ordinal": parent["winner_ordinal"],
            "aggregate_record_sha256": parent["aggregate_record_sha256"],
            "dependencies": [
                {"kind": item.kind, "digest": item.digest}
                for item in unique_dependencies
            ],
        }
        return manifest, unique_dependencies

    def _record_lifecycle(self, execution_sha256: str) -> None:
        if self.lifecycle is None:
            return
        terminal = self._terminal_manifest(execution_sha256)
        if terminal is None:
            return
        manifest, dependencies = terminal
        self.lifecycle.record_artifact(
            ArtifactRef("partition-execution", execution_sha256),
            encoded_bytes=len(_canonical_json(manifest)),
            edges=dependencies,
            lease=self.lifecycle_lease,
        )

    def synchronize_lifecycle(
        self, *, max_entries: int = 1_000_000
    ) -> dict[str, int | bool]:
        maximum = _integer(
            max_entries, "partition execution lifecycle scan", 1, 10_000_000
        )
        with self._connect() as db:
            rows = db.execute(
                "SELECT execution_sha256 FROM partition_executions "
                "WHERE state IN ('sat','unsat','incomplete') "
                "ORDER BY execution_sha256 LIMIT ?",
                (maximum + 1,),
            ).fetchall()
            active = int(
                db.execute(
                    "SELECT COUNT(*) FROM partition_executions WHERE state='active'"
                ).fetchone()[0]
            )
        complete = len(rows) <= maximum and active == 0
        selected = rows[:maximum]
        if self.lifecycle is not None:
            for row in selected:
                self._record_lifecycle(str(row["execution_sha256"]))
        return {
            "indexed": len(selected),
            "active": active,
            "complete": complete,
        }

    def delete_lifecycle_artifact(
        self, kind: str, digest: str, expected_size: int
    ) -> int:
        if kind != "partition-execution":
            raise PartitionExecutionError("lifecycle artifact kind is not an execution")
        execution = _hex_digest(digest, "partition execution artifact")
        terminal = self._terminal_manifest(execution)
        if terminal is None:
            raise PartitionExecutionError("active execution cannot be collected")
        size = len(_canonical_json(terminal[0]))
        if size != expected_size:
            raise PartitionExecutionError("execution lifecycle size changed")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            if row is None:
                return 0
            if row["state"] not in {"sat", "unsat", "incomplete"}:
                raise PartitionExecutionError("active execution cannot be collected")
            changed = db.execute(
                "DELETE FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).rowcount
            if changed != 1:
                raise PartitionExecutionError("execution collection lost its fence")
        return size

    def _result_rows(self, execution: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        with self._connect() as db:
            parent = db.execute(
                "SELECT * FROM partition_executions WHERE execution_sha256=?",
                (execution,),
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM partition_cube_tasks WHERE execution_sha256=? "
                "ORDER BY ordinal",
                (execution,),
            ).fetchall()
        if parent is None:
            raise PartitionExecutionError("partition execution does not exist")
        return parent, rows

    @staticmethod
    def _verify_cube_inventory(
        rows: Sequence[sqlite3.Row], cubes: Sequence[Mapping[str, Any]]
    ) -> None:
        expected = [
            (
                cube["ordinal"],
                cube["cube_sha256"],
                cube["assumption_sha256"],
                _canonical_json(cube["literals"]).decode("ascii"),
                _canonical_json(cube["assumptions"]).decode("ascii"),
            )
            for cube in cubes
        ]
        observed = [
            (
                int(row["ordinal"]),
                str(row["cube_sha256"]),
                str(row["assumption_sha256"]),
                str(row["literals_json"]),
                str(row["assumptions_json"]),
            )
            for row in rows
        ]
        if observed != expected:
            raise PartitionExecutionError("execution cube inventory changed")

    def finalize_unsat(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        execution_sha256: str,
        *,
        checker: IncrementalProofChecker,
        proof_store: IncrementalProofStore,
    ) -> dict[str, Any]:
        execution = _hex_digest(execution_sha256, "execution")
        verified = verify_proof_prefix_partition(plan, certificate, checker=checker)
        parent, rows = self._result_rows(execution)
        if (
            parent["partition_sha256"] != verified["partition_sha256"]
            or parent["state"] not in {"active", "unsat"}
            or not rows
            or any(row["status"] != "unsat" for row in rows)
        ):
            raise PartitionExecutionError("execution is not ready for UNSAT aggregation")
        if parent["state"] == "unsat":
            receipt = _load_canonical_json(
                parent["aggregate_receipt_json"], "aggregate receipt"
            )
            authorization = checker.verify_result_receipt(plan, receipt)
            if authorization.clause_receipt_sha256 != parent["aggregate_record_sha256"]:
                raise PartitionExecutionError("stored aggregate receipt changed")
            return self.result(plan, execution, checker=checker)

        imported = []
        leaf_references: dict[tuple[int, ...], int] = {}
        base_count = len(plan.clauses)
        cubes = verified["cubes"]
        assert isinstance(cubes, list)
        self._verify_cube_inventory(rows, cubes)
        for row, cube in zip(rows, cubes, strict=True):
            raw_result = _load_canonical_json(
                row["result_json"], "UNSAT cube result"
            )
            derived = _derived_plan(plan, cube)
            status, raw_result = verify_cube_result(
                plan,
                verified,
                int(row["ordinal"]),
                raw_result,
                checker=checker,
            )
            if status != "unsat":
                raise PartitionExecutionError("aggregate leaf is not UNSAT")
            result_authorization = checker.verify_result_receipt(
                derived, raw_result["backend_incremental_result_receipt"]
            )
            clause_authorization = checker.verify_clause_record(
                plan, proof_store.load(result_authorization.clause_receipt_sha256)
            )
            expected_clause = normalize_clause(
                tuple(-literal for literal in derived.assumptions),
                max_variable=plan.max_variable,
            )
            if clause_authorization.clause != expected_clause:
                raise PartitionExecutionError("cube proof clause changed")
            imported.append(clause_authorization)
            leaf_references[tuple(cube["literals"])] = base_count + len(imported)

        derived_steps: list[tuple[Sequence[int], Sequence[int]]] = []
        next_id = base_count + len(imported)
        split_steps = verified["split_steps"]
        assert isinstance(split_steps, list)
        for split in reversed(split_steps):
            negative = tuple(split["negative_child"])
            positive = tuple(split["positive_child"])
            parent_path = tuple(split["parent"])
            try:
                negative_id = leaf_references.pop(negative)
                positive_id = leaf_references.pop(positive)
            except KeyError as error:
                raise PartitionExecutionError(
                    "partition result tree is incomplete"
                ) from error
            target = normalize_clause(
                tuple(-literal for literal in plan.assumptions + parent_path),
                max_variable=plan.max_variable,
            )
            derived_steps.append((target, (negative_id, positive_id)))
            next_id += 1
            leaf_references[parent_path] = next_id
        if set(leaf_references) != {()}:
            raise PartitionExecutionError("partition result tree has extra leaves")
        record = make_imported_lrup_clause_record(
            plan,
            imported,
            derived_steps,
            dependency_assumptions=plan.assumptions,
            source_worker=f"partition-aggregate:{execution[:32]}",
            worker_epoch=0,
            sequence=0,
        )
        authorization = checker.verify_clause_record(plan, record)
        expected_root = normalize_clause(
            tuple(-literal for literal in plan.assumptions),
            max_variable=plan.max_variable,
        )
        if authorization.clause != expected_root:
            raise PartitionExecutionError("aggregate proof did not reach the root")
        digest, created = proof_store.publish(record)
        if digest != authorization.record_sha256:
            raise PartitionExecutionError("aggregate proof identity changed on publish")
        receipt = make_unsat_result_receipt(plan, digest, plan.assumptions)
        checker.verify_result_receipt(plan, receipt)
        timestamp = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            still_unsat = db.execute(
                "SELECT COUNT(*) FROM partition_cube_tasks "
                "WHERE execution_sha256=? AND status='unsat'",
                (execution,),
            ).fetchone()[0]
            if int(still_unsat) != len(rows):
                raise PartitionExecutionError("cube results changed during aggregation")
            changed = db.execute(
                "UPDATE partition_executions SET state='unsat',"
                "aggregate_record_sha256=?,aggregate_receipt_json=?,updated=? "
                "WHERE execution_sha256=? AND state='active'",
                (
                    digest,
                    _canonical_json(receipt).decode("ascii"),
                    timestamp,
                    execution,
                ),
            ).rowcount
            if changed != 1:
                current = db.execute(
                    "SELECT state,aggregate_record_sha256 FROM partition_executions "
                    "WHERE execution_sha256=?",
                    (execution,),
                ).fetchone()
                if current is None or (
                    current["state"], current["aggregate_record_sha256"]
                ) != ("unsat", digest):
                    raise PartitionExecutionError("aggregate commit lost its fence")
        self._record_lifecycle(execution)
        result = self.result(plan, execution, checker=checker)
        result["backend_incremental_proof_created"] = created
        return result

    @staticmethod
    def _strip_cube_only_fields(result: dict[str, Any]) -> None:
        for key in tuple(result):
            if key.startswith(("backend_native_", "backend_realtime_", "backend_lidrup_")):
                result.pop(key, None)

    def result(
        self,
        plan: BitBlastPlan,
        execution_sha256: str,
        *,
        checker: IncrementalProofChecker,
    ) -> dict[str, Any]:
        execution = _hex_digest(execution_sha256, "execution")
        parent, rows = self._result_rows(execution)
        state = str(parent["state"])
        if state == "sat":
            winner = int(parent["winner_ordinal"])
            selected = rows[winner]
            result = dict(_load_canonical_json(
                selected["result_json"], "SAT cube result"
            ))
            self._strip_cube_only_fields(result)
            result["bitblast_certificate"] = dict(plan.certificate)
            result["solver"] = "proof-aware-partition-sat"
        elif state == "unsat":
            aggregate_digest = _hex_digest(
                parent["aggregate_record_sha256"], "aggregate proof"
            )
            receipt = _load_canonical_json(
                parent["aggregate_receipt_json"], "aggregate receipt"
            )
            authorization = checker.verify_result_receipt(plan, receipt)
            clause = checker.verify_clause_record(
                plan, checker.store.load(aggregate_digest)  # type: ignore[union-attr]
            )
            if authorization.clause_receipt_sha256 != aggregate_digest:
                raise PartitionExecutionError("aggregate result receipt changed")
            template = dict(_load_canonical_json(
                rows[0]["result_json"], "UNSAT cube result"
            ))
            self._strip_cube_only_fields(template)
            for key in tuple(template):
                if (
                    key.startswith("backend_incremental_proof_")
                    and key
                    not in {
                        "backend_incremental_proof_protocol",
                        "backend_incremental_proof_policy_sha256",
                    }
                ) or key == "backend_incremental_result_receipt":
                    template.pop(key, None)
            imports = [
                _hex_digest(
                    _load_canonical_json(
                        row["result_json"], "UNSAT cube result"
                    )[
                        "backend_incremental_proof_record_sha256"
                    ],
                    "cube proof",
                )
                for row in rows
            ]
            template.update(
                {
                    "status": "unsat",
                    "assignments": {},
                    "solver": "proof-aware-partition-unsat",
                    "bitblast_certificate": dict(plan.certificate),
                    "backend_unsat_authorized": True,
                    "backend_incremental_import_candidates": len(imports),
                    "backend_incremental_imported_clauses": len(imports),
                    "backend_incremental_import_record_sha256": imports,
                    "backend_incremental_import_checker_elapsed_us": sum(
                        checker.verify_clause_record(
                            plan, checker.store.load(digest)  # type: ignore[union-attr]
                        ).checker_elapsed_us
                        for digest in imports
                    ),
                    "backend_incremental_proof_verified": True,
                    "backend_incremental_proof_created": False,
                    "backend_incremental_proof_record_sha256": aggregate_digest,
                    "backend_incremental_proof_steps": clause.proof_steps,
                    "backend_incremental_proof_propagations": clause.propagation_count,
                    "backend_incremental_proof_checker_elapsed_us": max(
                        1, clause.checker_elapsed_us
                    ),
                    "backend_incremental_result_receipt": receipt,
                }
            )
            result = template
        elif state == "incomplete":
            result = {
                "status": "unknown",
                "assignments": {},
                "solver": "proof-aware-partition-incomplete",
                "elapsed_us": sum(
                    int(
                        _load_canonical_json(
                            row["result_json"] if row["result_json"] is not None else "{}",
                            "incomplete cube result",
                        ).get("elapsed_us", 0)
                    )
                    for row in rows
                ),
                "reason": "partition cube attempts were exhausted",
            }
        else:
            raise PartitionExecutionError("partition execution is not terminal")
        result.update(
            {
                "backend_partition_execution_protocol": PARTITION_EXECUTION_PROTOCOL,
                "backend_partition_execution_sha256": execution,
                "backend_partition_sha256": str(parent["partition_sha256"]),
                "backend_partition_cube_count": len(rows),
                "backend_partition_completed_cubes": sum(
                    row["status"] in {"sat", "unsat"} for row in rows
                ),
                "backend_partition_execution_result": state,
            }
        )
        if state == "sat":
            result["backend_partition_winner_cube_sha256"] = str(
                rows[int(parent["winner_ordinal"])]["cube_sha256"]
            )
        elif state == "unsat":
            result["backend_partition_aggregate_proof_sha256"] = str(
                parent["aggregate_record_sha256"]
            )
        self._record_lifecycle(execution)
        return result


class ProofAwarePartitionExecutor:
    """Run one certified partition through a bounded reusable worker pool."""

    def __init__(
        self,
        store: PartitionExecutionStore,
        proof_store: IncrementalProofStore,
        checker: IncrementalProofChecker,
        backend_factory: Callable[[int], Any],
    ) -> None:
        if checker.store is None or checker.store.root != proof_store.root:
            raise PartitionExecutionError("executor checker and proof CAS must match")
        self.store = store
        self.proof_store = proof_store
        self.checker = checker
        self.backend_factory = backend_factory

    def execute(
        self,
        plan: BitBlastPlan,
        certificate: Mapping[str, Any],
        parent_lease: Any,
        policy: PartitionExecutionPolicy,
        *,
        candidate_validator: Callable[[bytes], bool] | None = None,
        source_worker: str = "partition-coordinator",
    ) -> dict[str, Any]:
        execution = self.store.create(
            plan, certificate, policy, checker=self.checker
        )
        initial = self.store.snapshot(execution)
        if initial["state"] in {"sat", "unsat", "incomplete"}:
            return self.store.result(plan, execution, checker=self.checker)
        stop = threading.Event()
        active_lock = threading.Lock()
        active: set[Any] = set()

        def worker(index: int) -> None:
            owner = f"{source_worker}-slot-{index}"
            with ExitStack() as stack:
                backend = self.backend_factory(index)
                if hasattr(backend, "__enter__") and hasattr(backend, "__exit__"):
                    backend = stack.enter_context(backend)
                with active_lock:
                    active.add(backend)
                try:
                    while not stop.is_set():
                        cube_lease = self.store.claim(execution, owner)
                        if cube_lease is None:
                            current = self.store.snapshot(execution)
                            if (
                                current["state"] == "active"
                                and current["task_counts"].get("leased", 0) > 0
                            ):
                                stop.wait(0.05)
                                continue
                            return
                        request = _CubeSolverRequest(
                            query_id=plan.query_id,
                            token=cube_lease.token,
                            timeout_ms=cube_lease.timeout_ms,
                            input_hex=str(parent_lease.input_hex),
                        )
                        heartbeat_stop = threading.Event()
                        heartbeat_failed = threading.Event()

                        def renew_cube_lease() -> None:
                            interval = max(0.05, policy.task_lease_ms / 3000.0)
                            while not heartbeat_stop.wait(interval):
                                if not self.store.heartbeat(cube_lease):
                                    heartbeat_failed.set()
                                    return

                        heartbeat = threading.Thread(
                            target=renew_cube_lease,
                            name=f"symcc-qfbv-cube-heartbeat-{index}",
                            daemon=True,
                        )
                        heartbeat.start()
                        result: Mapping[str, Any] = {
                            "status": "error",
                            "assignments": {},
                            "reason": "cube backend did not return a result",
                        }
                        try:
                            result = backend.solve_with_assumptions(
                                request, cube_lease.literals
                            )
                            heartbeat_stop.set()
                            heartbeat.join()
                            if heartbeat_failed.is_set():
                                continue
                            accepted = self.store.complete(
                                plan,
                                certificate,
                                cube_lease,
                                result,
                                checker=self.checker,
                                candidate_validator=candidate_validator,
                                input_hex=str(parent_lease.input_hex),
                            )
                        except Exception as error:
                            heartbeat_stop.set()
                            heartbeat.join()
                            if heartbeat_failed.is_set():
                                continue
                            accepted = self.store.complete(
                                plan,
                                certificate,
                                cube_lease,
                                {
                                    "status": "error",
                                    "assignments": {},
                                    "reason": str(error)[:512],
                                },
                                checker=self.checker,
                                input_hex=str(parent_lease.input_hex),
                            )
                        finally:
                            heartbeat_stop.set()
                            heartbeat.join()
                        if accepted and result.get("status") == "sat":
                            stop.set()
                            with active_lock:
                                peers = tuple(active)
                            for peer in peers:
                                if peer is not backend and hasattr(peer, "cancel"):
                                    peer.cancel(request)
                            return
                finally:
                    with active_lock:
                        active.discard(backend)

        with ThreadPoolExecutor(
            max_workers=min(policy.parallelism, len(certificate["cubes"])),
            thread_name_prefix="symcc-qfbv-cube",
        ) as pool:
            futures = [pool.submit(worker, index) for index in range(policy.parallelism)]
            for future in futures:
                future.result()
        snapshot = self.store.snapshot(execution)
        if snapshot["state"] == "active" and snapshot["task_counts"] == {
            "unsat": snapshot["cube_count"]
        }:
            return self.store.finalize_unsat(
                plan,
                certificate,
                execution,
                checker=self.checker,
                proof_store=self.proof_store,
            )
        return self.store.result(plan, execution, checker=self.checker)


class PartitioningQfbvBackend:
    """Query-service backend that automatically partitions and executes QF_BV."""

    def __init__(
        self,
        query_store: Any,
        partition_store: ProofPrefixPartitionStore,
        execution_store: PartitionExecutionStore,
        proof_store: IncrementalProofStore,
        checker: IncrementalProofChecker,
        backend_factory: Callable[[int], Any],
        *,
        capabilities: Mapping[str, Any],
        cube_count: int,
        policy: PartitionExecutionPolicy,
        source_worker: str,
        parent_lease_seconds: float,
        online_policy_store: OnlineCubingPolicyStore | None = None,
        online_activity_available: bool = False,
        prerun_backend_factory: Callable[[int], Any] | None = None,
    ) -> None:
        if type(cube_count) is not int or not 2 <= cube_count <= 4096:
            raise PartitionExecutionError("partition cube count must be in [2, 4096]")
        self.query_store = query_store
        self.partition_store = partition_store
        self.proof_store = proof_store
        self.checker = checker
        self.capabilities = dict(capabilities)
        self.cube_count = cube_count
        self.policy = policy
        self.online_policy_store = online_policy_store
        if type(online_activity_available) is not bool:
            raise PartitionExecutionError(
                "online cubing activity availability must be boolean"
            )
        self.online_activity_available = online_activity_available
        self.prerun_backend_factory = prerun_backend_factory
        self._prerun_lock = threading.Lock()
        self._prerun_backend: Any | None = None
        if online_policy_store is not None:
            if online_policy_store.policy.base_cube_count != cube_count:
                raise PartitionExecutionError(
                    "online cubing base count differs from partition cube count"
                )
            if online_policy_store.policy.base_cube_timeout_ms != (
                policy.cube_timeout_ms
            ):
                raise PartitionExecutionError(
                    "online cubing timeout differs from partition cube timeout"
                )
            if online_policy_store.policy.max_cube_attempts != policy.max_attempts:
                raise PartitionExecutionError(
                    "online cubing attempts differ from partition attempts"
                )
            guided = bool(
                {"activity", "cost"} & set(online_policy_store.policy.strategies)
            )
            if online_activity_available and guided and prerun_backend_factory is None:
                raise PartitionExecutionError(
                    "online guided cubing requires a prerun backend"
                )
        self.source_worker = _identity(source_worker, "partition service worker")
        if (
            not math.isfinite(float(parent_lease_seconds))
            or not 0.1 <= float(parent_lease_seconds) <= 86_400.0
        ):
            raise PartitionExecutionError(
                "parent query lease must be in [0.1, 86400] seconds"
            )
        self.parent_lease_seconds = float(parent_lease_seconds)
        self.executor = ProofAwarePartitionExecutor(
            execution_store,
            proof_store,
            checker,
            backend_factory,
        )

    @staticmethod
    def _activity_evidence(
        result: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
        raw_acks = result.get("backend_realtime_import_acks", ())
        raw_receipts = result.get(
            "backend_realtime_clause_activity_receipts", ()
        )
        if not isinstance(raw_acks, list) or not isinstance(raw_receipts, list):
            return ()
        acks = {
            str(ack.get("ack_sha256")): ack
            for ack in raw_acks
            if isinstance(ack, Mapping)
        }
        evidence: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for receipt in raw_receipts:
            if not isinstance(receipt, Mapping):
                continue
            ack = acks.get(str(receipt.get("ack_sha256")))
            if ack is None:
                continue
            evidence.append((receipt, ack))
        return tuple(evidence)

    def _run_prerun(self, lease: Any, timeout_ms: int) -> dict[str, Any]:
        if self.prerun_backend_factory is None:
            raise PartitionExecutionError("online cubing prerun backend is unavailable")
        request = _PrerunSolverRequest(
            query_id=str(lease.query_id),
            token=int(lease.token),
            timeout_ms=timeout_ms,
            input_hex=str(lease.input_hex),
        )
        with self._prerun_lock:
            if self._prerun_backend is None:
                self._prerun_backend = self.prerun_backend_factory(MAX_WORKERS)
            try:
                result = self._prerun_backend(request)
            except Exception:
                close = getattr(self._prerun_backend, "close", None)
                if callable(close):
                    close()
                self._prerun_backend = None
                raise
            if not isinstance(result, Mapping):
                close = getattr(self._prerun_backend, "close", None)
                if callable(close):
                    close()
                self._prerun_backend = None
                raise PartitionExecutionError(
                    "online cubing prerun returned a non-object result"
                )
            return dict(result)

    def close(self) -> None:
        with self._prerun_lock:
            backend = self._prerun_backend
            self._prerun_backend = None
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "PartitioningQfbvBackend":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _prerun_terminal_is_verified(
        self, lease: Any, result: Mapping[str, Any]
    ) -> bool:
        status = result.get("status")
        if status == "unsat":
            return (
                result.get("backend_unsat_authorized") is True
                and result.get("backend_incremental_proof_verified") is True
            )
        if status != "sat" or result.get("backend_model_verified") is not True:
            return False
        assignments = result.get("assignments")
        if not isinstance(assignments, Mapping):
            return False
        try:
            candidate = bytearray.fromhex(str(lease.input_hex))
            for raw_offset, raw_value in assignments.items():
                offset = int(raw_offset)
                value = int(raw_value)
                if not 0 <= offset < len(candidate) or not 0 <= value <= 255:
                    return False
                candidate[offset] = value
        except (TypeError, ValueError):
            return False
        return bool(
            self.query_store.validate_candidate(str(lease.query_id), bytes(candidate))
        )

    def _online_result_fields(
        self,
        decision: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        assert self.online_policy_store is not None
        return {
            "backend_online_cubing_protocol": ONLINE_CUBING_PROTOCOL,
            "backend_online_cubing_policy": (
                self.online_policy_store.policy.as_dict()
            ),
            "backend_online_cubing_policy_sha256": (
                self.online_policy_store.policy.sha256
            ),
            "backend_online_cubing_query_id": str(decision["query_id"]),
            "backend_online_cubing_formula_family_sha256": str(
                decision["formula_family_sha256"]
            ),
            "backend_online_cubing_decision": dict(decision),
            "backend_online_cubing_outcome": dict(outcome),
        }

    def __call__(self, lease: Any) -> Mapping[str, Any]:
        loaded = self.query_store.load_query_ir(str(lease.query_id))
        if loaded is None:
            return {
                "status": "error",
                "assignments": {},
                "solver": "proof-aware-partition",
                "elapsed_us": 0,
                "reason": "Query IR is unavailable",
            }
        started = time.monotonic_ns()
        decision: dict[str, Any] | None = None
        prerun_elapsed_us = 0
        activity_evidence: tuple[
            tuple[Mapping[str, Any], Mapping[str, Any]], ...
        ] = ()
        partition_executed = False
        heartbeat_stop = threading.Event()
        heartbeat_failed = threading.Event()

        def renew_parent_lease() -> None:
            interval = max(0.05, self.parent_lease_seconds / 3.0)
            while not heartbeat_stop.wait(interval):
                if not self.query_store.renew(
                    lease,
                    self.source_worker,
                    self.parent_lease_seconds,
                ):
                    heartbeat_failed.set()
                    return

        try:
            if not self.query_store.renew(
                lease,
                self.source_worker,
                self.parent_lease_seconds,
            ):
                raise PartitionExecutionError("parent query lease is stale")
            heartbeat = threading.Thread(
                target=renew_parent_lease,
                name="symcc-qfbv-parent-heartbeat",
                daemon=True,
            )
            heartbeat.start()
            plan = bitblast_qfbv_query(
                str(lease.query_id), loaded[0], loaded[1], self.capabilities
            )
            selected_cube_count = self.cube_count
            if self.online_policy_store is not None:
                family = formula_family_sha256(plan.certificate)
                decision = self.online_policy_store.decide(
                    str(lease.query_id),
                    family,
                    activity_available=self.online_activity_available,
                )
                selected_cube_count = int(decision["cube_count"])
                if decision["prerun_requested"]:
                    prerun_started = time.monotonic_ns()
                    prerun = self._run_prerun(
                        lease,
                        min(
                            self.online_policy_store.policy.prerun_budget_ms,
                            max(1, int(lease.timeout_ms)),
                        ),
                    )
                    prerun_elapsed_us = (
                        time.monotonic_ns() - prerun_started
                    ) // 1000
                    activity_evidence = self._activity_evidence(prerun)
                    if self._prerun_terminal_is_verified(lease, prerun):
                        heartbeat_stop.set()
                        heartbeat.join()
                        if heartbeat_failed.is_set():
                            raise PartitionExecutionError(
                                "parent query lease renewal was fenced"
                            )
                        total_elapsed_us = (time.monotonic_ns() - started) // 1000
                        prerun["elapsed_us"] = total_elapsed_us
                        outcome = self.online_policy_store.observe(
                            decision,
                            status=str(prerun["status"]),
                            partition_executed=False,
                            elapsed_us=total_elapsed_us,
                            prerun_elapsed_us=min(
                                prerun_elapsed_us, total_elapsed_us
                            ),
                            completed_cubes=0,
                            activity_receipts=len(activity_evidence),
                        )
                        prerun.update(self._online_result_fields(decision, outcome))
                        return prerun
            partition_policy = ProofPrefixPartitionPolicy(
                cube_count=selected_cube_count,
                max_depth=max(1, math.ceil(math.log2(selected_cube_count))),
            )
            certificate = build_proof_prefix_partition(
                plan,
                partition_policy,
                activity_evidence=activity_evidence,
                checker=self.checker,
            )
            digest, _created = self.partition_store.publish(
                plan, certificate, checker=self.checker
            )
            if digest != certificate["partition_sha256"]:
                raise PartitionExecutionError(
                    "partition identity changed during publication"
                )
            execution_policy = self.policy
            if self.online_policy_store is not None:
                budget = online_cubing_budget(
                    self.online_policy_store.policy,
                    cube_count=selected_cube_count,
                    prerun_elapsed_us=prerun_elapsed_us,
                )
                execution_policy = PartitionExecutionPolicy(
                    parallelism=self.policy.parallelism,
                    max_attempts=self.policy.max_attempts,
                    cube_timeout_ms=budget["effective_cube_timeout_ms"],
                    task_lease_ms=max(
                        self.policy.task_lease_ms,
                        budget["effective_cube_timeout_ms"],
                    ),
                )
            partition_executed = True
            result = self.executor.execute(
                plan,
                certificate,
                lease,
                execution_policy,
                candidate_validator=lambda candidate: self.query_store.validate_candidate(
                    str(lease.query_id), candidate
                ),
                source_worker=self.source_worker,
            )
            heartbeat_stop.set()
            heartbeat.join()
            if heartbeat_failed.is_set():
                raise PartitionExecutionError(
                    "parent query lease renewal was fenced"
                )
            total_elapsed_us = (time.monotonic_ns() - started) // 1000
            result["elapsed_us"] = total_elapsed_us
            if self.online_policy_store is not None and decision is not None:
                outcome = self.online_policy_store.observe(
                    decision,
                    status=str(result.get("status", "unknown")),
                    partition_executed=True,
                    elapsed_us=total_elapsed_us,
                    prerun_elapsed_us=min(prerun_elapsed_us, total_elapsed_us),
                    completed_cubes=int(
                        result.get("backend_partition_completed_cubes", 0)
                    ),
                    activity_receipts=len(activity_evidence),
                )
                result.update(self._online_result_fields(decision, outcome))
            return result
        except (
            OSError,
            sqlite3.Error,
            IncrementalProofError,
            ProofPrefixPartitionError,
            PartitionExecutionError,
            QfbvBitBlastError,
            OnlineCubingError,
            UtilityPairingError,
        ) as error:
            if self.online_policy_store is not None and decision is not None:
                try:
                    elapsed_us = (time.monotonic_ns() - started) // 1000
                    self.online_policy_store.observe(
                        decision,
                        status="unknown",
                        partition_executed=partition_executed,
                        elapsed_us=elapsed_us,
                        prerun_elapsed_us=min(prerun_elapsed_us, elapsed_us),
                        completed_cubes=0,
                        activity_receipts=len(activity_evidence),
                    )
                except (OSError, sqlite3.Error, OnlineCubingError):
                    pass
            return {
                "status": "unknown",
                "assignments": {},
                "solver": "proof-aware-partition",
                "elapsed_us": (time.monotonic_ns() - started) // 1000,
                "reason": f"certified partition execution failed: {error}"[:512],
            }
        finally:
            heartbeat_stop.set()
            heartbeat_thread = locals().get("heartbeat")
            if isinstance(heartbeat_thread, threading.Thread):
                heartbeat_thread.join()
