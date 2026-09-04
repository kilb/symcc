#!/usr/bin/env python3
"""Executable counterexamples for F373 QueryStore artifact reachability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

from query_store import QueryStore  # noqa: E402


def _envelope(target_value: int = 66) -> dict[str, object]:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f373-evidence",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "41"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{target_value:02x}"},
            },
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [0, 3],
                "attrs": {},
            },
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "smt2": f"(assert (= input0 #x41))\n(assert (= input0 #x{target_value:02x}))\n",
        "prefix_smt2": "(assert (= input0 #x41))\n",
        "target_smt2": f"(assert (= input0 #x{target_value:02x}))\n",
        "input_hex": "41",
        "metadata": {"source": "f373"},
        "timeout_ms": 1000,
        "priority": 1.0,
    }


def _orphan_case(root: Path) -> tuple[dict[str, object], QueryStore]:
    store = QueryStore(root)
    store.ingest(_envelope())
    indexed_digest = store._store_artifact(
        "smt2",
        b"(assert indexed-orphan)\n",
        ".smt2",
    )
    unindexed_content = b"(assert unindexed-orphan)\n"
    unindexed_store = store._artifact_stores["smt2-target"]
    unindexed_digest = unindexed_store.digest(unindexed_content)
    _, unindexed_path = unindexed_store.put(unindexed_content, unindexed_digest)
    malformed = store.object_dir / "smt2" / "not-a-shard"
    malformed.write_bytes(b"must remain untouched")

    with store._connect() as db:
        artifact_rows = int(db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        query_references = int(
            db.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT smt2_hash FROM queries "
                "UNION SELECT prefix_smt2_hash FROM queries "
                "UNION SELECT target_smt2_hash FROM queries)"
            ).fetchone()[0]
        )
        indexed_orphans = int(
            db.execute(
                "SELECT COUNT(*) FROM artifacts a WHERE NOT EXISTS ("
                "SELECT 1 FROM queries q WHERE a.hash IN "
                "(q.smt2_hash, q.prefix_smt2_hash, q.target_smt2_hash))"
            ).fetchone()[0]
        )
    physical_objects = sum(
        1
        for path in store.object_dir.glob("*/*/*")
        if path.is_file() and path.name.endswith(".smt2")
    )
    legacy = {
        "artifact_rows_visible": artifact_rows,
        "indexed_orphans_visible": indexed_orphans,
        "namespace_anomalies_visible": False,
        "physical_objects_present": physical_objects,
        "query_references_visible": query_references,
        "unindexed_orphans_visible": False,
    }

    audit = store.audit_artifacts(max_entries=100)
    partial = store.audit_artifacts(max_entries=1)
    production = {
        "indexed_orphans": audit["database"]["unreferenced_artifact_rows"],
        "namespace_reference_closure": audit["verdict"][
            "namespace_reference_closure"
        ],
        "noncanonical_entries": audit["scan"]["noncanonical_entries"],
        "orphan_physical": audit["physical"]["orphan_objects_observed"],
        "report_only": audit["mode"] == "report-only",
        "scan_complete": audit["scan"]["complete"],
        "sweep_authorized": audit["verdict"]["sweep_authorized"],
        "unindexed_physical": audit["physical"]["unindexed_objects_observed"],
        "objects_preserved": (
            Path(unindexed_path).is_file()
            and Path(store._artifact_stores["smt2"].object_path(indexed_digest)).is_file()
            and malformed.is_file()
        ),
    }
    bounded = {
        "missing_primary_is_unknown": partial["candidates"]["missing_primary"] is None,
        "scan_complete": partial["scan"]["complete"],
        "scanned_entries": partial["scan"]["scanned_entries"],
        "sweep_authorized": partial["verdict"]["sweep_authorized"],
    }
    return {
        "bounded_incomplete_scan": bounded,
        "counterfactual_database_only": legacy,
        "production_report_only": production,
    }, store


def _publication_fence(root: Path) -> dict[str, object]:
    store = QueryStore(root)
    store.ingest(_envelope())
    physical_store = store._artifact_stores["smt2"]
    original_scan = physical_store.scan_objects
    entered = threading.Event()
    release = threading.Event()
    audit_results: list[dict[str, object]] = []
    ingest_results: list[tuple[str, bool]] = []
    errors: list[str] = []

    def blocking_scan(*, max_entries):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("audit fence was not released")
        return original_scan(max_entries=max_entries)

    def audit() -> None:
        try:
            audit_results.append(store.audit_artifacts(max_entries=100))
        except BaseException as error:
            errors.append(type(error).__name__)

    def ingest() -> None:
        try:
            ingest_results.append(store.ingest(_envelope(68)))
        except BaseException as error:
            errors.append(type(error).__name__)

    with mock.patch.object(
        physical_store,
        "scan_objects",
        side_effect=blocking_scan,
    ):
        auditor = threading.Thread(target=audit)
        publisher = threading.Thread(target=ingest)
        auditor.start()
        if not entered.wait(2):
            raise RuntimeError("audit did not enter physical scan")
        publisher.start()
        time.sleep(0.05)
        blocked = publisher.is_alive() and store.stats()["queries"] == 1
        release.set()
        auditor.join(2)
        publisher.join(2)

    return {
        "audit_reference_count": (
            audit_results[0]["database"]["query_reference_count"]
            if audit_results
            else -1
        ),
        "errors": errors,
        "publisher_blocked_during_audit": blocked,
        "publishers_completed": len(ingest_results),
        "queries_after_release": store.stats()["queries"],
        "threads_terminated": not auditor.is_alive() and not publisher.is_alive(),
    }


def _cli_case(store: QueryStore) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(ROOT / "util")
    base = [
        sys.executable,
        str(ROOT / "util/symcc_query_service.py"),
        "--store",
        str(store.root),
        "--artifact-audit-only",
        "--artifact-audit-max-entries",
    ]
    complete = subprocess.run(
        [*base, "100"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10,
    )
    partial = subprocess.run(
        [*base, "1"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10,
    )
    complete_payload = json.loads(complete.stdout.decode("ascii"))
    partial_payload = json.loads(partial.stdout.decode("ascii"))
    return {
        "complete_exit": complete.returncode,
        "complete_report": complete_payload["scan"]["complete"],
        "partial_exit": partial.returncode,
        "partial_report": partial_payload["scan"]["complete"],
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        orphan, store = _orphan_case(root / "orphan")
        fence = _publication_fence(root / "fence")
        cli = _cli_case(store)

    legacy = orphan["counterfactual_database_only"]
    production = orphan["production_report_only"]
    bounded = orphan["bounded_incomplete_scan"]
    checks = [
        legacy["artifact_rows_visible"] == 4,
        legacy["query_references_visible"] == 3,
        legacy["indexed_orphans_visible"] == 1,
        legacy["physical_objects_present"] == 5,
        legacy["unindexed_orphans_visible"] is False,
        legacy["namespace_anomalies_visible"] is False,
        production["indexed_orphans"] == 1,
        production["unindexed_physical"] == 1,
        production["orphan_physical"] == 2,
        production["noncanonical_entries"] == 1,
        production["scan_complete"] is True,
        production["report_only"] is True,
        production["sweep_authorized"] is False,
        production["objects_preserved"] is True,
        production["namespace_reference_closure"] is True,
        bounded["scan_complete"] is False,
        bounded["scanned_entries"] == 1,
        bounded["missing_primary_is_unknown"] is True,
        bounded["sweep_authorized"] is False,
        fence["publisher_blocked_during_audit"] is True,
        fence["audit_reference_count"] == 3,
        fence["publishers_completed"] == 1,
        fence["queries_after_release"] == 2,
        fence["threads_terminated"] is True,
        fence["errors"] == [],
        cli == {
            "complete_exit": 0,
            "complete_report": True,
            "partial_exit": 2,
            "partial_report": False,
        },
    ]
    output = {
        "all_checks_passed": all(checks),
        **orphan,
        "production_cli": cli,
        "publication_fence": fence,
        "schema": "symcc-f373-query-artifact-reachability-evidence-v1",
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
