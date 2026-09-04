#!/usr/bin/env python3
"""Executable counterfactuals for F372 bounded CAS revalidation."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import distributed_state as state  # noqa: E402
from distributed_state import ContentAddressedInputStore  # noqa: E402


CONTENT = b"f372-content-equivalent-publication"


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _competitor_replace(content: bytes):
    original_replace = state.durable_replace

    def replace(source: str, destination: str, *, directory_fd=None) -> None:
        if directory_fd is None:
            raise RuntimeError("descriptor-relative publication was not used")
        original_replace(source, destination, directory_fd=directory_fd)
        competitor = destination + ".competitor"
        _write_at(directory_fd, competitor, content)
        os.replace(
            competitor,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )

    return replace


def _transient_case(root: Path, *, legacy: bool) -> dict[str, object]:
    store = ContentAddressedInputStore(str(root))
    object_id = store.digest(CONTENT)
    original_snapshot = state.stable_regular_file_snapshot
    attempts = 0

    def snapshot(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise OSError(errno.ESTALE, "injected exact-competitor replacement")
        return original_snapshot(*args, **kwargs)

    def one_shot(self, object_directory, candidate_id):
        return self._verified_object_in_directory(object_directory, candidate_id)

    admitted = False
    error = ""
    patches = [
        mock.patch.object(
            state,
            "durable_replace",
            side_effect=_competitor_replace(CONTENT),
        ),
        mock.patch.object(
            state,
            "stable_regular_file_snapshot",
            side_effect=snapshot,
        ),
    ]
    if legacy:
        patches.append(
            mock.patch.object(
                ContentAddressedInputStore,
                "_verified_object_after_publication",
                one_shot,
            )
        )
    with patches[0], patches[1]:
        context = patches[2] if legacy else nullcontext()
        with context:
            try:
                observed_id, path = store.put(CONTENT, object_id)
                admitted = (
                    observed_id == object_id and Path(path).read_bytes() == CONTENT
                )
            except OSError as exception:
                error = str(exception)
    return {
        "admitted_exact_content": admitted,
        "attempts": attempts,
        "failed_closed": "changed during publication" in error,
    }


def _wrong_content_case(root: Path) -> dict[str, object]:
    store = ContentAddressedInputStore(str(root))
    object_id = store.digest(CONTENT)
    original_replace = state.durable_replace
    original_snapshot = state.stable_regular_file_snapshot
    attempts = 0

    def corrupt(source: str, destination: str, *, directory_fd=None) -> None:
        if directory_fd is None:
            raise RuntimeError("descriptor-relative publication was not used")
        original_replace(source, destination, directory_fd=directory_fd)
        _write_at(directory_fd, destination, b"wrong-content")

    def counted_snapshot(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_snapshot(*args, **kwargs)

    error = ""
    with (
        mock.patch.object(state, "durable_replace", side_effect=corrupt),
        mock.patch.object(
            state,
            "stable_regular_file_snapshot",
            side_effect=counted_snapshot,
        ),
    ):
        try:
            store.put(CONTENT, object_id)
        except OSError as exception:
            error = str(exception)
    return {
        "attempts": attempts,
        "failed_closed": "changed during publication" in error,
        "wrong_digest_rejected_immediately": attempts == 1,
    }


def _persistent_instability_case(root: Path) -> dict[str, object]:
    store = ContentAddressedInputStore(str(root))
    object_id = store.digest(CONTENT)
    attempts = 0

    def unstable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError(errno.ESTALE, "injected persistent replacement")

    error = ""
    with (
        mock.patch.object(
            state,
            "durable_replace",
            side_effect=_competitor_replace(CONTENT),
        ),
        mock.patch.object(
            state,
            "stable_regular_file_snapshot",
            side_effect=unstable,
        ),
    ):
        try:
            store.put(CONTENT, object_id)
        except OSError as exception:
            error = str(exception)
    return {
        "attempts": attempts,
        "configured_attempts": state._INPUT_STORE_PUBLICATION_VERIFY_ATTEMPTS,
        "failed_closed": "changed during publication" in error,
    }


def _real_convergence(root: Path) -> dict[str, object]:
    stores = [ContentAddressedInputStore(str(root)) for _ in range(2)]
    object_id = stores[0].digest(CONTENT)
    barrier = threading.Barrier(8)

    def publish(index: int) -> tuple[str, str]:
        barrier.wait(timeout=5)
        return stores[index % 2].put(CONTENT, object_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        observed = list(executor.map(publish, range(8)))
    path = Path(observed[0][1])
    return {
        "all_writers_returned_same_identity": len(set(observed)) == 1,
        "final_content_exact": path.read_bytes() == CONTENT,
        "temporary_files": len(list(path.parent.glob("*.tmp"))),
        "writers": len(observed),
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        counterfactual = _transient_case(root / "legacy", legacy=True)
        production = _transient_case(root / "production", legacy=False)
        wrong_content = _wrong_content_case(root / "wrong-content")
        persistent = _persistent_instability_case(root / "persistent")
        convergence = _real_convergence(root / "convergence")

    checks = [
        counterfactual["admitted_exact_content"] is False,
        counterfactual["attempts"] == 1,
        counterfactual["failed_closed"] is True,
        production["admitted_exact_content"] is True,
        production["attempts"] == 4,
        production["failed_closed"] is False,
        wrong_content["wrong_digest_rejected_immediately"] is True,
        wrong_content["failed_closed"] is True,
        persistent["attempts"] == persistent["configured_attempts"] == 32,
        persistent["failed_closed"] is True,
        convergence["writers"] == 8,
        convergence["all_writers_returned_same_identity"] is True,
        convergence["final_content_exact"] is True,
        convergence["temporary_files"] == 0,
    ]
    output = {
        "all_checks_passed": all(checks),
        "counterfactual_one_shot": counterfactual,
        "persistent_instability": persistent,
        "production_bounded_retry": production,
        "real_convergence": convergence,
        "schema": "symcc-f372-bounded-cas-publication-revalidation-evidence-v1",
        "stable_wrong_content": wrong_content,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
