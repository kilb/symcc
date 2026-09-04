#!/usr/bin/env python3
"""Reproduce and close persistent solver dual-channel desynchronization."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

import query_store as query_store_module  # noqa: E402
from query_store import PersistentSubprocessSolver, QueryStore, WorkLease  # noqa: E402


def _envelope(target_value: int, source: str) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f368-evidence",
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
        "input_hex": "41",
        "timeout_ms": 1000,
        "metadata": {"source": source},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            f"(assert (= |0| #x{target_value:02x}))\n"
        ),
    }


_HELPER = r"""
import array
import hashlib
import json
import os
import pathlib
import socket
import sys
import time

marker = pathlib.Path(os.environ["SYMCC_F368_STARTUPS"])
with marker.open("a", encoding="ascii") as output:
    output.write("start\n")
channel = socket.socket(fileno=int(os.environ["SYMCC_QUERY_FD_CHANNEL"]))
for line in sys.stdin:
    fields = line.rstrip("\n").split("\t")
    request_id = fields[0]
    if request_id == "wrong-id":
        print(json.dumps({"request_id": "other", "status": "sat", "assignments": {}}), flush=True)
        continue
    if request_id == "oversized":
        print("x" * 64, flush=True)
        continue
    if request_id == "partial":
        sys.stdout.write("{")
        sys.stdout.flush()
        time.sleep(5)
        continue
    descriptor_request = fields[3] == "@symcc-fd:prefix"
    fd_request_id = request_id
    hashes = []
    if descriptor_request:
        payload, ancillary, flags, _ = channel.recvmsg(256, socket.CMSG_SPACE(8))
        rights = array.array("i")
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                rights.frombytes(data[: len(data) - (len(data) % rights.itemsize)])
        hashes = [
            hashlib.sha256(os.pread(fd, os.fstat(fd).st_size, 0)).hexdigest()
            for fd in rights
        ]
        for fd in rights:
            os.close(fd)
        fd_request_id = payload.decode("ascii")
    print(
        json.dumps(
            {
                "request_id": request_id,
                "status": "sat",
                "assignments": {},
                "helper_pid": os.getpid(),
                "fd_request_id": fd_request_id,
                "artifact_sha256": hashes,
            }
        ),
        flush=True,
    )
"""


def _manual_lease(query_id: str) -> WorkLease:
    return WorkLease(
        query_id,
        1,
        Path("query.smt2"),
        "prefix",
        Path("prefix.smt2"),
        Path("target.smt2"),
        1000,
    )


def _solver(marker: Path) -> PersistentSubprocessSolver:
    marker.parent.mkdir(parents=True, exist_ok=True)
    return PersistentSubprocessSolver(
        (sys.executable, "-c", _HELPER),
        environment={"SYMCC_F368_STARTUPS": str(marker)},
    )


def _lease_pair(root: Path) -> tuple[WorkLease, WorkLease, list[str], list[str]]:
    store = QueryStore(root)
    first_envelope = _envelope(66, "first")
    second_envelope = _envelope(65, "second")
    store.ingest(first_envelope)
    first = store.claim("first")
    store.ingest(second_envelope)
    second = store.claim("second")
    if first is None or second is None:
        raise RuntimeError("failed to claim deterministic evidence leases")
    first_hashes = [
        hashlib.sha256(first_envelope[name].encode("utf-8")).hexdigest()
        for name in ("prefix_smt2", "target_smt2")
    ]
    second_hashes = [
        hashlib.sha256(second_envelope[name].encode("utf-8")).hexdigest()
        for name in ("prefix_smt2", "target_smt2")
    ]
    return first, second, first_hashes, second_hashes


def _send_then_fail(
    solver: PersistentSubprocessSolver,
    lease: WorkLease,
) -> None:
    original = solver._artifact_fields

    def injected(item: WorkLease) -> tuple[str, str]:
        original(item)
        raise OSError("injected failure after descriptor transfer")

    with mock.patch.object(solver, "_artifact_fields", side_effect=injected):
        solver(lease)


def _legacy_case(root: Path) -> dict:
    marker = root / "legacy-startups.log"
    first, second, first_hashes, second_hashes = _lease_pair(root / "store")
    solver = _solver(marker)
    original_pid = solver.process.pid
    failure_observed = False
    try:
        with mock.patch.object(solver, "_invalidate_process", return_value=None):
            try:
                _send_then_fail(solver, first)
            except OSError:
                failure_observed = True
        observed = dict(solver(second))
        return {
            "injected_failure_observed": failure_observed,
            "helper_generation_reused": observed["helper_pid"] == original_pid,
            "cross_request_id_mismatch": (observed["fd_request_id"] != second.query_id),
            "stale_artifacts_consumed": (
                observed["artifact_sha256"] == first_hashes
                and observed["artifact_sha256"] != second_hashes
            ),
            "helper_startups": len(marker.read_text(encoding="ascii").splitlines()),
        }
    finally:
        solver.close()
        first.close_artifacts()
        second.close_artifacts()


def _production_case(root: Path) -> dict:
    marker = root / "production-startups.log"
    first, second, _, second_hashes = _lease_pair(root / "store")
    solver = _solver(marker)
    original_pid = solver.process.pid
    failure_observed = False
    try:
        initial_generation_ready = (
            solver(_manual_lease("production-warmup"))["helper_pid"] == original_pid
        )
        try:
            _send_then_fail(solver, first)
        except OSError:
            failure_observed = True
        failed_generation_terminated = solver.process.poll() is not None
        observed = dict(solver(second))
        return {
            "initial_generation_ready": initial_generation_ready,
            "injected_failure_observed": failure_observed,
            "failed_generation_terminated": failed_generation_terminated,
            "generation_replaced": observed["helper_pid"] != original_pid,
            "request_id_bound": observed["fd_request_id"] == second.query_id,
            "expected_artifacts_consumed": (
                observed["artifact_sha256"] == second_hashes
            ),
            "helper_startups": len(marker.read_text(encoding="ascii").splitlines()),
        }
    finally:
        solver.close()
        first.close_artifacts()
        second.close_artifacts()


def _protocol_case(root: Path) -> dict:
    marker = root / "protocol-startups.log"
    solver = _solver(marker)
    initial_pid = solver.process.pid
    prevalidation_rejected = False
    response_mismatch_rejected = False
    oversized_rejected = False
    partial_timeout_rejected = False
    try:
        try:
            solver(_manual_lease("bad\nfield"))
        except ValueError:
            prevalidation_rejected = True
        prevalidation_generation_preserved = (
            solver.process.pid == initial_pid and solver.process.poll() is None
        )
        valid_after_prevalidation = dict(solver(_manual_lease("after-bad-field")))

        mismatch_pid = solver.process.pid
        try:
            solver(_manual_lease("wrong-id"))
        except RuntimeError:
            response_mismatch_rejected = True
        mismatch_generation_terminated = solver.process.poll() is not None
        valid_after_mismatch = dict(solver(_manual_lease("after-wrong-id")))

        oversized_pid = solver.process.pid
        with mock.patch.object(
            query_store_module,
            "_MAX_SOLVER_RESPONSE_BYTES",
            16,
        ):
            try:
                solver(_manual_lease("oversized"))
            except RuntimeError:
                oversized_rejected = True
        oversized_generation_terminated = solver.process.poll() is not None
        valid_after_oversized = dict(solver(_manual_lease("after-oversized")))

        partial_process = solver._ensure_process()
        if partial_process.stdin is None:
            raise RuntimeError("partial-response helper stdin is unavailable")
        partial_process.stdin.write("partial\t1\tprefix\tprefix.smt2\ttarget.smt2\t-\n")
        partial_process.stdin.flush()
        try:
            solver._read_response(partial_process, 0.05)
        except subprocess.TimeoutExpired:
            partial_timeout_rejected = True
        solver._invalidate_process(partial_process)
        timeout_generation_terminated = partial_process.poll() is not None
        valid_after_timeout = dict(solver(_manual_lease("after-timeout")))

        return {
            "prevalidation_rejected_before_transport": prevalidation_rejected,
            "prevalidation_generation_preserved": prevalidation_generation_preserved,
            "valid_after_prevalidation": (
                valid_after_prevalidation["helper_pid"] == initial_pid
            ),
            "response_mismatch_rejected": response_mismatch_rejected,
            "response_mismatch_generation_terminated": (mismatch_generation_terminated),
            "valid_after_response_mismatch": (
                valid_after_mismatch["helper_pid"] != mismatch_pid
            ),
            "oversized_response_rejected": oversized_rejected,
            "oversized_generation_terminated": oversized_generation_terminated,
            "valid_after_oversized_response": (
                valid_after_oversized["helper_pid"] != oversized_pid
            ),
            "partial_response_timeout_rejected": partial_timeout_rejected,
            "timeout_generation_terminated": timeout_generation_terminated,
            "valid_after_partial_timeout": (
                valid_after_timeout["helper_pid"] != partial_process.pid
            ),
            "helper_startups": len(marker.read_text(encoding="ascii").splitlines()),
        }
    finally:
        solver.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f368-") as temporary:
        root = Path(temporary)
        legacy = _legacy_case(root / "legacy")
        production = _production_case(root / "production")
        protocol = _protocol_case(root / "protocol")

    payload = {
        "schema": "symcc-f368-persistent-generation-reset-v1",
        "legacy": legacy,
        "production": production,
        "protocol": protocol,
    }
    payload["all_checks_passed"] = bool(
        legacy
        == {
            "injected_failure_observed": True,
            "helper_generation_reused": True,
            "cross_request_id_mismatch": True,
            "stale_artifacts_consumed": True,
            "helper_startups": 1,
        }
        and production
        == {
            "initial_generation_ready": True,
            "injected_failure_observed": True,
            "failed_generation_terminated": True,
            "generation_replaced": True,
            "request_id_bound": True,
            "expected_artifacts_consumed": True,
            "helper_startups": 2,
        }
        and all(
            value is True for key, value in protocol.items() if key != "helper_startups"
        )
        and protocol["helper_startups"] == 4
    )
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    print(
        "f368-persistent-generation-reset: "
        f"{'PASS' if payload['all_checks_passed'] else 'FAIL'} "
        "(orphan replay, generation reset, framing bounds)"
    )
    return 0 if payload["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
