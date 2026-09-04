#!/usr/bin/env python3
"""Build or replay a checked proof-prefix QF_BV partition certificate."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbv_artifact_lifecycle import ArtifactLifecycleRegistry
from qfbv_incremental_proof import IncrementalProofChecker, IncrementalProofStore
from qfbv_incremental_sat import bitblast_qfbv_query
from qfbv_proof_prefix_partition import (
    MAX_ACTIVITY_RECEIPTS,
    MAX_RECORD_BYTES,
    ProofPrefixPartitionError,
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
    partition_job_catalog,
)
from query_store import QueryStore


CLI_SCHEMA = "symcc-qfbv-proof-prefix-partition-cli-result-v1"


def _reject_duplicate_members(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProofPrefixPartitionError(
                "partition activity input contains duplicate JSON members"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProofPrefixPartitionError(
        f"partition activity input contains non-finite constant {value}"
    )


def _load_bounded_json(path: Path) -> Mapping[str, Any]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for partition activity input")
    descriptor = os.open(
        path,
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
            raise ProofPrefixPartitionError(
                "partition activity input is not a bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                raise ProofPrefixPartitionError(
                    "partition activity input was truncated"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProofPrefixPartitionError(
                "partition activity input grew while reading"
            )
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProofPrefixPartitionError(
                "partition activity input identity changed"
            )
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProofPrefixPartitionError(
            "partition activity input is not strict JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise ProofPrefixPartitionError(
            "partition activity input must be a JSON object"
        )
    return value


def _activity_evidence(
    result: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
    raw_receipts = result.get(
        "backend_realtime_clause_activity_receipts", []
    )
    raw_acks = result.get("backend_realtime_import_acks", [])
    if (
        not isinstance(raw_receipts, list)
        or not isinstance(raw_acks, list)
        or len(raw_receipts) > MAX_ACTIVITY_RECEIPTS
        or len(raw_acks) > MAX_ACTIVITY_RECEIPTS
    ):
        raise ProofPrefixPartitionError(
            "partition activity result exceeds its evidence bound"
        )
    acks: dict[str, Mapping[str, Any]] = {}
    for raw_ack in raw_acks:
        if not isinstance(raw_ack, Mapping):
            raise ProofPrefixPartitionError("partition import ACK is invalid")
        digest = raw_ack.get("ack_sha256")
        if not isinstance(digest, str) or digest in acks:
            raise ProofPrefixPartitionError(
                "partition import ACK identity is invalid or duplicated"
            )
        acks[digest] = raw_ack
    evidence: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    seen: set[str] = set()
    for raw_receipt in raw_receipts:
        if not isinstance(raw_receipt, Mapping):
            raise ProofPrefixPartitionError(
                "partition activity receipt is invalid"
            )
        identity = raw_receipt.get("activity_sha256")
        ack_identity = raw_receipt.get("ack_sha256")
        if not isinstance(identity, str) or identity in seen:
            raise ProofPrefixPartitionError(
                "partition activity receipt identity is invalid or duplicated"
            )
        if not isinstance(ack_identity, str) or ack_identity not in acks:
            raise ProofPrefixPartitionError(
                "partition activity receipt has no matching checked ACK"
            )
        seen.add(identity)
        evidence.append((raw_receipt, acks[ack_identity]))
    return tuple(evidence)


def _write_output(path: Path | None, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if len(encoded) > MAX_RECORD_BYTES:
        raise ProofPrefixPartitionError("partition CLI result exceeds its byte bound")
    if path is None:
        sys.stdout.buffer.write(encoded)
        return
    if path.is_symlink():
        raise ProofPrefixPartitionError("partition output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short partition CLI output write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query-store", required=True, type=Path)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--proof-store", required=True, type=Path)
    parser.add_argument("--partition-store", required=True, type=Path)
    parser.add_argument("--lifecycle-store", type=Path)
    parser.add_argument("--activity-result", type=Path)
    parser.add_argument("--verify-digest")
    parser.add_argument("--cubes", type=int, default=8)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--max-activity-receipts", type=int, default=4096)
    parser.add_argument(
        "--input-variables-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--static-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--backlog-per-cube", type=int, default=1)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    query_store = QueryStore(args.query_store)
    loaded = query_store.load_query_ir(args.query_id)
    if loaded is None:
        raise ProofPrefixPartitionError("Query IR is unavailable or invalid")
    plan = bitblast_qfbv_query(args.query_id, loaded[0], loaded[1])
    lifecycle = (
        ArtifactLifecycleRegistry(args.lifecycle_store)
        if args.lifecycle_store is not None
        else None
    )
    proof_store = IncrementalProofStore(
        args.proof_store,
        lifecycle=lifecycle,
    )
    checker = IncrementalProofChecker(proof_store)
    partitions = ProofPrefixPartitionStore(
        args.partition_store,
        lifecycle=lifecycle,
    )
    created = False
    mode = "verified"
    if args.verify_digest:
        certificate = partitions.load(
            plan, args.verify_digest, checker=checker
        )
    else:
        result = (
            _load_bounded_json(args.activity_result)
            if args.activity_result is not None
            else {}
        )
        evidence = _activity_evidence(result)
        policy = ProofPrefixPartitionPolicy(
            cube_count=args.cubes,
            max_depth=args.max_depth,
            max_activity_receipts=args.max_activity_receipts,
            input_variables_only=args.input_variables_only,
            allow_static_fallback=args.static_fallback,
        )
        certificate = build_proof_prefix_partition(
            plan,
            policy,
            activity_evidence=evidence,
            checker=checker,
        )
        digest, created = partitions.publish(
            plan, certificate, checker=checker
        )
        if digest != certificate["partition_sha256"]:
            raise ProofPrefixPartitionError(
                "partition publication returned a different identity"
            )
        mode = "generated"
    catalog = partition_job_catalog(
        plan,
        certificate,
        checker=checker,
        backlog_per_cube=args.backlog_per_cube,
    )
    jobs = []
    for (job_id, (family, backlog)), cube in zip(
        catalog.items(), certificate["cubes"]
    ):
        jobs.append(
            {
                "job_id": job_id,
                "formula_family_sha256": family,
                "backlog": backlog,
                "cube_sha256": cube["cube_sha256"],
                "assumptions": cube["assumptions"],
                "assumption_sha256": cube["assumption_sha256"],
            }
        )
    payload = {
        "schema": CLI_SCHEMA,
        "mode": mode,
        "query_id": args.query_id,
        "formula_sha256": plan.formula_sha256,
        "partition_sha256": certificate["partition_sha256"],
        "created": created,
        "selection_source": certificate["selection_source"],
        "cube_count": len(certificate["cubes"]),
        "jobs": jobs,
        "certificate": certificate,
    }
    _write_output(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
