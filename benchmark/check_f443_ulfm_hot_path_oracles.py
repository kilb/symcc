#!/usr/bin/env python3
"""Verify production hot-path ULFM recovery artifacts for F443."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from mpi_ulfm_recovery import (  # noqa: E402
    UlfmRecoveryError,
    content_digest,
    verify_recovery_receipt,
    verify_recovery_snapshot,
)


SCHEMA = "symcc-f443-ulfm-hot-path-oracle-v1"
_HASH_NAME = re.compile(r"[0-9a-f]{64}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_campaign_oracle(
    campaign_output: Path,
    log_path: Path,
    expected_failed_rank: int,
) -> dict[str, Any]:
    expected_endpoint = f"rank-{int(expected_failed_rank)}"
    retired = sorted(campaign_output.glob(".retired-standalone-work-*"))
    active = sorted(campaign_output.glob(".standalone-work-*"))
    if len(retired) != 1 or active:
        raise UlfmRecoveryError(
            "F443 campaign did not durably retire exactly one completed epoch"
        )
    retired_root = retired[0]
    databases = tuple(sorted(set(
        retired_root.glob("ulfm-query-store/master-0/index.sqlite3")
    ) | set(
        retired_root.glob(
            "ulfm-query-store/master-0/generation-*/index.sqlite3"
        )
    )))
    if len(databases) != 1:
        raise UlfmRecoveryError("F443 campaign lacks one durable QueryStore")

    database = databases[0]
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        snapshots = connection.execute(
            "SELECT generation, recovery_count, state_ordinal, pending, "
            "snapshot_json FROM ulfm_recovery_snapshots"
        ).fetchall()
        receipts = connection.execute(
            "SELECT receipt_json FROM ulfm_recovery_receipts "
            "ORDER BY target_generation"
        ).fetchall()
    finally:
        connection.close()
    if len(snapshots) != 1 or len(receipts) != 1:
        raise UlfmRecoveryError("F443 durable recovery cardinality changed")
    row = snapshots[0]
    snapshot = verify_recovery_snapshot(json.loads(str(row["snapshot_json"])))
    receipt = verify_recovery_receipt(
        json.loads(str(receipts[0]["receipt_json"]))
    )
    final_assignments = {
        shard_id: {
            "owner_endpoint": shard["owner_endpoint"],
            "shard_token": shard["shard_token"],
        }
        for shard_id, shard in sorted(snapshot["shards"].items())
    }
    active_work = sum(
        bool(shard["active_work_id"]) for shard in snapshot["shards"].values()
    )
    completed_work = sum(
        int(shard["completed"]) for shard in snapshot["shards"].values()
    )
    if (
        int(row["generation"]) != 1
        or int(row["recovery_count"]) != 1
        or bool(row["pending"])
        or int(row["state_ordinal"]) < 4
        or receipt["failed_endpoints"] != [expected_endpoint]
        or receipt["target_generation"] != snapshot["generation"]
        or receipt["new_generation_token"] != snapshot["generation_token"]
        or receipt["new_members"] != snapshot["members"]
        or receipt["shard_assignment_sha256"] != content_digest(
            final_assignments
        )
        or snapshot["recovery_queue"]
        or active_work
        or completed_work < 1
    ):
        raise UlfmRecoveryError("F443 recovered controller did not drain cleanly")

    marker = retired_root / f".ulfm-test-failure-rank-{expected_failed_rank}"
    if marker.read_bytes() != b"claimed\n":
        raise UlfmRecoveryError("F443 one-shot failure marker is invalid")
    public_objects = sorted(
        path for path in campaign_output.iterdir()
        if path.is_file() and _HASH_NAME.fullmatch(path.name)
    )
    if len(public_objects) < 2:
        raise UlfmRecoveryError("F443 recovered campaign produced too few objects")
    for path in public_objects:
        if _file_sha256(path) != path.name:
            raise UlfmRecoveryError("F443 public corpus content hash changed")
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    recovery_line = (
        "ULFM recovery committed: generation=1 "
        f"failed=['{expected_endpoint}']"
    )
    if recovery_line not in log_text or "=== Final Statistics ===" not in log_text:
        raise UlfmRecoveryError("F443 survivor completion evidence is missing")

    body = {
        "schema": SCHEMA,
        "expected_failed_endpoint": expected_endpoint,
        "generation": int(row["generation"]),
        "recovery_count": int(row["recovery_count"]),
        "state_ordinal": int(row["state_ordinal"]),
        "survivor_endpoints": sorted(snapshot["members"]),
        "requeued_at_repair": len(receipt["requeued_work"]),
        "final_recovery_queue": len(snapshot["recovery_queue"]),
        "final_active_work": active_work,
        "completed_work": completed_work,
        "public_objects": len(public_objects),
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "database_sha256": _file_sha256(database),
        "log_sha256": _file_sha256(log_path),
        "claim_boundary": (
            "same-host physical process-failure gate under pinned Open MPI ULFM; "
            "proves the production driver repairs, rebinds, replays, drains, and "
            "retires one campaign; it is not a multi-node performance result"
        ),
    }
    body["result_sha256"] = content_digest(body)
    return verify_campaign_oracle(body)


def verify_campaign_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("F443 oracle must be an object")
    body = dict(raw)
    supplied = body.pop("result_sha256", "")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("F443 oracle identity changed")
    if (
        body.get("schema") != SCHEMA
        or not isinstance(body.get("expected_failed_endpoint"), str)
        or body.get("generation") != 1
        or body.get("recovery_count") != 1
        or body.get("final_recovery_queue") != 0
        or body.get("final_active_work") != 0
        or not isinstance(body.get("completed_work"), int)
        or body["completed_work"] < 1
        or not isinstance(body.get("public_objects"), int)
        or body["public_objects"] < 2
    ):
        raise UlfmRecoveryError("F443 oracle invariants changed")
    body["result_sha256"] = supplied
    return body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-output", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--expected-failed-rank", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        result = verify_campaign_oracle(
            json.loads(args.verify.read_text(encoding="ascii"))
        )
        print(json.dumps(result, sort_keys=True))
        return
    if any(
        value is None
        for value in (
            args.campaign_output,
            args.log,
            args.expected_failed_rank,
            args.output,
        )
    ):
        parser.error("campaign build mode requires all campaign arguments")
    result = build_campaign_oracle(
        args.campaign_output, args.log, args.expected_failed_rank
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )


if __name__ == "__main__":
    main()
