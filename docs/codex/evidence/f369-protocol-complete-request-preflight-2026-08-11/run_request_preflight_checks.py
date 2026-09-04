#!/usr/bin/env python3
"""Reproduce and close persistent-solver path-field request injection."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from query_store import PersistentSubprocessSolver, QueryStore, WorkLease  # noqa: E402


_HELPER = r"""
import array
import json
import os
import pathlib
import socket
import sys
import time

marker = pathlib.Path(os.environ["SYMCC_F369_STARTUPS"])
with marker.open("a", encoding="ascii") as output:
    output.write("start\n")
channel = socket.socket(fileno=int(os.environ["SYMCC_QUERY_FD_CHANNEL"]))
for line in sys.stdin:
    fields = line.rstrip("\n").split("\t")
    request_id = fields[0]
    injected = len(fields) > 2 and fields[2] == "injected"
    if injected:
        time.sleep(0.1)
    fd_request_id = request_id
    descriptor_count = 0
    if len(fields) > 4 and fields[3] == "@symcc-fd:prefix":
        payload, ancillary, _, _ = channel.recvmsg(256, socket.CMSG_SPACE(8))
        rights = array.array("i")
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                rights.frombytes(data[: len(data) - (len(data) % rights.itemsize)])
        descriptor_count = len(rights)
        for descriptor in rights:
            os.close(descriptor)
        fd_request_id = payload.decode("ascii")
    print(
        json.dumps(
            {
                "request_id": request_id,
                "status": "sat",
                "assignments": {"0": 66 if injected else 99},
                "observed_fields": fields,
                "helper_pid": os.getpid(),
                "fd_request_id": fd_request_id,
                "descriptor_count": descriptor_count,
            }
        ),
        flush=True,
    )
    if injected:
        time.sleep(0.2)
"""


def _envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f369-evidence",
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
        "input_hex": "41",
        "timeout_ms": 1000,
        "metadata": {"source": "f369"},
        "smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "prefix_smt2": "(declare-fun |0| () (_ BitVec 8))\n",
        "target_smt2": "(assert (= |0| #x41))\n",
    }


def _lease(query_id: str, prefix_key: str, prefix_path: str) -> WorkLease:
    return WorkLease(
        query_id,
        1,
        Path("query.smt2"),
        prefix_key,
        Path(prefix_path),
        Path("target.smt2"),
        1000,
    )


def _solver(marker: Path) -> PersistentSubprocessSolver:
    marker.parent.mkdir(parents=True, exist_ok=True)
    return PersistentSubprocessSolver(
        (sys.executable, "-c", _HELPER),
        environment={"SYMCC_F369_STARTUPS": str(marker)},
    )


def _legacy_case(root: Path) -> dict:
    marker = root / "startups.log"
    solver = _solver(marker)
    initial_pid = solver.process.pid
    try:
        malicious = _lease(
            "attacker",
            "attacker-prefix",
            "safe\nvictim\t1\tinjected\tprefix.smt2",
        )
        with mock.patch.object(solver, "_validate_request_fields", return_value=None):
            attacker = dict(solver(malicious))
        victim = dict(solver(_lease("victim", "legitimate", "prefix.smt2")))
        return {
            "attacker_accepted": attacker["status"] == "sat",
            "attacker_observed_truncated_first_line": (
                attacker["observed_fields"]
                == ["attacker", "1000", "attacker-prefix", "safe"]
            ),
            "generation_reused": (
                attacker["helper_pid"] == victim["helper_pid"] == initial_pid
            ),
            "victim_accepted_stale_injected_response": (
                victim["assignments"] == {"0": 66}
            ),
            "victim_observed_injected_fields": (
                victim["observed_fields"]
                == [
                    "victim",
                    "1",
                    "injected",
                    "prefix.smt2",
                    "target.smt2",
                    "-",
                ]
            ),
            "helper_startups": len(marker.read_text(encoding="ascii").splitlines()),
        }
    finally:
        solver.close()


def _production_case(root: Path) -> dict:
    marker = root / "startups.log"
    solver = _solver(marker)
    initial_pid = solver.process.pid
    malicious_rejected = False
    rejected_delimiters: list[str] = []
    rejected_descriptor_ids: list[str] = []
    sealed_lease: WorkLease | None = None
    try:
        try:
            solver(
                _lease(
                    "attacker",
                    "attacker-prefix",
                    "safe\nvictim\t1\tinjected\tprefix.smt2",
                )
            )
        except ValueError:
            malicious_rejected = True

        delimiter_cases = {
            "newline": "prefix\ninjected.smt2",
            "tab": "prefix\tinjected.smt2",
            "carriage-return": "prefix\rinjected.smt2",
            "nul": "prefix\x00truncated.smt2",
        }
        for name, path in delimiter_cases.items():
            try:
                solver(_lease(f"bad-{name}", "prefix", path))
            except ValueError:
                rejected_delimiters.append(name)

        victim = dict(solver(_lease("victim", "legitimate", "prefix.smt2")))

        store = QueryStore(root / "store")
        store.ingest(_envelope())
        sealed_lease = store.claim("f369")
        if sealed_lease is None:
            raise RuntimeError("failed to claim sealed evidence lease")
        for name, request_id in (
            ("non-ascii", "not-ascii-\u00e9"),
            ("oversized", "q" * 257),
        ):
            try:
                solver(replace(sealed_lease, query_id=request_id))
            except ValueError:
                rejected_descriptor_ids.append(name)
        sealed_result = dict(solver(sealed_lease))

        return {
            "malicious_path_rejected_before_transport": malicious_rejected,
            "delimiter_classes_rejected": sorted(rejected_delimiters),
            "descriptor_id_classes_rejected": sorted(rejected_descriptor_ids),
            "healthy_generation_preserved": (
                solver.process.pid
                == victim["helper_pid"]
                == sealed_result["helper_pid"]
                == initial_pid
                and solver.process.poll() is None
            ),
            "victim_observed_own_request": (
                victim["assignments"] == {"0": 99}
                and victim["observed_fields"][:3] == ["victim", "1000", "legitimate"]
            ),
            "sealed_request_valid_after_rejections": (
                sealed_result["fd_request_id"] == sealed_lease.query_id
                and sealed_result["descriptor_count"] == 2
                and sealed_result["observed_fields"][3:5]
                == ["@symcc-fd:prefix", "@symcc-fd:target"]
            ),
            "helper_startups": len(marker.read_text(encoding="ascii").splitlines()),
        }
    finally:
        if sealed_lease is not None:
            sealed_lease.close_artifacts()
        solver.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="symcc-f369-") as temporary:
        root = Path(temporary)
        legacy = _legacy_case(root / "legacy")
        production = _production_case(root / "production")

    payload = {
        "schema": "symcc-f369-protocol-complete-preflight-v1",
        "legacy": legacy,
        "production": production,
    }
    payload["all_checks_passed"] = bool(
        all(value is True for key, value in legacy.items() if key != "helper_startups")
        and legacy["helper_startups"] == 1
        and production
        == {
            "malicious_path_rejected_before_transport": True,
            "delimiter_classes_rejected": [
                "carriage-return",
                "newline",
                "nul",
                "tab",
            ],
            "descriptor_id_classes_rejected": ["non-ascii", "oversized"],
            "healthy_generation_preserved": True,
            "victim_observed_own_request": True,
            "sealed_request_valid_after_rejections": True,
            "helper_startups": 1,
        }
    )
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    print(
        "f369-request-preflight: "
        f"{'PASS' if payload['all_checks_passed'] else 'FAIL'} "
        "(path injection, delimiter closure, descriptor ID bounds)"
    )
    return 0 if payload["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
