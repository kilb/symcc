#!/usr/bin/env python3
"""Exercise F365 Query spool outcome separation and retry semantics."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
sys.path.insert(0, str(UTIL))

import query_store as query_store_module  # noqa: E402
import symcc_query_service as query_service_module  # noqa: E402
from query_store import QueryStore  # noqa: E402
from symcc_query_service import ingest_spool  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _envelope() -> dict[str, object]:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f365-evidence",
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
                "attrs": {"value_hex": "42"},
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
        "input_hex": "41",
        "timeout_ms": 1000,
        "priority": 0.0,
        "metadata": {"source": "f365", "site": 365},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            "(assert (= |0| #x42))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x42))\n"),
    }


class _SuccessfulStore:
    def __init__(self) -> None:
        self.calls = 0

    def ingest_file(self, _path: Path) -> tuple[str, bool]:
        self.calls += 1
        return "query", True


def _legacy_ingest_spool(store: _SuccessfulStore, spool: Path) -> tuple[int, int]:
    incoming = spool / "incoming"
    accepted = spool / "accepted"
    rejected = spool / "rejected"
    incoming.mkdir(parents=True, exist_ok=True)
    imported = 0
    failed = 0
    for path in sorted(incoming.glob("*.json")):
        try:
            store.ingest_file(path)
            if accepted.name == "accepted":
                raise OSError("simulated accepted publication failure")
            os.replace(path, accepted / path.name)
            imported += 1
        except Exception as error:
            rejected.mkdir(parents=True, exist_ok=True)
            (rejected / f"{path.name}.error").write_text(
                f"{type(error).__name__}: {error}\n",
                encoding="utf-8",
            )
            os.replace(path, rejected / path.name)
            failed += 1
    return imported, failed


def _write_query(spool: Path, name: str, serialized: str) -> Path:
    incoming = spool / "incoming"
    incoming.mkdir(parents=True)
    path = incoming / name
    path.write_text(serialized, encoding="utf-8")
    return path


def _classification_state(spool: Path, name: str) -> dict[str, bool]:
    return {
        "incoming": (spool / "incoming" / name).is_file(),
        "accepted": (spool / "accepted" / name).is_file(),
        "rejected": (spool / "rejected" / name).is_file(),
        "error_file": (spool / "rejected" / f"{name}.error").is_file(),
    }


def _publication_case(root: Path, serialized: str) -> dict[str, object]:
    spool = root / "production-publication"
    name = "publication.json"
    _write_query(spool, name, serialized)
    store = QueryStore(root / "publication-store")
    original_move = query_service_module._move

    def fail_accepted(source: Path, destination: Path) -> None:
        if destination.name == "accepted":
            raise OSError("simulated accepted publication failure")
        original_move(source, destination)

    error_type = ""
    with mock.patch.object(
        query_service_module,
        "_move",
        side_effect=fail_accepted,
    ):
        try:
            ingest_spool(store, spool)
        except BaseException as error:
            error_type = type(error).__name__
    before_retry = store.stats()
    failure_classification = _classification_state(spool, name)
    retry = ingest_spool(store, spool)
    after_retry = store.stats()
    return {
        "error_type": error_type,
        "after_failure": failure_classification
        | {
            "stored_queries": before_retry["queries"],
            "stored_witnesses": before_retry["witnesses"],
        },
        "retry_result": list(retry),
        "after_retry": _classification_state(spool, name)
        | {
            "stored_queries": after_retry["queries"],
            "stored_witnesses": after_retry["witnesses"],
        },
    }


def _persistence_case(root: Path, serialized: str) -> dict[str, object]:
    spool = root / "production-persistence"
    name = "persistence.json"
    _write_query(spool, name, serialized)
    store = QueryStore(root / "persistence-store")
    error_type = ""
    with mock.patch.object(
        store,
        "_connect",
        side_effect=sqlite3.OperationalError(
            "simulated QueryStore persistence failure"
        ),
    ):
        try:
            ingest_spool(store, spool)
        except BaseException as error:
            error_type = type(error).__name__
    before_retry = store.stats()
    failure_classification = _classification_state(spool, name)
    retry = ingest_spool(store, spool)
    after_retry = store.stats()
    return {
        "error_type": error_type,
        "after_failure": failure_classification
        | {
            "stored_queries": before_retry["queries"],
            "stored_witnesses": before_retry["witnesses"],
        },
        "retry_result": list(retry),
        "after_retry": _classification_state(spool, name)
        | {
            "stored_queries": after_retry["queries"],
            "stored_witnesses": after_retry["witnesses"],
        },
    }


def _read_fault_case(root: Path, serialized: str) -> dict[str, object]:
    spool = root / "production-read-fault"
    name = "read-fault.json"
    _write_query(spool, name, serialized)
    store = QueryStore(root / "read-fault-store")
    error_type = ""
    with mock.patch.object(
        query_store_module,
        "_load_query_envelope",
        side_effect=OSError("simulated envelope read failure"),
    ):
        try:
            ingest_spool(store, spool)
        except BaseException as error:
            error_type = type(error).__name__
    before_retry = store.stats()
    failure_classification = _classification_state(spool, name)
    retry = ingest_spool(store, spool)
    after_retry = store.stats()
    return {
        "error_type": error_type,
        "after_failure": failure_classification
        | {
            "stored_queries": before_retry["queries"],
            "stored_witnesses": before_retry["witnesses"],
        },
        "retry_result": list(retry),
        "after_retry": _classification_state(spool, name)
        | {
            "stored_queries": after_retry["queries"],
            "stored_witnesses": after_retry["witnesses"],
        },
    }


def main() -> int:
    arguments = _parser().parse_args()
    serialized = json.dumps(_envelope())
    with tempfile.TemporaryDirectory(prefix="symcc-f365-") as temporary:
        root = Path(temporary)

        legacy_spool = root / "legacy"
        legacy_name = "query.json"
        _write_query(legacy_spool, legacy_name, serialized)
        legacy_store = _SuccessfulStore()
        legacy_result = _legacy_ingest_spool(legacy_store, legacy_spool)
        legacy_error = (legacy_spool / "rejected" / f"{legacy_name}.error").read_text(
            encoding="utf-8"
        )

        invalid_spool = root / "invalid"
        invalid_name = "invalid.json"
        _write_query(invalid_spool, invalid_name, "{")
        invalid_store = QueryStore(root / "invalid-store")
        invalid_result = ingest_spool(invalid_store, invalid_spool)
        invalid_stats = invalid_store.stats()
        invalid_error = (
            invalid_spool / "rejected" / f"{invalid_name}.error"
        ).read_text(encoding="utf-8")

        payload = {
            "schema": "symcc-f365-outcome-separated-spool-check-v1",
            "legacy_publication_misclassification": {
                "result": list(legacy_result),
                "store_calls": legacy_store.calls,
                "classification": _classification_state(
                    legacy_spool,
                    legacy_name,
                ),
                "error": legacy_error.strip(),
            },
            "production_publication_failure": _publication_case(root, serialized),
            "production_persistence_failure": _persistence_case(root, serialized),
            "production_read_failure": _read_fault_case(root, serialized),
            "invalid_input_rejection": {
                "result": list(invalid_result),
                "classification": _classification_state(
                    invalid_spool,
                    invalid_name,
                ),
                "error_is_value_error": invalid_error.startswith("ValueError: "),
                "stored_queries": invalid_stats["queries"],
                "stored_witnesses": invalid_stats["witnesses"],
            },
        }

    failure_state = {
        "incoming": True,
        "accepted": False,
        "rejected": False,
        "error_file": False,
    }
    recovered_state = {
        "incoming": False,
        "accepted": True,
        "rejected": False,
        "error_file": False,
        "stored_queries": 1,
        "stored_witnesses": 1,
    }
    publication = payload["production_publication_failure"]
    persistence = payload["production_persistence_failure"]
    read_failure = payload["production_read_failure"]
    ok = (
        payload["legacy_publication_misclassification"]
        == {
            "result": [0, 1],
            "store_calls": 1,
            "classification": {
                "incoming": False,
                "accepted": False,
                "rejected": True,
                "error_file": True,
            },
            "error": "OSError: simulated accepted publication failure",
        }
        and publication["error_type"] == "OSError"
        and publication["after_failure"]
        == failure_state | {"stored_queries": 1, "stored_witnesses": 1}
        and publication["retry_result"] == [1, 0]
        and publication["after_retry"] == recovered_state
        and persistence["error_type"] == "OperationalError"
        and persistence["after_failure"]
        == failure_state | {"stored_queries": 0, "stored_witnesses": 0}
        and persistence["retry_result"] == [1, 0]
        and persistence["after_retry"] == recovered_state
        and read_failure["error_type"] == "OSError"
        and read_failure["after_failure"]
        == failure_state | {"stored_queries": 0, "stored_witnesses": 0}
        and read_failure["retry_result"] == [1, 0]
        and read_failure["after_retry"] == recovered_state
        and payload["invalid_input_rejection"]
        == {
            "result": [0, 1],
            "classification": {
                "incoming": False,
                "accepted": False,
                "rejected": True,
                "error_file": True,
            },
            "error_is_value_error": True,
            "stored_queries": 0,
            "stored_witnesses": 0,
        }
    )
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    print(
        "f365-outcome-separated-spool-check: "
        f"{'PASS' if ok else 'FAIL'} "
        "(reject, read, persist, publish, retry)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
