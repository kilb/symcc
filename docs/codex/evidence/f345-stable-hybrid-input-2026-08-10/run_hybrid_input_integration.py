#!/usr/bin/env python3
"""Exercise F345 master/worker/CAS input primitives on real files."""

from __future__ import annotations

import hashlib
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

import mpi_fuzzing_helper as runner  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symcc-f345-integration-") as tmp:
        root = Path(tmp)
        afl = root / "afl"
        queue = afl / "queue"
        queue.mkdir(parents=True)
        (afl / "fuzzer_stats").write_text(
            "command_line : /bin/true -- /bin/true @@\n",
            encoding="ascii",
        )
        seed = queue / "id:000000,orig:seed"
        first = b"F345-first-version"
        second = b"F345-second-version"
        seed.write_bytes(first)
        config = runner.AflConfig(str(afl), max_input_bytes=64)
        first_scan = config.best_new_testcases(set())
        first_hash = config._file_cache[str(seed)]["hash"]

        master_store = runner.ContentAddressedInputStore(
            str(root / "master-objects"), 64)
        replacement = root / "replacement"
        replacement.write_bytes(second)
        os.replace(replacement, seed)
        replacement_rejected = False
        try:
            runner._admit_hybrid_master_input(
                master_store, config._file_cache, str(seed))
        except ValueError:
            replacement_rejected = True
        cache_cleared = str(seed) not in config._file_cache

        second_scan = config.best_new_testcases(set())
        second_hash = config._file_cache[str(seed)]["hash"]
        object_id, stored_path, content = runner._admit_hybrid_master_input(
            master_store, config._file_cache, str(seed))

        worker_store = runner.ContentAddressedInputStore(
            str(root / "worker-objects"), 64)
        local_path, first_materialized = runner._materialize_hybrid_worker_input(
            worker_store,
            {
                "sha256": object_id,
                "object_id": object_id,
                "object_content": content,
            },
            str(seed),
        )
        worker_objects = {1: set()}
        first_ack = runner._acknowledge_worker_input_object(
            worker_objects, 1, object_id, first_materialized)
        reused_path, reused_id = runner._materialize_hybrid_worker_input(
            worker_store,
            {"sha256": object_id, "object_id": object_id},
            str(seed),
        )

        Path(local_path).write_bytes(b"corrupt-worker-object")
        corruption_rejected = False
        try:
            runner._materialize_hybrid_worker_input(
                worker_store,
                {"sha256": object_id, "object_id": object_id},
                str(seed),
            )
        except ValueError:
            corruption_rejected = True
        missing_ack_removed = not runner._acknowledge_worker_input_object(
            worker_objects, 1, object_id, "") and not worker_objects[1]
        repaired_path, repaired_id = runner._materialize_hybrid_worker_input(
            worker_store,
            {
                "sha256": object_id,
                "object_id": object_id,
                "object_content": content,
            },
            str(seed),
        )
        repair_ack = runner._acknowledge_worker_input_object(
            worker_objects, 1, object_id, repaired_id)

        path_store = runner.ContentAddressedInputStore(
            str(root / "path-worker-objects"), 64)
        path_materialized, path_id = runner._materialize_hybrid_worker_input(
            path_store,
            {"sha256": object_id},
            str(seed),
        )
        changed = root / "changed"
        changed.write_bytes(b"changed-after-master-admission")
        os.replace(changed, seed)
        path_replacement_rejected = False
        try:
            runner._materialize_hybrid_worker_input(
                path_store,
                {"sha256": object_id},
                str(seed),
            )
        except ValueError:
            path_replacement_rejected = True

        small_store = runner.ContentAddressedInputStore(
            str(root / "small-objects"), 4)
        oversized = root / "oversized"
        oversized.write_bytes(b"12345")
        oversized_rejected = False
        try:
            runner._admit_hybrid_master_input(small_store, {}, str(oversized))
        except ValueError:
            oversized_rejected = True
        alias = root / "alias"
        alias.symlink_to(oversized)
        symlink_rejected = False
        try:
            runner._admit_hybrid_master_input(small_store, {}, str(alias))
        except OSError:
            symlink_rejected = True

        ranked = []
        for index in (3, 4, 5):
            marker = "+cov," if index == 3 else ""
            candidate = queue / f"id:{index:06d},{marker}orig:ranked"
            candidate.write_bytes(b"ranked-input")
            ranked.append(candidate)
        with mock.patch.object(runner, "MAX_FILE_CACHE", 2):
            selected = config.best_new_testcases(set(), batch_size=2)
        top_k_pinned = (
            selected == [str(ranked[0]), str(ranked[2])]
            and set(config._file_cache) == set(selected)
        )
        top_k_cache_names = sorted(
            Path(path).name for path in config._file_cache)
        selected_replacement = root / "selected-replacement"
        selected_replacement.write_bytes(b"changed-after-selection")
        os.replace(selected_replacement, ranked[0])
        selected_replacement_rejected = False
        try:
            runner._admit_hybrid_master_input(
                master_store, config._file_cache, str(ranked[0]))
        except ValueError:
            selected_replacement_rejected = True

        continuation = {
            "schema": "symcc-live-continuation-v1",
            "engine": "symcc",
            "frames": [{
                "function": "main",
                "block": "entry",
                "instruction": 0,
                "call_depth": 0,
            }],
            "path_condition_root": "a" * 64,
        }
        descriptor = runner.LiveContinuationDescriptor.from_mapping(
            continuation)
        assert descriptor is not None
        continuation_admission = runner._admit_hybrid_master_work(
            master_store,
            config._file_cache,
            str(root / "missing-self-contained-seed"),
            continuation,
        )

        expected_second_hash = hashlib.sha256(second).hexdigest()
        checks = {
            "first_queue_snapshot_exact": (
                first_scan == [str(seed)]
                and first_hash == hashlib.sha256(first).hexdigest()
            ),
            "replacement_invalidates_queue_score": (
                replacement_rejected and cache_cleared
            ),
            "rescored_master_admission_exact": (
                second_scan == [str(seed)]
                and second_hash == expected_second_hash
                and object_id == expected_second_hash
                and Path(stored_path).read_bytes() == second
                and content == second
            ),
            "first_transfer_and_ack_exact": (
                first_materialized == object_id
                and first_ack
                and worker_objects[1] == {object_id}
            ),
            "contentless_reuse_verified": (
                reused_id == object_id
                and reused_path == local_path
            ),
            "corruption_rejected_and_ack_evicted": (
                corruption_rejected and missing_ack_removed
            ),
            "content_resend_repairs_cache": (
                repair_ack
                and repaired_id == object_id
                and Path(repaired_path).read_bytes() == second
            ),
            "path_mode_is_digest_fenced": (
                path_id == object_id
                and Path(path_materialized).read_bytes() == second
                and path_replacement_rejected
            ),
            "oversized_and_symlink_inputs_rejected": (
                oversized_rejected and symlink_rejected
            ),
            "top_k_score_fence_survives_cache_eviction": (
                top_k_pinned and selected_replacement_rejected
            ),
            "self_contained_continuation_uses_checkpoint_identity": (
                continuation_admission
                == (None, descriptor.checkpoint_id(), b"", 0)
            ),
        }

    result = {
        "schema": "symcc-f345-stable-hybrid-input-integration-v1",
        "configuration": {
            "input_limit_bytes": 64,
            "small_limit_bytes": 4,
            "object_transport_modes": ["content-object", "path-digest"],
            "source": "real regular files on a temporary local filesystem",
        },
        "observations": {
            "first_sha256": first_hash,
            "second_sha256": second_hash,
            "admitted_object_id": object_id,
            "worker_cached_objects": sorted(worker_objects[1]),
            "replacement_rejected": replacement_rejected,
            "corruption_rejected": corruption_rejected,
            "path_replacement_rejected": path_replacement_rejected,
            "oversized_rejected": oversized_rejected,
            "symlink_rejected": symlink_rejected,
            "top_k_selected_names": [Path(path).name for path in selected],
            "top_k_cache_names": top_k_cache_names,
            "selected_replacement_rejected": selected_replacement_rejected,
            "continuation_dispatch_identity": continuation_admission[1],
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "proof_boundary": (
            "This local integration exercises production queue admission, "
            "content-addressed stores, worker materialization, digest fences, "
            "Top-K score-fence retention, self-contained continuation identity, "
            "and acknowledgement accounting on real files. It does not run "
            "MPI transport, afl-showmap, a target, a symbolic solver, or a "
            "fuzzing campaign, and makes no coverage, throughput, bug-"
            "discovery, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
