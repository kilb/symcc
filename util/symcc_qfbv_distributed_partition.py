#!/usr/bin/env python3
"""Operate one F451 generation-fenced distributed QF_BV partition."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from mpi_ulfm_recovery import (
    DurableUlfmCoordinator,
    EndpointIdentity,
    RecoveryShard,
    UlfmRecoveryController,
    UlfmRecoveryPolicy,
    content_digest,
)
from qfbv_distributed_partition_execution import (
    DistributedCubeBindingStore,
    DistributedPartitionCoordinator,
    distributed_run_id,
)
from qfbv_incremental_proof import IncrementalProofChecker, IncrementalProofStore
from qfbv_incremental_sat import bitblast_qfbv_query
from qfbv_partition_execution import PartitionExecutionPolicy, PartitionExecutionStore
from qfbv_proof_prefix_partition import (
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
)
from query_store import QueryStore


CLI_SCHEMA = "symcc-qfbv-distributed-partition-cli-v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024


def _reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("distributed CLI JSON contains duplicate members")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"distributed CLI JSON contains {value}")


def _load_json(path: Path) -> Any:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is required for distributed CLI input")
    descriptor = os.open(
        path,
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise ValueError("distributed CLI input is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                raise ValueError("distributed CLI input was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("distributed CLI input grew while reading")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("distributed CLI input identity changed")
    finally:
        os.close(descriptor)
    try:
        return json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("distributed CLI input is not strict JSON") from error


def _endpoints(path: Path) -> list[EndpointIdentity]:
    raw = _load_json(path)
    if not isinstance(raw, list) or not 2 <= len(raw) <= 4096:
        raise ValueError("endpoint inventory must contain 2..4096 entries")
    endpoints = [EndpointIdentity.from_mapping(item) for item in raw]
    if sorted(item.initial_rank for item in endpoints) != list(range(len(endpoints))):
        raise ValueError("endpoint ranks must be dense")
    if len({item.endpoint_id for item in endpoints}) != len(endpoints):
        raise ValueError("endpoint identities must be unique")
    return sorted(endpoints, key=lambda item: item.initial_rank)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "claim",
            "heartbeat",
            "complete",
            "recover",
            "reconcile",
            "finalize",
            "stats",
        ),
    )
    parser.add_argument("--query-store", required=True, type=Path)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--proof-store", required=True, type=Path)
    parser.add_argument("--partition-store", required=True, type=Path)
    parser.add_argument("--execution-store", required=True, type=Path)
    parser.add_argument("--binding-store", required=True, type=Path)
    parser.add_argument("--endpoints", required=True, type=Path)
    parser.add_argument("--cubes", required=True, type=int)
    parser.add_argument("--parallelism", type=int)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--cube-timeout-ms", type=int, default=30_000)
    parser.add_argument("--task-lease-ms", type=int, default=60_000)
    parser.add_argument("--endpoint")
    parser.add_argument("--lease", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--failed-endpoint", action="append", default=[])
    parser.add_argument("--attestations", type=Path)
    parser.add_argument("--input-hex", required=True)
    return parser


def _coordinator(args: argparse.Namespace) -> DistributedPartitionCoordinator:
    endpoints = _endpoints(args.endpoints)
    if not 2 <= args.cubes <= 4096:
        raise ValueError("--cubes must be in [2, 4096]")
    parallelism = args.parallelism or min(args.cubes, len(endpoints))
    policy = PartitionExecutionPolicy(
        parallelism=parallelism,
        max_attempts=args.max_attempts,
        cube_timeout_ms=args.cube_timeout_ms,
        task_lease_ms=args.task_lease_ms,
    )
    query_store = QueryStore(args.query_store)
    loaded = query_store.load_query_ir(args.query_id)
    if loaded is None:
        raise ValueError("Query IR is unavailable")
    plan = bitblast_qfbv_query(args.query_id, loaded[0], loaded[1])
    proof_store = IncrementalProofStore(args.proof_store)
    checker = IncrementalProofChecker(proof_store)
    certificate = build_proof_prefix_partition(
        plan,
        ProofPrefixPartitionPolicy(
            cube_count=args.cubes,
            max_depth=max(1, (args.cubes - 1).bit_length()),
        ),
    )
    ProofPrefixPartitionStore(args.partition_store).publish(
        plan, certificate, checker=checker
    )
    partition_store = PartitionExecutionStore(args.execution_store)
    execution = partition_store.create(
        plan, certificate, policy, checker=checker
    )
    run_id = distributed_run_id(execution)
    recovery_policy = UlfmRecoveryPolicy(
        max_endpoints=max(2, len(endpoints)),
        max_shards=max(2, len(endpoints)),
        max_recovery_queue=max(2, len(endpoints)),
    )
    existing = query_store.load_ulfm_recovery_snapshot(run_id, recovery_policy)
    if existing is None:
        controller = UlfmRecoveryController(
            run_id,
            endpoints,
            [
                RecoveryShard(
                    shard_id=f"cube-slot-{index}",
                    owner_endpoint=endpoints[index].endpoint_id,
                    checkpoint_sha256=content_digest(
                        {
                            "protocol": CLI_SCHEMA,
                            "execution_sha256": execution,
                            "shard": index,
                        }
                    ),
                )
                for index in range(len(endpoints))
            ],
            recovery_policy,
        )
        durable = DurableUlfmCoordinator(controller, query_store)
    else:
        durable = DurableUlfmCoordinator.restore(
            run_id, recovery_policy, query_store
        )
    return DistributedPartitionCoordinator(
        plan,
        certificate,
        policy,
        partition_store,
        DistributedCubeBindingStore(args.binding_store),
        durable,
        proof_store,
        checker,
        candidate_validator=lambda candidate: query_store.validate_candidate(
            args.query_id, candidate
        ),
        input_hex=args.input_hex,
    )


def _emit(command: str, coordinator: DistributedPartitionCoordinator, value: Any) -> None:
    payload = {
        "schema": CLI_SCHEMA,
        "command": command,
        "execution_sha256": coordinator.execution_sha256,
        "value": value,
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    coordinator = _coordinator(args)
    if args.command == "init":
        value: Any = coordinator.stats()
    elif args.command == "claim":
        if not args.endpoint:
            raise ValueError("claim requires --endpoint")
        value = coordinator.claim(args.endpoint)
    elif args.command == "heartbeat":
        if args.lease is None:
            raise ValueError("heartbeat requires --lease")
        value = coordinator.heartbeat(_load_json(args.lease))
    elif args.command == "complete":
        if args.lease is None or args.result is None:
            raise ValueError("complete requires --lease and --result")
        value = coordinator.complete(
            _load_json(args.lease), _load_json(args.result)
        )
    elif args.command == "recover":
        if not args.failed_endpoint or args.attestations is None:
            raise ValueError(
                "recover requires --failed-endpoint and --attestations"
            )
        attestations = _load_json(args.attestations)
        if not isinstance(attestations, list):
            raise ValueError("recovery attestations must be a list")
        value = coordinator.recover(args.failed_endpoint, attestations)
    elif args.command == "reconcile":
        value = {
            "resumed": coordinator.resume_recovery_queue(),
            "reconciled": coordinator.reconcile(),
        }
    elif args.command == "finalize":
        value = coordinator.finalize_if_ready()
    else:
        value = coordinator.stats()
    _emit(args.command, coordinator, value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"symcc-qfbv-distributed-partition: {error}", file=sys.stderr)
        raise SystemExit(2) from error
