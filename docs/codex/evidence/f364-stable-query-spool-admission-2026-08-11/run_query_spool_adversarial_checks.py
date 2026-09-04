#!/usr/bin/env python3
"""Exercise F364 stable Query IR reads and crash-released spool admission."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hold-spool", type=Path)
    parser.add_argument("--ready", type=Path)
    return parser


def _envelope() -> dict[str, object]:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f364-evidence",
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
        "metadata": {"source": "f364", "site": 364},
        "smt2": (
            "(declare-fun |0| () (_ BitVec 8))\n"
            "(assert (= |0| #x41))\n"
            "(assert (= |0| #x42))\n"
        ),
        "prefix_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x41))\n"),
        "target_smt2": ("(declare-fun |0| () (_ BitVec 8))\n(assert (= |0| #x42))\n"),
    }


class _HoldingStore:
    def __init__(self, ready: Path):
        self.ready = ready

    def ingest_file(self, _path: Path) -> tuple[str, bool]:
        self.ready.write_text("locked\n", encoding="ascii")
        while True:
            time.sleep(60)


class _MustNotRunStore:
    def __init__(self):
        self.calls = 0

    def ingest_file(self, _path: Path) -> tuple[str, bool]:
        self.calls += 1
        raise AssertionError("busy spool consumer reached QueryStore")


class _RacingStore:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()
        self.first_entered = threading.Event()
        self.release_first = threading.Event()

    def ingest_file(self, _path: Path) -> tuple[str, bool]:
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.first_entered.set()
            self.release_first.wait(5)
        return "query", call == 1


def _legacy_move(source: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination_dir / source.name)


def _legacy_ingest_spool(store: _RacingStore, spool: Path) -> tuple[int, int]:
    incoming = spool / "incoming"
    accepted = spool / "accepted"
    rejected = spool / "rejected"
    incoming.mkdir(parents=True, exist_ok=True)
    imported = 0
    failed = 0
    for path in sorted(incoming.glob("*.json")):
        try:
            store.ingest_file(path)
            _legacy_move(path, accepted)
            imported += 1
        except Exception as error:
            error_path = rejected / f"{path.name}.error"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(
                f"{type(error).__name__}: {error}\n",
                encoding="utf-8",
            )
            _legacy_move(path, rejected)
            failed += 1
    return imported, failed


def _legacy_race_case(root: Path) -> dict[str, object]:
    spool = root / "legacy-race-spool"
    incoming = spool / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "query.json").write_text("{}", encoding="ascii")
    store = _RacingStore()
    results: list[tuple[int, int]] = []
    errors: list[str] = []

    def consume() -> None:
        try:
            results.append(_legacy_ingest_spool(store, spool))
        except BaseException as error:
            errors.append(type(error).__name__)

    first = threading.Thread(target=consume)
    first.start()
    if not store.first_entered.wait(2):
        raise TimeoutError("legacy first consumer did not enter ingestion")
    second = threading.Thread(target=consume)
    second.start()
    second.join(2)
    store.release_first.set()
    first.join(2)
    if first.is_alive() or second.is_alive():
        raise TimeoutError("legacy race did not terminate")
    return {
        "ingest_calls": store.calls,
        "successful_results": [list(result) for result in results],
        "uncaught_errors": errors,
        "accepted_files": sorted(path.name for path in (spool / "accepted").iterdir()),
        "misleading_error_files": sorted(
            path.name
            for path in (spool / "rejected").iterdir()
            if path.suffix == ".error"
        ),
    }


def _hold_spool(spool: Path, ready: Path) -> int:
    ingest_spool(_HoldingStore(ready), spool)
    return 2


def _wait_for(path: Path, child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if child.poll() is not None:
            raise RuntimeError(f"lock holder exited early: {child.returncode}")
        time.sleep(0.01)
    raise TimeoutError("lock holder did not acquire the spool")


def _poisoned_lock_case(root: Path, kind: str, serialized: str) -> dict[str, object]:
    spool = root / f"{kind}-lock-spool"
    incoming = spool / "incoming"
    incoming.mkdir(parents=True)
    query = incoming / "query.json"
    query.write_text(serialized, encoding="utf-8")
    lock_path = spool / ".ingest.lock"
    external = root / "external-lock"
    if kind == "symlink":
        external.write_text("unchanged", encoding="ascii")
        lock_path.symlink_to(external)
    else:
        os.mkfifo(lock_path)
    store = _MustNotRunStore()
    rejected = False
    try:
        ingest_spool(store, spool)
    except OSError:
        rejected = True
    return {
        "rejected": rejected,
        "store_calls": store.calls,
        "input_preserved": query.is_file(),
        "external_unchanged": (
            external.read_text(encoding="ascii") == "unchanged"
            if kind == "symlink"
            else True
        ),
    }


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.hold_spool is not None:
        if arguments.ready is None:
            raise SystemExit("--hold-spool requires --ready")
        return _hold_spool(arguments.hold_spool, arguments.ready)
    if arguments.output is None:
        raise SystemExit("--output is required")

    serialized = json.dumps(_envelope(), sort_keys=True)
    with tempfile.TemporaryDirectory(
        prefix=".f364-query-spool-", dir=REPO
    ) as temporary_text:
        root = Path(temporary_text)
        legacy_race = _legacy_race_case(root)

        crash_spool = root / "crash-spool"
        crash_incoming = crash_spool / "incoming"
        crash_incoming.mkdir(parents=True)
        crash_query = crash_incoming / "query.json"
        crash_query.write_text(serialized, encoding="utf-8")
        ready = root / "holder-ready"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hold-spool",
                str(crash_spool),
                "--ready",
                str(ready),
            ],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for(ready, child)
            blocked_store = _MustNotRunStore()
            blocked_result = ingest_spool(blocked_store, crash_spool)
            input_while_locked = crash_query.is_file()
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        recovery_store = QueryStore(root / "recovery-store")
        recovered_result = ingest_spool(recovery_store, crash_spool)
        recovery_stats = recovery_store.stats()

        strict_spool = root / "strict-spool"
        strict_incoming = strict_spool / "incoming"
        strict_incoming.mkdir(parents=True)
        (strict_incoming / "good.json").write_text(
            serialized,
            encoding="utf-8",
        )
        symlink_target = root / "symlink-target.json"
        symlink_target.write_text(serialized, encoding="utf-8")
        (strict_incoming / "symlink.json").symlink_to(symlink_target)
        os.mkfifo(strict_incoming / "fifo.json")
        strict_store = QueryStore(root / "strict-store")
        started = time.monotonic()
        strict_result = ingest_spool(strict_store, strict_spool)
        strict_elapsed = time.monotonic() - started
        strict_stats = strict_store.stats()
        symlink_error = (strict_spool / "rejected" / "symlink.json.error").read_text(
            encoding="utf-8"
        )
        fifo_error = (strict_spool / "rejected" / "fifo.json.error").read_text(
            encoding="utf-8"
        )

        identity_path = root / "identity.json"
        identity_path.write_text(serialized, encoding="utf-8")
        identity_store = QueryStore(root / "identity-store")
        metadata = os.stat(identity_path, follow_symlinks=False)
        replacement = mock.Mock(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino + 1,
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns,
        )
        identity_error = ""
        with mock.patch.object(
            query_store_module.os,
            "stat",
            return_value=replacement,
        ):
            try:
                identity_store.ingest_file(identity_path)
            except ValueError as error:
                identity_error = str(error)

        symlink_lock = _poisoned_lock_case(root, "symlink", serialized)
        fifo_lock = _poisoned_lock_case(root, "fifo", serialized)

        payload = {
            "schema": "symcc-f364-stable-query-spool-check-v1",
            "legacy_double_consumer": legacy_race,
            "crash_released_lock": {
                "blocked_result": list(blocked_result),
                "blocked_store_calls": blocked_store.calls,
                "input_preserved_while_locked": input_while_locked,
                "holder_returncode": child.returncode,
                "recovered_result": list(recovered_result),
                "accepted_after_recovery": (
                    crash_spool / "accepted" / "query.json"
                ).is_file(),
                "stored_queries": recovery_stats["queries"],
                "stored_witnesses": recovery_stats["witnesses"],
            },
            "stable_regular_inputs": {
                "result": list(strict_result),
                "completed_under_one_second": strict_elapsed < 1.0,
                "accepted_files": sorted(
                    path.name for path in (strict_spool / "accepted").iterdir()
                ),
                "rejected_files": sorted(
                    path.name
                    for path in (strict_spool / "rejected").iterdir()
                    if path.suffix == ".json"
                ),
                "symlink_rejected": "cannot read query envelope" in symlink_error,
                "symlink_preserved": (
                    strict_spool / "rejected" / "symlink.json"
                ).is_symlink(),
                "fifo_rejected": (
                    "query envelope must be a regular file" in fifo_error
                ),
                "fifo_preserved": stat.S_ISFIFO(
                    os.lstat(strict_spool / "rejected" / "fifo.json").st_mode
                ),
                "stored_queries": strict_stats["queries"],
                "stored_witnesses": strict_stats["witnesses"],
            },
            "path_identity_closure": {
                "rejected": (
                    identity_error == "query envelope identity changed while reading"
                ),
                "stored_queries": identity_store.stats()["queries"],
            },
            "lock_object_admission": {
                "symlink": symlink_lock,
                "fifo": fifo_lock,
            },
        }

    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    crash = payload["crash_released_lock"]
    legacy = payload["legacy_double_consumer"]
    inputs = payload["stable_regular_inputs"]
    identity = payload["path_identity_closure"]
    lock_objects = payload["lock_object_admission"]
    ok = (
        legacy
        == {
            "ingest_calls": 2,
            "successful_results": [[1, 0]],
            "uncaught_errors": ["FileNotFoundError"],
            "accepted_files": ["query.json"],
            "misleading_error_files": ["query.json.error"],
        }
        and crash
        == {
            "blocked_result": [0, 0],
            "blocked_store_calls": 0,
            "input_preserved_while_locked": True,
            "holder_returncode": -signal.SIGKILL,
            "recovered_result": [1, 0],
            "accepted_after_recovery": True,
            "stored_queries": 1,
            "stored_witnesses": 1,
        }
        and inputs
        == {
            "result": [1, 2],
            "completed_under_one_second": True,
            "accepted_files": ["good.json"],
            "rejected_files": ["fifo.json", "symlink.json"],
            "symlink_rejected": True,
            "symlink_preserved": True,
            "fifo_rejected": True,
            "fifo_preserved": True,
            "stored_queries": 1,
            "stored_witnesses": 1,
        }
        and identity == {"rejected": True, "stored_queries": 0}
        and all(
            case
            == {
                "rejected": True,
                "store_calls": 0,
                "input_preserved": True,
                "external_unchanged": True,
            }
            for case in lock_objects.values()
        )
    )
    print(
        "f364-stable-query-spool-check: "
        f"{'PASS' if ok else 'FAIL'} "
        "(SIGKILL recovery, contention, no-follow, regular-file, identity)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
