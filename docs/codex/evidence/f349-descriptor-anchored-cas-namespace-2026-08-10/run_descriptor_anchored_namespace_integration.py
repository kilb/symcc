#!/usr/bin/env python3
"""Exercise descriptor-anchored CAS root, shard, leaf, and cache closure."""

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


def _error_name(callback) -> str:
    try:
        callback()
    except (OSError, ValueError) as error:
        return type(error).__name__
    return ""


def main() -> int:
    content = b"F349-descriptor-anchored-CAS-namespace"
    with tempfile.TemporaryDirectory(prefix="symcc-f349-integration-") as tmp:
        root = Path(tmp)
        object_id = state.ContentAddressedInputStore.digest(content)
        leaf = object_id[2:]

        normal_store = state.ContentAddressedInputStore(
            str(root / "normal"), 128)
        normal_id, normal_path = normal_store.put(content, object_id)
        with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as cached_hash:
            cached_id, cached_path = normal_store.put(content, object_id)
        normal_snapshot = normal_store.snapshot(
            object_id, retain_content=True)

        outside_root = root / "outside-root-alias"
        outside_root.mkdir()
        root_alias = root / "root-alias"
        root_alias.symlink_to(outside_root, target_is_directory=True)
        root_alias_error = _error_name(
            lambda: state.ContentAddressedInputStore(str(root_alias), 128))

        shard_store = state.ContentAddressedInputStore(
            str(root / "shard-alias"), 128)
        outside_shard = root / "outside-shard-alias"
        outside_shard.mkdir()
        (Path(shard_store.root) / object_id[:2]).symlink_to(
            outside_shard,
            target_is_directory=True,
        )
        shard_alias_error = _error_name(
            lambda: shard_store.put(content, object_id))

        original_replace = state.durable_replace
        root_race_store = state.ContentAddressedInputStore(
            str(root / "root-race"), 128)
        root_race_path = Path(root_race_store.root)
        detached_root = root / "detached-root"
        outside_root_race = root / "outside-root-race"
        outside_root_race.mkdir()
        root_race_descriptor_seen = False

        def replace_root_after_publish(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal root_race_descriptor_seen
            assert directory_fd is not None
            root_race_descriptor_seen = True
            original_replace(
                source, destination, directory_fd=directory_fd)
            root_race_path.rename(detached_root)
            root_race_path.symlink_to(
                outside_root_race, target_is_directory=True)

        with mock.patch.object(
                state,
                "durable_replace",
                side_effect=replace_root_after_publish):
            root_race_error = _error_name(
                lambda: root_race_store.put(content, object_id))

        shard_race_store = state.ContentAddressedInputStore(
            str(root / "shard-race"), 128)
        shard_race_path = Path(shard_race_store.root) / object_id[:2]
        detached_shard = root / "detached-shard"
        outside_shard_race = root / "outside-shard-race"
        outside_shard_race.mkdir()
        shard_race_descriptor_seen = False

        def replace_shard_after_publish(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal shard_race_descriptor_seen
            assert directory_fd is not None
            shard_race_descriptor_seen = True
            original_replace(
                source, destination, directory_fd=directory_fd)
            shard_race_path.rename(detached_shard)
            shard_race_path.symlink_to(
                outside_shard_race, target_is_directory=True)

        with mock.patch.object(
                state,
                "durable_replace",
                side_effect=replace_shard_after_publish):
            shard_race_error = _error_name(
                lambda: shard_race_store.put(content, object_id))

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

        expression = {"op": "bool", "value": True}
        live_store = state.LiveStateStore(
            str(root / "live-read"), page_size=64)
        expression_id = live_store.put_expression(expression)
        expression_path = Path(live_store.object_path(expression_id))
        expression_shard = expression_path.parent
        expression_leaf = expression_path.name
        expression_content = expression_path.read_bytes()
        detached_live_shard = root / "detached-live-shard"
        outside_live_shard = root / "outside-live-shard"
        outside_live_shard.mkdir()
        (outside_live_shard / expression_leaf).write_bytes(expression_content)
        original_read = state.os.read
        read_replaced = False

        def replace_shard_after_first_read(
            descriptor: int,
            size: int,
        ) -> bytes:
            nonlocal read_replaced
            chunk = original_read(descriptor, size)
            if chunk and not read_replaced:
                expression_shard.rename(detached_live_shard)
                expression_shard.symlink_to(
                    outside_live_shard, target_is_directory=True)
                read_replaced = True
            return chunk

        with mock.patch.object(
                state.os,
                "read",
                side_effect=replace_shard_after_first_read):
            read_race_error = _error_name(
                lambda: live_store.get_expression(expression_id))

        redirected_directories = {
            "root_alias": sorted(path.name for path in outside_root.iterdir()),
            "root_race": sorted(
                path.name for path in outside_root_race.iterdir()),
            "shard_alias": sorted(
                path.name for path in outside_shard.iterdir()),
            "shard_race": sorted(
                path.name for path in outside_shard_race.iterdir()),
        }
        failed_caches = {
            "root_race": sorted(root_race_store._verified_identities),
            "shard_alias": sorted(shard_store._verified_identities),
            "shard_race": sorted(shard_race_store._verified_identities),
            "live_read": sorted(
                live_store._object_store._verified_identities),
        }
        detached_objects_exact = (
            (detached_root / object_id[:2] / leaf).read_bytes() == content
            and (detached_shard / leaf).read_bytes() == content
            and (detached_live_shard / expression_leaf).read_bytes()
            == expression_content
        )
        temporary_residue = _temporary_residue(root)
        checks = {
            "normal_publication_and_snapshot_round_trip": (
                normal_id == cached_id == object_id
                and normal_path == cached_path
                and Path(normal_path).read_bytes() == content
                and normal_snapshot.sha256 == object_id
                and normal_snapshot.content == content
            ),
            "verified_identity_fast_path_avoids_payload_rehash": (
                cached_hash.call_count == 0
            ),
            "root_symlink_is_rejected_without_external_write": (
                root_alias_error == "NotADirectoryError"
                and redirected_directories["root_alias"] == []
            ),
            "shard_symlink_is_rejected_without_external_write": (
                shard_alias_error == "NotADirectoryError"
                and redirected_directories["shard_alias"] == []
            ),
            "root_replacement_during_publication_fails_closed": (
                root_race_error == "NotADirectoryError"
                and root_race_descriptor_seen
            ),
            "shard_replacement_during_publication_fails_closed": (
                shard_race_error == "NotADirectoryError"
                and shard_race_descriptor_seen
            ),
            "publication_never_redirects_and_detached_bytes_are_exact": (
                redirected_directories["root_race"] == []
                and redirected_directories["shard_race"] == []
                and detached_objects_exact
            ),
            "exact_leaf_competitor_converges_inside_anchor": (
                competitor_descriptor_seen
                and competitor_id == object_id
                and Path(competitor_path).read_bytes() == content
                and competitor_hash.call_count == 1
            ),
            "live_read_shard_replacement_rejects_exact_alias": (
                read_replaced
                and read_race_error == "ValueError"
                and (outside_live_shard / expression_leaf).read_bytes()
                == expression_content
            ),
            "failed_operations_admit_no_verified_identity": (
                not any(failed_caches.values())
            ),
            "temporary_publication_names_are_cleaned": (
                temporary_residue == []
            ),
        }
        observations = {
            "cache_fallback_hashes": cached_hash.call_count,
            "competitor_fallback_hashes": competitor_hash.call_count,
            "descriptor_relative_callbacks": {
                "competitor": competitor_descriptor_seen,
                "root_race": root_race_descriptor_seen,
                "shard_race": shard_race_descriptor_seen,
            },
            "detached_objects_exact": detached_objects_exact,
            "errors": {
                "live_read_race": read_race_error,
                "root_alias": root_alias_error,
                "root_race": root_race_error,
                "shard_alias": shard_alias_error,
                "shard_race": shard_race_error,
            },
            "failed_identity_caches": failed_caches,
            "normal_object_id": object_id,
            "redirected_directory_entries": redirected_directories,
            "temporary_residue": temporary_residue,
        }

    result = {
        "schema": "symcc-f349-descriptor-anchored-cas-namespace-v1",
        "configuration": {
            "cases": [
                "normal-round-trip",
                "root-symlink",
                "shard-symlink",
                "root-publication-replacement",
                "shard-publication-replacement",
                "exact-leaf-competitor",
                "live-read-shard-replacement",
            ],
            "max_object_bytes": 128,
            "source": (
                "real regular files, directories, and symlinks with "
                "deterministic directory-replacement fault injection"
            ),
        },
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local mechanism integration exercises production CAS and "
            "live-state directory-descriptor paths. It does not execute MPI, "
            "a target, afl-showmap, a symbolic solver, or a fuzzing campaign, "
            "and makes no throughput, coverage, bug-discovery, or LAVA-M "
            "uplift claim. Ancestors above the configured CAS root remain a "
            "trusted deployment boundary."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
