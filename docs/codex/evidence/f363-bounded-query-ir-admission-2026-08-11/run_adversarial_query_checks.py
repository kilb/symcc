#!/usr/bin/env python3
"""Reproduce F363 Query IR admission counterexamples with production APIs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
sys.path.insert(0, str(UTIL))

import query_store as query_store_module  # noqa: E402
from query_store import QueryStore  # noqa: E402
from symcc_query_service import ingest_spool  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _envelope() -> dict[str, object]:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f363-evidence",
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
        "metadata": {"source": "f363", "site": 363},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            "(assert (= |0| #x42))\n"
        ),
        "prefix_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
        ),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x42))\n"
        ),
    }


def main() -> int:
    arguments = _parser().parse_args()
    serialized = json.dumps(_envelope(), sort_keys=True)
    encoded_bytes = len(serialized.encode("utf-8"))

    with tempfile.TemporaryDirectory(
        prefix=".f363-query-ir-", dir=REPO
    ) as temporary_text:
        temporary = Path(temporary_text)
        store = QueryStore(temporary / "bounded-store")
        boundary = temporary / "boundary.json"
        boundary.write_text(serialized, encoding="utf-8")
        with mock.patch.object(
            query_store_module,
            "_MAX_ENVELOPE_BYTES",
            encoded_bytes,
        ):
            boundary_query_id, boundary_created = store.ingest_file(boundary)

        forged_stat = mock.Mock(st_size=1)
        bounded_error = ""
        with (
            mock.patch.object(
                query_store_module,
                "_MAX_ENVELOPE_BYTES",
                64,
            ),
            mock.patch.object(
                Path,
                "stat",
                return_value=forged_stat,
            ) as stat_probe,
        ):
            try:
                store.ingest_file(boundary)
            except ValueError as error:
                bounded_error = str(error)

        spool = temporary / "spool"
        incoming = spool / "incoming"
        incoming.mkdir(parents=True)
        (incoming / "good.json").write_text(serialized, encoding="utf-8")
        (incoming / "duplicate.json").write_text(
            serialized.replace(
                '"schema": "symcc-query-ir-v1"',
                '"schema": "symcc-query-ir-v1", '
                '"schema": "symcc-query-ir-v1"',
                1,
            ),
            encoding="utf-8",
        )
        (incoming / "nonfinite.json").write_text(
            serialized.replace('"priority": 0.0', '"priority": NaN', 1),
            encoding="utf-8",
        )
        spool_store = QueryStore(temporary / "spool-store")
        imported, failed = ingest_spool(spool_store, spool)
        duplicate_error = (
            spool / "rejected" / "duplicate.json.error"
        ).read_text(encoding="utf-8").strip()
        nonfinite_error = (
            spool / "rejected" / "nonfinite.json.error"
        ).read_text(encoding="utf-8").strip()
        spool_stats = spool_store.stats()
        accepted_files = sorted(
            path.name for path in (spool / "accepted").iterdir()
        )
        rejected_files = sorted(
            path.name
            for path in (spool / "rejected").iterdir()
            if path.suffix == ".json"
        )

    payload = {
        "schema": "symcc-f363-adversarial-query-admission-v1",
        "exact_limit": {
            "configured_limit": encoded_bytes,
            "encoded_bytes": encoded_bytes,
            "accepted": boundary_created,
            "query_id_length": len(boundary_query_id),
        },
        "single_snapshot_size_bound": {
            "configured_limit": 64,
            "encoded_bytes": encoded_bytes,
            "forged_stat_bytes": 1,
            "path_stat_calls_during_load": stat_probe.call_count,
            "rejected": bounded_error == "query envelope exceeds 64 bytes",
            "error": bounded_error,
        },
        "strict_spool": {
            "imported": imported,
            "failed": failed,
            "accepted_files": accepted_files,
            "rejected_files": rejected_files,
            "duplicate_error": duplicate_error,
            "nonfinite_error": nonfinite_error,
            "stored_queries": spool_stats["queries"],
            "stored_witnesses": spool_stats["witnesses"],
        },
    }
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    strict = payload["strict_spool"]
    ok = (
        payload["exact_limit"]
        == {
            "configured_limit": encoded_bytes,
            "encoded_bytes": encoded_bytes,
            "accepted": True,
            "query_id_length": 64,
        }
        and payload["single_snapshot_size_bound"]
        == {
            "configured_limit": 64,
            "encoded_bytes": encoded_bytes,
            "forged_stat_bytes": 1,
            "path_stat_calls_during_load": 0,
            "rejected": True,
            "error": "query envelope exceeds 64 bytes",
        }
        and strict["imported"] == 1
        and strict["failed"] == 2
        and strict["accepted_files"] == ["good.json"]
        and strict["rejected_files"] == ["duplicate.json", "nonfinite.json"]
        and "duplicate JSON object member 'schema'" in strict["duplicate_error"]
        and "non-finite JSON number 'NaN' is not supported"
        in strict["nonfinite_error"]
        and strict["stored_queries"] == 1
        and strict["stored_witnesses"] == 1
    )
    print(
        "f363-adversarial-query-admission: "
        f"{'PASS' if ok else 'FAIL'} "
        "(exact-limit, bounded-read, duplicate-key, nonfinite, store-isolation)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
