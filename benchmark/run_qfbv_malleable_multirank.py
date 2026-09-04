#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Run F438 replay and operation-ownership attestation on physical MPI ranks."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

from mpi4py import MPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_malleable_evaluation import (  # noqa: E402
    MALLEABLE_MPI_PROTOCOL,
    MALLEABLE_MPI_SCHEMA,
    MalleableEvaluationConfig,
    build_malleable_trial,
    canonical_json,
    content_digest,
    malleable_rank_report,
    verify_malleable_mpi_result,
)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0xF438)
    parser.add_argument("--backlog-per-slot", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    communicator = MPI.COMM_WORLD
    rank = communicator.Get_rank()
    world_size = communicator.Get_size()
    trial = None
    if rank == 0:
        trial = build_malleable_trial(
            MalleableEvaluationConfig(
                world_size=world_size,
                epochs=args.epochs,
                seed=args.seed,
                jobs=args.jobs,
                backlog_per_slot=args.backlog_per_slot,
            )
        )
    encoded = communicator.bcast(
        canonical_json(trial) if rank == 0 else None,
        root=0,
    )
    local_trial = json.loads(encoded.decode("ascii"))
    report = malleable_rank_report(local_trial, rank, socket.gethostname())
    reports = communicator.gather(report, root=0)
    if rank != 0:
        return 0
    assert trial is not None and reports is not None
    body = {
        "schema": MALLEABLE_MPI_SCHEMA,
        "protocol": MALLEABLE_MPI_PROTOCOL,
        "physical_world_size": world_size,
        "mpi_library_version": MPI.Get_library_version().strip()[:4096],
        "trial": trial,
        "rank_reports": reports,
        "worker_operations_attested": sum(
            len(row["worker_operation_sha256"]) for row in reports
        ),
        "claim_boundary": (
            "same-run physical MPI rank membership plus deterministic protocol "
            "replay and operation-ownership attestation; no dynamic MPI spawn, "
            "multi-node speedup, coverage, or defect-yield claim"
        ),
    }
    body["artifact_sha256"] = content_digest(body)
    verified = verify_malleable_mpi_result(body)
    _atomic_json(args.output, verified)
    print(
        json.dumps(
            {
                "status": "pass",
                "artifact_sha256": verified["artifact_sha256"],
                "physical_world_size": world_size,
                "worker_operations_attested": verified["worker_operations_attested"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
