#!/usr/bin/env python3
"""Reproduce F367's path race and verify sealed descriptor handoff."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import query_store as query_store_module  # noqa: E402
from query_store import (  # noqa: E402
    PersistentSubprocessSolver,
    QueryStore,
    SubprocessSolver,
)


def _envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f367-evidence",
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
        "metadata": {"source": "f367", "output_dir": ""},
        "smt2": "(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert (= |0| #x41))\n",
    }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _expected_hashes(envelope: dict) -> dict[str, str]:
    return {
        role: _sha256(envelope[name].encode("utf-8"))
        for role, name in (
            ("full", "smt2"),
            ("prefix", "prefix_smt2"),
            ("target", "target_smt2"),
        )
    }


def _replace_paths(lease) -> dict[str, str]:
    replacement = b"(assert false)\n"
    paths = {
        "full": lease.smt2_path,
        "prefix": lease.prefix_smt2_path,
        "target": lease.target_smt2_path,
    }
    for path in paths.values():
        path.write_bytes(replacement)
    return {role: _sha256(path.read_bytes()) for role, path in paths.items()}


def _legacy_path_case(root: Path, envelope: dict) -> dict:
    store = QueryStore(root)
    store.ingest(envelope)
    lease = store.claim("legacy-path-reader")
    assert lease is not None
    expected = _expected_hashes(envelope)["full"]
    _replace_paths(lease)
    observed = _sha256(lease.smt2_path.read_bytes())
    lease.close_artifacts()
    return {
        "expected_digest": expected,
        "observed_digest": observed,
        "path_only_consumer_read_wrong_object": observed != expected,
    }


def _sealed_snapshot_case(root: Path, envelope: dict) -> dict:
    store = QueryStore(root)
    store.ingest(envelope)
    lease = store.claim("sealed-snapshot")
    assert lease is not None
    expected = _expected_hashes(envelope)
    path_hashes = _replace_paths(lease)
    descriptors = lease.duplicate_artifacts("full", "prefix", "target")
    try:
        observed = {
            role: _sha256(os.pread(descriptor, os.fstat(descriptor).st_size, 0))
            for role, descriptor in zip(("full", "prefix", "target"), descriptors)
        }
        seals = [
            fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) for descriptor in descriptors
        ]
        write_errno = 0
        try:
            os.pwrite(descriptors[0], b"x", 0)
        except OSError as error:
            write_errno = int(error.errno or 0)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    lease.close_artifacts()
    closed_error = ""
    try:
        lease.duplicate_artifacts("full")
    except ValueError as error:
        closed_error = str(error)
    return {
        "expected_hashes": expected,
        "pathname_hashes_after_replacement": path_hashes,
        "snapshot_hashes_after_replacement": observed,
        "all_snapshots_match": observed == expected,
        "required_seals": query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS,
        "observed_seals": seals,
        "all_required_seals_present": all(
            value & query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS
            == query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS
            for value in seals
        ),
        "write_rejected_errno": write_errno,
        "closed_bundle_rejected": "lease is closed" in closed_error,
    }


def _one_shot_case(root: Path, envelope: dict) -> dict:
    store = QueryStore(root)
    store.ingest(envelope)
    lease = store.claim("one-shot")
    assert lease is not None
    expected = _expected_hashes(envelope)["full"]
    path_hashes = _replace_paths(lease)
    helper = (
        "import hashlib,json,pathlib,sys;"
        "content=pathlib.Path(sys.argv[1]).read_bytes();"
        "print(json.dumps({'status':'unknown','assignments':{},"
        "'solver':'f367-helper','artifact_sha256':"
        "hashlib.sha256(content).hexdigest()}))"
    )
    result = dict(SubprocessSolver((sys.executable, "-c", helper))(lease))
    lease.close_artifacts()
    return {
        "expected_digest": expected,
        "pathname_digest_after_replacement": path_hashes["full"],
        "helper_observed_digest": result["artifact_sha256"],
        "helper_consumed_sealed_snapshot": result["artifact_sha256"] == expected,
    }


_PERSISTENT_RECEIVER = r"""
import array
import fcntl
import hashlib
import json
import os
import socket
import sys

channel = socket.socket(fileno=int(os.environ["SYMCC_QUERY_FD_CHANNEL"]))
for line in sys.stdin:
    fields = line.rstrip("\n").split("\t")
    request_id = fields[0]
    message, ancillary, flags, _ = channel.recvmsg(256, socket.CMSG_SPACE(8))
    descriptors = array.array("i")
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors.frombytes(
                data[: len(data) - (len(data) % descriptors.itemsize)]
            )
    contents = [
        os.pread(descriptor, os.fstat(descriptor).st_size, 0)
        for descriptor in descriptors
    ]
    seals = [fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) for descriptor in descriptors]
    for descriptor in descriptors:
        os.close(descriptor)
    print(
        json.dumps(
            {
                "request_id": request_id,
                "status": "unknown",
                "assignments": {},
                "solver": "f367-persistent-helper",
                "fd_request_id": message.decode("ascii"),
                "artifact_sha256": [hashlib.sha256(value).hexdigest() for value in contents],
                "artifact_seals": seals,
                "markers": fields[3:5],
                "message_flags": flags,
            }
        ),
        flush=True,
    )
"""


def _persistent_case(root: Path, envelope: dict) -> dict:
    store = QueryStore(root)
    store.ingest(envelope)
    lease = store.claim("persistent")
    assert lease is not None
    expected = _expected_hashes(envelope)
    _replace_paths(lease)
    with PersistentSubprocessSolver(
        (sys.executable, "-c", _PERSISTENT_RECEIVER)
    ) as solver:
        result = dict(solver(lease))
    lease.close_artifacts()
    expected_pair = [expected["prefix"], expected["target"]]
    required = query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS
    return {
        "request_id": lease.query_id,
        "received_request_id": result["fd_request_id"],
        "request_id_bound": result["fd_request_id"] == lease.query_id,
        "markers": result["markers"],
        "received_descriptors": len(result["artifact_sha256"]),
        "expected_hashes": expected_pair,
        "received_hashes": result["artifact_sha256"],
        "all_snapshots_match": result["artifact_sha256"] == expected_pair,
        "all_required_seals_present": all(
            int(value) & required == required for value in result["artifact_seals"]
        ),
        "message_flags": result["message_flags"],
    }


def _failure_atomic_case(root: Path, envelope: dict) -> dict:
    store = QueryStore(root)
    query_id, _ = store.ingest(envelope)
    original = query_store_module._sealed_memfd
    created_descriptor = -1
    calls = 0

    def fail_second(content: bytes, *, role: str, digest: str) -> int:
        nonlocal calls, created_descriptor
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOMEM, "injected memfd allocation failure")
        created_descriptor = original(content, role=role, digest=digest)
        return created_descriptor

    query_store_module._sealed_memfd = fail_second
    error_text = ""
    try:
        try:
            store.claim("allocation-failure")
        except OSError as error:
            error_text = str(error)
    finally:
        query_store_module._sealed_memfd = original
    descriptor_closed = False
    try:
        os.fstat(created_descriptor)
    except OSError as error:
        descriptor_closed = error.errno == errno.EBADF
    with store._connect() as database:
        row = database.execute(
            "SELECT status, attempts FROM queries WHERE query_id = ?",
            (query_id,),
        ).fetchone()
    assert row is not None
    return {
        "allocation_error_observed": "injected memfd" in error_text,
        "partially_created_descriptor_closed": descriptor_closed,
        "query_status": str(row["status"]),
        "query_attempts": int(row["attempts"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    envelope = _envelope()
    with tempfile.TemporaryDirectory(prefix="symcc-f367-") as temporary:
        root = Path(temporary)
        payload = {
            "schema": "symcc-f367-sealed-query-fd-check-v1",
            "legacy": _legacy_path_case(root / "legacy", envelope),
            "sealed_snapshot": _sealed_snapshot_case(root / "sealed", envelope),
            "one_shot": _one_shot_case(root / "one-shot", envelope),
            "persistent": _persistent_case(root / "persistent", envelope),
            "failure_atomicity": _failure_atomic_case(root / "failure", envelope),
        }

    required = query_store_module._SEALED_ARTIFACT_REQUIRED_SEALS
    passed = bool(
        payload["legacy"]["path_only_consumer_read_wrong_object"]
        and payload["sealed_snapshot"]["all_snapshots_match"]
        and payload["sealed_snapshot"]["all_required_seals_present"]
        and payload["sealed_snapshot"]["write_rejected_errno"] == errno.EPERM
        and payload["sealed_snapshot"]["closed_bundle_rejected"]
        and payload["one_shot"]["helper_consumed_sealed_snapshot"]
        and payload["persistent"]["request_id_bound"]
        and payload["persistent"]["received_descriptors"] == 2
        and payload["persistent"]["markers"] == ["@symcc-fd:prefix", "@symcc-fd:target"]
        and payload["persistent"]["all_snapshots_match"]
        and payload["persistent"]["all_required_seals_present"]
        and payload["failure_atomicity"]["allocation_error_observed"]
        and payload["failure_atomicity"]["partially_created_descriptor_closed"]
        and payload["failure_atomicity"]["query_status"] == "pending"
        and payload["failure_atomicity"]["query_attempts"] == 0
        and required == 15
    )
    payload["all_checks_passed"] = passed
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    print(
        "f367-sealed-query-fd-check: "
        + (
            "PASS (sealed snapshots, one-shot, SCM_RIGHTS, rollback)"
            if passed
            else "FAIL"
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
