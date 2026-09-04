#!/usr/bin/env python3
"""Exercise component-wise, no-follow CAS-root creation and reopening."""

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


def _error_name(callback) -> str:
    try:
        callback()
    except (OSError, ValueError) as error:
        return type(error).__name__
    return ""


def _temporary_residue(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*.tmp"))


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
    content = b"F350-component-wise-anchored-CAS-root"
    original_working_directory = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="symcc-f350-integration-") as tmp:
        root = Path(tmp)
        object_id = state.ContentAddressedInputStore.digest(content)

        nested_root = root / "level-one" / "level-two" / "objects"
        with mock.patch.object(
                state.os, "mkdir", wraps=state.os.mkdir) as mkdir_trace:
            nested_store = state.ContentAddressedInputStore(
                str(nested_root), 128)
            nested_id, nested_path = nested_store.put(content, object_id)
            nested_snapshot = nested_store.snapshot(
                object_id, retain_content=True)
        mkdir_calls = [
            {
                "dir_fd_present": call.kwargs.get("dir_fd") is not None,
                "name": call.args[0],
            }
            for call in mkdir_trace.call_args_list
        ]

        relative_working_directory = root / "relative-working-directory"
        relative_working_directory.mkdir()
        try:
            os.chdir(relative_working_directory)
            relative_store = state.ContentAddressedInputStore(
                "relative/objects", 128)
            relative_id, relative_path = relative_store.put(
                content, object_id)
            os.chdir(os.path.sep)
            relative_snapshot = relative_store.snapshot(
                object_id, retain_content=True)
        finally:
            os.chdir(original_working_directory)

        outside_missing = root / "outside-missing"
        outside_missing.mkdir()
        missing_alias = root / "missing-alias"
        missing_alias.symlink_to(outside_missing, target_is_directory=True)
        missing_alias_error = _error_name(
            lambda: state.ContentAddressedInputStore(
                str(missing_alias / "new" / "objects"), 128
            )
        )

        outside_existing = root / "outside-existing"
        existing_objects = outside_existing / "objects"
        existing_objects.mkdir(parents=True)
        existing_sentinel = existing_objects / "sentinel"
        existing_sentinel.write_bytes(b"existing-unchanged")
        existing_alias = root / "existing-alias"
        existing_alias.symlink_to(
            outside_existing, target_is_directory=True)
        existing_alias_error = _error_name(
            lambda: state.ContentAddressedInputStore(
                str(existing_alias / "objects"), 128
            )
        )

        publication_namespace = root / "publication-namespace"
        publication_stable = publication_namespace / "stable"
        publication_root = publication_stable / "objects"
        publication_store = state.ContentAddressedInputStore(
            str(publication_root), 128)
        publication_detached = root / "publication-detached"
        publication_outside = root / "publication-outside"
        publication_outside_objects = publication_outside / "objects"
        publication_outside_objects.mkdir(parents=True)
        publication_sentinel = publication_outside_objects / "sentinel"
        publication_sentinel.write_bytes(b"publication-unchanged")
        original_replace = state.durable_replace
        publication_descriptor_seen = False

        def replace_ancestor_after_publish(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal publication_descriptor_seen
            assert directory_fd is not None
            publication_descriptor_seen = True
            original_replace(
                source, destination, directory_fd=directory_fd)
            publication_stable.rename(publication_detached)
            publication_stable.symlink_to(
                publication_outside, target_is_directory=True)

        with mock.patch.object(
                state,
                "durable_replace",
                side_effect=replace_ancestor_after_publish):
            publication_error = _error_name(
                lambda: publication_store.put(content, object_id))

        competitor_store = state.ContentAddressedInputStore(
            str(root / "competitor"), 128)
        competitor_descriptor_seen = False

        def exact_competitor_after_publish(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal competitor_descriptor_seen
            assert directory_fd is not None
            competitor_descriptor_seen = True
            original_replace(
                source, destination, directory_fd=directory_fd)
            competitor = destination + ".competitor"
            _write_at(directory_fd, competitor, content)
            os.replace(
                competitor,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )

        with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as competitor_hash:
            with mock.patch.object(
                    state,
                    "durable_replace",
                    side_effect=exact_competitor_after_publish):
                competitor_id, competitor_path = competitor_store.put(
                    content, object_id)

        live_namespace = root / "live-namespace"
        live_stable = live_namespace / "stable"
        live_store = state.LiveStateStore(
            str(live_stable / "state"), page_size=64)
        expression = {"op": "bool", "value": True}
        expression_id = live_store.put_expression(expression)
        expression_path = Path(live_store.object_path(expression_id))
        expression_content = expression_path.read_bytes()
        expression_relative = expression_path.relative_to(live_stable)
        live_detached = root / "live-detached"
        live_outside = root / "live-outside"
        live_alias_object = live_outside / expression_relative
        live_alias_object.parent.mkdir(parents=True)
        live_alias_object.write_bytes(expression_content)
        original_read = state.os.read
        live_replaced = False

        def replace_live_ancestor_after_first_read(
            descriptor: int,
            size: int,
        ) -> bytes:
            nonlocal live_replaced
            chunk = original_read(descriptor, size)
            if chunk and not live_replaced:
                live_stable.rename(live_detached)
                live_stable.symlink_to(
                    live_outside, target_is_directory=True)
                live_replaced = True
            return chunk

        with mock.patch.object(
                state.os,
                "read",
                side_effect=replace_live_ancestor_after_first_read):
            live_read_error = _error_name(
                lambda: live_store.get_expression(expression_id))

        publication_detached_object = (
            publication_detached
            / "objects"
            / object_id[:2]
            / object_id[2:]
        )
        live_detached_object = live_detached / expression_relative
        failed_caches = {
            "live_read": sorted(
                live_store._object_store._verified_identities),
            "publication": sorted(publication_store._verified_identities),
        }
        temporary_residue = _temporary_residue(root)
        checks = {
            "nested_root_round_trip_is_exact": (
                nested_id == object_id
                and Path(nested_path).read_bytes() == content
                and nested_snapshot.sha256 == object_id
                and nested_snapshot.content == content
            ),
            "new_components_use_descriptor_relative_mkdir": (
                [call["name"] for call in mkdir_calls]
                == ["level-one", "level-two", "objects", object_id[:2]]
                and all(call["dir_fd_present"] for call in mkdir_calls)
                and all(os.path.sep not in call["name"] for call in mkdir_calls)
            ),
            "relative_root_remains_absolute_across_chdir": (
                relative_id == object_id
                and os.path.isabs(relative_path)
                and relative_path == relative_store.object_path(object_id)
                and relative_snapshot.sha256 == object_id
                and relative_snapshot.content == content
            ),
            "missing_descendants_below_symlink_ancestor_are_not_created": (
                missing_alias_error == "NotADirectoryError"
                and sorted(path.name for path in outside_missing.iterdir()) == []
            ),
            "existing_root_below_symlink_ancestor_is_rejected": (
                existing_alias_error == "NotADirectoryError"
                and existing_sentinel.read_bytes() == b"existing-unchanged"
                and sorted(path.name for path in existing_objects.iterdir())
                == ["sentinel"]
            ),
            "publication_ancestor_replacement_fails_closed": (
                publication_error == "NotADirectoryError"
                and publication_descriptor_seen
            ),
            "publication_stays_on_detached_anchor_without_redirect": (
                publication_detached_object.read_bytes() == content
                and publication_sentinel.read_bytes()
                == b"publication-unchanged"
                and sorted(
                    path.name for path in publication_outside_objects.iterdir()
                ) == ["sentinel"]
            ),
            "live_read_rejects_exact_alias_after_ancestor_replacement": (
                live_replaced
                and live_read_error == "ValueError"
                and live_alias_object.read_bytes() == expression_content
                and live_detached_object.read_bytes() == expression_content
            ),
            "exact_leaf_competitor_still_converges": (
                competitor_descriptor_seen
                and competitor_id == object_id
                and Path(competitor_path).read_bytes() == content
                and competitor_hash.call_count == 1
            ),
            "failed_operations_admit_no_positive_cache": (
                not any(failed_caches.values())
            ),
            "temporary_publication_names_are_cleaned": (
                temporary_residue == []
            ),
        }
        observations = {
            "competitor_fallback_hashes": competitor_hash.call_count,
            "errors": {
                "existing_alias": existing_alias_error,
                "live_read": live_read_error,
                "missing_alias": missing_alias_error,
                "publication": publication_error,
            },
            "failed_identity_caches": failed_caches,
            "mkdir_calls": mkdir_calls,
            "normal_object_id": object_id,
            "publication_descriptor_seen": publication_descriptor_seen,
            "publication_detached_object_exact": (
                publication_detached_object.read_bytes() == content
            ),
            "relative_public_path_is_absolute": os.path.isabs(relative_path),
            "temporary_residue": temporary_residue,
        }

    result = {
        "schema": "symcc-f350-component-wise-anchored-cas-root-v1",
        "configuration": {
            "cases": [
                "nested-root-create-and-round-trip",
                "relative-root-cross-chdir",
                "missing-descendants-below-symlink-ancestor",
                "existing-root-below-symlink-ancestor",
                "ancestor-replacement-during-publication",
                "same-digest-leaf-competitor",
                "ancestor-replacement-during-live-read",
            ],
            "max_object_bytes": 128,
            "source": (
                "real regular files, directories, and symlinks with "
                "deterministic ancestor-replacement fault injection"
            ),
        },
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local overlayfs mechanism integration exercises production "
            "CAS and live-state paths. It does not test openat2, remote shared "
            "filesystems, mount replacement, another host, MPI, a target, "
            "afl-showmap, a symbolic solver, or a fuzzing campaign, and makes "
            "no throughput, coverage, bug-discovery, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
