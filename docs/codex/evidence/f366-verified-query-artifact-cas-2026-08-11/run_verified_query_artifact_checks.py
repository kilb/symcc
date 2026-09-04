#!/usr/bin/env python3
"""Reproduce F366's legacy artifact counterexample and production invariants."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

from query_store import QueryStore  # noqa: E402


def _envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f366-evidence",
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
        ],
        "prefix_roots": [],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {"source": "f366", "output_dir": ""},
        "smt2": "(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert (= |0| #x41))\n",
    }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _legacy_existing_path_case(root: Path, content: bytes) -> dict:
    digest = _sha256(content)
    path = root / "objects" / "smt2" / digest[:2] / f"{digest}.smt2"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"(assert false)\n")

    # Exact pre-F366 existence-only publication rule.
    if not path.exists():
        path.write_bytes(content)
    observed = path.read_bytes()
    return {
        "expected_digest": digest,
        "observed_digest": _sha256(observed),
        "observed_text": observed.decode("ascii").strip(),
        "silently_admitted_wrong_object": observed != content,
    }


def _production_integrity_cases(root: Path, envelope: dict) -> dict:
    store = QueryStore(root / "store")
    content = envelope["smt2"].encode("utf-8")
    digest = _sha256(content)
    path = Path(store._artifact_stores["smt2"].object_path(digest))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"(assert false)\n")

    query_id, created = store.ingest(envelope)
    repaired_before_commit = path.read_bytes() == content

    path.write_bytes(b"(assert false)\n")
    integrity_error = ""
    try:
        store.artifact_path(digest)
    except ValueError as error:
        integrity_error = str(error)

    claim_error = ""
    try:
        store.claim("f366-corrupt-claim")
    except ValueError as error:
        claim_error = str(error)
    with store._connect() as db:
        state = db.execute(
            "SELECT status, attempts FROM queries WHERE query_id = ?",
            (query_id,),
        ).fetchone()

    outside = root / "outside-smt2"
    outside.write_bytes(b"outside-must-not-change")
    path.unlink()
    path.symlink_to(outside)
    store.ingest(envelope)
    symlink_repaired = not path.is_symlink() and path.read_bytes() == content
    outside_unchanged = outside.read_bytes() == b"outside-must-not-change"

    path.unlink()
    os.mkfifo(path)
    store.ingest(envelope)
    fifo_repaired = stat.S_ISREG(path.stat().st_mode) and path.read_bytes() == content

    with store._connect() as db:
        db.execute(
            "UPDATE artifacts SET relative_path = ? WHERE hash = ?",
            ("../../outside-smt2", digest),
        )
    path_error = ""
    try:
        store.artifact_path(digest)
    except ValueError as error:
        path_error = str(error)
    store.ingest(envelope)

    reopened = QueryStore(root / "store")
    reopened_path = reopened.artifact_path(digest)
    with reopened._connect() as db:
        artifact_row = db.execute(
            "SELECT kind, relative_path, size FROM artifacts WHERE hash = ?",
            (digest,),
        ).fetchone()
    assert artifact_row is not None and state is not None
    return {
        "query_created": created,
        "query_id": query_id,
        "digest": digest,
        "preexisting_corruption_repaired": repaired_before_commit,
        "post_commit_corruption_rejected": "integrity verification" in integrity_error,
        "claim_rejected_before_commit": "integrity verification" in claim_error,
        "query_status_after_failed_claim": str(state["status"]),
        "query_attempts_after_failed_claim": int(state["attempts"]),
        "symlink_repaired": symlink_repaired,
        "symlink_target_unchanged": outside_unchanged,
        "fifo_repaired": fifo_repaired,
        "database_path_tamper_rejected": "non-canonical path" in path_error,
        "reopen_digest_verified": _sha256(reopened_path.read_bytes()) == digest,
        "artifact_kind": str(artifact_row["kind"]),
        "artifact_relative_path": str(artifact_row["relative_path"]),
        "artifact_size": int(artifact_row["size"]),
    }


def _production_convergence_case(root: Path) -> dict:
    stores = [QueryStore(root / "store"), QueryStore(root / "store")]
    content = b"(assert (= #x41 #x41))\n"
    digest = _sha256(content)
    barrier = threading.Barrier(8)

    def publish(index: int) -> str:
        barrier.wait(timeout=5)
        return stores[index % 2]._store_artifact("smt2", content, ".smt2")

    with ThreadPoolExecutor(max_workers=8) as executor:
        observed = list(executor.map(publish, range(8)))
    with stores[0]._connect() as db:
        row_count = int(
            db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE hash = ?", (digest,)
            ).fetchone()[0]
        )
    path = stores[1].artifact_path(digest)
    return {
        "writers": 8,
        "unique_returned_digests": len(set(observed)),
        "database_rows": row_count,
        "final_digest_verified": _sha256(path.read_bytes()) == digest,
        "temporary_files": len(list(path.parent.glob("*.tmp"))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    envelope = _envelope()
    content = envelope["smt2"].encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="symcc-f366-") as temporary:
        root = Path(temporary)
        payload = {
            "schema": "symcc-f366-verified-query-artifact-check-v1",
            "legacy": _legacy_existing_path_case(root / "legacy", content),
            "production": _production_integrity_cases(root / "production", envelope),
            "convergence": _production_convergence_case(root / "convergence"),
        }

    production = payload["production"]
    convergence = payload["convergence"]
    passed = bool(
        payload["legacy"]["silently_admitted_wrong_object"]
        and production["preexisting_corruption_repaired"]
        and production["post_commit_corruption_rejected"]
        and production["claim_rejected_before_commit"]
        and production["query_status_after_failed_claim"] == "pending"
        and production["query_attempts_after_failed_claim"] == 0
        and production["symlink_repaired"]
        and production["symlink_target_unchanged"]
        and production["fifo_repaired"]
        and production["database_path_tamper_rejected"]
        and production["reopen_digest_verified"]
        and convergence["unique_returned_digests"] == 1
        and convergence["database_rows"] == 1
        and convergence["final_digest_verified"]
        and convergence["temporary_files"] == 0
    )
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.write_text(serialized + "\n", encoding="ascii")
    print(serialized)
    print(
        "f366-verified-query-artifact-check: "
        + ("PASS" if passed else "FAIL")
        + " (repair, reject, pre-lease rollback, convergence)"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
