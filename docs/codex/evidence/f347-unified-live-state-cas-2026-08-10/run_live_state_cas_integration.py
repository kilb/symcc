#!/usr/bin/env python3
"""Exercise unified no-follow CAS semantics for live continuation objects."""

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


def _expression_bytes(
    store: state.LiveStateStore,
    expression: dict,
) -> tuple[bytes, str, Path]:
    content = store._canonical({
        "schema": "symcc-live-expression-v1",
        "expression": expression,
    })
    object_id = store._digest(content)
    return content, object_id, Path(store.object_path(object_id))


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
    expression = {"op": "equal", "children": ["input[0]", 65]}
    with tempfile.TemporaryDirectory(prefix="symcc-f347-integration-") as tmp:
        root = Path(tmp)

        graph_store = state.LiveStateStore(str(root / "graph"), page_size=64)
        expression_id = graph_store.put_expression(expression)
        frame_id = graph_store.put_solver_frame("", [expression_id])
        symbolic_store_id = graph_store.put_symbolic_store({
            "byte": expression_id,
        })
        memory_id = graph_store.create_memory(
            b"A" * 64,
            {0: expression_id},
        )
        descriptor = state.LiveContinuationDescriptor.from_mapping({
            "schema": "symcc-live-continuation-v1",
            "engine": "symcc",
            "frames": [{
                "function": "main",
                "block": "entry",
                "instruction": 1,
                "call_depth": 0,
            }],
            "path_condition_root": frame_id,
            "symbolic_store_root": symbolic_store_id,
            "symbolic_memory_root": memory_id,
            "target_branch": 7,
        })
        assert descriptor is not None
        checkpoint_id = graph_store.put_continuation(descriptor)
        restored = graph_store.restore_continuation(checkpoint_id)
        graph_paths = [
            path for path in (root / "graph" / "objects").rglob("*")
            if path.is_file()
        ]

        link_store = state.LiveStateStore(str(root / "link"), page_size=64)
        link_content, link_id, link_path = _expression_bytes(
            link_store, expression)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        outside = root / "outside-exact.json"
        outside.write_bytes(link_content)
        link_path.symlink_to(outside)
        linked_id = link_store.put_expression(expression)

        link_path.write_bytes(b"x" * len(link_content))
        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as repair_hash:
            repaired_id = link_store.put_expression(expression)

        competitor_store = state.LiveStateStore(
            str(root / "competitor"), page_size=64)
        competitor_content, competitor_id, competitor_path = _expression_bytes(
            competitor_store, expression)
        original_replace = state.durable_replace

        def exact_competitor(
            source: str,
            destination: str,
            *,
            directory_fd: int | None = None,
        ) -> None:
            assert directory_fd is not None
            original_replace(
                source, destination, directory_fd=directory_fd)
            replacement = destination + ".exact-competitor"
            _write_at(directory_fd, replacement, competitor_content)
            os.replace(
                replacement,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )

        with mock.patch.object(
                state, "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot) as competitor_hash:
            with mock.patch.object(
                    state, "durable_replace", side_effect=exact_competitor):
                converged_id = competitor_store.put_expression(expression)

        race_store = state.LiveStateStore(str(root / "read-race"), page_size=64)
        race_id = race_store.put_expression(expression)
        race_path = Path(race_store.object_path(race_id))
        wrong_path = root / "wrong-read-object.json"
        wrong_path.write_bytes(b"{}")
        original_read = state.os.read
        replaced = False

        def replace_after_first_read(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, size)
            if chunk and not replaced:
                replaced = True
                os.replace(wrong_path, race_path)
            return chunk

        unstable_read_rejected = False
        with mock.patch.object(
                state.os, "read", side_effect=replace_after_first_read):
            try:
                race_store.get_expression(race_id)
            except ValueError:
                unstable_read_rejected = True

        symlink_store = state.LiveStateStore(
            str(root / "read-symlink"), page_size=64)
        symlink_content, symlink_id, symlink_path = _expression_bytes(
            symlink_store, expression)
        symlink_store.put_expression(expression)
        symlink_outside = root / "read-outside-exact.json"
        symlink_outside.write_bytes(symlink_content)
        symlink_path.unlink()
        symlink_path.symlink_to(symlink_outside)
        symlink_read_rejected = False
        try:
            symlink_store.get_expression(symlink_id)
        except ValueError:
            symlink_read_rejected = True

        checks = {
            "continuation_graph_round_trip_exact": (
                restored.checkpoint_id == checkpoint_id
                and restored.solver_frames == ((expression_id,),)
                and dict(restored.symbolic_store) == {
                    "byte": expression_id,
                }
                and restored.memory_size == 64
            ),
            "json_layout_is_compatible_and_regular": (
                bool(graph_paths)
                and all(path.suffix == ".json" for path in graph_paths)
                and all(not path.is_symlink() for path in graph_paths)
            ),
            "preexisting_exact_symlink_is_replaced_not_followed": (
                linked_id == link_id
                and not link_path.is_symlink()
                and link_path.read_bytes() == link_content
                and outside.read_bytes() == link_content
            ),
            "cached_corruption_is_repaired": (
                repaired_id == link_id
                and link_path.read_bytes() == link_content
                and repair_hash.call_count == 1
            ),
            "exact_competing_writer_converges": (
                converged_id == competitor_id
                and competitor_path.read_bytes() == competitor_content
                and competitor_hash.call_count == 1
            ),
            "path_replacement_during_read_is_rejected": (
                replaced
                and unstable_read_rejected
                and race_id not in race_store._object_store._verified_identities
            ),
            "exact_symlink_read_is_rejected_without_follow": (
                symlink_read_rejected
                and symlink_id
                not in symlink_store._object_store._verified_identities
                and symlink_outside.read_bytes() == symlink_content
            ),
            "temporary_publication_names_are_cleaned": (
                not _temporary_residue(root)
            ),
        }
        observations = {
            "checkpoint_id": checkpoint_id,
            "graph_object_count": len(graph_paths),
            "graph_object_suffixes": sorted({
                path.suffix for path in graph_paths
            }),
            "repair_fallback_hashes": repair_hash.call_count,
            "competitor_fallback_hashes": competitor_hash.call_count,
            "failed_read_identity_caches": {
                "path_replacement": sorted(
                    race_store._object_store._verified_identities),
                "symlink": sorted(
                    symlink_store._object_store._verified_identities),
            },
            "temporary_residue": _temporary_residue(root),
        }

    result = {
        "schema": "symcc-f347-unified-live-state-cas-v1",
        "configuration": {
            "page_size": 64,
            "object_suffix": ".json",
            "source": (
                "real regular files and symlinks with deterministic "
                "publication/read fault injection"
            ),
        },
        "observations": observations,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local mechanism integration exercises production live-state "
            "and CAS primitives. It does not execute MPI, a target, afl-showmap, "
            "a symbolic solver, or a fuzzing campaign, and makes no throughput, "
            "coverage, bug-discovery, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
