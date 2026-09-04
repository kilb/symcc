#!/usr/bin/env python3
"""Exercise descriptor-bound CAS publication closure with injected races."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
if str(UTIL) not in sys.path:
    sys.path.insert(0, str(UTIL))

import distributed_state as state  # noqa: E402


def _temporary_residue(root: Path) -> list[str]:
    return sorted(path.name for path in root.rglob("*.tmp"))


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


def main() -> int:
    content = b"F346-descriptor-bound-object"
    wrong = b"F346-wrong-publication-object"
    with tempfile.TemporaryDirectory(prefix="symcc-f346-integration-") as tmp:
        root = Path(tmp)
        object_id = state.ContentAddressedInputStore.digest(content)

        baseline_store = state.ContentAddressedInputStore(
            str(root / "baseline"), 128)
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as baseline_hash:
            baseline_id, baseline_path = baseline_store.put(content, object_id)

        corrupt_store = state.ContentAddressedInputStore(
            str(root / "corrupt"), 128)
        original_replace = state.durable_replace

        def corrupt_after_publish(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            assert directory_fd is not None
            original_replace(
                source, destination, directory_fd=directory_fd)
            _write_at(directory_fd, destination, wrong)

        corrupt_rejected = False
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as corrupt_hash:
            with mock.patch.object(
                    state, "durable_replace",
                    side_effect=corrupt_after_publish):
                try:
                    corrupt_store.put(content, object_id)
                except OSError:
                    corrupt_rejected = True

        wrong_store = state.ContentAddressedInputStore(
            str(root / "wrong-competitor"), 128)

        def wrong_competitor(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            assert directory_fd is not None
            original_replace(
                source, destination, directory_fd=directory_fd)
            competitor = destination + ".wrong-competitor"
            _write_at(directory_fd, competitor, wrong)
            os.replace(
                competitor,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )

        wrong_competitor_rejected = False
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as wrong_hash:
            with mock.patch.object(
                    state, "durable_replace", side_effect=wrong_competitor):
                try:
                    wrong_store.put(content, object_id)
                except OSError:
                    wrong_competitor_rejected = True

        exact_store = state.ContentAddressedInputStore(
            str(root / "exact-competitor"), 128)

        def exact_competitor(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            assert directory_fd is not None
            original_replace(
                source, destination, directory_fd=directory_fd)
            competitor = destination + ".exact-competitor"
            _write_at(directory_fd, competitor, content)
            os.replace(
                competitor,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )

        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as exact_hash:
            with mock.patch.object(
                    state, "durable_replace", side_effect=exact_competitor):
                exact_id, exact_path = exact_store.put(content, object_id)

        symlink_store = state.ContentAddressedInputStore(
            str(root / "symlink-competitor"), 128)
        outside = root / "outside"
        outside.write_bytes(b"outside-must-not-change")

        def symlink_competitor(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            assert directory_fd is not None
            original_replace(
                source, destination, directory_fd=directory_fd)
            os.unlink(destination, dir_fd=directory_fd)
            os.symlink(outside, destination, dir_fd=directory_fd)

        symlink_rejected = False
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as symlink_hash:
            with mock.patch.object(
                    state, "durable_replace", side_effect=symlink_competitor):
                try:
                    symlink_store.put(content, object_id)
                except OSError:
                    symlink_rejected = True

        failed_caches = {
            "corrupt": sorted(corrupt_store._verified_identities),
            "symlink": sorted(symlink_store._verified_identities),
            "wrong_competitor": sorted(wrong_store._verified_identities),
        }
        checks = {
            "baseline_publication_exact": (
                baseline_id == object_id
                and Path(baseline_path).read_bytes() == content
            ),
            "baseline_avoids_fallback_rehash": baseline_hash.call_count == 0,
            "same_inode_corruption_rejected": (
                corrupt_rejected and corrupt_hash.call_count == 1
            ),
            "wrong_inode_competitor_rejected": (
                wrong_competitor_rejected and wrong_hash.call_count == 1
            ),
            "exact_inode_competitor_converges": (
                exact_id == object_id
                and Path(exact_path).read_bytes() == content
                and exact_hash.call_count == 1
                and object_id in exact_store._verified_identities
            ),
            "symlink_competitor_rejected_without_follow": (
                symlink_rejected
                and symlink_hash.call_count == 0
                and outside.read_bytes() == b"outside-must-not-change"
            ),
            "failed_publications_are_not_cached": not any(
                failed_caches.values()),
            "temporary_publication_names_are_cleaned": not _temporary_residue(root),
        }
        observations = {
            "object_id": object_id,
            "baseline_fallback_hashes": baseline_hash.call_count,
            "corrupt_fallback_hashes": corrupt_hash.call_count,
            "wrong_competitor_fallback_hashes": wrong_hash.call_count,
            "exact_competitor_fallback_hashes": exact_hash.call_count,
            "symlink_competitor_fallback_hashes": symlink_hash.call_count,
            "failed_identity_caches": failed_caches,
            "temporary_residue": _temporary_residue(root),
        }

    result = {
        "schema": "symcc-f346-descriptor-bound-cas-publication-v1",
        "configuration": {
            "cases": [
                "baseline",
                "same-inode-corruption",
                "wrong-inode-competitor",
                "exact-inode-competitor",
                "symlink-competitor",
            ],
            "max_object_bytes": 128,
            "source": "real regular files with deterministic publication fault injection",
        },
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local mechanism integration exercises the production CAS writer "
            "and deterministic post-rename fault injection. It does not execute "
            "MPI, a target, afl-showmap, a symbolic solver, or a fuzzing campaign, "
            "and makes no throughput, coverage, bug-discovery, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
