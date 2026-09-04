#!/usr/bin/env python3
"""Verify multi-master, warm-spare, and continuous ULFM campaign evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from mpi_ulfm_recovery import (  # noqa: E402
    UlfmRecoveryError,
    content_digest,
    verify_recovery_receipt,
    verify_recovery_snapshot,
)


SCHEMA = "symcc-f446-elastic-ulfm-oracle-v1"
_HASH_NAME = re.compile(r"[0-9a-f]{64}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _statistic(log: str, label: str) -> int:
    matches = re.findall(rf"{re.escape(label)}:\s*(\d+)", log)
    if len(matches) != 1:
        raise UlfmRecoveryError(f"F446 log lacks one {label} statistic")
    return int(matches[0])


def _verified_layout(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(raw, dict):
        raise UlfmRecoveryError("F446 generation layout is not an object")
    body = dict(raw)
    supplied = body.pop("manifest_sha256", "")
    if (
        body.get("schema") != "symcc-ulfm-generation-layout-v1"
        or content_digest(body) != supplied
    ):
        raise UlfmRecoveryError("F446 generation layout seal changed")
    body["manifest_sha256"] = supplied
    active = body.get("active_endpoints")
    standby = body.get("standby_endpoints")
    masters = body.get("masters")
    if (
        not isinstance(active, list)
        or not isinstance(standby, list)
        or not isinstance(masters, list)
        or any(type(endpoint) is not int for endpoint in active + standby)
        or len(set(active + standby)) != len(active + standby)
    ):
        raise UlfmRecoveryError("F446 generation role inventory is malformed")
    assigned: list[int] = []
    master_endpoints: list[int] = []
    for master in masters:
        if (
            not isinstance(master, dict)
            or type(master.get("transport_rank")) is not int
            or type(master.get("stable_endpoint")) is not int
            or not isinstance(master.get("workers"), list)
            or any(type(worker) is not int for worker in master["workers"])
        ):
            raise UlfmRecoveryError("F446 master group is malformed")
        master_endpoints.append(master["stable_endpoint"])
        assigned.extend(master["workers"])
    if sorted(master_endpoints + assigned) != sorted(active):
        raise UlfmRecoveryError("F446 active endpoints are not partitioned")
    return body


def _sqlite_recovery_state(
    database: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT generation, recovery_count, pending, snapshot_json "
            "FROM ulfm_recovery_snapshots"
        ).fetchall()
        receipt_rows = connection.execute(
            "SELECT receipt_json FROM ulfm_recovery_receipts "
            "ORDER BY target_generation"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise UlfmRecoveryError("F446 global membership cardinality changed")
    row = rows[0]
    snapshot = verify_recovery_snapshot(json.loads(str(row["snapshot_json"])))
    receipts = [
        verify_recovery_receipt(json.loads(str(item["receipt_json"])))
        for item in receipt_rows
    ]
    if (
        bool(row["pending"])
        or int(row["generation"]) != snapshot["generation"]
        or int(row["recovery_count"]) != len(receipts)
        or snapshot["recovery_queue"]
    ):
        raise UlfmRecoveryError("F446 global recovery state did not drain")
    return snapshot, receipts, int(row["recovery_count"])


def _atomic_recovery_state(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    raw = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(raw, dict):
        raise UlfmRecoveryError("F446 atomic recovery state is not an object")
    body = dict(raw)
    supplied = body.pop("envelope_sha256", "")
    if (
        body.get("schema") != "symcc-ulfm-atomic-snapshot-store-v1"
        or content_digest(body) != supplied
        or type(body.get("state_ordinal")) is not int
        or body["state_ordinal"] < 0
        or not isinstance(body.get("receipts"), list)
    ):
        raise UlfmRecoveryError("F446 atomic recovery envelope changed")
    snapshot = verify_recovery_snapshot(body.get("snapshot"))
    receipts = [verify_recovery_receipt(item) for item in body["receipts"]]
    if (
        body.get("run_id") != snapshot["run_id"]
        or body.get("policy_sha256") != snapshot["policy_sha256"]
        or snapshot["pending_recovery"] is not None
        or snapshot["recovery_count"] != len(receipts)
        or snapshot["recovery_queue"]
        or [item["target_generation"] for item in receipts]
        != list(range(1, len(receipts) + 1))
    ):
        raise UlfmRecoveryError("F446 atomic recovery state did not drain")
    return snapshot, receipts, len(receipts)


def _global_recovery_state(
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, Path, str]:
    atomic = root / "recovery-state.json"
    database = root / "index.sqlite3"
    if atomic.is_file():
        snapshot, receipts, count = _atomic_recovery_state(atomic)
        return snapshot, receipts, count, atomic, "atomic-json"
    if database.is_file():
        snapshot, receipts, count = _sqlite_recovery_state(database)
        return snapshot, receipts, count, database, "sqlite-wal"
    raise UlfmRecoveryError("F446 global recovery artifact is missing")


def _final_group_state(retired: Path, generation: int) -> dict[str, int]:
    controllers = 0
    completed = 0
    active = 0
    queued = 0
    databases = set(retired.glob("ulfm-query-store/master-*/index.sqlite3"))
    databases.update(
        retired.glob("ulfm-query-store/master-*/generation-*/index.sqlite3")
    )
    for database in sorted(databases):
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT run_id, pending, snapshot_json "
                "FROM ulfm_recovery_snapshots"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if f"-generation-{generation}-master-" not in str(row["run_id"]):
                continue
            snapshot = verify_recovery_snapshot(
                json.loads(str(row["snapshot_json"]))
            )
            controllers += 1
            if bool(row["pending"]):
                raise UlfmRecoveryError("F446 final group has pending repair")
            queued += len(snapshot["recovery_queue"])
            active += sum(
                bool(shard["active_work_id"])
                for shard in snapshot["shards"].values()
            )
            completed += sum(
                int(shard["completed"])
                for shard in snapshot["shards"].values()
            )
    paths = set(
        retired.glob("ulfm-query-store/master-*/recovery-state.json")
    )
    paths.update(
        retired.glob(
            "ulfm-query-store/master-*/generation-*/recovery-state.json"
        )
    )
    for path in sorted(paths):
        snapshot, _receipts, _count = _atomic_recovery_state(path)
        if f"-generation-{generation}-master-" not in snapshot["run_id"]:
            continue
        controllers += 1
        queued += len(snapshot["recovery_queue"])
        active += sum(
            bool(shard["active_work_id"])
            for shard in snapshot["shards"].values()
        )
        completed += sum(
            int(shard["completed"])
            for shard in snapshot["shards"].values()
        )
    if controllers < 1 or active or queued or completed < 1:
        raise UlfmRecoveryError("F446 final generation controllers did not drain")
    return {
        "controllers": controllers,
        "completed": completed,
        "active": active,
        "queued": queued,
    }


def _work_wal_statistics(retired: Path) -> dict[str, int]:
    statuses: dict[str, int] = {}
    analyzed = 0
    generated = 0
    for path in retired.glob("[0-9][0-9][0-9]/*.json"):
        record = json.loads(path.read_text(encoding="ascii"))
        status = record.get("status")
        if not isinstance(status, str):
            raise UlfmRecoveryError("F446 WAL status is malformed")
        statuses[status] = statuses.get(status, 0) + 1
        if status != "done":
            continue
        commit = record.get("commit")
        if (
            not isinstance(commit, dict)
            or commit.get("schema") != "symcc-standalone-result-commit-v1"
            or type(commit.get("num_generated")) is not int
            or commit["num_generated"] < 0
        ):
            raise UlfmRecoveryError("F446 completed WAL manifest is malformed")
        analyzed += 1
        generated += commit["num_generated"]
    if statuses.get("committing", 0) or analyzed < 1:
        raise UlfmRecoveryError("F446 WAL did not cross a clean commit boundary")
    return {
        "analyzed": analyzed,
        "generated": generated,
        "leased_at_deadline": statuses.get("leased", 0),
        "done": statuses.get("done", 0),
    }


def build_campaign_oracle(
    campaign_output: Path,
    log_path: Path,
    failed_ranks: list[int],
    warm_spares: int,
    minimum_hosts: int,
) -> dict[str, Any]:
    retired_roots = sorted(campaign_output.glob(".retired-standalone-work-*"))
    if len(retired_roots) != 1 or list(campaign_output.glob(".standalone-work-*")):
        raise UlfmRecoveryError("F446 epoch did not retire exactly once")
    retired = retired_roots[0]
    global_root = (
        retired / "ulfm-query-store" / "global-membership"
    )
    (
        snapshot,
        receipts,
        recovery_count,
        global_artifact,
        global_backend,
    ) = _global_recovery_state(global_root)
    expected_failures = [f"rank-{rank}" for rank in failed_ranks]
    if (
        recovery_count != len(failed_ranks)
        or [receipt["failed_endpoints"] for receipt in receipts]
        != [[endpoint] for endpoint in expected_failures]
        or snapshot["generation"] != len(failed_ranks)
    ):
        raise UlfmRecoveryError("F446 recovery sequence changed")

    layout_paths = sorted(
        (retired / "ulfm-query-store" / "global-membership").glob(
            "generation-*-layout.json"
        )
    )
    layouts = [_verified_layout(path) for path in layout_paths]
    if [layout["generation"] for layout in layouts] != list(
        range(len(failed_ranks) + 1)
    ):
        raise UlfmRecoveryError("F446 generation layout sequence changed")
    initial_members = layouts[0]["active_endpoints"] + layouts[0][
        "standby_endpoints"
    ]
    active_budget = len(initial_members) - warm_spares
    survivors = list(initial_members)
    for generation, layout in enumerate(layouts):
        if generation:
            survivors.remove(failed_ranks[generation - 1])
        expected_active = survivors[:min(active_budget, len(survivors))]
        expected_standby = survivors[len(expected_active):]
        if (
            layout["active_budget"] != active_budget
            or layout["active_endpoints"] != expected_active
            or layout["standby_endpoints"] != expected_standby
        ):
            raise UlfmRecoveryError("F446 warm-spare promotion sequence changed")

    final_groups = _final_group_state(retired, len(failed_ranks))
    wal = _work_wal_statistics(retired)
    public_objects = sorted(
        path for path in campaign_output.iterdir()
        if path.is_file() and _HASH_NAME.fullmatch(path.name)
    )
    if len(public_objects) < 3:
        raise UlfmRecoveryError("F446 public corpus is unexpectedly small")
    for path in public_objects:
        if _file_sha256(path) != path.name:
            raise UlfmRecoveryError("F446 corpus content address changed")
    host_count = len({member["host_id"] for member in snapshot["members"].values()})
    if host_count < minimum_hosts:
        raise UlfmRecoveryError("F446 surviving host diversity is too small")

    log = log_path.read_text(encoding="utf-8", errors="replace")
    for generation, endpoint in enumerate(expected_failures, 1):
        expected = (
            "ULFM global recovery committed: "
            f"generation={generation} failed=['{endpoint}']"
        )
        if expected not in log:
            raise UlfmRecoveryError("F446 recovery log sequence is incomplete")
    logged_generated = _statistic(log, "Total test cases generated")
    logged_analyzed = _statistic(log, "Total analysis observations")
    logged_unique = _statistic(log, "New interesting test cases")
    if (
        logged_generated != wal["generated"]
        or logged_analyzed != wal["analyzed"]
        or logged_unique != len(public_objects) - 2
        or "=== Final Statistics ===" not in log
    ):
        raise UlfmRecoveryError("F446 final statistics are not durable totals")

    promoted_by_generation = [
        [endpoint for endpoint in layout["active_endpoints"]
         if endpoint >= active_budget]
        for layout in layouts
    ]
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "failed_endpoints": expected_failures,
        "warm_spares": warm_spares,
        "final_generation": snapshot["generation"],
        "recovery_count": recovery_count,
        "surviving_hosts": host_count,
        "promoted_by_generation": promoted_by_generation,
        "final_active_endpoints": layouts[-1]["active_endpoints"],
        "final_standby_endpoints": layouts[-1]["standby_endpoints"],
        "final_group_controllers": final_groups["controllers"],
        "final_group_completed": final_groups["completed"],
        "wal_analyzed": wal["analyzed"],
        "wal_generated": wal["generated"],
        "wal_leased_at_deadline": wal["leased_at_deadline"],
        "public_objects": len(public_objects),
        "global_snapshot_sha256": snapshot["snapshot_sha256"],
        "global_database_sha256": _file_sha256(global_artifact),
        "global_state_backend": global_backend,
        "global_state_artifact": global_artifact.name,
        "global_state_sha256": _file_sha256(global_artifact),
        "log_sha256": _file_sha256(log_path),
        "claim_boundary": (
            "physical process-failure evidence with durable global membership, "
            "generation layouts, group fences, work WAL, and corpus hashes; "
            "performance claims require the separate R-grade protocol"
        ),
    }
    body["result_sha256"] = content_digest(body)
    return verify_campaign_oracle(body)


def verify_campaign_oracle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("F446 oracle must be an object")
    body = dict(raw)
    supplied = body.pop("result_sha256", "")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("F446 oracle identity changed")
    if (
        body.get("schema") != SCHEMA
        or not isinstance(body.get("failed_endpoints"), list)
        or body.get("final_generation") != len(body["failed_endpoints"])
        or body.get("recovery_count") != len(body["failed_endpoints"])
        or not isinstance(body.get("wal_analyzed"), int)
        or body["wal_analyzed"] < 1
        or not isinstance(body.get("public_objects"), int)
        or body["public_objects"] < 3
    ):
        raise UlfmRecoveryError("F446 oracle invariants changed")
    body["result_sha256"] = supplied
    return body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-output", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--failed-ranks", default="")
    parser.add_argument("--warm-spares", type=int, default=0)
    parser.add_argument("--minimum-hosts", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        result = verify_campaign_oracle(
            json.loads(args.verify.read_text(encoding="ascii"))
        )
        print(json.dumps(result, sort_keys=True))
        return
    if args.campaign_output is None or args.log is None or args.output is None:
        parser.error("campaign build mode requires output, log, and oracle paths")
    try:
        failed_ranks = [
            int(value, 10) for value in args.failed_ranks.split(",") if value
        ]
    except ValueError as error:
        parser.error(f"invalid failed-rank sequence: {error}")
    result = build_campaign_oracle(
        args.campaign_output,
        args.log,
        failed_ranks,
        args.warm_spares,
        args.minimum_hosts,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )


if __name__ == "__main__":
    main()
