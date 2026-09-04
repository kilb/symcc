#!/usr/bin/env python3
"""Solver-native PalRUP production with fail-closed global confirmation.

A fault-isolated helper process hosts a bounded pool of CaDiCaL rank threads
and writes only into a private staging tree.  The complete proof bundle is
published atomically only after the SAT 2026 PalRUP local/redistribute/confirm
pipeline accepts every rank.  The project codec is an early structural gate,
never the UNSAT authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence

from distributed_state import durable_rename_noreplace, fsync_directory
from qfbv_palrup_pipeline import (
    MAX_PALRUP_FORMULA_BYTES,
    MAX_PALRUP_SOLVERS,
    PalrupGlobalChecker,
    _snapshot_path,
)
from qfbv_proof_wire import (
    MAX_WIRE_BYTES,
    PALRUP_BINARY_PROTOCOL,
    PalrupDelete,
    PalrupImport,
    PalrupProduce,
    ProofWireError,
    _canonical_json,
    _digest,
    _hex_digest,
    _integer,
    _read_regular,
    _run_bounded,
    decode_palrup_fragment,
)


NATIVE_PALRUP_PROTOCOL = "symcc-qfbv-native-palrup-production-v1"
NATIVE_PALRUP_POLICY_SCHEMA = "symcc-qfbv-native-palrup-policy-v1"
NATIVE_PALRUP_RECEIPT_SCHEMA = "symcc-qfbv-native-palrup-receipt-v1"
NATIVE_POOL_RESULT_SCHEMA = "symcc-qfbv-native-palrup-pool-result-v1"
NATIVE_ABI_PROTOCOL = "symcc-qfbv-native-palrup-clause-sharing-pool-v1"
NATIVE_PRODUCER_COMMIT = "be7a0f84190b3216c589696b2010e8cbf8a8252e"

MAX_NATIVE_LIBRARY_BYTES = 512 * 1024 * 1024
MAX_NATIVE_HELPER_BYTES = 1 * 1024 * 1024
MAX_NATIVE_PARALLEL = 256
MAX_NATIVE_POOL_SOLVERS = 256
MAX_NATIVE_TIMEOUT_MS = 24 * 60 * 60 * 1000
MAX_NATIVE_FRAGMENT_BYTES = MAX_WIRE_BYTES
MAX_NATIVE_SHARED_CLAUSE_LENGTH = 1024
MAX_NATIVE_QUEUE_CLAUSES = 1_000_000
_WORKER_GRACE_MS = 5_000
_RECEIPT_NAME = "native-palrup-receipt.json"
_FORMULA_NAME = "formula.cnf"


def _regular_identity(path: Path, maximum: int, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProofWireError(f"cannot open {label}") from error
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise ProofWireError(f"{label} is empty, oversized, or not regular")
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ProofWireError(f"{label} exceeds its byte bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ProofWireError(f"{label} changed while it was read")
        return {"sha256": digest.hexdigest(), "bytes": total}
    finally:
        os.close(descriptor)


def _dimacs_header(path: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProofWireError("cannot open native PalRUP formula snapshot") from error
    consumed = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for raw_line in stream:
                consumed += len(raw_line)
                if consumed > 1 << 20:
                    raise ProofWireError("DIMACS header exceeds its byte bound")
                line = raw_line.strip()
                if not line or line.startswith(b"c"):
                    continue
                fields = line.split()
                if len(fields) != 4 or fields[:2] != [b"p", b"cnf"]:
                    raise ProofWireError("DIMACS formula has no canonical CNF header")
                try:
                    variables = int(fields[2], 10)
                    clauses = int(fields[3], 10)
                except ValueError as error:
                    raise ProofWireError("DIMACS header counts are invalid") from error
                if not 0 <= variables <= (1 << 31) - 1 or not 1 <= clauses <= (1 << 31) - 1:
                    raise ProofWireError("DIMACS header counts exceed native bounds")
                if str(variables).encode("ascii") != fields[2] or str(clauses).encode("ascii") != fields[3]:
                    raise ProofWireError("DIMACS header counts are not canonical")
                return variables, clauses
    finally:
        os.close(descriptor)
    raise ProofWireError("DIMACS formula has no CNF header")


def _bounded_int(value: Any, label: str, upper: int = (1 << 64) - 1) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise ProofWireError(f"{label} is outside its integer bound")
    return value


def _parse_pool_result(
    content: bytes,
    *,
    solvers: int,
    skipped_epochs: int,
    original_clauses: int,
    maximum_shared_clause_length: int,
    queue_capacity_clauses: int,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(content.decode("ascii", "strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProofWireError("native PalRUP pool returned invalid JSON") from error
    if not isinstance(payload, dict) or _canonical_json(payload) + b"\n" != content:
        raise ProofWireError("native PalRUP pool result is not canonical")
    expected = {
        "schema",
        "protocol",
        "source_commit",
        "native_status",
        "solver_count",
        "skipped_epochs",
        "maximum_shared_clause_length",
        "queue_capacity_clauses",
        "workers",
    }
    if set(payload) != expected:
        raise ProofWireError("native PalRUP pool result fields changed")
    for name, upper in (
        ("native_status", 20),
        ("solver_count", MAX_NATIVE_POOL_SOLVERS),
        ("skipped_epochs", 2_000_000_000),
        ("maximum_shared_clause_length", MAX_NATIVE_SHARED_CLAUSE_LENGTH),
        ("queue_capacity_clauses", MAX_NATIVE_QUEUE_CLAUSES),
    ):
        _bounded_int(payload[name], f"native PalRUP pool {name}", upper)
    if (
        payload["schema"] != NATIVE_POOL_RESULT_SCHEMA
        or payload["protocol"] != NATIVE_ABI_PROTOCOL
        or payload["source_commit"] != NATIVE_PRODUCER_COMMIT
        or payload["solver_count"] != solvers
        or payload["skipped_epochs"] != skipped_epochs
        or payload["native_status"] != 20
        or payload["maximum_shared_clause_length"]
        != maximum_shared_clause_length
        or payload["queue_capacity_clauses"] != queue_capacity_clauses
    ):
        raise ProofWireError("native PalRUP pool did not prove UNSAT")
    workers = payload["workers"]
    if not isinstance(workers, list) or len(workers) != solvers:
        raise ProofWireError("native PalRUP pool worker set is incomplete")
    statistic_names = {
        "variables",
        "original_clauses",
        "conflicts",
        "decisions",
        "propagations",
        "restarts",
        "imported",
        "discarded",
        "exported",
        "delivered",
        "dropped",
        "pending",
        "elapsed_us",
    }
    normalized: list[dict[str, Any]] = []
    for rank, worker in enumerate(workers):
        if (
            not isinstance(worker, dict)
            or set(worker) != {"rank", "native_status", "statistics"}
            or type(worker["rank"]) is not int
            or worker["rank"] != rank
            or type(worker["native_status"]) is not int
            or worker["native_status"] != 20
        ):
            raise ProofWireError(f"native PalRUP worker {rank} did not prove UNSAT")
        statistics = worker["statistics"]
        if not isinstance(statistics, dict) or set(statistics) != statistic_names:
            raise ProofWireError(f"native PalRUP worker {rank} statistics changed")
        for name in statistic_names:
            _bounded_int(statistics[name], f"native PalRUP worker {rank} {name}")
        if statistics["original_clauses"] != original_clauses:
            raise ProofWireError(
                f"native PalRUP worker {rank} formula identity changed"
            )
        if statistics["pending"] > queue_capacity_clauses:
            raise ProofWireError(
                f"native PalRUP worker {rank} pending queue exceeds capacity"
            )
        normalized.append(worker)
    return normalized


def _fragment_metadata(
    path: Path,
    *,
    rank: int,
    solvers: int,
    maximum: int,
) -> dict[str, Any]:
    content = _read_regular(path, maximum)
    directives = decode_palrup_fragment(content, max_bytes=maximum)
    produced = imported = deleted = empty = 0
    previous_id = 0
    for directive in directives:
        if isinstance(directive, PalrupProduce):
            produced += 1
            if directive.external_id % solvers != rank:
                raise ProofWireError(f"PalRUP rank {rank} produced outside its ID namespace")
            if directive.external_id <= previous_id:
                raise ProofWireError(f"PalRUP rank {rank} produced non-monotonic IDs")
            if any(hint >= directive.external_id for hint in directive.hints):
                raise ProofWireError(f"PalRUP rank {rank} contains a cyclic proof hint")
            previous_id = directive.external_id
            empty += int(not directive.literals)
        elif isinstance(directive, PalrupImport):
            imported += 1
            if directive.external_id % solvers == rank:
                raise ProofWireError(f"PalRUP rank {rank} imported its own ID namespace")
        else:
            assert isinstance(directive, PalrupDelete)
            deleted += len(directive.external_ids)
    if produced < 1:
        raise ProofWireError(f"PalRUP rank {rank} emitted no native proof clause")
    return {
        "rank": rank,
        "sha256": _digest(content),
        "bytes": len(content),
        "directive_count": len(directives),
        "produced": produced,
        "imported": imported,
        "deleted_ids": deleted,
        "empty_clauses": empty,
        "maximum_produced_id": previous_id,
    }


def _write_durable(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short native PalRUP receipt write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(str(path.parent))


def _validate_bundle_layout(root: Path, solvers: int, width: int) -> None:
    expected_shards = {str(rank // width) for rank in range(solvers)}
    expected_root = {_FORMULA_NAME, _RECEIPT_NAME, *expected_shards}
    if {entry.name for entry in root.iterdir()} != expected_root:
        raise ProofWireError("native PalRUP proof root layout changed")
    for shard_name in expected_shards:
        shard = root / shard_name
        if shard.is_symlink() or not shard.is_dir():
            raise ProofWireError("native PalRUP proof shard is not a real directory")
        expected_ranks = {
            str(rank) for rank in range(solvers) if rank // width == int(shard_name)
        }
        if {entry.name for entry in shard.iterdir()} != expected_ranks:
            raise ProofWireError("native PalRUP proof shard layout changed")
        for rank_name in expected_ranks:
            directory = shard / rank_name
            if directory.is_symlink() or not directory.is_dir():
                raise ProofWireError(
                    "native PalRUP rank artifact is not a real directory"
                )
            if {entry.name for entry in directory.iterdir()} != {"out.palrup"}:
                raise ProofWireError("native PalRUP rank artifact layout changed")


class NativePalrupProducer:
    """Produce and officially confirm a durable solver-native PalRUP bundle."""

    def __init__(
        self,
        library: str | os.PathLike[str],
        helper: str | os.PathLike[str],
        *,
        library_sha256: str,
        helper_sha256: str,
        timeout_ms: int = 300_000,
        max_parallel: int = 16,
        max_formula_bytes: int = MAX_PALRUP_FORMULA_BYTES,
        max_fragment_bytes: int = MAX_NATIVE_FRAGMENT_BYTES,
        maximum_shared_clause_length: int = 32,
        queue_capacity_clauses: int = 65_536,
    ) -> None:
        supplied_library = Path(library)
        supplied_helper = Path(helper)
        if supplied_library.is_symlink() or supplied_helper.is_symlink():
            raise ProofWireError("native PalRUP tools must not be symlinks")
        self.library_path = supplied_library.resolve(strict=True)
        self.helper_path = supplied_helper.resolve(strict=True)
        self.library_identity = _regular_identity(
            self.library_path, MAX_NATIVE_LIBRARY_BYTES, "native PalRUP library"
        )
        self.helper_identity = _regular_identity(
            self.helper_path, MAX_NATIVE_HELPER_BYTES, "native PalRUP helper"
        )
        if self.library_identity["sha256"] != _hex_digest(
            library_sha256, "native PalRUP library"
        ):
            raise ProofWireError("native PalRUP library identity differs from policy")
        if self.helper_identity["sha256"] != _hex_digest(
            helper_sha256, "native PalRUP helper"
        ):
            raise ProofWireError("native PalRUP helper identity differs from policy")
        self.timeout_ms = _integer(
            timeout_ms, "native PalRUP timeout", 1, MAX_NATIVE_TIMEOUT_MS
        )
        self.max_parallel = _integer(
            max_parallel, "native PalRUP parallel bound", 1, MAX_NATIVE_PARALLEL
        )
        self.max_formula_bytes = _integer(
            max_formula_bytes,
            "native PalRUP formula byte bound",
            1,
            MAX_PALRUP_FORMULA_BYTES,
        )
        self.max_fragment_bytes = _integer(
            max_fragment_bytes,
            "native PalRUP fragment byte bound",
            1,
            MAX_NATIVE_FRAGMENT_BYTES,
        )
        self.maximum_shared_clause_length = _integer(
            maximum_shared_clause_length,
            "native PalRUP shared clause length",
            1,
            MAX_NATIVE_SHARED_CLAUSE_LENGTH,
        )
        self.queue_capacity_clauses = _integer(
            queue_capacity_clauses,
            "native PalRUP queue clause capacity",
            1,
            MAX_NATIVE_QUEUE_CLAUSES,
        )
        self.policy: dict[str, Any] = {
            "schema": NATIVE_PALRUP_POLICY_SCHEMA,
            "protocol": NATIVE_PALRUP_PROTOCOL,
            "binary_protocol": PALRUP_BINARY_PROTOCOL,
            "native_abi_protocol": NATIVE_ABI_PROTOCOL,
            "producer_source_commit": NATIVE_PRODUCER_COMMIT,
            "library": self.library_identity,
            "helper": self.helper_identity,
            "timeout_ms": self.timeout_ms,
            "max_parallel": self.max_parallel,
            "max_formula_bytes": self.max_formula_bytes,
            "max_fragment_bytes": self.max_fragment_bytes,
            "maximum_shared_clause_length": self.maximum_shared_clause_length,
            "queue_capacity_clauses": self.queue_capacity_clauses,
            "worker_isolation": "one-native-pool-process-with-rank-threads",
            "clause_exchange": "bounded-id-preserving-in-memory-fanout",
            "id_namespace": "produced-id-mod-solver-count-equals-rank",
            "publication": "durable-renameat2-noreplace-after-official-confirm",
        }
        self.policy_sha256 = _digest(_canonical_json(self.policy))

    def _snapshot_tools(self, root: Path) -> tuple[Path, Path]:
        tool_root = root / "tools"
        tool_root.mkdir(mode=0o700)
        library = tool_root / "libsymcc_qfbv_palrup_pool.so"
        helper = tool_root / "qfbv_palrup_native_pool_worker.py"
        library_snapshot = _snapshot_path(
            self.library_path,
            library,
            maximum=MAX_NATIVE_LIBRARY_BYTES,
            label="native PalRUP library",
        )
        helper_snapshot = _snapshot_path(
            self.helper_path,
            helper,
            maximum=MAX_NATIVE_HELPER_BYTES,
            label="native PalRUP helper",
        )
        if (
            library_snapshot.metadata() != self.library_identity
            or helper_snapshot.metadata() != self.helper_identity
        ):
            raise ProofWireError("native PalRUP tool identity changed after initialization")
        return library, helper

    def _run_pool(
        self,
        *,
        helper: Path,
        library: Path,
        formula: Path,
        outputs: Sequence[Path],
        solvers: int,
        original_clauses: int,
        skipped_epochs: int,
    ) -> tuple[list[dict[str, Any]], int]:
        command = [
            sys.executable,
            "-I",
            str(helper),
            "--library",
            str(library),
            "--formula",
            str(formula),
        ]
        for output in outputs:
            command.extend(("--output", str(output)))
        command.extend(
            (
                "--solver-count",
                str(solvers),
                "--original-clause-count",
                str(original_clauses),
                "--skipped-epochs",
                str(skipped_epochs),
                "--timeout-ms",
                str(self.timeout_ms),
                "--maximum-shared-clause-length",
                str(self.maximum_shared_clause_length),
                "--queue-capacity-clauses",
                str(self.queue_capacity_clauses),
            )
        )
        process = _run_bounded(
            command, timeout_ms=self.timeout_ms + _WORKER_GRACE_MS
        )
        if process.returncode != 0 or process.stderr:
            raise ProofWireError("native PalRUP clause-sharing pool failed closed")
        workers = _parse_pool_result(
            process.stdout,
            solvers=solvers,
            skipped_epochs=skipped_epochs,
            original_clauses=original_clauses,
            maximum_shared_clause_length=self.maximum_shared_clause_length,
            queue_capacity_clauses=self.queue_capacity_clauses,
        )
        return workers, process.elapsed_us

    def produce(
        self,
        formula_path: str | os.PathLike[str],
        proof_root: str | os.PathLike[str],
        num_solvers: int,
        *,
        checker: PalrupGlobalChecker,
        skipped_epochs: int = 0,
    ) -> dict[str, Any]:
        """Publish one proof root only after authoritative global confirmation."""

        if not isinstance(checker, PalrupGlobalChecker):
            raise ProofWireError("native PalRUP publication requires PalrupGlobalChecker")
        solvers = _integer(
            num_solvers,
            "native PalRUP solver count",
            1,
            min(
                MAX_PALRUP_SOLVERS,
                MAX_NATIVE_POOL_SOLVERS,
                self.max_parallel,
            ),
        )
        epoch = _integer(
            skipped_epochs,
            "native PalRUP skipped epochs",
            0,
            2_000_000_000,
        )
        requested = Path(proof_root)
        if requested.name in {"", ".", ".."}:
            raise ProofWireError("native PalRUP proof root has an invalid name")
        requested.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = requested.parent.resolve(strict=True)
        target = parent / requested.name
        if target.exists() or target.is_symlink():
            raise ProofWireError("native PalRUP proof root already exists")

        work = Path(tempfile.mkdtemp(prefix=".symcc-palrup-native-", dir=parent))
        try:
            bundle = work / "bundle"
            bundle.mkdir(mode=0o700)
            formula = bundle / _FORMULA_NAME
            formula_snapshot = _snapshot_path(
                Path(formula_path),
                formula,
                maximum=self.max_formula_bytes,
                label="native PalRUP formula",
            )
            variables, original_clauses = _dimacs_header(formula)
            library, helper = self._snapshot_tools(work)
            width = math.isqrt(solvers)
            if width * width < solvers:
                width += 1
            temporary_outputs: list[Path] = []
            final_outputs: list[Path] = []
            for rank in range(solvers):
                directory = bundle / str(rank // width) / str(rank)
                directory.mkdir(mode=0o700, parents=True)
                temporary_outputs.append(directory / ".out.palrup.native")
                final_outputs.append(directory / "out.palrup")

            workers, pool_elapsed_us = self._run_pool(
                helper=helper,
                library=library,
                formula=formula,
                outputs=temporary_outputs,
                solvers=solvers,
                original_clauses=original_clauses,
                skipped_epochs=epoch,
            )
            fragments: list[dict[str, Any]] = []
            total_fragment_bytes = 0
            total_empty_clauses = 0
            for rank, temporary in enumerate(temporary_outputs):
                metadata = _fragment_metadata(
                    temporary,
                    rank=rank,
                    solvers=solvers,
                    maximum=self.max_fragment_bytes,
                )
                os.replace(temporary, final_outputs[rank])
                fsync_directory(str(final_outputs[rank].parent))
                total_fragment_bytes += metadata["bytes"]
                total_empty_clauses += metadata["empty_clauses"]
                statistics = workers[rank]["statistics"]
                if statistics["variables"] != variables:
                    raise ProofWireError(
                        f"PalRUP rank {rank} variable count differs from formula"
                    )
                if not (
                    statistics["imported"]
                    <= metadata["imported"]
                    <= statistics["imported"] + 1
                ):
                    raise ProofWireError(
                        f"PalRUP rank {rank} import telemetry differs from its fragment"
                    )
                if (
                    statistics["exported"]
                    > metadata["produced"] - metadata["empty_clauses"]
                ):
                    raise ProofWireError(
                        f"PalRUP rank {rank} exported unrecorded proof clauses"
                    )
                if (
                    statistics["delivered"] + statistics["dropped"]
                    != statistics["exported"] * (solvers - 1)
                ):
                    raise ProofWireError(
                        f"PalRUP rank {rank} clause fanout conservation failed"
                    )
                fragments.append(metadata)
            if total_empty_clauses < 1:
                raise ProofWireError("native PalRUP bundle contains no empty clause")

            official = checker.verify(formula, bundle, solvers)
            receipt: dict[str, Any] = {
                "schema": NATIVE_PALRUP_RECEIPT_SCHEMA,
                "protocol": NATIVE_PALRUP_PROTOCOL,
                "status": "solver-native-global-unsat-confirmed",
                "scope": "native-production-plus-official-palrup-confirmation",
                "policy": self.policy,
                "policy_sha256": self.policy_sha256,
                "formula": {
                    **formula_snapshot.metadata(),
                    "variables": variables,
                    "original_clauses": original_clauses,
                },
                "num_solvers": solvers,
                "matrix_width": width,
                "skipped_epochs": epoch,
                "workers": workers,
                "pool_elapsed_us": pool_elapsed_us,
                "clause_sharing_active": sum(
                    item["imported"] for item in fragments
                )
                > 0,
                "fragments": fragments,
                "total_fragment_bytes": total_fragment_bytes,
                "official_checker": official,
                "official_checker_policy_sha256": checker.policy_sha256,
            }
            receipt["receipt_sha256"] = _digest(_canonical_json(receipt))
            _write_durable(bundle / _RECEIPT_NAME, _canonical_json(receipt) + b"\n")
            fsync_directory(str(bundle))
            checked = self.validate_receipt(
                receipt, bundle, checker=checker, recheck=True
            )
            if checked != receipt:
                raise ProofWireError(
                    "native PalRUP staging recheck changed the receipt"
                )
            durable_rename_noreplace(str(bundle), str(target))
            return receipt
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def validate_receipt(
        self,
        receipt: Mapping[str, Any],
        proof_root: str | os.PathLike[str],
        *,
        checker: PalrupGlobalChecker,
        recheck: bool = True,
    ) -> dict[str, Any]:
        if type(recheck) is not bool or not isinstance(checker, PalrupGlobalChecker):
            raise ProofWireError("native PalRUP validation policy is invalid")
        if not isinstance(receipt, Mapping):
            raise ProofWireError("native PalRUP receipt must be an object")
        normalized = dict(receipt)
        digest = _hex_digest(
            normalized.pop("receipt_sha256", None), "native PalRUP receipt"
        )
        if _digest(_canonical_json(normalized)) != digest:
            raise ProofWireError("native PalRUP receipt identity changed")
        normalized["receipt_sha256"] = digest
        required = {
            "schema",
            "protocol",
            "status",
            "scope",
            "policy",
            "policy_sha256",
            "formula",
            "num_solvers",
            "matrix_width",
            "skipped_epochs",
            "workers",
            "pool_elapsed_us",
            "clause_sharing_active",
            "fragments",
            "total_fragment_bytes",
            "official_checker",
            "official_checker_policy_sha256",
            "receipt_sha256",
        }
        if set(normalized) != required or normalized.get("policy") != self.policy:
            raise ProofWireError("native PalRUP receipt fields or policy changed")
        if (
            normalized["schema"] != NATIVE_PALRUP_RECEIPT_SCHEMA
            or normalized["protocol"] != NATIVE_PALRUP_PROTOCOL
            or normalized["status"] != "solver-native-global-unsat-confirmed"
            or normalized["scope"] != "native-production-plus-official-palrup-confirmation"
            or normalized["policy_sha256"] != self.policy_sha256
            or normalized["official_checker_policy_sha256"] != checker.policy_sha256
        ):
            raise ProofWireError("native PalRUP receipt protocol changed")
        root = Path(proof_root)
        if root.is_symlink() or not root.is_dir():
            raise ProofWireError("native PalRUP proof root is not a real directory")
        persisted = _read_regular(root / _RECEIPT_NAME, MAX_WIRE_BYTES)
        if persisted != _canonical_json(normalized) + b"\n":
            raise ProofWireError("native PalRUP persisted receipt changed")
        formula = normalized["formula"]
        if not isinstance(formula, dict) or set(formula) != {
            "sha256",
            "bytes",
            "variables",
            "original_clauses",
        }:
            raise ProofWireError("native PalRUP formula receipt is invalid")
        _hex_digest(formula.get("sha256"), "native PalRUP formula")
        _bounded_int(formula.get("bytes"), "native PalRUP formula bytes", self.max_formula_bytes)
        _bounded_int(formula.get("variables"), "native PalRUP formula variables", (1 << 31) - 1)
        original_clauses = _bounded_int(
            formula.get("original_clauses"),
            "native PalRUP formula clauses",
            (1 << 31) - 1,
        )
        if original_clauses < 1:
            raise ProofWireError("native PalRUP formula clause count is empty")
        formula_content = _read_regular(root / _FORMULA_NAME, self.max_formula_bytes)
        if (
            _digest(formula_content) != formula.get("sha256")
            or len(formula_content) != formula.get("bytes")
        ):
            raise ProofWireError("native PalRUP formula artifact changed")
        observed_variables, observed_clauses = _dimacs_header(root / _FORMULA_NAME)
        if (
            observed_variables != formula["variables"]
            or observed_clauses != original_clauses
        ):
            raise ProofWireError("native PalRUP formula metadata changed")
        solvers = _integer(
            normalized["num_solvers"],
            "native PalRUP receipt solver count",
            1,
            min(
                MAX_PALRUP_SOLVERS,
                MAX_NATIVE_POOL_SOLVERS,
                self.max_parallel,
            ),
        )
        fragments = normalized["fragments"]
        if not isinstance(fragments, list) or len(fragments) != solvers:
            raise ProofWireError("native PalRUP fragment receipt set is incomplete")
        width = math.isqrt(solvers)
        if width * width < solvers:
            width += 1
        if normalized["matrix_width"] != width:
            raise ProofWireError("native PalRUP matrix width changed")
        _validate_bundle_layout(root, solvers, width)
        _bounded_int(
            normalized["skipped_epochs"],
            "native PalRUP receipt skipped epochs",
            2_000_000_000,
        )
        workers = normalized["workers"]
        if not isinstance(workers, list) or len(workers) != solvers:
            raise ProofWireError("native PalRUP worker receipt set is incomplete")
        statistic_names = {
            "variables",
            "original_clauses",
            "conflicts",
            "decisions",
            "propagations",
            "restarts",
            "imported",
            "discarded",
            "exported",
            "delivered",
            "dropped",
            "pending",
            "elapsed_us",
        }
        for rank, raw_worker in enumerate(workers):
            if (
                not isinstance(raw_worker, dict)
                or set(raw_worker) != {"rank", "native_status", "statistics"}
                or type(raw_worker["rank"]) is not int
                or raw_worker["rank"] != rank
                or type(raw_worker["native_status"]) is not int
                or raw_worker["native_status"] != 20
            ):
                raise ProofWireError(f"native PalRUP worker {rank} receipt changed")
            statistics = raw_worker["statistics"]
            if not isinstance(statistics, dict) or set(statistics) != statistic_names:
                raise ProofWireError(
                    f"native PalRUP worker {rank} statistics changed"
                )
            for name in statistic_names:
                _bounded_int(
                    statistics[name], f"native PalRUP worker {rank} {name}"
                )
            if statistics["original_clauses"] != original_clauses:
                raise ProofWireError(
                    f"native PalRUP worker {rank} formula identity changed"
                )
            if statistics["variables"] != formula["variables"]:
                raise ProofWireError(
                    f"native PalRUP worker {rank} variable count changed"
                )
            if statistics["pending"] > self.queue_capacity_clauses:
                raise ProofWireError(
                    f"native PalRUP worker {rank} pending queue exceeds capacity"
                )
            if (
                statistics["delivered"] + statistics["dropped"]
                != statistics["exported"] * (solvers - 1)
            ):
                raise ProofWireError(
                    f"native PalRUP worker {rank} fanout conservation changed"
                )
        _bounded_int(
            normalized["pool_elapsed_us"], "native PalRUP pool elapsed time"
        )
        if type(normalized["clause_sharing_active"]) is not bool:
            raise ProofWireError("native PalRUP clause-sharing flag is invalid")
        observed = [
            _fragment_metadata(
                root / str(rank // width) / str(rank) / "out.palrup",
                rank=rank,
                solvers=solvers,
                maximum=self.max_fragment_bytes,
            )
            for rank in range(solvers)
        ]
        total_fragment_bytes = _bounded_int(
            normalized["total_fragment_bytes"],
            "native PalRUP total fragment bytes",
            MAX_NATIVE_FRAGMENT_BYTES * solvers,
        )
        for rank, metadata in enumerate(observed):
            statistics = workers[rank]["statistics"]
            if (
                not (
                    statistics["imported"]
                    <= metadata["imported"]
                    <= statistics["imported"] + 1
                )
                or statistics["exported"]
                > metadata["produced"] - metadata["empty_clauses"]
            ):
                raise ProofWireError(
                    f"native PalRUP rank {rank} exchange telemetry changed"
                )
        sharing_active = sum(item["imported"] for item in observed) > 0
        if normalized["clause_sharing_active"] != sharing_active:
            raise ProofWireError("native PalRUP clause-sharing evidence changed")
        if observed != fragments or sum(item["bytes"] for item in observed) != total_fragment_bytes:
            raise ProofWireError("native PalRUP fragment artifacts changed")
        checked = checker.validate_receipt(
            normalized["official_checker"],
            formula_path=root / _FORMULA_NAME,
            proof_root=root,
            recheck=recheck,
        )
        if checked != normalized["official_checker"]:
            raise ProofWireError("native PalRUP official checker receipt changed")
        return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula", type=Path, required=True)
    parser.add_argument("--proof-root", type=Path, required=True)
    parser.add_argument("--num-solvers", type=int, required=True)
    parser.add_argument("--skipped-epochs", type=int, default=0)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--library-sha256", required=True)
    parser.add_argument(
        "--helper",
        type=Path,
        default=Path(__file__).with_name("qfbv_palrup_native_pool_worker.py"),
    )
    parser.add_argument("--helper-sha256", required=True)
    parser.add_argument("--local-check", type=Path, required=True)
    parser.add_argument("--redistribute", type=Path, required=True)
    parser.add_argument("--confirm", type=Path, required=True)
    parser.add_argument("--local-check-sha256", required=True)
    parser.add_argument("--redistribute-sha256", required=True)
    parser.add_argument("--confirm-sha256", required=True)
    parser.add_argument("--producer-timeout-ms", type=int, default=300_000)
    parser.add_argument("--checker-timeout-ms", type=int, default=300_000)
    parser.add_argument("--max-parallel", type=int, default=16)
    parser.add_argument("--maximum-shared-clause-length", type=int, default=32)
    parser.add_argument("--queue-capacity-clauses", type=int, default=65_536)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    checker = PalrupGlobalChecker(
        arguments.local_check,
        arguments.redistribute,
        arguments.confirm,
        local_checker_sha256=arguments.local_check_sha256,
        redistribute_sha256=arguments.redistribute_sha256,
        confirm_sha256=arguments.confirm_sha256,
        timeout_ms=arguments.checker_timeout_ms,
        max_parallel=arguments.max_parallel,
    )
    producer = NativePalrupProducer(
        arguments.library,
        arguments.helper,
        library_sha256=arguments.library_sha256,
        helper_sha256=arguments.helper_sha256,
        timeout_ms=arguments.producer_timeout_ms,
        max_parallel=arguments.max_parallel,
        maximum_shared_clause_length=arguments.maximum_shared_clause_length,
        queue_capacity_clauses=arguments.queue_capacity_clauses,
    )
    receipt = producer.produce(
        arguments.formula,
        arguments.proof_root,
        arguments.num_solvers,
        checker=checker,
        skipped_epochs=arguments.skipped_epochs,
    )
    print(json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
