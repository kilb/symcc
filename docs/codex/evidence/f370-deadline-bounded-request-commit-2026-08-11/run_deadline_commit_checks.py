#!/usr/bin/env python3
"""Executable counterfactuals for F370 persistent request commit bounds."""

from __future__ import annotations

import array
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import query_store as query_store_module  # noqa: E402
from query_store import (  # noqa: E402
    PersistentSubprocessSolver,
    QueryStore,
    WorkLease,
)


def _envelope() -> dict[str, object]:
    return {
        "schema": "symcc-query-ir-v1",
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
                "attrs": {"value_hex": "42"},
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
        "smt2": "(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x42))\n",
        "prefix_smt2": "(declare-fun |0| () (_ BitVec 8))\n",
        "target_smt2": "(assert (= |0| #x42))\n",
        "input_hex": "41",
        "metadata": {"producer": "f370-evidence"},
        "timeout_ms": 1000,
    }


def _legacy_blocking_pipe() -> bool:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    finished = threading.Event()

    def write_like_f369() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write("x" * (1024 * 1024))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            finished.set()

    writer = threading.Thread(target=write_like_f369, daemon=True)
    writer.start()
    blocked = not finished.wait(0.1)
    process.kill()
    process.wait(timeout=2.0)
    writer.join(timeout=2.0)
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    return blocked and finished.is_set()


def _fill_seqpacket(channel: socket.socket) -> None:
    channel.setblocking(False)
    while True:
        try:
            channel.send(b"x" * 256)
        except BlockingIOError:
            return


def _legacy_blocking_descriptor() -> bool:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    descriptor = os.open("/dev/null", os.O_RDONLY)
    finished = threading.Event()
    try:
        _fill_seqpacket(sender)
        sender.setblocking(True)

        def send_like_f369() -> None:
            try:
                rights = array.array("i", [descriptor, descriptor])
                sender.sendmsg(
                    [b"legacy"],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)],
                )
            except OSError:
                pass
            finally:
                finished.set()

        writer = threading.Thread(target=send_like_f369, daemon=True)
        writer.start()
        blocked = not finished.wait(0.1)
        receiver.close()
        sender.close()
        writer.join(timeout=2.0)
        return blocked and finished.is_set()
    finally:
        try:
            sender.close()
        except OSError:
            pass
        try:
            receiver.close()
        except OSError:
            pass
        os.close(descriptor)


def _helper(marker: Path) -> PersistentSubprocessSolver:
    script = (
        "import json,os,pathlib,sys,time;"
        f"\np=pathlib.Path({str(marker)!r})"
        "\ntry: n=int(p.read_text())"
        "\nexcept (FileNotFoundError,ValueError): n=0"
        "\np.write_text(str(n+1))"
        "\nif n == 0: time.sleep(60)"
        "\nfor line in sys.stdin:"
        "\n q=line.rstrip('\\n').split('\\t')[0]"
        "\n print(json.dumps({'request_id':q,'status':'sat',"
        "'assignments':{},'solver':'f370-helper','generation':n+1}),flush=True)"
    )
    return PersistentSubprocessSolver((sys.executable, "-c", script))


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    if not path.exists():
        raise RuntimeError(f"helper did not publish {path}")


def _compatibility_lease(
    query_id: str,
    *,
    input_hex: str = "",
    timeout_ms: int = 1,
) -> WorkLease:
    return WorkLease(
        query_id,
        1,
        Path("query.smt2"),
        "prefix",
        Path("prefix.smt2"),
        Path("target.smt2"),
        timeout_ms,
        input_hex,
    )


def _production_pipe(root: Path) -> dict[str, bool]:
    marker = root / "pipe-generations"
    solver = _helper(marker)
    try:
        _wait_for(marker)
        original_pid = solver.process.pid
        started = time.monotonic()
        timed_out = False
        with mock.patch.object(
            query_store_module,
            "_PERSISTENT_SOLVER_GRACE_SECONDS",
            0.05,
        ):
            try:
                solver(
                    _compatibility_lease(
                        "blocked-pipe",
                        input_hex="aa" * (512 * 1024),
                    )
                )
            except subprocess.TimeoutExpired:
                timed_out = True
        elapsed_bounded = time.monotonic() - started < 1.0
        generation_retired = solver.process.poll() is not None
        recovery = solver(_compatibility_lease("pipe-recovery"))
        return {
            "deadline_enforced": timed_out and elapsed_bounded,
            "partial_generation_retired": generation_retired,
            "cold_recovery": (
                recovery.get("generation") == 2 and solver.process.pid != original_pid
            ),
        }
    finally:
        solver.close()


def _production_descriptor(root: Path) -> dict[str, bool]:
    marker = root / "descriptor-generations"
    solver = _helper(marker)
    store = QueryStore(root / "store")
    store.ingest(_envelope())
    lease = store.claim("f370")
    if lease is None:
        raise RuntimeError("failed to create sealed evidence lease")
    try:
        _wait_for(marker)
        original_pid = solver.process.pid
        channel = solver._fd_channel
        if channel is None:
            raise RuntimeError("descriptor channel is unavailable")
        _fill_seqpacket(channel)
        started = time.monotonic()
        timed_out = False
        with mock.patch.object(
            query_store_module,
            "_PERSISTENT_SOLVER_GRACE_SECONDS",
            0.05,
        ):
            try:
                solver(
                    WorkLease(
                        lease.query_id,
                        lease.token,
                        lease.smt2_path,
                        lease.prefix_key,
                        lease.prefix_smt2_path,
                        lease.target_smt2_path,
                        1,
                        lease.input_hex,
                        lease._sealed_artifacts,
                    )
                )
            except subprocess.TimeoutExpired:
                timed_out = True
        elapsed_bounded = time.monotonic() - started < 1.0
        generation_retired = solver.process.poll() is not None
        recovery = solver(_compatibility_lease("descriptor-recovery"))
        return {
            "deadline_enforced": timed_out and elapsed_bounded,
            "partial_generation_retired": generation_retired,
            "cold_recovery": (
                recovery.get("generation") == 2 and solver.process.pid != original_pid
            ),
        }
    finally:
        store.fail(lease, "f370", "bounded descriptor evidence")
        solver.close()


def _preflight(root: Path) -> dict[str, object]:
    marker = root / "preflight-generations"
    marker.write_text("1", encoding="ascii")
    solver = _helper(marker)
    try:
        before = solver(_compatibility_lease("before-preflight"))
        generation = solver.process.pid
        rejected: list[str] = []
        cases = (
            (
                "zero-timeout",
                _compatibility_lease("zero-timeout", timeout_ms=0),
            ),
            (
                "non-integer-timeout",
                WorkLease(
                    "bad-timeout",
                    1,
                    Path("query.smt2"),
                    "prefix",
                    Path("prefix.smt2"),
                    Path("target.smt2"),
                    "1",  # type: ignore[arg-type]
                ),
            ),
            (
                "invalid-utf8",
                _compatibility_lease("invalid-utf8", input_hex="\ud800"),
            ),
        )
        for name, lease in cases:
            try:
                solver(lease)
            except ValueError:
                rejected.append(name)
        with mock.patch.object(
            query_store_module,
            "_MAX_SOLVER_REQUEST_BYTES",
            32,
        ):
            try:
                solver(_compatibility_lease("oversized-frame"))
            except ValueError:
                rejected.append("oversized-frame")
        after = solver(_compatibility_lease("after-preflight"))
        return {
            "classes_rejected": sorted(rejected),
            "healthy_generation_preserved": (
                before.get("generation") == 2
                and after.get("generation") == 2
                and solver.process.pid == generation
            ),
            "query_ir_max_witness_representable": (
                query_store_module._MAX_SOLVER_REQUEST_BYTES
                >= 2 * query_store_module._MAX_ARTIFACT_BYTES
            ),
        }
    finally:
        solver.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symcc-f370-") as temporary:
        root = Path(temporary)
        payload = {
            "schema": "symcc-f370-deadline-bounded-request-commit-v1",
            "legacy": {
                "pipe_write_blocked_without_deadline": _legacy_blocking_pipe(),
                "descriptor_send_blocked_without_deadline": (
                    _legacy_blocking_descriptor()
                ),
            },
            "production": {
                "pipe": _production_pipe(root),
                "descriptor": _production_descriptor(root),
                "preflight": _preflight(root),
            },
        }
    payload["all_checks_passed"] = bool(
        all(payload["legacy"].values())
        and all(payload["production"]["pipe"].values())
        and all(payload["production"]["descriptor"].values())
        and payload["production"]["preflight"]["classes_rejected"]
        == [
            "invalid-utf8",
            "non-integer-timeout",
            "oversized-frame",
            "zero-timeout",
        ]
        and payload["production"]["preflight"]["healthy_generation_preserved"]
        and payload["production"]["preflight"]["query_ir_max_witness_representable"]
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
